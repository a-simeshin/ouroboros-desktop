"""Unit tests for the git remote auto-sync stack.

Covers 11 scenarios across three concerns:

* ``configure_remote_url`` (URL-encoded credentials, GitHub backwards-compat
  shim, password-not-logged contract).
* ``push_to_remote`` retry/backoff (success after retry, give-up after retries,
  fast-bail on non-fast-forward).
* ``shutdown_push_sync`` / ``bootstrap_remote_sync`` / ``resolve_remote_config``
  high-level orchestration in ``ouroboros/git_sync.py``.

The module deliberately avoids ``import pytest`` at module scope so the
project ``ty`` validator stays green; pytest fixtures (``mocker``,
``monkeypatch``, ``caplog``, ``tmp_path``) are still available as test-method
parameters because pytest resolves them by name at collection time.
"""

from __future__ import annotations

import logging
import subprocess
import time
import types
from unittest.mock import patch
from urllib.parse import quote

# ---------------------------------------------------------------------------
# configure_remote_url + backwards-compat shim
# ---------------------------------------------------------------------------


class TestConfigureRemoteUrl:
    def test_configure_remote_url_writes_credentials(self, tmp_path, monkeypatch):
        """Generic HTTPS git server: credentials file is URL-encoded + written."""
        from supervisor import git_ops

        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
        # Pretend ``origin`` does not yet exist so the code path goes through
        # ``git remote add`` (mocked via ``git_capture``).
        monkeypatch.setattr(git_ops, "_has_remote", lambda *a, **kw: False)
        monkeypatch.setattr(git_ops, "git_capture", lambda *a, **kw: (0, "", ""))

        password = "pa$$word/with+special@chars"
        username = "user@email.com"
        ok, msg = git_ops.configure_remote_url(
            "https://gitlab.example.com/team/repo.git", username, password
        )

        assert ok is True, f"configure_remote_url failed: {msg}"
        cred_path = tmp_path / ".git" / "credentials"
        assert cred_path.exists(), "credentials file was not created"
        cred_text = cred_path.read_text(encoding="utf-8").strip()

        # URL-encoding contract: each special character is percent-escaped.
        assert quote(username, safe="") in cred_text, (
            f"username not URL-encoded in credentials line: {cred_text!r}"
        )
        assert quote(password, safe="") in cred_text, (
            f"password not URL-encoded in credentials line: {cred_text!r}"
        )
        assert "gitlab.example.com" in cred_text
        assert cred_text.startswith("https://")

    def test_configure_remote_backwards_compat_github(self, tmp_path, monkeypatch):
        """The legacy ``configure_remote(slug, token)`` shim still delegates to URL."""
        from supervisor import git_ops

        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)

        called_with: list[tuple[str, str, str]] = []

        def fake_url(url, user, pw):
            called_with.append((url, user, pw))
            return (True, "ok")

        monkeypatch.setattr(git_ops, "configure_remote_url", fake_url)
        # ``_configure_credential_helper`` writes a GitHub-form credentials
        # line; intercept it so the shim works even with no real git.
        monkeypatch.setattr(
            git_ops,
            "_configure_credential_helper",
            lambda slug, token: None,
        )

        ok, msg = git_ops.configure_remote("org/repo", "tok123")
        assert ok is True, f"configure_remote(slug, token) failed: {msg}"
        # The shim must have invoked configure_remote_url with the canonical
        # GitHub HTTPS URL and the ``x-access-token`` username.
        assert any(
            url == "https://github.com/org/repo.git" for url, _, _ in called_with
        ), f"expected canonical GitHub URL in calls: {called_with}"
        assert any(
            user == "x-access-token" for _, user, _ in called_with
        ), f"expected x-access-token username in calls: {called_with}"

    def test_configure_remote_url_does_not_log_password(
        self, tmp_path, monkeypatch, caplog
    ):
        """The password must never appear in log output (capsys-style assert)."""
        from supervisor import git_ops

        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
        monkeypatch.setattr(git_ops, "_has_remote", lambda *a, **kw: False)
        monkeypatch.setattr(git_ops, "git_capture", lambda *a, **kw: (0, "", ""))

        secret = "TopSecret_42_pwd_DEADBEEF"
        with caplog.at_level(logging.DEBUG):
            git_ops.configure_remote_url(
                "https://gitlab.example.com/x.git", "user", secret
            )
        all_logs = "\n".join(rec.getMessage() for rec in caplog.records)
        assert secret not in all_logs, (
            f"Password leaked into logs:\n{all_logs}"
        )


# ---------------------------------------------------------------------------
# push_to_remote retry/backoff
# ---------------------------------------------------------------------------


class TestPushRetry:
    def test_push_to_remote_retries_on_failure(self, monkeypatch):
        """2 transient fails + 1 success = 3 attempts, sleep series ``[1.0, 2.0]``."""
        from supervisor import git_ops

        calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            calls.append(list(cmd))
            if len(calls) < 3:
                return types.SimpleNamespace(
                    returncode=1, stdout="", stderr="connection refused"
                )
            return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

        sleeps: list[float] = []
        monkeypatch.setattr(git_ops, "_has_remote", lambda *a, **kw: True)
        monkeypatch.setattr(git_ops.subprocess, "run", fake_run)
        monkeypatch.setattr(git_ops.time, "sleep", lambda s: sleeps.append(s))
        # Tags push is fire-and-forget after success — keep it a no-op so
        # the assertion focuses purely on the main push call series.
        monkeypatch.setattr(git_ops, "git_capture", lambda *a, **kw: (0, "", ""))

        ok, _msg = git_ops.push_to_remote()
        assert ok is True
        push_calls = [c for c in calls if "push" in c and "-u" in c]
        assert len(push_calls) == 3, (
            f"Expected 3 push attempts, got {len(push_calls)}: {push_calls}"
        )
        assert sleeps == [1.0, 2.0], (
            f"Expected sleep series [1.0, 2.0], got {sleeps}"
        )

    def test_push_to_remote_gives_up_after_retries(self, monkeypatch):
        """All retries fail with transient error -> ``(False, "...3 retries...")``."""
        from supervisor import git_ops

        monkeypatch.setattr(git_ops, "_has_remote", lambda *a, **kw: True)
        monkeypatch.setattr(
            git_ops.subprocess,
            "run",
            lambda *a, **kw: types.SimpleNamespace(
                returncode=1, stdout="", stderr="permanent failure"
            ),
        )
        monkeypatch.setattr(git_ops.time, "sleep", lambda s: None)
        monkeypatch.setattr(git_ops, "git_capture", lambda *a, **kw: (0, "", ""))

        ok, msg = git_ops.push_to_remote()
        assert ok is False
        assert "3 retries" in msg or "after 3" in msg, (
            f"Expected message to mention 3 retries, got: {msg}"
        )

    def test_push_to_remote_does_not_retry_on_non_ff(self, monkeypatch):
        """Stderr says ``! [rejected] non-fast-forward`` -> 1 attempt, no retry."""
        from supervisor import git_ops

        calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            calls.append(list(cmd))
            return types.SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="! [rejected] master -> master (non-fast-forward)",
            )

        monkeypatch.setattr(git_ops, "_has_remote", lambda *a, **kw: True)
        monkeypatch.setattr(git_ops.subprocess, "run", fake_run)
        monkeypatch.setattr(git_ops, "git_capture", lambda *a, **kw: (0, "", ""))

        ok, msg = git_ops.push_to_remote()
        assert ok is False
        push_calls = [c for c in calls if "push" in c and "-u" in c]
        assert len(push_calls) == 1, (
            f"Expected 1 push attempt (no retry on non-FF), got "
            f"{len(push_calls)}: {push_calls}"
        )
        assert "non-fast-forward" in msg


# ---------------------------------------------------------------------------
# shutdown_push_sync
# ---------------------------------------------------------------------------


class TestShutdownPush:
    def test_shutdown_push_sync_commits_dirty(self, monkeypatch):
        """Dirty repo -> ``git add -A`` + ``commit "shutdown sync"`` then push."""
        from ouroboros import git_sync
        from supervisor import git_ops

        monkeypatch.setattr(
            git_sync,
            "resolve_remote_config",
            lambda settings: ("https://x/repo.git", "u", "p"),
        )
        monkeypatch.setattr(
            git_sync, "_try_acquire_lock", lambda timeout=30: "lock-handle"
        )
        monkeypatch.setattr(git_sync, "_release_lock", lambda h: None)
        monkeypatch.setattr(
            git_ops, "configure_remote_url", lambda *a, **kw: (True, "ok")
        )
        monkeypatch.setattr(
            git_ops, "push_to_remote", lambda *a, **kw: (True, "pushed")
        )

        run_calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            run_calls.append(list(cmd))
            if "status" in cmd and "--porcelain" in cmd:
                return types.SimpleNamespace(
                    returncode=0, stdout="M README.md\n", stderr=""
                )
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(git_sync.subprocess, "run", fake_run)

        status, _reason = git_sync.shutdown_push_sync({})
        assert status == "ok"
        # The commit subprocess invocation MUST be one of the calls — that's
        # the contract the dirty branch enforces.
        assert any("commit" in cmd for cmd in run_calls), (
            f"Expected a `git ... commit` call in: {run_calls}"
        )

    def test_shutdown_push_sync_skips_clean(self, monkeypatch):
        """Clean repo -> no commit subprocess invocation."""
        from ouroboros import git_sync
        from supervisor import git_ops

        monkeypatch.setattr(
            git_sync,
            "resolve_remote_config",
            lambda settings: ("https://x/repo.git", "u", "p"),
        )
        monkeypatch.setattr(
            git_sync, "_try_acquire_lock", lambda timeout=30: "lock"
        )
        monkeypatch.setattr(git_sync, "_release_lock", lambda h: None)
        monkeypatch.setattr(
            git_ops, "configure_remote_url", lambda *a, **kw: (True, "ok")
        )
        monkeypatch.setattr(
            git_ops, "push_to_remote", lambda *a, **kw: (True, "pushed")
        )

        run_calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            run_calls.append(list(cmd))
            # Clean working tree: empty stdout from `git status --porcelain`.
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(git_sync.subprocess, "run", fake_run)

        status, _reason = git_sync.shutdown_push_sync({})
        assert status == "ok"
        assert not any("commit" in cmd for cmd in run_calls), (
            f"Did not expect a commit call on a clean tree, got: {run_calls}"
        )


# ---------------------------------------------------------------------------
# bootstrap_remote_sync
# ---------------------------------------------------------------------------


class TestBootstrapSync:
    def test_bootstrap_remote_sync_skipped_no_remote(self):
        """No remote configured -> ``("skipped", ...)`` no-op."""
        from ouroboros.git_sync import bootstrap_remote_sync

        status, _reason = bootstrap_remote_sync({})
        assert status == "skipped"

    def test_bootstrap_remote_sync_non_ff_creates_rescue(self, monkeypatch):
        """Non-FF pull -> ``_create_rescue_snapshot`` called, status ``"rescue"``."""
        from ouroboros import git_sync
        from supervisor import git_ops

        monkeypatch.setattr(
            git_ops, "configure_remote_url", lambda *a, **kw: (True, "ok")
        )

        rescue_calls: list[tuple] = []

        def _fake_rescue(*args, **kwargs):
            rescue_calls.append((args, kwargs))
            return {}

        monkeypatch.setattr(git_ops, "_create_rescue_snapshot", _fake_rescue)
        monkeypatch.setattr(
            git_ops, "_collect_repo_sync_state", lambda *a, **kw: {}
        )
        monkeypatch.setattr(
            git_sync, "_try_acquire_lock", lambda timeout=60: "lock"
        )
        monkeypatch.setattr(git_sync, "_release_lock", lambda h: None)

        def fake_run(cmd, **kw):
            if "fetch" in cmd:
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            if "pull" in cmd:
                return types.SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr=(
                        "error: Your local changes... refusing to merge "
                        "non-fast-forward"
                    ),
                )
            return types.SimpleNamespace(returncode=0, stdout="abc1234", stderr="")

        monkeypatch.setattr(git_sync.subprocess, "run", fake_run)

        status, _reason = git_sync.bootstrap_remote_sync(
            {
                "OUROBOROS_GIT_REMOTE_URL": "https://x/y.git",
                "OUROBOROS_GIT_USERNAME": "u",
                "OUROBOROS_GIT_PASSWORD": "p",
            }
        )
        assert status == "rescue"
        assert len(rescue_calls) == 1, (
            f"Expected 1 rescue snapshot, got {len(rescue_calls)}"
        )


# ---------------------------------------------------------------------------
# resolve_remote_config
# ---------------------------------------------------------------------------


class TestResolveRemoteConfig:
    def test_resolve_remote_config_prefers_generic_url(self):
        """Generic ``OUROBOROS_GIT_REMOTE_URL`` wins over ``GITHUB_*`` legacy pair."""
        from ouroboros.git_sync import resolve_remote_config

        cfg = resolve_remote_config(
            {
                "OUROBOROS_GIT_REMOTE_URL": "https://gitlab.example/x.git",
                "OUROBOROS_GIT_USERNAME": "u",
                "OUROBOROS_GIT_PASSWORD": "p",
                "GITHUB_TOKEN": "should-be-ignored",
                "GITHUB_REPO": "should/be-ignored",
            }
        )
        assert cfg is not None, "Expected a (url, user, pass) tuple"
        url, user, password = cfg
        assert "gitlab" in url
        assert user == "u"
        assert password == "p"

    def test_resolve_remote_config_falls_back_to_github(self):
        """No generic URL -> fall back to ``GITHUB_TOKEN+GITHUB_REPO`` -> canonical URL."""
        from ouroboros.git_sync import resolve_remote_config

        cfg = resolve_remote_config(
            {"GITHUB_TOKEN": "tok", "GITHUB_REPO": "org/repo"}
        )
        assert cfg == (
            "https://github.com/org/repo.git",
            "x-access-token",
            "tok",
        )


# ---------------------------------------------------------------------------
# Smoke: imports do not pull pytest into module-level scope (validator
# friendliness). Keeping these inert references silences ``unused import``
# lints from the project ``ruff`` validator without polluting test collection.
# ---------------------------------------------------------------------------


_UNUSED_FRIENDS = (patch, subprocess, time)
