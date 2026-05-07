"""Phase 2 regression tests for ``server.ensure_repo_present``.

The hook fail-fasts on startup when ``REPO_DIR`` is missing or is not
a git checkout. Docker/k8s deployments rely on the message body to
nudge operators toward the right escape hatch (``OUROBOROS_REPO_DIR``
override). These tests pin the happy path + the diagnostic message
content.

Implementation note: ``server.REPO_DIR`` is captured at module import
time, so each test directly monkeypatches the module attribute to a
``tmp_path`` rather than re-importing the module (which would re-run
side-effectful module bodies — logging setup, env-driven host bind,
etc.).
"""

from __future__ import annotations

import pytest


def _import_server():
    import server  # noqa: PLC0415 — defer to keep import side-effects per-test.
    return server


def test_ensure_repo_present_passes_when_repo_dir_exists(tmp_path, monkeypatch):
    server = _import_server()
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(server, "REPO_DIR", tmp_path)
    # Must not raise.
    server.ensure_repo_present()


def test_ensure_repo_present_passes_when_git_subdir_exists(tmp_path, monkeypatch):
    """Accept the common dir-style git checkout (``.git/`` directory,
    not a ``.git`` file pointing at a worktree)."""
    server = _import_server()
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    # Drop a HEAD ref so the tmp checkout looks more realistic; the
    # hook only checks for ``.git`` being a directory, but anchoring
    # the test to a concrete shape catches a future change to
    # ``is_file()``-based detection.
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    monkeypatch.setattr(server, "REPO_DIR", tmp_path)
    server.ensure_repo_present()


def test_ensure_repo_present_raises_systemexit_when_missing(tmp_path, monkeypatch):
    server = _import_server()
    # tmp_path exists but has no .git subdirectory.
    monkeypatch.setattr(server, "REPO_DIR", tmp_path)
    with pytest.raises(SystemExit):
        server.ensure_repo_present()


def test_ensure_repo_present_error_message_mentions_repo_dir_path(tmp_path, monkeypatch):
    server = _import_server()
    monkeypatch.setattr(server, "REPO_DIR", tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        server.ensure_repo_present()
    message = str(exc_info.value)
    assert str(tmp_path) in message


def test_ensure_repo_present_error_message_mentions_data_dir_env(tmp_path, monkeypatch):
    """The diagnostic must surface the ``OUROBOROS_REPO_DIR`` escape
    hatch so the operator knows which env var to set."""
    server = _import_server()
    monkeypatch.setattr(server, "REPO_DIR", tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        server.ensure_repo_present()
    message = str(exc_info.value)
    assert "OUROBOROS_REPO_DIR" in message
