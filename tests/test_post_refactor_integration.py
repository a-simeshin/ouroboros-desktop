"""Post-refactor integration scenarios for the runtime-simplification line.

This file is the MANDATORY in-process integration layer. It carries two
generations of post-refactor invariants:

Launcher / PyInstaller removal (remove-pyinstaller, retained here):

1. ``python server.py`` boots standalone (no launcher in the process tree)
   and serves the health endpoint.
2. ``server.py`` fails fast with a non-zero exit when REPO_DIR is not a
   git checkout — the ``ensure_repo_present()`` contract.
3. ``ouroboros.skill_loader.ensure_data_skills_seeded`` is callable from
   source-mode startup paths and writes the bootstrap marker.

   NOTE: the remove-pyinstaller effort also asserted that an extracted
   ``ouroboros.claude_runtime`` module was importable and exposed SDK
   validation symbols. That scenario is now intentionally INVERTED — the
   Claude Code SDK layer (gateway + ``claude_runtime``) was removed wholesale
   by the remove-claude-code-integration change, so the module is gone and
   importing it must raise ``ModuleNotFoundError``. See
   ``test_import_without_claude_agent_sdk`` below, which replaces the old
   ``test_claude_runtime_module_importable_post_migration`` and asserts the
   opposite contract.

Claude Code SDK removal (remove-claude-code-integration):

4. ``repo_commit`` drives the real in-process tool pipeline on a temp git
   repo and succeeds WITHOUT any advisory pre-review gate; no
   ``skip_advisory_pre_review`` parameter exists.
5. The blocking review triad (``OUROBOROS_REVIEW_ENFORCEMENT``) is still
   wired and still gates a commit per its mode — this is KEEP-list
   machinery, not Claude SDK advisory.
6. The built tool registry (full and core_only) excludes ``claude_code_edit``
   and ``claude_advisory_review`` / ``advisory_pre_review``.
7. With ``claude_agent_sdk`` forced absent the key modules still import, and
   the deleted Claude modules are physically gone.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest  # ty: ignore[unresolved-import]

# NOTE: this file is the User-Declared MANDATORY integration layer and its
# runner is the bare `uv run pytest -q tests/test_post_refactor_integration.py`
# (see specs/remove-claude-code-integration.md → Test Infrastructure). The
# repo-wide pyproject `addopts` carries `-m 'not integration'`, so a blanket
# `pytestmark = pytest.mark.integration` here would make that exact runner
# collect zero tests. These scenarios are genuinely in-process — they need NO
# real provider API keys (the LLM-bound triad review is stubbed) and no
# external services (real local git on tmp_path + a locally spawned
# server.py) — which is precisely what the `integration` marker is reserved
# for in this repo ("requires real provider API keys"). The two heavy
# subprocess server-boot scenarios keep an explicit `integration` mark so the
# default fast regress still skips them; everything else runs under the
# declared runner.

# httpx is a runtime project dep; import lazily via importorskip so the test
# is self-skipping if the env is misconfigured. ty: ignore — the validator
# hook runs in an isolated uv env without project deps installed.
httpx = pytest.importorskip("httpx")

# Repo top — contains server.py, skills/, ouroboros/, etc.
REPO_ROOT = Path(__file__).resolve().parent.parent
# Use the current interpreter (the venv python pytest is running under).
PYTHON = sys.executable

# /api/health is the actual route registered in server.py (Route("/api/health", ...))
HEALTH_PATH = "/api/health"


def _free_port() -> int:
    """Bind to port 0, get the assigned port, release immediately."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(port: int, timeout: float = 30.0) -> dict:
    """Poll the health endpoint until 200 or timeout."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}{HEALTH_PATH}", timeout=2.0)
            if r.status_code == 200:
                ctype = r.headers.get("content-type", "")
                return r.json() if ctype.startswith("application/json") else {"raw": r.text}
        except Exception as exc:
            last_exc = exc
        time.sleep(0.5)
    raise AssertionError(
        f"{HEALTH_PATH} did not return 200 within {timeout}s; last error: {last_exc!r}"
    )


def _terminate(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@pytest.mark.integration
def test_server_boots_standalone_and_health_returns_200(tmp_path):
    """python server.py starts standalone (no launcher), serves /api/health."""
    port = _free_port()
    # Isolate DATA_DIR per-test so the spawned server doesn't write into
    # ~/Ouroboros/data on the developer machine.
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "OUROBOROS_REPO_DIR": str(REPO_ROOT),
        "OUROBOROS_DATA_DIR": str(data_dir),
    }
    cmd = [PYTHON, str(REPO_ROOT / "server.py"), "--port", str(port), "--host", "127.0.0.1"]
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        body = _wait_health(port, timeout=30.0)
        assert isinstance(body, dict)
        assert body.get("status") == "ok", f"unexpected health body: {body!r}"
        # No launcher in the spawned process command line — exactly two
        # supported entry points post-refactor: `python server.py` and `docker run`.
        cmdline = " ".join(cmd)
        assert "launcher" not in cmdline.lower(), f"launcher in cmdline: {cmdline!r}"
    finally:
        _terminate(proc)


@pytest.mark.integration
def test_server_fails_fast_when_repo_dir_missing():
    """ensure_repo_present() raises SystemExit when REPO_DIR is not a git checkout."""
    with tempfile.TemporaryDirectory() as td:
        # tmp dir has no .git, OUROBOROS_REPO_DIR override forces server.py to
        # resolve REPO_DIR to it, so ensure_repo_present() should bail before uvicorn.
        result = subprocess.run(
            [PYTHON, str(REPO_ROOT / "server.py"), "--port", "0"],
            cwd=str(REPO_ROOT),
            env={**os.environ, "OUROBOROS_REPO_DIR": td},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0, (
            f"expected nonzero exit, got {result.returncode}; "
            f"stdout={result.stdout!r}; stderr={result.stderr!r}"
        )
        combined = result.stdout + result.stderr
        assert "REPO_DIR not found" in combined, (
            f"missing fail-fast marker; output: {combined[:500]!r}"
        )


def _init_temp_git_repo(repo_dir: Path, branch: str = "ouroboros") -> str:
    """Create a real git repo with one baseline commit; return its HEAD sha.

    Real git on a tmp_path tree — the integration layer deliberately avoids
    mocking the git plumbing. Only the LLM-bound triad review is stubbed by
    callers, because there is no external review service available in-process.
    """
    repo_dir.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(  # noqa: E731
        a, cwd=str(repo_dir), capture_output=True, text=True, check=True
    )
    run("git", "init", "-q", "-b", branch, ".")
    run("git", "config", "user.email", "integration@ouroboros.test")
    run("git", "config", "user.name", "Ouroboros Integration")
    (repo_dir / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    run("git", "add", "module.py")
    run("git", "commit", "-q", "-m", "baseline")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _head_sha(repo_dir: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_repo_commit_without_advisory_gate(tmp_path, monkeypatch):
    """repo_commit drives the real in-process pipeline and commits with no
    advisory pre-review gate, and exposes no skip_advisory_pre_review param.

    This is the central post-removal invariant: before the
    remove-claude-code-integration change a fresh ``repo_commit`` was blocked
    with ``ADVISORY_PRE_REVIEW_REQUIRED`` until ``advisory_pre_review`` ran.
    Now the advisory gate is gone entirely — a clean commit must pass through
    the real tool pipeline (real git add/commit, real attempt ledger, real
    fingerprinting) with only the LLM-bound triad review stubbed (no review
    service exists in-process).
    """
    import inspect
    from unittest.mock import patch

    from ouroboros.tools import git as git_mod
    from ouroboros.tools.registry import ToolContext

    repo_dir = tmp_path / "repo"
    drive_root = tmp_path / "drive"
    (drive_root / "state").mkdir(parents=True)
    (drive_root / "logs").mkdir(parents=True)
    head_before = _init_temp_git_repo(repo_dir)
    (repo_dir / "module.py").write_text("VALUE = 2  # changed\n", encoding="utf-8")

    # No advisory gate must exist anywhere on the commit surface.
    sig = inspect.signature(git_mod._repo_commit_push)
    assert "skip_advisory_pre_review" not in sig.parameters, (
        "repo_commit handler must not carry a skip_advisory_pre_review param"
    )
    commit_tool = next(t for t in git_mod.get_tools() if t.name == "repo_commit")
    props = commit_tool.schema["parameters"]["properties"]
    assert "skip_advisory_pre_review" not in props, (
        "repo_commit JSON schema must not expose skip_advisory_pre_review"
    )
    assert not hasattr(git_mod, "_check_advisory_freshness"), (
        "_check_advisory_freshness must be gone from git.py"
    )

    monkeypatch.setenv("OUROBOROS_PRE_PUSH_TESTS", "0")
    ctx = ToolContext(repo_dir=repo_dir, drive_root=drive_root)

    # Only the LLM-bound triad review is stubbed (no review service available
    # in-process); everything else is the real production pipeline.
    with patch.object(
        git_mod, "_run_parallel_review", return_value=(None, None, "", [])
    ), patch.object(
        git_mod,
        "_aggregate_review_verdict",
        return_value=(False, None, "", [], []),
    ):
        result = git_mod._repo_commit_push(
            ctx, "integration: clean commit", skip_tests=True
        )

    assert "ADVISORY_PRE_REVIEW_REQUIRED" not in result, (
        f"advisory gate must not fire on a clean commit: {result!r}"
    )
    assert result.lower().startswith("ok") or "committed" in result.lower(), (
        f"expected a successful commit, got: {result!r}"
    )
    head_after = _head_sha(repo_dir)
    assert head_after != head_before, (
        f"HEAD must advance — commit was not created (result={result!r})"
    )
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "integration: clean commit" in log


def test_blocking_review_triad_still_functions(tmp_path, monkeypatch):
    """The blocking review triad (OUROBOROS_REVIEW_ENFORCEMENT) is still wired
    and still gates a commit per its mode — this is KEEP-list machinery.

    The Claude SDK advisory layer was removed, but the separate blocking
    review triad must NOT regress. We assert (a) the enforcement switch reads
    both modes from config, and (b) when the triad returns a critical block
    the real commit pipeline refuses to create the commit (HEAD unchanged).
    """
    from unittest.mock import patch

    from ouroboros import config as ouroboros_config
    from ouroboros.tools import git as git_mod
    from ouroboros.tools.registry import ToolContext

    # (a) The enforcement switch is live and round-trips both modes.
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    assert ouroboros_config.get_review_enforcement() == "advisory"
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    assert ouroboros_config.get_review_enforcement() == "blocking"

    repo_dir = tmp_path / "repo"
    drive_root = tmp_path / "drive"
    (drive_root / "state").mkdir(parents=True)
    (drive_root / "logs").mkdir(parents=True)
    head_before = _init_temp_git_repo(repo_dir)
    (repo_dir / "module.py").write_text("VALUE = 3  # risky\n", encoding="utf-8")

    monkeypatch.setenv("OUROBOROS_PRE_PUSH_TESTS", "0")
    ctx = ToolContext(repo_dir=repo_dir, drive_root=drive_root)

    block_msg = "⚠️ REVIEW_BLOCKED: Critical issues found."
    critical = [{
        "item": "tests_affected",
        "verdict": "FAIL",
        "severity": "critical",
        "reason": "no tests for risky change",
    }]

    with patch.object(
        git_mod,
        "_run_parallel_review",
        return_value=(block_msg, None, "critical_findings", []),
    ), patch.object(
        git_mod,
        "_aggregate_review_verdict",
        return_value=(True, block_msg, "critical_findings", critical, []),
    ):
        result = git_mod._repo_commit_push(
            ctx, "integration: should be blocked", skip_tests=True
        )

    assert "REVIEW_BLOCKED" in result, (
        f"blocking triad must surface its block message, got: {result!r}"
    )
    head_after = _head_sha(repo_dir)
    assert head_after == head_before, (
        "blocking triad must prevent the commit — HEAD must not advance"
    )

    # The blocking ledger (KEEP-list) recorded the blocked attempt and the
    # severity-tagged finding survived the advisory removal.
    from ouroboros.review_state import load_state

    state = load_state(drive_root)
    blocked = [
        a for a in state.blocking_history if a.status == "blocked"
    ]
    assert blocked, "blocking attempt must be persisted to the blocking ledger"
    # CommitAttemptRecord.advisory_findings is the blocking-triad severity tag
    # (KEEP), distinct from the removed Claude advisory machinery.
    assert hasattr(blocked[-1], "advisory_findings"), (
        "CommitAttemptRecord.advisory_findings severity tag must be retained"
    )


def test_tool_registry_excludes_claude_tools(tmp_path):
    """Both the full and core_only tool sets exclude claude_code_edit and
    claude_advisory_review / advisory_pre_review."""
    from ouroboros.tools.registry import ToolRegistry

    repo_dir = Path(__file__).resolve().parent.parent
    drive_root = tmp_path / "drive"
    drive_root.mkdir(parents=True)

    registry = ToolRegistry(repo_dir=repo_dir, drive_root=drive_root)

    banned = {"claude_code_edit", "claude_advisory_review", "advisory_pre_review"}

    available = set(registry.available_tools())
    assert not (available & banned), (
        f"loaded tool registry still contains Claude tools: {available & banned}"
    )

    full_schema_names = {
        s["function"]["name"] for s in registry.schemas(core_only=False)
    }
    core_schema_names = {
        s["function"]["name"] for s in registry.schemas(core_only=True)
    }
    assert not (full_schema_names & banned), (
        f"full tool schema set leaks Claude tools: {full_schema_names & banned}"
    )
    assert not (core_schema_names & banned), (
        f"core_only tool schema set leaks Claude tools: {core_schema_names & banned}"
    )
    # Sanity: the registry is non-empty and still serves the core surface,
    # so the absence of Claude tools is a real exclusion, not an empty build.
    assert "repo_commit" in full_schema_names
    assert "repo_commit" in core_schema_names


def test_import_without_claude_agent_sdk(monkeypatch):
    """With claude_agent_sdk forced absent the key modules still import, and
    the deleted Claude SDK modules are physically gone.

    This inverts the inherited remove-pyinstaller scenario
    ``test_claude_runtime_module_importable_post_migration`` (which asserted
    ``ouroboros.claude_runtime`` was importable). The SDK layer is now removed
    wholesale, so the opposite contract holds.
    """
    import importlib

    # Force the SDK absent regardless of whether it's installed in the env.
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)

    key_modules = [
        "ouroboros",
        "ouroboros.tools.shell",
        "ouroboros.tools.registry",
        "ouroboros.tools.commit_gate",
        "ouroboros.review_state",
        "server",
    ]
    for mod_name in key_modules:
        mod = importlib.import_module(mod_name)
        assert mod is not None, f"{mod_name} failed to import without the SDK"

    # The deleted Claude SDK modules must be physically absent on disk.
    repo_root = Path(__file__).resolve().parent.parent
    assert not (repo_root / "ouroboros" / "gateways" / "claude_code.py").exists(), (
        "ouroboros/gateways/claude_code.py must be deleted"
    )
    assert not (repo_root / "ouroboros" / "claude_runtime.py").exists(), (
        "ouroboros/claude_runtime.py must be deleted"
    )

    # And importing the removed module must raise ModuleNotFoundError — the
    # exact inversion of the old remove-pyinstaller assertion.
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("ouroboros.claude_runtime")


def test_skill_loader_ensure_data_skills_seeded_post_migration(tmp_path, monkeypatch):
    """ensure_data_skills_seeded callable from source-mode startup paths.

    Builds a minimal repo/skills layout with a single seed skill, points
    DATA_DIR + REPO_DIR (in ouroboros.config) at tmp_path, invokes the
    function, and asserts the bootstrap marker landed.
    """
    from ouroboros import config as ouroboros_config
    from ouroboros import skill_loader

    repo_dir = tmp_path / "repo"
    data_dir = tmp_path / "data"
    seed_skills = repo_dir / "skills" / "demo_skill"
    seed_skills.mkdir(parents=True)
    (seed_skills / "SKILL.md").write_text(
        "---\nname: demo_skill\nversion: 0.1\n---\n# Demo seed skill\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(ouroboros_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(ouroboros_config, "REPO_DIR", repo_dir)

    copied = skill_loader.ensure_data_skills_seeded()

    native_root = data_dir / "skills" / "native"
    marker = native_root / skill_loader._SEED_COMPLETE_MARKER
    assert marker.is_file(), f"bootstrap marker missing at {marker}"
    assert (native_root / "demo_skill" / "SKILL.md").is_file(), (
        "seed skill payload not copied into native bucket"
    )
    assert copied >= 1, f"expected >=1 skill copied, got {copied}"
