"""Testcontainers-based integration test fixtures (gitea git server)."""
from __future__ import annotations

import subprocess
import time

import pytest
import requests


def _docker_available() -> bool:
    """Return True if `docker info` succeeds."""
    try:
        r = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=5
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# Apply at module level so all tests in this package skip together when Docker is missing.
pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_available(), reason="docker daemon unavailable"),
]


@pytest.fixture(scope="session")
def gitea_container():
    """Boot a Gitea container, bootstrap admin + repo, yield connection info."""
    if not _docker_available():
        pytest.skip("docker daemon unavailable")
    from testcontainers.core.container import DockerContainer

    container = (
        DockerContainer("gitea/gitea:1.21")
        .with_exposed_ports(3000)
        .with_env("INSTALL_LOCK", "true")
        .with_env("USER_UID", "1000")
        .with_env("USER_GID", "1000")
        .with_env("GITEA__database__DB_TYPE", "sqlite3")
        .with_env("GITEA__server__OFFLINE_MODE", "true")
    )
    container.start()
    try:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(3000))
        base_url = f"http://{host}:{port}"

        # Wait for Gitea HTTP to come up.
        deadline = time.time() + 90
        ready = False
        while time.time() < deadline:
            try:
                r = requests.get(f"{base_url}/", timeout=3)
                if r.status_code in (200, 302):
                    ready = True
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        if not ready:
            raise RuntimeError("gitea did not become ready in 90s")

        # Bootstrap admin user via `gitea admin user create` inside the container.
        cid = container.get_wrapped_container().id
        username = "testuser"
        password = "testpass1234"  # >= 8 chars (gitea requirement).
        email = "t@t.local"
        cmd = [
            "docker", "exec", cid, "gitea", "admin", "user", "create",
            "--admin", "--username", username, "--password", password,
            "--email", email, "--must-change-password=false",
        ]
        last_err = ""
        created = False
        for _attempt in range(10):
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                created = True
                break
            last_err = r.stderr or r.stdout or ""
            # If user already exists from a prior session-scoped boot, accept it.
            if "user already exists" in last_err.lower():
                created = True
                break
            time.sleep(2)
        if not created:
            raise RuntimeError(f"failed to create gitea admin: {last_err}")

        # Create a repo via REST API.
        repo_name = "test-repo"
        api = f"{base_url}/api/v1"
        rr = requests.post(
            f"{api}/user/repos",
            auth=(username, password),
            json={
                "name": repo_name,
                "auto_init": True,
                "default_branch": "main",
                "private": False,
            },
            timeout=10,
        )
        if rr.status_code not in (201, 409):
            raise RuntimeError(f"failed to create repo: {rr.status_code} {rr.text}")
        repo_url = f"{base_url}/{username}/{repo_name}.git"

        yield {
            "base_url": base_url,
            "api": api,
            "username": username,
            "password": password,
            "repo_url": repo_url,
            "repo_name": repo_name,
            "container": container,
            "cid": cid,
        }
    finally:
        try:
            container.stop()
        except Exception:
            pass
