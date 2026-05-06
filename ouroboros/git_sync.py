"""Git remote auto-sync helpers (bootstrap pull on start, push on shutdown)."""
from __future__ import annotations

import logging
import subprocess
from typing import Optional, Tuple

log = logging.getLogger(__name__)


def resolve_remote_config(settings: dict) -> Optional[Tuple[str, str, str]]:
    """Return ``(remote_url, username, password)`` or ``None``.

    Selection priority:

    1. Generic ``OUROBOROS_GIT_REMOTE_URL`` (any HTTPS git server).
       Username defaults to ``"x-access-token"`` for compatibility with
       GitHub-style PAT auth when only a password/token is provided.
    2. Legacy ``GITHUB_TOKEN`` + ``GITHUB_REPO`` pair, assembled into a
       canonical ``https://github.com/<repo>.git`` URL with the
       ``x-access-token`` username.

    Returns ``None`` when neither configuration is present.
    """
    url = (settings.get("OUROBOROS_GIT_REMOTE_URL") or "").strip()
    if url:
        username = (settings.get("OUROBOROS_GIT_USERNAME") or "").strip() or "x-access-token"
        password = settings.get("OUROBOROS_GIT_PASSWORD") or ""
        return (url, username, password)

    gh_token = (settings.get("GITHUB_TOKEN") or "").strip()
    gh_repo = (settings.get("GITHUB_REPO") or "").strip()
    if gh_token and gh_repo:
        return (f"https://github.com/{gh_repo}.git", "x-access-token", gh_token)

    return None


def _try_acquire_lock(timeout: int = 60):
    """Best-effort attempt to acquire the shared git lock.

    Returns the acquired ``pathlib.Path`` lock handle, or ``None`` if the
    helper cannot be used (e.g. ``ToolContext`` cannot be constructed at
    early startup, or the timeout elapses). The bootstrap pull is
    idempotent and runs before workers, so proceeding without a lock is
    safe — we still try to coordinate when possible.
    """
    try:
        from ouroboros.tools.git import _acquire_git_lock  # type: ignore
        from ouroboros.tools.registry import ToolContext  # type: ignore
        from supervisor import git_ops as _git_ops  # type: ignore
    except Exception as exc:
        log.debug("[git_sync] lock helper unavailable: %s", exc)
        return None

    try:
        ctx = ToolContext(repo_dir=_git_ops.REPO_DIR, drive_root=_git_ops.DRIVE_ROOT)
        return _acquire_git_lock(ctx, timeout_sec=timeout)
    except Exception as exc:
        log.warning("[git_sync] git lock unavailable: %s", exc)
        return None


def _release_lock(lock_handle) -> None:
    """Release the lock acquired by :func:`_try_acquire_lock`. Never raises."""
    if lock_handle is None:
        return
    try:
        from ouroboros.tools.git import _release_git_lock  # type: ignore

        _release_git_lock(lock_handle)
    except Exception as exc:
        log.debug("[git_sync] lock release failed: %s", exc)


def bootstrap_remote_sync(settings: dict) -> Tuple[str, str]:
    """git fetch + pull --ff-only on startup. Idempotent. Never raises.

    Returns ``(status, reason)`` where status is one of
    ``"skipped" | "ok" | "rescue" | "error"``.

    On non-FF pull a rescue snapshot is created and the function returns
    ``("rescue", ...)``; on transient/network failure a warning is logged
    and ``("error", ...)`` is returned. The caller must never let this
    abort server startup — it is wrapped in defensive try/except at the
    call site.
    """
    from supervisor import git_ops

    cfg = resolve_remote_config(settings)
    if cfg is None:
        return ("skipped", "no remote configured")
    url, user, password = cfg

    # 1) Configure remote (idempotent — set-url if exists, add otherwise).
    try:
        ok, reason = git_ops.configure_remote_url(url, user, password)
    except Exception as exc:
        log.warning("[git_sync] configure_remote_url raised: %s", exc)
        return ("error", f"configure_remote_url raised: {exc}")
    if not ok:
        log.warning("[git_sync] configure_remote_url failed: %s", reason)
        return ("error", f"configure_remote_url failed: {reason}")

    # 2) Acquire shared git lock if helper available (best-effort).
    lock_handle = _try_acquire_lock(timeout=60)

    try:
        repo_dir = str(git_ops.REPO_DIR)
        branch = (
            getattr(git_ops, "BRANCH_DEV", None)
            or settings.get("BRANCH_DEV")
            or "ouroboros"
        )

        # 3) git fetch origin BRANCH
        try:
            fetch = subprocess.run(
                ["git", "-C", repo_dir, "fetch", "origin", branch],
                capture_output=True,
                text=True,
            )
        except Exception as exc:
            log.warning("[git_sync] fetch raised: %s", exc)
            return ("error", f"fetch raised: {exc}")
        if fetch.returncode != 0:
            stderr = (fetch.stderr or "").strip()
            log.warning("[git_sync] fetch failed: %s", stderr)
            return ("error", f"fetch failed: {stderr}")

        # 4) git pull --ff-only origin BRANCH
        try:
            pull = subprocess.run(
                ["git", "-C", repo_dir, "pull", "--ff-only", "origin", branch],
                capture_output=True,
                text=True,
            )
        except Exception as exc:
            log.warning("[git_sync] pull raised: %s", exc)
            return ("error", f"pull raised: {exc}")
        if pull.returncode != 0:
            stderr_raw = (pull.stderr or "").strip()
            stderr_lower = stderr_raw.lower()
            non_ff_markers = (
                "non-fast-forward",
                "diverged",
                "refusing to merge",
                "would clobber",
                "not possible to fast-forward",
            )
            if any(marker in stderr_lower for marker in non_ff_markers):
                # Non-FF: capture rescue snapshot but never crash startup.
                try:
                    repo_state = git_ops._collect_repo_sync_state()
                except Exception as exc:
                    log.warning("[git_sync] could not collect repo state for rescue: %s", exc)
                    repo_state = {
                        "current_branch": branch,
                        "dirty_lines": [],
                        "unpushed_lines": [],
                        "warnings": [f"collect_repo_state failed: {exc}"],
                    }
                try:
                    git_ops._create_rescue_snapshot(
                        branch=branch,
                        reason="bootstrap_pull_non_ff",
                        repo_state=repo_state,
                    )
                except Exception as exc:
                    log.warning("[git_sync] rescue snapshot failed: %s", exc)
                log.warning("[git_sync] non-FF pull, rescue snapshot saved")
                return ("rescue", "non-ff pull, rescue snapshot created")
            log.warning("[git_sync] pull failed: %s", stderr_raw)
            return ("error", f"pull failed: {stderr_raw}")

        # 5) Success — capture HEAD short SHA for the log line.
        try:
            head = subprocess.run(
                ["git", "-C", repo_dir, "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
            )
            sha = (head.stdout or "").strip() or "unknown"
        except Exception:
            sha = "unknown"
        log.info("[git_sync] bootstrap pull OK, HEAD=%s", sha)
        return ("ok", f"pulled HEAD: {sha}")
    finally:
        _release_lock(lock_handle)


def shutdown_push_sync(settings: dict) -> Tuple[str, str]:
    """commit (if dirty) + push with retry on graceful shutdown.

    Returns ``(status, reason)`` where status is one of
    ``"skipped" | "ok" | "error"``.

    Sequence:

    1. Resolve remote configuration (skip if not configured).
    2. Acquire the shared git lock with a 30s budget (error on timeout —
       another git operation is in flight and we must not race it).
    3. Stage and commit any dirty changes under the synthetic
       ``"shutdown sync"`` message using author/committer identity from
       the environment (or ``ouroboros@local`` fallback).
    4. (Re)configure ``origin`` URL + credentials — idempotent and cheap.
    5. ``push_to_remote`` with retry/backoff (defaults: ``retries=3``,
       ``backoff_base=2.0`` → sleep series ``[1s, 2s]``).

    Never raises — failures are logged and surfaced via the return tuple
    so the caller (FastAPI shutdown handler) can finish cleanly.
    """
    import os

    from supervisor import git_ops

    cfg = resolve_remote_config(settings)
    if cfg is None:
        return ("skipped", "no remote configured")
    url, user, password = cfg

    lock_handle = _try_acquire_lock(timeout=30)
    if lock_handle is None:
        log.error("[git_sync] shutdown_push: lock timeout")
        return ("error", "lock timeout")

    try:
        repo_dir = str(git_ops.REPO_DIR)
        branch = (
            getattr(git_ops, "BRANCH_DEV", None)
            or settings.get("BRANCH_DEV")
            or "ouroboros"
        )

        # 1) Check if working tree is dirty.
        try:
            status = subprocess.run(
                ["git", "-C", repo_dir, "status", "--porcelain"],
                capture_output=True,
                text=True,
            )
        except Exception as exc:
            log.warning("[git_sync] shutdown_push: status raised: %s", exc)
            return ("error", f"git status raised: {exc}")
        if status.returncode != 0:
            stderr = (status.stderr or "").strip()
            log.warning("[git_sync] shutdown_push: status check failed: %s", stderr)
            return ("error", f"git status failed: {stderr}")

        if status.stdout.strip():
            user_email = (
                os.environ.get("GIT_AUTHOR_EMAIL")
                or os.environ.get("GIT_COMMITTER_EMAIL")
                or "ouroboros@local"
            )
            user_name = (
                os.environ.get("GIT_AUTHOR_NAME")
                or os.environ.get("GIT_COMMITTER_NAME")
                or "ouroboros"
            )
            try:
                add = subprocess.run(
                    ["git", "-C", repo_dir, "add", "-A"],
                    capture_output=True,
                    text=True,
                )
            except Exception as exc:
                log.warning("[git_sync] shutdown_push: git add raised: %s", exc)
                return ("error", f"git add raised: {exc}")
            if add.returncode != 0:
                stderr = (add.stderr or "").strip()
                log.warning("[git_sync] shutdown_push: git add failed: %s", stderr)
                return ("error", f"git add failed: {stderr}")
            try:
                commit = subprocess.run(
                    [
                        "git",
                        "-C",
                        repo_dir,
                        "-c",
                        f"user.email={user_email}",
                        "-c",
                        f"user.name={user_name}",
                        "commit",
                        "-m",
                        "shutdown sync",
                    ],
                    capture_output=True,
                    text=True,
                )
            except Exception as exc:
                log.warning("[git_sync] shutdown_push: commit raised: %s", exc)
                return ("error", f"commit raised: {exc}")
            if commit.returncode != 0:
                stderr = (commit.stderr or "").strip()
                log.warning("[git_sync] shutdown_push: commit failed: %s", stderr)
                return ("error", f"commit failed: {stderr}")
            log.info("[git_sync] shutdown_push: committed dirty changes")
        else:
            log.info("[git_sync] shutdown_push: nothing to commit (clean)")

        # 2) Idempotently (re)configure origin in case it was never set up.
        try:
            ok, reason = git_ops.configure_remote_url(url, user, password)
        except Exception as exc:
            log.warning("[git_sync] shutdown_push: configure_remote_url raised: %s", exc)
            return ("error", f"configure_remote_url raised: {exc}")
        if not ok:
            log.warning("[git_sync] shutdown_push: configure_remote_url failed: %s", reason)
            return ("error", f"configure_remote_url failed: {reason}")

        # 3) Push with retry/backoff.
        try:
            ok, msg = git_ops.push_to_remote(
                branch=branch, retries=3, backoff_base=2.0
            )
        except Exception as exc:
            log.warning("[git_sync] shutdown_push: push raised: %s", exc)
            return ("error", f"push raised: {exc}")
        if ok:
            log.info("[git_sync] shutdown_push: push OK — %s", msg)
            return ("ok", msg)
        log.warning("[git_sync] shutdown_push: push failed — %s", msg)
        return ("error", msg)
    finally:
        _release_lock(lock_handle)


def register_shutdown_handler(app) -> None:
    """Register an ASGI on-shutdown handler that runs :func:`shutdown_push_sync`.

    Works for both FastAPI (``app.router.on_shutdown`` is a list) and any
    Starlette/FastAPI app exposing ``add_event_handler``. The handler runs
    ``shutdown_push_sync`` in a daemon thread bounded by a 25-second
    ``threading.Event.wait`` timeout — Windows-compatible (no
    ``signal.alarm``) and decoupled from uvicorn's own SIGTERM handler.

    Never raises — registration failures are logged and ignored so the
    server can still come up if the underlying ASGI app does not support
    the shutdown hook (e.g. Starlette 1.x, which uses lifespan instead).
    """
    import threading

    async def _shutdown_push_coro() -> None:
        from ouroboros.config import load_settings

        try:
            settings = load_settings()
        except Exception as exc:
            log.warning("[git_sync] shutdown_push: failed to load settings: %s", exc)
            return

        done = threading.Event()
        result: dict = {}

        def _run() -> None:
            try:
                status, reason = shutdown_push_sync(settings)
                result["status"] = status
                result["reason"] = reason
            except Exception as exc:
                result["status"] = "error"
                result["reason"] = f"shutdown_push_sync raised: {exc}"
            finally:
                done.set()

        t = threading.Thread(
            target=_run, daemon=True, name="git_sync_shutdown_push"
        )
        t.start()
        if not done.wait(timeout=25):
            log.warning("[git_sync] shutdown_push timed out after 25s")
            return
        log.info(
            "[git_sync] shutdown_push: %s — %s",
            result.get("status"),
            result.get("reason"),
        )

    # Prefer the canonical FastAPI/Starlette<1.0 hook list when present.
    router = getattr(app, "router", None)
    on_shutdown = getattr(router, "on_shutdown", None)
    if isinstance(on_shutdown, list):
        on_shutdown.append(_shutdown_push_coro)
        return

    add_event_handler = getattr(app, "add_event_handler", None)
    if callable(add_event_handler):
        try:
            add_event_handler("shutdown", _shutdown_push_coro)
            return
        except Exception as exc:
            log.warning(
                "[git_sync] add_event_handler('shutdown') failed: %s", exc
            )
            return

    log.warning(
        "[git_sync] register_shutdown_handler: app has no on_shutdown list "
        "or add_event_handler — shutdown push will not be wired"
    )
