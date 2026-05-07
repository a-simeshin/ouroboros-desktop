"""E2E smoke: /api/health loads in a Playwright browser after launcher removal."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui_browser

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
HEALTH_PATH = "/api/health"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_health_http(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    url = f"http://127.0.0.1:{port}{HEALTH_PATH}"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:  # noqa: S310 - local test server
                if resp.status == 200:
                    return
        except Exception as exc:
            last_exc = exc
        time.sleep(0.5)
    raise AssertionError(
        f"server {HEALTH_PATH} did not become ready within {timeout}s (last error: {last_exc})"
    )


@pytest.fixture
def server_proc():
    """Boot server.py on a free port, yield (proc, port), terminate on teardown."""
    port = _free_port()
    env = {
        **os.environ,
        "OUROBOROS_REPO_DIR": str(REPO_ROOT),
        "OUROBOROS_SERVER_HOST": "127.0.0.1",
        "OUROBOROS_SERVER_PORT": str(port),
    }
    proc = subprocess.Popen(
        [PYTHON, str(REPO_ROOT / "server.py"), "--port", str(port), "--host", "127.0.0.1"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_health_http(port, timeout=30.0)
        yield proc, port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_health_endpoint_loads_in_browser(server_proc):
    """Playwright Chromium hits /api/health and verifies a 200 + 'ok' body."""
    _proc, port = server_proc
    sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    sync_playwright = sync_api.sync_playwright
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                response = page.goto(
                    f"http://127.0.0.1:{port}{HEALTH_PATH}",
                    wait_until="load",
                    timeout=15000,
                )
                assert response is not None
                assert response.status == 200
                body = page.content()
                response_text = response.text() or ""
                assert "ok" in body.lower() or "ok" in response_text.lower()
            finally:
                browser.close()
    except Exception as exc:
        msg = str(exc)
        if "Executable doesn't exist" in msg or "playwright install" in msg.lower():
            pytest.skip(msg)
        raise
