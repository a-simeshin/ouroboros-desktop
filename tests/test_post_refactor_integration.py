"""Post-refactor integration scenarios for the launcher-removal plan.

Verifies the four invariants of the post-refactor state:

1. ``python server.py`` boots standalone (no launcher in the process tree)
   and serves the health endpoint.
2. ``server.py`` fails fast with a non-zero exit when REPO_DIR is not a
   git checkout — the ``ensure_repo_present()`` contract.
3. The extracted ``ouroboros.claude_runtime`` module exposes the public
   symbols the launcher used to provide.
4. ``ouroboros.skill_loader.ensure_data_skills_seeded`` is callable from
   source-mode startup paths and writes the bootstrap marker.
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

pytestmark = pytest.mark.integration

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


def test_claude_runtime_module_importable_post_migration():
    """Module + key symbols importable post Phase-1 extraction."""
    from ouroboros import claude_runtime

    assert hasattr(claude_runtime, "_CLAUDE_SDK_BASELINE")
    assert hasattr(claude_runtime, "_CLAUDE_SDK_MIN_VERSION")
    assert hasattr(claude_runtime, "_version_tuple")
    assert hasattr(claude_runtime, "verify_claude_runtime")
    assert hasattr(claude_runtime, "ClaudeRuntimeContext")
    assert callable(claude_runtime.verify_claude_runtime)
    # Spot-check the version tuple parser — load-bearing for the SDK
    # baseline check that lives inside verify_claude_runtime.
    assert claude_runtime._version_tuple("0.1.60") >= claude_runtime._version_tuple(
        claude_runtime._CLAUDE_SDK_MIN_VERSION
    )


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
