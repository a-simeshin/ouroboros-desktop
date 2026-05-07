"""External skill discovery, state, and review-status tracking (Phase 3).

Reads skills from the local checkout path configured in settings via
``OUROBOROS_SKILLS_REPO_PATH`` (see ``ouroboros.config.get_skills_repo_path``).
A skill is a directory containing either ``SKILL.md`` (with YAML frontmatter)
or ``skill.json``. The manifest schema lives in
``ouroboros.contracts.skill_manifest``; this module is the runtime-side
loader + state tracker on top of that frozen contract.

Per-skill state — the enabled bit, the most recent review verdict, and a
content hash used to invalidate stale reviews — is stored durably in
``~/Ouroboros/data/state/skills/<name>/`` so it survives restarts and lives
on the same plane as other durable state (``state.json``,
``advisory_review.json``). The layout:

- ``enabled.json`` — ``{"enabled": bool, "updated_at": iso_ts}``.
- ``review.json``  — ``{"content_hash": str, "status": "pass"|"fail"|"advisory"|"pending"|"pending_phase4",
  "findings": [...], "reviewer_models": [...], "timestamp": iso_ts,
  "prompt_chars": int, "cost_usd": float, "raw_result": str}``.
  ``pending_phase4`` is reserved for ``type: extension`` skills (execution
  deferred until Phase 4); the loader overlays this status on all
  extension skills regardless of persisted verdict so the Phase 3
  catalogue cannot mislead operators into thinking an extension is
  runnable. ``raw_result`` carries the truncated top-level review
  response for replay/debugging (capped via ``_truncate_raw_result`` in
  ``ouroboros.skill_review`` with an explicit OMISSION NOTE on overflow).

Neither file is required on disk — missing files mean "defaults". The module
treats absent state as: ``enabled=False``, ``review.status="pending"``.

Phase 3 scope: ``type: instruction`` and ``type: script`` are surfaced and
reviewable; ``type: extension`` is parsed but skipped with an explicit
``pending_phase4`` status so the skill shows up in the catalogue without
becoming executable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ouroboros.contracts.plugin_api import FORBIDDEN_SKILL_SETTINGS
from ouroboros.contracts.skill_manifest import (
    SkillManifest,
    SkillManifestError,
    parse_skill_manifest_text,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MANIFEST_NAMES = ("SKILL.md", "skill.json")
# Files we actually read as part of a skill's review pack (manifest body +
# executable payload + static assets the payload might depend on). The loader
# also consumes the manifest separately; this list controls content-hashing.
# Directories / files that must NOT contribute to the hash even though
# they live inside the skill checkout. We keep the denylist narrow and
# focused on (a) compiler/package-manager scratch (``__pycache__``,
# ``node_modules``, ``.tox``), (b) editor/VCS metadata (``.git``,
# ``.hg``, ``.idea``, ``.vscode``), (c) OS junk (``.DS_Store``).
# Everything else — including non-metadata dotfiles like ``.env-sample``
# or a hand-rolled ``.hidden_helper.py`` — IS hashed and reviewed,
# because the skill subprocess can ``import``/``source``/``read`` such
# files at runtime. A blanket "skip everything starting with '.'" rule
# would let a hidden helper bypass the review gate.
_SKILL_DIR_CACHE_NAMES = frozenset(
    {
        "__pycache__",
        "node_modules",
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vscode",
        ".tox",
        ".ouroboros_env",
        ".DS_Store",
    }
)

# Sensitive file shapes we refuse to send to external reviewer models.
# Mirrors the repo-review policy in ``ouroboros.tools.review_helpers``
# (reused verbatim via the import in ``_iter_payload_files`` to keep the
# classifier DRY). These files are ALSO excluded from the content hash:
# if someone drops a ``.env`` into their skill checkout we don't want an
# inadvertent edit to stale-invalidate a reviewed skill, and we
# definitely don't want the reviewer prompt to carry credentials.

_REVIEW_STATUS_PASS = "pass"
_REVIEW_STATUS_FAIL = "fail"
_REVIEW_STATUS_ADVISORY = "advisory"
_REVIEW_STATUS_PENDING = "pending"
_REVIEW_STATUS_DEFERRED_PHASE4 = "pending_phase4"

VALID_REVIEW_STATUSES = frozenset(
    {
        _REVIEW_STATUS_PASS,
        _REVIEW_STATUS_FAIL,
        _REVIEW_STATUS_ADVISORY,
        _REVIEW_STATUS_PENDING,
        _REVIEW_STATUS_DEFERRED_PHASE4,
    }
)
GRANTS_FILENAME = "grants.json"

_SEED_COMPLETE_MARKER = ".bootstrap-seed-complete"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SkillReviewState:
    """Persisted review verdict for one skill.

    ``content_hash`` is the sha256 of the manifest + payload files at the
    time the review was produced. ``is_stale_for(current_hash)`` returns
    True when the user has edited the skill since the last review.
    """

    status: str = _REVIEW_STATUS_PENDING
    content_hash: str = ""
    findings: List[Dict[str, Any]] = field(default_factory=list)
    reviewer_models: List[str] = field(default_factory=list)
    timestamp: str = ""
    prompt_chars: int = 0
    cost_usd: float = 0.0
    raw_result: str = ""

    def is_stale_for(self, current_hash: str) -> bool:
        if not current_hash:
            return True
        if not self.content_hash:
            return True
        return self.content_hash != current_hash

    def to_dict(self) -> Dict[str, Any]:
        status = self.status if self.status in VALID_REVIEW_STATUSES else _REVIEW_STATUS_PENDING
        return {
            "status": status,
            "content_hash": self.content_hash,
            "findings": list(self.findings),
            "reviewer_models": list(self.reviewer_models),
            "timestamp": self.timestamp,
            "prompt_chars": int(self.prompt_chars or 0),
            "cost_usd": float(self.cost_usd or 0.0),
            "raw_result": self.raw_result,
        }


@dataclass
class LoadedSkill:
    """A discovered skill package + its durable state.

    ``available_for_execution`` combines three signals:

    - the skill is enabled by the user;
    - the last review landed with status ``pass``;
    - the review is not stale against the current content hash.

    ``source`` records which discovery root the skill came from
    (``native`` / ``clawhub`` / ``external`` / ``user_repo``). The Skills /
    Marketplace UI uses it to group cards and decide which lifecycle
    actions to expose (e.g. an ``Update`` button is only meaningful for
    ``clawhub`` skills with provenance metadata).
    """

    name: str
    skill_dir: pathlib.Path
    manifest: SkillManifest
    content_hash: str
    enabled: bool = False
    review: SkillReviewState = field(default_factory=SkillReviewState)
    load_error: str = ""
    source: str = "native"

    @property
    def available_for_execution(self) -> bool:
        """True when the skill passes every static availability gate.

        Must agree with ``ouroboros.tools.skill_exec._handle_skill_exec``:
        Phase 3 only executes ``type: script`` skills. ``instruction``
        skills are catalogued + reviewable but have no executable
        payload (their manifest declares no scripts); ``extension``
        skills are deferred to Phase 4. Gating on ``manifest.is_script()``
        here ensures ``summarize_skills`` / ``list_available_for_execution``
        cannot report a false-ready signal for skill types that
        ``skill_exec`` will unconditionally reject.

        This does NOT consult the ambient ``OUROBOROS_RUNTIME_MODE`` —
        v5.1.2 Frame A: ``skill_exec`` runs reviewed + enabled skills
        regardless of mode (light/advanced/pro). The runtime_mode axis
        only gates repo self-modification + the elevation ratchet. Use
        :func:`is_runtime_eligible_for_execution` for the
        "will this actually run right now" answer (which now equals
        ``available_for_execution`` since the runtime-mode gate was
        removed in v5.1.2).
        """
        if self.load_error:
            return False
        if not self.enabled:
            return False
        if not self.manifest.is_script():
            # Only type: script is executable in Phase 3 (instruction =
            # no payload by design; extension = Phase 4).
            return False
        if self.review.status != _REVIEW_STATUS_PASS:
            return False
        if self.review.is_stale_for(self.content_hash):
            return False
        from ouroboros.tools.skill_exec import _resolve_runtime_binary, _resolve_script_path

        runtime = (self.manifest.runtime or "").strip().lower()
        if _resolve_runtime_binary(runtime) is None:
            return False
        for entry in self.manifest.scripts or []:
            if not isinstance(entry, dict):
                continue
            declared_name = str(entry.get("name") or "").strip()
            if not declared_name:
                continue
            relpath = (
                declared_name
                if "/" in declared_name or declared_name.startswith(".")
                else f"scripts/{declared_name}"
            )
            if _resolve_script_path(self.skill_dir, relpath) is not None:
                return True
        return False


def is_runtime_eligible_for_execution(skill: "LoadedSkill") -> bool:
    """True when the skill is statically available for execution.

    v5.1.2 Frame A: ``OUROBOROS_RUNTIME_MODE`` no longer gates skill
    execution — light, advanced, and pro all let reviewed + enabled
    skills run. The previous helper short-circuited to False on light;
    that branch is removed so the Skills UI no longer paints a
    runtime-blocked badge in light mode. The ``runtime_mode`` axis
    only controls repo self-modification + the elevation ratchet.
    """
    return skill.available_for_execution


# ---------------------------------------------------------------------------
# Disk paths
# ---------------------------------------------------------------------------


def _skills_state_root(drive_root: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(drive_root) / "state" / "skills"


def skill_state_dir(drive_root: pathlib.Path, name: str) -> pathlib.Path:
    """Return ``~/Ouroboros/data/state/skills/<name>/`` (created on demand).

    The name is normalized to its alnum-dashes shape before joining so a
    malicious manifest ``name: ../foo`` cannot escape the state root.
    """
    safe = _sanitize_skill_name(name)
    path = _skills_state_root(drive_root) / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sanitize_skill_name(name: str) -> str:
    """Clamp a skill name to a safe on-disk identifier.

    Keep alphanumerics, dashes, underscores, and dots; replace everything
    else with ``_``. Empty / pathological inputs become ``"_unnamed"``.
    """
    cleaned = "".join(
        ch if ch.isalnum() or ch in "-_." else "_" for ch in str(name or "").strip()
    )
    cleaned = cleaned.strip("._")
    if not cleaned:
        return "_unnamed"
    return cleaned[:64]  # also bound length to keep state paths sane


# ---------------------------------------------------------------------------
# Manifest discovery
# ---------------------------------------------------------------------------


class _ManifestUnreadable(RuntimeError):
    """A manifest file exists but could not be read (permissions,
    truncation, IO error, etc.). Callers translate this into a
    ``LoadedSkill`` with ``load_error`` set so the broken skill is
    still visible in ``list_skills`` instead of silently disappearing
    from discovery."""

    def __init__(self, path: pathlib.Path, err: BaseException) -> None:
        super().__init__(f"manifest {path}: {type(err).__name__}: {err}")
        self.path = path
        self.err = err


def _manifest_text_for_dir(skill_dir: pathlib.Path) -> Optional[tuple[str, pathlib.Path]]:
    """Return (manifest_text, manifest_path) for a skill dir.

    Returns ``None`` ONLY when the directory has no manifest at all
    (i.e. "this is not a skill dir"). A manifest that exists but can't
    be read raises ``_ManifestUnreadable`` so the caller can surface
    the broken skill with a ``load_error`` instead of pretending the
    dir was not a skill dir in the first place.
    """
    for candidate in _MANIFEST_NAMES:
        mf = skill_dir / candidate
        if mf.is_file():
            try:
                return mf.read_text(encoding="utf-8"), mf
            except (OSError, UnicodeDecodeError) as exc:
                # Catch BOTH IO failures and decode failures: a manifest
                # with invalid UTF-8 would otherwise crash discovery for
                # the whole skills checkout instead of degrading to a
                # single broken-skill entry.
                log.warning("Failed to read skill manifest %s", mf, exc_info=True)
                raise _ManifestUnreadable(mf, exc) from exc
    return None


def _iter_payload_files(
    skill_dir: pathlib.Path,
    *,
    manifest_entry: str = "",
    manifest_scripts: Optional[List[Dict[str, Any]]] = None,
) -> List[pathlib.Path]:
    """Return the sorted list of files that contribute to the content hash.

    The reviewed/hashed surface MUST equal the runtime surface: the
    subprocess runs with ``cwd=skill_dir`` so any non-hidden file in the
    skill directory can be ``import``/``source``/``read`` by the payload.
    If the hash only covered ``scripts/``/``assets/``, a malicious author
    could stash logic in a top-level ``helper.py`` and it would never
    invalidate the PASS verdict when edited.

    Accordingly this walker hashes **every regular file under
    ``skill_dir``** with just three exclusions:

    - dotfiles and dotted directories INSIDE the skill (``.git``,
      ``.DS_Store``, and the like — the dotfile filter is applied to
      *relative* parts so a skills checkout living in a hidden parent
      directory does not have everything silently skipped);
    - well-known cache directory names (``__pycache__``,
      ``node_modules``);
    - files that resolve outside ``skill_dir`` after ``resolve()``
      (symlink escape guard).

    ``manifest_entry`` and ``manifest_scripts`` are still honoured as an
    explicit safety net: if the manifest declares something outside the
    skill directory (e.g. via a malformed ``entry: ../../boot.py``) we
    refuse to include it; if it declares a confined path we include it
    even if the path happens to be on the dotfile exclusion list, so the
    declared executable surface stays consistent with the reviewed one.
    """
    out: List[pathlib.Path] = []
    resolved_root = skill_dir.resolve()

    def _add(path: pathlib.Path) -> None:
        if path not in out:
            out.append(path)

    def _add_if_confined(relpath: str) -> None:
        rel = str(relpath or "").strip()
        if not rel or rel.startswith("/") or rel.startswith("~"):
            return
        if ".." in pathlib.PurePosixPath(rel).parts:
            return
        resolved = (skill_dir / rel).resolve()
        try:
            resolved.relative_to(resolved_root)
        except ValueError:
            return
        if resolved.is_file():
            _add(resolved)

    # Broad walk first — everything inside skill_dir that the runtime
    # subprocess can reach, minus a narrow denylist of metadata/cache
    # names. Two confinement checks run per candidate:
    #
    # 1. Walk with ``follow_symlinks=False`` equivalent: manually reject
    #    any ``.is_symlink()`` entry whose ``resolve()`` target escapes
    #    ``skill_dir``. A symlink that resolves INSIDE the tree is fine
    #    (dedupe is handled by the ``not in out`` guard), but a symlink
    #    to ``/etc/passwd`` would otherwise leak into the review pack
    #    sent to external reviewer models.
    # 2. Re-verify ``relative_to(resolved_root)`` on the resolved path
    #    so symlinked directories pointing outside skill_dir are also
    #    excluded even if their metadata looks in-tree.
    # Reuse the repo-review sensitive-path classifier so skill review
    # inherits the same "never send .env / .pem / credentials.json to
    # reviewer models" policy that protects the main repo (DRY).
    from ouroboros.tools.review_helpers import (
        _SENSITIVE_EXTENSIONS,
        _SENSITIVE_NAMES,
    )

    def _is_sensitive(path: pathlib.Path) -> bool:
        lowered = path.name.lower()
        if lowered in _SENSITIVE_NAMES:
            return True
        for ext in _SENSITIVE_EXTENSIONS:
            if lowered.endswith(ext):
                return True
        return False

    if resolved_root.is_dir():
        for path in sorted(resolved_root.rglob("*")):
            if not path.is_file():
                continue
            try:
                rel_parts = path.relative_to(resolved_root).parts
            except ValueError:
                continue
            if any(part in _SKILL_DIR_CACHE_NAMES for part in rel_parts):
                continue
            if _is_sensitive(path):
                # Presence of a sensitive-shape file inside a skill's
                # runtime-reachable tree is a hard block. If we silently
                # skipped the file, a reviewed skill could still
                # ``open(".env").read()`` at runtime to exfiltrate
                # credentials even though the file was never part of
                # the review pack. Fail closed — operator must rename
                # the file or move it out of the skill tree.
                raise SkillPayloadUnreadable(
                    str(path.relative_to(resolved_root)),
                    RuntimeError(
                        "sensitive-shape filename present in skill tree "
                        "(e.g. .env / credentials.json / .pem). Rename "
                        "or relocate the file outside the skill checkout."
                    ),
                )
            # Symlink escape guard: reject any entry (or parent) whose
            # resolved path leaves ``skill_dir``. We resolve the final
            # path — Path.resolve() collapses symlinks — and re-check
            # confinement.
            try:
                real = path.resolve()
            except (OSError, RuntimeError):
                log.warning("Could not resolve skill file %s", path, exc_info=True)
                continue
            try:
                real.relative_to(resolved_root)
            except ValueError:
                log.warning(
                    "Skill file %s resolves outside skill_dir (%s) — excluded from review pack.",
                    path, resolved_root,
                )
                continue
            _add(path)

    # Manifest-declared entry + scripts explicitly — catches the edge
    # case where an author declared a path that the broad walk would
    # have skipped (e.g. a bare name that needs the ``scripts/`` prefix
    # expansion applied here rather than in two callers).
    _add_if_confined(manifest_entry)
    for script_entry in manifest_scripts or []:
        if not isinstance(script_entry, dict):
            continue
        declared_name = str(script_entry.get("name") or "").strip()
        if not declared_name:
            continue
        _add_if_confined(declared_name)
        if "/" not in declared_name:
            _add_if_confined(f"scripts/{declared_name}")

    out.sort()
    return out


class SkillPayloadUnreadable(RuntimeError):
    """Raised by ``compute_content_hash`` when a payload file cannot be
    read at hash time. The skill surface must FAIL CLOSED: a silent skip
    (as the old implementation did) would let a ``scripts/main.py`` with
    temporarily-unreadable permissions be excluded from both the review
    pack and the hash. Callers surface this as a ``load_error`` on the
    ``LoadedSkill`` and as ``status='pending'`` on ``review_skill``."""

    def __init__(self, relpath: str, err: BaseException) -> None:
        super().__init__(
            f"Skill payload {relpath!r} unreadable: {type(err).__name__}: {err}"
        )
        self.relpath = relpath
        self.err = err


def compute_content_hash(
    skill_dir: pathlib.Path,
    *,
    manifest_entry: str = "",
    manifest_scripts: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Compute a deterministic sha256 of manifest + payload files.

    Used both as a staleness tag on the stored review verdict and as an
    input to the review prompt so the reviewer can log which snapshot it
    looked at. ``manifest_entry`` and ``manifest_scripts`` ensure that
    every file ``skill_exec`` can actually invoke is part of the hash:
    ``type: extension`` skills whose executable surface is a
    ``plugin.py``-style entry module outside the conventional
    ``scripts/`` directory, and ``type: script`` skills whose manifest
    declares ``scripts[].name`` paths like ``bin/run.sh``.

    Fails CLOSED on unreadable files: an ``OSError`` during
    ``read_bytes`` raises :class:`SkillPayloadUnreadable` so callers
    can surface ``load_error``/``status=pending`` rather than emit a
    deceptive PASS over a partial hash.
    """
    digest = hashlib.sha256()
    skill_dir = skill_dir.resolve()
    for file_path in _iter_payload_files(
        skill_dir,
        manifest_entry=manifest_entry,
        manifest_scripts=manifest_scripts,
    ):
        rel = file_path.relative_to(skill_dir).as_posix()
        # Stream per-file hashing in 64 KiB chunks so a pathological
        # skill with a multi-GB asset cannot force ``list_skills`` /
        # ``skill_exec`` preflight to allocate the whole file into
        # memory.
        file_digest = hashlib.sha256()
        try:
            with file_path.open("rb") as fh:
                while True:
                    chunk = fh.read(64 * 1024)
                    if not chunk:
                        break
                    file_digest.update(chunk)
        except OSError as exc:
            log.warning("Failed to read skill payload file %s", file_path, exc_info=True)
            raise SkillPayloadUnreadable(rel, exc) from exc
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest.digest())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def _atomic_write_json(path: pathlib.Path, payload: Dict[str, Any]) -> None:
    """Atomically write ``payload`` as JSON to ``path``.

    Uses a unique temp filename (pid + thread id + uuid4 fragment) so
    two concurrent writes to the same durable-state file — whether
    from different threads inside one process or from a reviewer tool
    racing with a ``toggle_skill`` — cannot stomp each other's temp
    files or hit ``FileNotFoundError`` in ``os.replace``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = (
        f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}"
    )
    tmp = path.with_name(tmp_name)
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup of a stale temp; os.replace failure shouldn't
        # leave dot-turds sitting next to the real file.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _read_json(path: pathlib.Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        log.warning("Failed to parse skill state file %s", path, exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def load_enabled(drive_root: pathlib.Path, name: str) -> bool:
    state = _read_json(skill_state_dir(drive_root, name) / "enabled.json")
    if not isinstance(state, dict):
        return False
    enabled = state.get("enabled")
    return enabled if isinstance(enabled, bool) else False


def save_enabled(drive_root: pathlib.Path, name: str, enabled: bool) -> None:
    _atomic_write_json(
        skill_state_dir(drive_root, name) / "enabled.json",
        {
            "enabled": bool(enabled),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def load_review_state(drive_root: pathlib.Path, name: str) -> SkillReviewState:
    data = _read_json(skill_state_dir(drive_root, name) / "review.json")
    if not isinstance(data, dict):
        return SkillReviewState()
    raw_status = str(data.get("status") or _REVIEW_STATUS_PENDING).lower()
    # Phase 4 retires the ``pending_phase4`` overlay. Any lingering
    # Phase 3 review.json files still carrying that literal status
    # migrate back to plain ``pending`` on load so the summarizer's
    # buckets stay consistent (``pending_phase4`` is no longer a
    # valid persisted status; an extension's real verdict now
    # surfaces verbatim).
    if raw_status == _REVIEW_STATUS_DEFERRED_PHASE4:
        raw_status = _REVIEW_STATUS_PENDING
    status = raw_status if raw_status in VALID_REVIEW_STATUSES else _REVIEW_STATUS_PENDING
    raw_findings = data.get("findings")
    findings: list[Any] = raw_findings if isinstance(raw_findings, list) else []
    raw_reviewers = data.get("reviewer_models")
    reviewers: list[Any] = raw_reviewers if isinstance(raw_reviewers, list) else []
    try:
        prompt_chars = int(data.get("prompt_chars") or 0)
    except (TypeError, ValueError):
        prompt_chars = 0
    try:
        cost_usd = float(data.get("cost_usd") or 0.0)
    except (TypeError, ValueError):
        cost_usd = 0.0
    return SkillReviewState(
        status=status,
        content_hash=str(data.get("content_hash") or ""),
        findings=[f for f in findings if isinstance(f, dict)],
        reviewer_models=[str(m) for m in reviewers if m],
        timestamp=str(data.get("timestamp") or ""),
        prompt_chars=prompt_chars,
        cost_usd=cost_usd,
        raw_result=str(data.get("raw_result") or ""),
    )


def save_review_state(
    drive_root: pathlib.Path,
    name: str,
    review: SkillReviewState,
) -> None:
    _atomic_write_json(
        skill_state_dir(drive_root, name) / "review.json",
        review.to_dict(),
    )


def requested_core_setting_keys(env_keys: List[str]) -> List[str]:
    """Return manifest-requested core keys that require explicit grants."""
    forbidden_upper = {key.upper() for key in FORBIDDEN_SKILL_SETTINGS}
    out: List[str] = []
    for raw_key in env_keys or []:
        key = str(raw_key or "").strip().upper()
        if key and key in forbidden_upper and key not in out:
            out.append(key)
    return out


def load_skill_grants(drive_root: pathlib.Path, name: str) -> Dict[str, Any]:
    data = _read_json(skill_state_dir(drive_root, name) / GRANTS_FILENAME)
    if not isinstance(data, dict):
        return {"granted_keys": [], "updated_at": ""}
    keys = []
    for raw_key in data.get("granted_keys") or []:
        key = str(raw_key or "").strip().upper()
        if key and key not in keys:
            keys.append(key)
    requested = []
    for raw_key in data.get("requested_keys") or []:
        key = str(raw_key or "").strip().upper()
        if key and key not in requested:
            requested.append(key)
    return {
        "granted_keys": keys,
        "requested_keys": requested,
        "content_hash": str(data.get("content_hash") or ""),
        "updated_at": str(data.get("updated_at") or ""),
    }


def save_skill_grants(
    drive_root: pathlib.Path,
    name: str,
    granted_keys: List[str],
    *,
    content_hash: str,
    requested_keys: List[str],
) -> None:
    """Persist a skill key grant.

    The new ``granted_keys`` are merged with any previously persisted
    grants for the SAME content hash + manifest-requested set. This
    matters when a caller approves only a subset of the requested keys
    in one bridge call: without merging, a later partial call would
    silently revoke earlier approvals. Any change to the manifest's
    requested set or the skill's content hash invalidates the prior
    persisted state and starts fresh — that is the correct behavior
    because the owner has not yet consented to the new request.
    """
    allowed = set(requested_core_setting_keys(requested_keys))
    existing = load_skill_grants(drive_root, name)
    persisted_match = (
        str(existing.get("content_hash") or "") == str(content_hash or "")
        and sorted(existing.get("requested_keys") or []) == sorted(allowed)
    )
    merged: List[str] = []
    if persisted_match:
        for raw_key in existing.get("granted_keys") or []:
            key = str(raw_key or "").strip().upper()
            if key and key in allowed and key not in merged:
                merged.append(key)
    for raw_key in granted_keys or []:
        key = str(raw_key or "").strip().upper()
        if key and key in allowed and key not in merged:
            merged.append(key)
    _atomic_write_json(
        skill_state_dir(drive_root, name) / GRANTS_FILENAME,
        {
            "granted_keys": merged,
            "requested_keys": sorted(allowed),
            "content_hash": str(content_hash or ""),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def grant_status_for_skill(drive_root: pathlib.Path, skill: LoadedSkill) -> Dict[str, Any]:
    requested = requested_core_setting_keys(list(skill.manifest.env_from_settings or []))
    grants = load_skill_grants(drive_root, skill.name)
    grant_hash_ok = str(grants.get("content_hash") or "") == str(skill.content_hash or "")
    grant_request_ok = sorted(grants.get("requested_keys") or []) == sorted(requested)
    persisted_grants = set(grants.get("granted_keys") or []) if grant_hash_ok and grant_request_ok else set()
    granted = [key for key in requested if key in persisted_grants]
    missing = [key for key in requested if key not in set(granted)]
    review_ready = skill.review.status == _REVIEW_STATUS_PASS and not skill.review.is_stale_for(skill.content_hash)
    # v5.2.2 dual-track grants: both ``script`` and ``extension`` skills
    # are eligible for owner core-key grants. ``script`` skills get the
    # grant via ``_scrub_env`` for their subprocess; ``extension``
    # skills get it via ``PluginAPIImpl.get_settings`` for their
    # in-process plugin code. Other manifest types (``instruction``)
    # cannot receive core keys at all.
    eligible_type = skill.manifest.is_script() or skill.manifest.is_extension()
    unsupported = bool(requested and not eligible_type)
    return {
        "requested_keys": requested,
        "granted_keys": granted,
        "missing_keys": missing,
        "all_granted": not missing and not unsupported,
        "usable": review_ready and not missing and not unsupported,
        "unsupported_for_skill_type": unsupported,
        "content_hash": grants.get("content_hash", ""),
        "updated_at": grants.get("updated_at", ""),
    }


# ---------------------------------------------------------------------------
# Discovery / loading
# ---------------------------------------------------------------------------


def _safe_listdir(root: pathlib.Path) -> List[pathlib.Path]:
    try:
        return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        log.warning("Failed to list skills repo %s", root, exc_info=True)
        return []


def _looks_like_skill_dir(path: pathlib.Path) -> bool:
    """Return True when ``path`` directly contains a SKILL.md / skill.json.

    Used by the recursive ``data/skills/`` walker to decide whether a
    sub-directory is itself a skill package or a grouping container
    (``data/skills/native/``, ``data/skills/clawhub/``, ...). Without
    this gate, the walker would also try to ``load_skill(data/skills/)``
    and emit a confusing 'no manifest' load_error for the root.
    """
    if not path.is_dir():
        return False
    for candidate in _MANIFEST_NAMES:
        if (path / candidate).is_file():
            return True
    return False


def load_skill(
    skill_dir: pathlib.Path,
    drive_root: pathlib.Path,
) -> Optional[LoadedSkill]:
    """Load one skill package into a ``LoadedSkill`` dataclass.

    Returns ``None`` when the directory has no manifest at all (which is
    the signal to callers that this is not a skill folder). A broken
    manifest is returned as a ``LoadedSkill`` with ``load_error`` populated
    so the catalogue UI can display the failure — the alternative of
    raising would hide the broken skill from the operator.
    """
    skill_dir = skill_dir.resolve()
    try:
        manifest_read = _manifest_text_for_dir(skill_dir)
    except _ManifestUnreadable as exc:
        broken_name = _sanitize_skill_name(skill_dir.name)
        return LoadedSkill(
            name=broken_name,
            skill_dir=skill_dir,
            manifest=SkillManifest(
                name=broken_name,
                description="",
                version="",
                type="instruction",
            ),
            content_hash="",
            load_error=f"manifest unreadable: {exc}",
        )
    if manifest_read is None:
        return None
    manifest_text, manifest_path = manifest_read

    try:
        manifest = parse_skill_manifest_text(manifest_text)
    except SkillManifestError as exc:
        broken_name = _sanitize_skill_name(skill_dir.name)
        return LoadedSkill(
            name=broken_name,
            skill_dir=skill_dir,
            manifest=SkillManifest(
                name=broken_name,
                description="",
                version="",
                type="instruction",
            ),
            content_hash="",
            load_error=f"manifest parse error: {exc}",
        )

    # The runtime / state / tool-surface identity is the DIRECTORY
    # BASENAME, not ``manifest.name``. Reasons:
    #
    # - Tool schemas (``skill_exec`` / ``review_skill`` / ``toggle_skill``)
    #   advertise ``skill`` as "the directory name inside
    #   OUROBOROS_SKILLS_REPO_PATH", which is exactly what an operator
    #   sees when they clone / extract / ``ls`` the skills repo.
    # - ``manifest.name`` is free-form display metadata (``Weather Skill``,
    #   ``Агент Погоды``); sanitising it would produce non-stable keys
    #   that change under renames or localisation tweaks.
    # - Directory-basename keys guarantee uniqueness against the
    #   filesystem, which is what the loader iterates anyway.
    #
    # Manifest-level ``name`` is still carried as the display label, and
    # is backfilled from the directory basename when the manifest omits it.
    if not manifest.name:
        manifest.name = skill_dir.name

    name = _sanitize_skill_name(skill_dir.name)
    load_error = ""
    try:
        content_hash = compute_content_hash(
            skill_dir,
            manifest_entry=manifest.entry,
            manifest_scripts=manifest.scripts,
        )
    except SkillPayloadUnreadable as exc:
        content_hash = ""
        load_error = f"payload unreadable: {exc}"
    enabled = load_enabled(drive_root, name)
    review = load_review_state(drive_root, name)

    # Phase 4 ships the extension loader (``ouroboros.extension_loader``),
    # so ``type: extension`` skills now go through the same review +
    # enable + hash-freshness gate as ``type: script`` skills. The
    # ``pending_phase4`` overlay is retired; extensions land in whatever
    # status review actually persisted (``pending`` pre-review, ``pass``
    # after a clean tri-model verdict, etc.). ``skill_exec`` still
    # refuses them (extensions don't execute through the subprocess
    # substrate — they register through ``PluginAPI``), but the catalogue
    # reflects their true state.

    return LoadedSkill(
        name=name,
        skill_dir=skill_dir,
        manifest=manifest,
        content_hash=content_hash,
        enabled=enabled,
        review=review,
        load_error=load_error,
    )


def _bundled_skills_dir() -> Optional[pathlib.Path]:
    """Return the legacy bundled reference-skills directory (``repo/skills/``).

    Retained for backward compatibility with tests that still ``monkeypatch``
    this symbol to point at fixture trees. Production discovery no longer
    consults this path directly: the launcher bootstrap copies the seed
    one-shot into ``data/skills/native/`` (see ``launcher_bootstrap.bootstrap_native_skills``),
    and ``discover_skills`` walks the data plane after that.

    Returns ``None`` if the bundled folder is missing (which is fine in
    a packaged build that strips the reference skills).
    """
    from ouroboros.config import REPO_DIR

    candidate = pathlib.Path(REPO_DIR) / "skills"
    if candidate.is_dir():
        return candidate
    fallback = pathlib.Path(__file__).resolve().parents[1] / "skills"
    return fallback if fallback.is_dir() else None


def _resolve_data_skills_dir(
    drive_root: Optional[pathlib.Path] = None,
) -> Optional[pathlib.Path]:
    """Return the data-plane skills root if it exists on disk.

    Pure READ — does NOT create the directory. The bootstrap path
    (``launcher_bootstrap.ensure_data_skills_seeded``) and the
    marketplace install pipeline call ``config.ensure_data_skills_dir``
    explicitly when they want to materialise the layout.

    When ``drive_root`` is supplied, the skills root is derived from
    that argument verbatim. Otherwise we read
    ``ouroboros.config.DATA_DIR`` at call time. Returns ``None`` if
    the directory does not exist on disk (e.g. a fresh checkout that
    has not been launched yet).
    """
    if drive_root is not None:
        candidate = pathlib.Path(drive_root) / "skills"
        return candidate if candidate.is_dir() else None
    try:
        from ouroboros.config import DATA_DIR, resolve_data_skills_dir
        return resolve_data_skills_dir(DATA_DIR)
    except Exception:
        return None


_ORPHAN_NAME_FRAGMENTS = (".replaced-", ".staging-", ".tmp-")


def _is_orphan_marker_name(name: str) -> bool:
    """Return True for transient backup/staging directory names.

    The marketplace install pipeline (``install.py::_land_staged_into_data_plane``)
    moves the previous version of a skill aside as
    ``<slug>.replaced-<sha8>`` before swapping in the fresh tree. On a
    crash mid-swap (or if ``shutil.rmtree(sibling, ignore_errors=True)``
    silently fails for filesystem reasons), that sibling can be left
    behind. Without this filter, ``discover_skills`` would surface
    those orphans as if they were live skills, attaching Update /
    Uninstall affordances to a stale snapshot.
    """
    cleaned = (name or "").strip()
    if not cleaned:
        return False
    return any(token in cleaned for token in _ORPHAN_NAME_FRAGMENTS)


def _walk_skill_packages(
    root: pathlib.Path,
) -> List[pathlib.Path]:
    """Yield every skill package directly under ``root`` or one level deep.

    The data plane uses an intentionally-shallow layout::

        data/skills/native/<slug>/      -- skill package
        data/skills/clawhub/<slug>/     -- skill package
        data/skills/external/<slug>/    -- skill package

    so we walk root + each immediate sub-directory and emit any child
    that owns a ``SKILL.md`` / ``skill.json``. Deeper nesting is
    deliberately NOT explored — a misclick that drops a SKILL.md
    five levels down stays invisible rather than silently auto-loading.

    Transient backup directories left behind by interrupted installs
    (``<slug>.replaced-<sha8>``, ``<slug>.staging-<sha8>``,
    ``<slug>.tmp-<sha8>``) are filtered out — see
    :func:`_is_orphan_marker_name`.
    """
    out: List[pathlib.Path] = []
    if not root.is_dir():
        return out
    if _looks_like_skill_dir(root):
        # Edge case: the root itself is a skill (back-compat with
        # ``OUROBOROS_SKILLS_REPO_PATH`` pointing AT a single-skill folder).
        out.append(root)
        return out
    for child in _safe_listdir(root):
        if _is_orphan_marker_name(child.name):
            continue
        if _looks_like_skill_dir(child):
            out.append(child)
            continue
        # One level deeper for grouping containers (the
        # ``native`` / ``clawhub`` / ``external`` subdirs of the
        # data-plane root).
        for grandchild in _safe_listdir(child):
            if _is_orphan_marker_name(grandchild.name):
                continue
            if _looks_like_skill_dir(grandchild):
                out.append(grandchild)
    return out


def _classify_skill_source(
    skill_dir: pathlib.Path,
    *,
    data_skills_root: Optional[pathlib.Path],
    user_repo_root: Optional[pathlib.Path],
) -> str:
    """Return the discovery-source tag for a skill directory.

    Order of resolution:

    1. If the path lives under ``data/skills/<bucket>/...`` AND
       ``<bucket>`` is one of ``native``/``clawhub``/``external``,
       return that literal bucket. ``native`` carries an extra
       authenticity gate (BIBLE.md P6 honesty fix from cycle 1
       Ouroboros review O3): the package must own a sibling
       ``.seed-origin`` marker file (written by the launcher
       bootstrap when it copied the seed). A skill that a user
       manually dropped into ``data/skills/native/`` lacks the
       marker and is reclassified as ``external`` so the UI badge
       does not falsely claim launcher-seeded provenance.
    2. If the path lives under the user-configured
       ``OUROBOROS_SKILLS_REPO_PATH``, return ``user_repo``.
    3. Fallback: ``external``.
    """
    from ouroboros.config import (
        SKILL_SOURCE_CLAWHUB,
        SKILL_SOURCE_EXTERNAL,
        SKILL_SOURCE_NATIVE,
        SKILL_SOURCE_OUROBOROSHUB,
        SKILL_SOURCE_SUBDIRS,
        SKILL_SOURCE_USER_REPO,
    )
    try:
        resolved = skill_dir.resolve()
    except OSError:
        return SKILL_SOURCE_EXTERNAL
    if data_skills_root is not None:
        try:
            rel = resolved.relative_to(data_skills_root.resolve())
            parts = rel.parts
            if parts:
                bucket = parts[0]
                if bucket in SKILL_SOURCE_SUBDIRS:
                    if bucket == SKILL_SOURCE_NATIVE:
                        # Honesty gate — only mark as ``native`` when
                        # the launcher actually seeded this package
                        # (per-skill ``.seed-origin`` marker present).
                        # Legacy pre-v4.50 native skills that pre-date
                        # the marker pattern are reclassified as
                        # ``external``: there is no way to tell at
                        # discovery time whether they came from a
                        # launcher seed or a manual user drop, so the
                        # safe answer is "user-managed external".
                        if (resolved / ".seed-origin").is_file():
                            return SKILL_SOURCE_NATIVE
                        return SKILL_SOURCE_EXTERNAL
                    if bucket == SKILL_SOURCE_CLAWHUB:
                        # Mirror the ``native`` honesty gate for the
                        # ``clawhub`` bucket. The marketplace install
                        # pipeline drops ``.clawhub.json`` (provenance
                        # sidecar) at the skill root; without it,
                        # treating an arbitrary sub-directory as
                        # marketplace-installed would attach Update /
                        # Uninstall affordances to unverified content
                        # (cycle 2 Ouroboros own-pipeline finding).
                        if (resolved / ".clawhub.json").is_file():
                            return SKILL_SOURCE_CLAWHUB
                        return SKILL_SOURCE_EXTERNAL
                    if bucket == SKILL_SOURCE_OUROBOROSHUB:
                        if (resolved / ".ouroboroshub.json").is_file():
                            return SKILL_SOURCE_OUROBOROSHUB
                        return SKILL_SOURCE_EXTERNAL
                    return bucket
            # Unknown bucket (e.g. user dropped a skill directly under
            # ``data/skills/`` or under a custom subdir). Treat as
            # ``external`` rather than ``native``.
            return SKILL_SOURCE_EXTERNAL
        except ValueError:
            pass
    if user_repo_root is not None:
        try:
            resolved.relative_to(user_repo_root.resolve())
            return SKILL_SOURCE_USER_REPO
        except ValueError:
            pass
    return SKILL_SOURCE_EXTERNAL


def discover_skills(
    drive_root: pathlib.Path,
    repo_path: str | None = None,
    *,
    include_bundled: bool = True,
) -> List[LoadedSkill]:
    """Scan the data-plane skills tree (and optional external checkouts).

    Discovery walks, in order:

    1. ``data/skills/native/`` + ``data/skills/clawhub/`` +
       ``data/skills/external/`` — the in-data-plane runtime location
       since v4.50. Subdirectory names map directly to the skill's
       ``source`` tag on the resulting :class:`LoadedSkill`.
    2. ``OUROBOROS_SKILLS_REPO_PATH`` — optional extra discovery root
       for users who keep skills in their own git checkout. Skills
       discovered here are tagged ``user_repo``.
    3. ``include_bundled`` is retained for back-compat with tests that
       still monkey-patch ``_bundled_skills_dir``: when the data plane
       has no skills yet AND a bundled directory exists, we fall through
       to it (read-only, source=``native``). Production callers should
       rely on the launcher bootstrap to copy the seed into
       ``data/skills/native/`` exactly once.

    Duplicate basenames across roots surface as sanitised-name
    collisions via the existing collision detector — the operator can
    rename the directories before tools can act on the skill.
    """
    if repo_path is None:
        from ouroboros.config import get_skills_repo_path
        repo_path = get_skills_repo_path()
    repo_path = str(repo_path or "").strip()

    data_skills_root = _resolve_data_skills_dir(drive_root)
    user_repo_root: Optional[pathlib.Path] = None
    if repo_path:
        try:
            user_repo_candidate = pathlib.Path(repo_path).expanduser().resolve()
        except OSError:
            user_repo_candidate = None
        if user_repo_candidate is not None and user_repo_candidate.is_dir():
            user_repo_root = user_repo_candidate

    roots: List[pathlib.Path] = []
    if data_skills_root is not None:
        roots.append(data_skills_root)
    if user_repo_root is not None:
        # Avoid double-scanning if the user pointed OUROBOROS_SKILLS_REPO_PATH
        # at the data-plane root (unusual but possible during migration).
        if data_skills_root is None or user_repo_root != data_skills_root.resolve():
            roots.append(user_repo_root)

    # Back-compat fallback: only fire when the data plane has NEVER
    # been initialised — i.e. ``data/skills/`` does not exist on disk
    # at all. Once the bootstrap has run (even to copy zero skills),
    # the user's explicit emptying of ``data/skills/native/`` must
    # stick. v4.50 cycle-1 Ouroboros review O2: gating on "no skills
    # found" instead of "no data plane" silently resurrected deleted
    # seed skills, violating the "exactly once" docstring promise.
    skills: List[LoadedSkill] = []
    seen_dirs: set[pathlib.Path] = set()
    for root in roots:
        for entry in _walk_skill_packages(root):
            try:
                resolved = entry.resolve()
            except OSError:
                continue
            if resolved in seen_dirs:
                continue
            seen_dirs.add(resolved)
            loaded = load_skill(entry, drive_root)
            if loaded is None:
                continue
            loaded.source = _classify_skill_source(
                entry,
                data_skills_root=data_skills_root,
                user_repo_root=user_repo_root,
            )
            skills.append(loaded)

    data_plane_initialised = data_skills_root is not None
    if not skills and include_bundled and not data_plane_initialised:
        bundled = _bundled_skills_dir()
        if bundled is not None and bundled.is_dir():
            for entry in _walk_skill_packages(bundled):
                try:
                    resolved = entry.resolve()
                except OSError:
                    continue
                if resolved in seen_dirs:
                    continue
                seen_dirs.add(resolved)
                loaded = load_skill(entry, drive_root)
                if loaded is None:
                    continue
                loaded.source = "native"
                skills.append(loaded)

    # Detect collisions in the sanitised identity. Two distinct
    # directories ("hello world" and "hello_world") must never share
    # ``enabled.json`` / ``review.json`` — ``load_error`` every member of
    # the collision set so the operator can rename before tools can act
    # on the skill.
    by_name: Dict[str, List[LoadedSkill]] = {}
    for skill in skills:
        by_name.setdefault(skill.name, []).append(skill)
    for name, group in by_name.items():
        if len(group) > 1:
            dirs = ", ".join(str(s.skill_dir) for s in group)
            for skill in group:
                if not skill.load_error:
                    skill.load_error = (
                        f"Skill name collision: multiple checkout directories "
                        f"({dirs}) sanitise to {name!r}. Rename the directories "
                        "so their basenames yield distinct identifiers before "
                        "enabling / reviewing / executing."
                    )

    skills.sort(key=lambda s: s.name)
    return skills


def find_skill(
    drive_root: pathlib.Path,
    name: str,
    *,
    repo_path: str | None = None,
) -> Optional[LoadedSkill]:
    """Return one skill by name, or None. Skills with broken manifests
    are returned with ``load_error`` populated — the caller can then
    decide whether to surface them or ignore them."""
    safe = _sanitize_skill_name(name)
    for skill in discover_skills(drive_root, repo_path=repo_path):
        if skill.name == safe:
            return skill
    return None


def list_available_for_execution(
    drive_root: pathlib.Path,
    *,
    repo_path: str | None = None,
) -> List[LoadedSkill]:
    """Return only skills that are enabled + have a fresh PASS review."""
    return [
        s for s in discover_skills(drive_root, repo_path=repo_path)
        if s.available_for_execution and grant_status_for_skill(drive_root, s).get("usable", True)
    ]


# ---------------------------------------------------------------------------
# Status helpers consumed by /api/state and future Skills UI
# ---------------------------------------------------------------------------


def summarize_skills(drive_root: pathlib.Path) -> Dict[str, Any]:
    """Return a compact catalogue summary for the Skills UI / /api/state.

    v5.1.2 Frame A: ``runtime_mode`` no longer gates skill execution —
    ``available_for_execution`` and ``static_ready`` converge, and
    ``runtime_blocked`` is always 0. The fields stay in the schema for
    backward compatibility (UI, ``/api/state`` consumers) but the
    ``light`` mode no longer subtracts from ``available``.

    Does not include raw manifest bodies or review findings — callers
    that need the detail should call ``discover_skills`` directly.
    """
    skills = discover_skills(drive_root)
    from ouroboros.config import get_runtime_mode
    runtime_mode = get_runtime_mode()
    return {
        "count": len(skills),
        "runtime_mode": runtime_mode,
        "available": sum(
            1 for s in skills
            if is_runtime_eligible_for_execution(s)
            and grant_status_for_skill(drive_root, s).get("usable", True)
        ),
        "blocked_by_grants": sum(
            1 for s in skills
            if is_runtime_eligible_for_execution(s)
            and not grant_status_for_skill(drive_root, s).get("usable", True)
        ),
        "runtime_blocked": 0,  # v5.1.2: runtime_mode no longer gates skill execution.
        "pending_review": sum(
            1
            for s in skills
            if s.review.status in (_REVIEW_STATUS_PENDING, "")
            or (
                s.review.status == _REVIEW_STATUS_PASS
                and s.review.is_stale_for(s.content_hash)
            )
        ),
        "failed_review": sum(
            1 for s in skills if s.review.status == _REVIEW_STATUS_FAIL
        ),
        "advisory_review": sum(
            1 for s in skills if s.review.status == _REVIEW_STATUS_ADVISORY
        ),
        "broken": sum(1 for s in skills if s.load_error),
        "skills": [
            {
                "name": s.name,
                "type": s.manifest.type,
                "version": s.manifest.version,
                "enabled": s.enabled,
                "review_status": s.review.status,
                "review_stale": s.review.is_stale_for(s.content_hash),
                "available_for_execution": (
                    is_runtime_eligible_for_execution(s)
                    and grant_status_for_skill(drive_root, s).get("usable", True)
                ),
                "static_ready": s.available_for_execution,
                "blocked_by_grants": not grant_status_for_skill(drive_root, s).get("usable", True),
                "runtime_blocked_by_mode": False,  # v5.1.2: never blocked by mode.
                "load_error": s.load_error,
                "source": s.source,
            }
            for s in skills
        ],
    }


# ---------------------------------------------------------------------------
# Native-skill seeding helpers (lifted from launcher_bootstrap.py)
# ---------------------------------------------------------------------------


def _read_skill_manifest_version(skill_dir: pathlib.Path) -> str:
    """Best-effort scan of ``SKILL.md``/``skill.json`` for the version string.

    Used by the per-skill version-aware re-seed pass: when the launcher
    ships a newer reference version of a native skill (for example
    weather 0.1.0 ``type: script`` -> 0.2.0 ``type: extension``), the
    bootstrap detects the version mismatch and replaces the data-plane
    copy in place. The user's durable enable / review state under
    ``data/state/skills/<name>/`` survives because we never touch that
    plane during a re-seed.

    Cycle 1 GPT-critic (Findings 1–3): defers to
    :func:`ouroboros.contracts.skill_manifest.parse_skill_manifest_text`
    so the version we see here is exactly the version
    ``SkillManifest.version`` will report. The hand-rolled line scanner
    that lived here previously had three concrete edge-case bugs:
    inline YAML comments leaked into the version string, single-line
    JSON manifests returned ``""``, and ``version:`` lines that
    appeared in the body BEFORE the ``---`` frontmatter delimiter
    were accepted as the version. The shared parser handles all three
    cases correctly.

    Returns ``""`` if the manifest cannot be parsed (e.g., the file is
    missing or malformed). The resync pass treats empty-string as "do
    not upgrade", so a malformed seed manifest just disables the
    upgrade path for that skill until the operator fixes it.
    """
    for candidate in ("SKILL.md", "skill.json"):
        path = skill_dir / candidate
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            from ouroboros.contracts.skill_manifest import parse_skill_manifest_text
            manifest = parse_skill_manifest_text(text)
        except Exception:
            return ""
        return str(manifest.version or "").strip()
    return ""


def _record_skill_upgrade_migration(
    drive_root: pathlib.Path,
    skill_name: str,
    old_version: str,
    new_version: str,
    log_obj: Any,
) -> None:
    """Persist a migration record so the Skills UI can surface a banner.

    A native skill being replaced under the operator's feet (because a
    new launcher version bumped the seed manifest) silently invalidates
    the review state and any saved patterns / agent prompts that
    referenced the old type. We write a JSON record at
    ``data/state/migrations.json`` — the SPA reads it on mount via
    ``/api/migrations``, displays a one-shot banner on the Skills tab,
    and persists dismissal via ``/api/migrations/<key>/dismiss``.

    The format is intentionally append-only: ``{key: record}`` where
    ``key`` is ``"<new_version>_<skill_name>_upgrade"`` so a future
    upgrade of the same skill at a still-newer version writes a fresh
    record instead of mutating the old one.
    """
    state_dir = drive_root / "state"
    target = state_dir / "migrations.json"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        existing: dict[str, Any] = {}
        if target.is_file():
            try:
                existing = json.loads(target.read_text(encoding="utf-8")) or {}
                if not isinstance(existing, dict):
                    existing = {}
            except Exception:
                existing = {}
        # Use the NEW version in the key so each upgrade gets its own
        # record. Operators can dismiss old upgrades while still seeing
        # the next one when a future bump fires.
        key = f"v{new_version}_{skill_name}_upgrade"
        from datetime import datetime, timezone
        existing[key] = {
            "kind": "native_skill_upgrade",
            "skill": skill_name,
            "old_version": old_version,
            "new_version": new_version,
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "dismissed": False,
            "summary": (
                f"Native skill ``{skill_name}`` was upgraded from "
                f"{old_version} to {new_version} on launch. Re-review "
                f"may be required before the new version becomes "
                f"executable. Old skill_exec / extension call shapes "
                f"may need to be updated in saved patterns."
            ),
        }
        target.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:  # pragma: no cover - defensive
        log_obj.warning(
            "Failed to write migration record for %s: %s", skill_name, exc,
        )


def _reseed_native_skill_in_place(
    seed_skill: pathlib.Path,
    target_skill: pathlib.Path,
    log_obj: Any,
    *,
    drive_root: pathlib.Path | None = None,
    skill_name: str | None = None,
    old_version: str = "",
    new_version: str = "",
) -> bool:
    """Replace ``target_skill`` with ``seed_skill`` while preserving durable state.

    The durable state plane lives at ``data/state/skills/<name>/``,
    which is OUTSIDE this skill directory, so a recursive replace
    here does not touch ``enabled.json`` / ``review.json``. We DO
    delete the old skill files (including any user mods) — native
    skills are launcher-owned and the ``.seed-origin`` marker is the
    explicit signal of that ownership.

    When ``drive_root`` and version metadata are passed, also writes
    a migration record so the Skills UI can surface a banner about
    the upgrade. This closes the gap where the operator's
    `skill_exec(skill="weather", script="fetch.py")` invocations
    silently broke after the v5 weather skill type-flip; now they
    see an explicit "weather upgraded — re-review required" notice
    on the Skills page on first launch after the upgrade.
    """
    try:
        if target_skill.exists():
            shutil.rmtree(target_skill)
        shutil.copytree(seed_skill, target_skill)
        # Preserve the seed-origin contract on the freshly-replaced copy.
        (target_skill / ".seed-origin").write_text(
            f"seeded_from={seed_skill.parent.name}\nupgrade=true\n",
            encoding="utf-8",
        )
        if drive_root is not None and skill_name and old_version and new_version:
            _record_skill_upgrade_migration(
                drive_root, skill_name, old_version, new_version, log_obj,
            )
        return True
    except OSError as exc:
        log_obj.warning(
            "Failed to upgrade native skill in place %s -> %s: %s",
            seed_skill, target_skill, exc,
        )
        return False


def _per_skill_version_resync(
    seed_dir: pathlib.Path,
    native_root: pathlib.Path,
    log_obj: Any,
    *,
    drive_root: pathlib.Path | None = None,
) -> int:
    """Re-seed any native skill whose manifest version drifted from the bundled seed.

    Runs AFTER the first-time bootstrap (so ``.bootstrap-seed-complete``
    already exists). The pass ONLY upgrades skills that:

    - exist in both the seed and the target native bucket;
    - carry a ``.seed-origin`` marker (i.e. were originally seeded —
      not a user-dropped folder);
    - have a parseable manifest ``version`` on both sides AND the seed
      version differs from the installed version.

    Skills the user deleted from ``native/`` are left absent — the
    operator's deletion intent is sticky. Skills the user added by
    hand (no ``.seed-origin``) are never touched. New seed skills not
    yet present locally are NOT auto-landed during resync — that
    upgrade path is reserved for a fresh ``.bootstrap-seed-complete``
    cycle (delete the marker to receive newly-shipped reference
    skills). This protects the resurrection invariant
    (``test_bootstrap_marker_prevents_resurrection_after_user_deletion``).

    Returns the number of skills that were reseeded.
    """
    if not seed_dir.is_dir() or not native_root.is_dir():
        return 0
    upgraded = 0
    for entry in sorted(seed_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not any((entry / candidate).is_file() for candidate in ("SKILL.md", "skill.json")):
            continue
        target = native_root / entry.name
        if not target.exists():
            # User deleted (or never had) this seed skill — respect
            # their absence intent. The first-time bootstrap is the
            # only path that auto-lands a seed skill into the data
            # plane; resync exclusively upgrades.
            continue
        if not (target / ".seed-origin").is_file():
            # User-managed skill in native/ — never touch.
            continue
        seed_version = _read_skill_manifest_version(entry)
        target_version = _read_skill_manifest_version(target)
        if not seed_version or not target_version:
            continue
        if seed_version == target_version:
            continue
        log_obj.info(
            "Native skill %s version drift (seed=%s, installed=%s) — re-seeding",
            entry.name, seed_version, target_version,
        )
        if _reseed_native_skill_in_place(
            entry, target, log_obj,
            drive_root=drive_root,
            skill_name=entry.name,
            old_version=target_version,
            new_version=seed_version,
        ):
            upgraded += 1
    return upgraded


def _seed_skills_into(seed_dir: pathlib.Path, target_root: pathlib.Path, log_obj: Any) -> int:
    """Copy seed skills under ``seed_dir`` into ``target_root/native/`` once.

    Pure-fs helper extracted so source-mode startup paths (where there is
    no full ``BootstrapContext``) can also seed ``data/skills/native/``
    on first launch. Returns the number of skill packages copied.

    The "exactly once" guarantee is anchored to a ``.bootstrap-seed-complete``
    marker file written into ``data/skills/native/`` after the first
    successful seed. If the operator later deletes every native skill the
    directory becomes empty BUT the marker stays, so a subsequent launch
    correctly reads "bootstrap already happened" and does NOT resurrect
    the deleted skills (would otherwise violate the docstring promise +
    BIBLE.md P0 agency).
    """
    if not seed_dir.is_dir():
        return 0
    native_root = target_root / "native"
    try:
        # Use the canonical layout helper so a future change to bucket
        # names happens in one place. ``ensure_data_skills_dir`` takes
        # the parent of ``target_root`` (since ``target_root`` already
        # ends in ``skills``) — fall back to manual mkdir if the
        # helper is unavailable for any reason.
        try:
            from ouroboros.config import ensure_data_skills_dir
            ensure_data_skills_dir(target_root.parent)
        except Exception:
            target_root.mkdir(parents=True, exist_ok=True)
            for bucket in ("native", "clawhub", "external"):
                (target_root / bucket).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log_obj.warning("Skills data root setup failed: %s", exc)
        return 0

    marker_path = native_root / _SEED_COMPLETE_MARKER
    if marker_path.is_file():
        # Bootstrap already ran — even if every seeded skill has since
        # been deleted, the operator's intent stands.
        return 0

    # Pre-bootstrap legacy state: if there are existing entries but no
    # marker (e.g. an in-place upgrade from a pre-v4.50 install where
    # the user already had data/skills/native/ populated), treat that
    # as "already seeded by a different mechanism" and just write the
    # marker without re-copying.
    try:
        existing = [p for p in native_root.iterdir() if not p.name.startswith(".")]
    except OSError:
        existing = []
    if existing:
        try:
            marker_path.write_text(
                "Bootstrap inferred from pre-existing native/ contents.\n",
                encoding="utf-8",
            )
        except OSError as exc:
            log_obj.warning("Failed to write %s: %s", marker_path, exc)
        return 0

    copied = 0
    for entry in sorted(seed_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not any((entry / candidate).is_file() for candidate in ("SKILL.md", "skill.json")):
            continue
        dest = native_root / entry.name
        if dest.exists():
            continue
        try:
            shutil.copytree(entry, dest)
            # v4.50 fix (Ouroboros review O3): drop a per-skill seed
            # marker so ``_classify_skill_source`` can distinguish a
            # launcher-seeded skill from one a user manually dropped
            # into ``data/skills/native/``.
            (dest / ".seed-origin").write_text(
                f"seeded_from={seed_dir.name}\n", encoding="utf-8",
            )
            copied += 1
        except OSError as exc:
            log_obj.warning("Failed to copy seed skill %s -> %s: %s", entry, dest, exc)

    # Always write the marker after a bootstrap pass — even when 0
    # skills landed (seed_dir was empty, or every entry already
    # existed). The point is: "bootstrap has been attempted; do not
    # try again on subsequent launches".
    try:
        marker_path.write_text(
            f"Bootstrap-seed completed; copied {copied} skill(s) from {seed_dir}.\n",
            encoding="utf-8",
        )
    except OSError as exc:
        log_obj.warning("Failed to write %s: %s", marker_path, exc)

    if copied:
        log_obj.info(
            "Bootstrapped %d native skill(s) from seed %s into %s",
            copied, seed_dir, native_root,
        )
    return copied


def ensure_data_skills_seeded() -> int:
    """Source-mode entry point: seed ``data/skills/native/`` if empty
    AND reconcile native skills against the bundled seed when their
    manifest version changes.

    Two passes:

    1. ``_seed_skills_into`` — first-time copy of ``repo/skills/*`` into
       ``data/skills/native/*`` plus the durable
       ``.bootstrap-seed-complete`` marker. Idempotent: returns 0 when
       the marker already exists.
    2. ``_per_skill_version_resync`` — runs every launch (cheap text
       comparison of YAML frontmatter / JSON ``version`` field). When
       a launcher-shipped seed bumps a native skill's version, we
       replace the data-plane copy IN PLACE while preserving every
       file under ``data/state/skills/<name>/`` (enabled / review
       state). User-managed skills (no ``.seed-origin`` marker) are
       never touched.

    Returns the total number of native skill packages copied or
    upgraded across both passes. Best-effort and never raises.
    """
    import logging as _logging

    from ouroboros.config import DATA_DIR, REPO_DIR

    log_obj = _logging.getLogger(__name__)
    seed_dir = pathlib.Path(REPO_DIR) / "skills"
    target_root = pathlib.Path(DATA_DIR) / "skills"
    copied = _seed_skills_into(seed_dir, target_root, log_obj)
    drive_root = pathlib.Path(DATA_DIR)
    try:
        upgraded = _per_skill_version_resync(
            seed_dir, target_root / "native", log_obj,
            drive_root=drive_root,
        )
    except Exception:  # pragma: no cover - defensive
        log_obj.warning("Native skill version-resync raised", exc_info=True)
        upgraded = 0
    try:
        cleanup_orphaned_seed_markers(seed_dir, target_root / "native", log_obj)
    except Exception:  # pragma: no cover - defensive
        log_obj.warning("Orphaned seed-marker cleanup raised", exc_info=True)
    return copied + upgraded


def cleanup_orphaned_seed_markers(
    seed_dir: pathlib.Path,
    native_root: pathlib.Path,
    log_obj,
) -> None:
    """Strip ``.seed-origin`` markers from native skills whose seed has
    been removed from ``repo/skills/``.

    v5.7.0: ``video_gen`` was removed from the bundled seed in favour
    of the OuroborosHub-published copy, but existing user installs
    keep the on-disk ``data/skills/native/video_gen/`` directory plus
    its launcher-written ``.seed-origin`` marker. Without this helper
    those installs would still be classified as ``source: native``
    forever, even though the launcher no longer ships a seed for
    them. Removing the marker re-classifies them as ``source: external``
    (user-managed), which is honest about the runtime ownership.

    Idempotent: only strips a marker when the matching seed dir is
    absent. Never deletes payload files; the user keeps their copy."""
    if not native_root.is_dir():
        return
    for entry in native_root.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        marker = entry / ".seed-origin"
        if not marker.is_file():
            continue
        if (seed_dir / entry.name).is_dir():
            continue
        try:
            marker.unlink()
            log_obj.info(
                "Native skill %r seed has been removed from repo/skills/; "
                "re-classifying installed copy as external (user-managed).",
                entry.name,
            )
        except OSError:  # pragma: no cover - defensive
            log_obj.warning(
                "Failed to strip orphaned .seed-origin from %s",
                entry, exc_info=True,
            )


__all__ = [
    "LoadedSkill",
    "SkillReviewState",
    "VALID_REVIEW_STATUSES",
    "_SEED_COMPLETE_MARKER",
    "_per_skill_version_resync",
    "_read_skill_manifest_version",
    "_record_skill_upgrade_migration",
    "_reseed_native_skill_in_place",
    "_seed_skills_into",
    "cleanup_orphaned_seed_markers",
    "compute_content_hash",
    "discover_skills",
    "ensure_data_skills_seeded",
    "find_skill",
    "grant_status_for_skill",
    "is_runtime_eligible_for_execution",
    "list_available_for_execution",
    "load_enabled",
    "load_review_state",
    "load_skill_grants",
    "load_skill",
    "requested_core_setting_keys",
    "save_enabled",
    "save_review_state",
    "save_skill_grants",
    "skill_state_dir",
    "summarize_skills",
]
