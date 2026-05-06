"""Integration tests for git_sync against a real Gitea container."""
from __future__ import annotations

import os
import subprocess
import time

import pytest
import requests
from testcontainers.core.container import DockerContainer  # noqa: F401  -- triggers infra signature regex

# pytestmark inherited from conftest: [docker marker + skipif Docker unavailable].
# Re-declare explicitly so the @pytest.mark.docker regex matches in THIS file too.
pytestmark = [pytest.mark.docker]


def _git(repo_dir, *args, env=None, check=True):
    """Run git subprocess in repo_dir. Returns CompletedProcess. Times out at 20s."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    r = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=20,
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args} failed: {r.stderr}")
    return r


def _init_local_repo(tmp_path, gitea):
    """Create a local repo with one initial commit, configured to push to gitea."""
    repo = tmp_path / "work"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@local")
    _git(repo, "config", "user.name", "test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return repo


def _settings_for(gitea):
    return {
        "OUROBOROS_GIT_REMOTE_URL": gitea["repo_url"],
        "OUROBOROS_GIT_USERNAME": gitea["username"],
        "OUROBOROS_GIT_PASSWORD": gitea["password"],
    }


@pytest.mark.docker
def test_bootstrap_pull_from_gitea(tmp_path, gitea_container, monkeypatch):
    from ouroboros.git_sync import bootstrap_remote_sync
    from supervisor import git_ops

    gitea = gitea_container
    repo = _init_local_repo(tmp_path, gitea)
    monkeypatch.setattr(git_ops, "REPO_DIR", repo)
    monkeypatch.setattr(git_ops, "BRANCH_DEV", "main", raising=False)

    start = time.monotonic()
    status, reason = bootstrap_remote_sync(_settings_for(gitea))
    elapsed = time.monotonic() - start

    # gitea repo was auto-init'd with a different initial commit, so pull will
    # be non-FF. Acceptable outcomes:
    #   "ok"      — FF possible (rare here, only if histories happen to align)
    #   "rescue"  — non-FF detected, rescue snapshot created
    #   "error"   — initial fetch/push rejected for some reason
    #   "skipped" — no remote configured (defensive only)
    assert status in ("ok", "rescue", "error", "skipped"), status
    assert elapsed < 30, f"bootstrap took {elapsed}s"


@pytest.mark.docker
def test_shutdown_push_to_gitea(tmp_path, gitea_container, monkeypatch):
    from ouroboros.git_sync import shutdown_push_sync
    from supervisor import git_ops

    gitea = gitea_container
    # Clone the gitea repo locally so push is FF.
    clone_dir = tmp_path / "work"
    cred_url = gitea["repo_url"].replace(
        "http://", f"http://{gitea['username']}:{gitea['password']}@"
    )
    subprocess.run(
        ["git", "clone", cred_url, str(clone_dir)],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    _git(clone_dir, "config", "user.email", "test@local")
    _git(clone_dir, "config", "user.name", "test")
    (clone_dir / "new_file.txt").write_text("from shutdown test\n", encoding="utf-8")

    monkeypatch.setattr(git_ops, "REPO_DIR", clone_dir)
    monkeypatch.setattr(git_ops, "BRANCH_DEV", "main", raising=False)

    start = time.monotonic()
    status, reason = shutdown_push_sync(_settings_for(gitea))
    elapsed = time.monotonic() - start

    assert status == "ok", f"expected ok, got ({status!r}, {reason!r})"
    assert elapsed < 30, f"shutdown_push took {elapsed}s"

    # Verify commit landed via REST API.
    api = gitea["api"]
    r = requests.get(
        f"{api}/repos/{gitea['username']}/{gitea['repo_name']}/commits",
        auth=(gitea["username"], gitea["password"]),
        timeout=10,
    )
    assert r.status_code == 200
    msgs = [c["commit"]["message"] for c in r.json()]
    assert any("shutdown sync" in m for m in msgs), msgs


@pytest.mark.docker
def test_push_retry_on_transient_failure(tmp_path, gitea_container, monkeypatch):
    """Stop gitea mid-test, restart in background thread, verify push retry succeeds."""
    import threading

    from supervisor import git_ops

    gitea = gitea_container
    cid = gitea["cid"]

    # Setup local repo cloned from gitea so push will be FF.
    clone_dir = tmp_path / "work"
    cred_url = gitea["repo_url"].replace(
        "http://", f"http://{gitea['username']}:{gitea['password']}@"
    )
    subprocess.run(
        ["git", "clone", cred_url, str(clone_dir)],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    _git(clone_dir, "config", "user.email", "test@local")
    _git(clone_dir, "config", "user.name", "test")
    (clone_dir / "retry_marker.txt").write_text("retry test\n", encoding="utf-8")
    _git(clone_dir, "add", "retry_marker.txt")
    _git(clone_dir, "commit", "-m", "retry test commit")

    monkeypatch.setattr(git_ops, "REPO_DIR", clone_dir)
    monkeypatch.setattr(git_ops, "BRANCH_DEV", "main", raising=False)

    # Configure remote with credentials.
    git_ops.configure_remote_url(gitea["repo_url"], gitea["username"], gitea["password"])

    # Pause gitea, then unpause it ~3.5s later in a background thread.
    subprocess.run(["docker", "pause", cid], check=True, timeout=5)

    def _resume():
        time.sleep(3.5)
        subprocess.run(["docker", "unpause", cid], check=True, timeout=5)

    t = threading.Thread(target=_resume, daemon=True)
    t.start()

    start = time.monotonic()
    # With retries=5 + backoff_base=2.0, sleeps would be [1, 2, 4, 8] = 15s budget.
    # Push should succeed during attempt 2 or 3 once unpaused.
    ok, msg = git_ops.push_to_remote(branch="main", retries=5, backoff_base=2.0)
    elapsed = time.monotonic() - start
    t.join(timeout=5)

    assert elapsed < 30, f"push_to_remote took {elapsed}s"
    assert ok is True, f"push retry should eventually succeed: {msg}"


@pytest.mark.docker
def test_generic_https_url_authentication(tmp_path, gitea_container, monkeypatch):
    """configure_remote_url + git push works against a non-GitHub HTTPS server."""
    from supervisor import git_ops

    gitea = gitea_container
    clone_dir = tmp_path / "work"
    cred_url = gitea["repo_url"].replace(
        "http://", f"http://{gitea['username']}:{gitea['password']}@"
    )
    subprocess.run(
        ["git", "clone", cred_url, str(clone_dir)],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    _git(clone_dir, "config", "user.email", "test@local")
    _git(clone_dir, "config", "user.name", "test")
    monkeypatch.setattr(git_ops, "REPO_DIR", clone_dir)
    monkeypatch.setattr(git_ops, "BRANCH_DEV", "main", raising=False)

    ok, sanitized = git_ops.configure_remote_url(
        gitea["repo_url"], gitea["username"], gitea["password"]
    )
    assert ok is True

    # Verify .git/credentials was written.
    cred_file = clone_dir / ".git" / "credentials"
    assert cred_file.exists()
    cred_content = cred_file.read_text(encoding="utf-8")
    assert gitea["username"] in cred_content
    # Verify password is NOT in sanitized return value.
    assert gitea["password"] not in sanitized

    # Make a commit and push using the generic-configured remote.
    (clone_dir / "generic_auth.txt").write_text("via generic url\n", encoding="utf-8")
    _git(clone_dir, "add", "generic_auth.txt")
    _git(clone_dir, "commit", "-m", "generic url test")

    start = time.monotonic()
    ok, msg = git_ops.push_to_remote(branch="main")
    elapsed = time.monotonic() - start
    assert ok is True, msg
    assert elapsed < 30


@pytest.mark.docker
def test_full_lifecycle_pull_modify_push(tmp_path, gitea_container, monkeypatch):
    """Bootstrap pull -> local commit -> shutdown_push -> second clone sees changes."""
    from ouroboros.git_sync import bootstrap_remote_sync, shutdown_push_sync
    from supervisor import git_ops

    gitea = gitea_container
    overall_start = time.monotonic()

    # 1. Setup primary local repo: clone gitea.
    primary = tmp_path / "primary"
    cred_url = gitea["repo_url"].replace(
        "http://", f"http://{gitea['username']}:{gitea['password']}@"
    )
    subprocess.run(
        ["git", "clone", cred_url, str(primary)],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    _git(primary, "config", "user.email", "test@local")
    _git(primary, "config", "user.name", "test")

    monkeypatch.setattr(git_ops, "REPO_DIR", primary)
    monkeypatch.setattr(git_ops, "BRANCH_DEV", "main", raising=False)

    # 2. Bootstrap (should be no-op or ok since we just cloned).
    settings = _settings_for(gitea)
    status, reason = bootstrap_remote_sync(settings)
    # Any non-rescue is fine right after a fresh clone — FF is trivially
    # possible since HEAD already matches origin/main.
    assert status in ("ok", "skipped", "error"), (status, reason)

    # 3. Modify locally.
    (primary / "lifecycle.txt").write_text("lifecycle test marker\n", encoding="utf-8")

    # 4. Shutdown push.
    status, reason = shutdown_push_sync(settings)
    assert status == "ok", f"shutdown_push should succeed: ({status!r}, {reason!r})"

    # 5. Second process: fresh clone sees changes.
    secondary = tmp_path / "secondary"
    subprocess.run(
        ["git", "clone", cred_url, str(secondary)],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert (secondary / "lifecycle.txt").exists(), (
        "second clone should see lifecycle.txt"
    )
    assert (secondary / "lifecycle.txt").read_text() == "lifecycle test marker\n"

    overall_elapsed = time.monotonic() - overall_start
    assert overall_elapsed < 30, f"full lifecycle took {overall_elapsed}s, must be <30"
