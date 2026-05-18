"""
Cross-platform compatibility layer.

Encapsulates all OS-specific operations (process management, file locking,
path conventions) so the rest of the codebase stays platform-agnostic.
"""

from __future__ import annotations

import logging
import os
import pathlib
import platform
import signal
import subprocess
import sys
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform flags
# ---------------------------------------------------------------------------
IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

PATH_SEP = ";" if IS_WINDOWS else ":"
_SUBPROCESS_NO_WINDOW = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if IS_WINDOWS else 0
)


def is_container_env() -> bool:
    """Return True when running inside a Docker/container environment.

    Checks:
    - OUROBOROS_CONTAINER=1 environment variable (explicit override)
    - /.dockerenv file presence (standard Docker sentinel, Linux only)
    """
    if os.environ.get("OUROBOROS_CONTAINER") == "1":
        return True
    # /.dockerenv is created by Docker on Linux; safe no-op on macOS/Windows
    if IS_LINUX and pathlib.Path("/.dockerenv").exists():
        return True
    return False


def _hidden_run(command: list[str], **kwargs):
    if _SUBPROCESS_NO_WINDOW:
        kwargs = dict(kwargs)
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _SUBPROCESS_NO_WINDOW
    return subprocess.run(command, **kwargs)


# ---------------------------------------------------------------------------
# PID file locking (single-instance guard)
# ---------------------------------------------------------------------------
_lock_fd: Any = None


def pid_lock_acquire(path: str) -> bool:
    """Acquire an exclusive PID lock. Returns True on success.

    The previous form opened the file before attempting the lock, then on
    lock-failure returned False with the file still open — slowly leaking
    file descriptors under repeated failed startup attempts. Close the
    file explicitly on lock-acquire failure so the FD count stays bounded.
    """
    global _lock_fd
    fd_obj = None
    try:
        fd_obj = open(path, "w")
        if IS_WINDOWS:
            _win32_lock(fd_obj.fileno(), exclusive=True, blocking=False)
        else:
            import fcntl
            fcntl.flock(fd_obj, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd_obj.write(str(os.getpid()))
        fd_obj.flush()
        # Promote to global only after lock + write both succeeded.
        _lock_fd = fd_obj
        return True
    except (IOError, OSError):
        if fd_obj is not None:
            try:
                fd_obj.close()
            except Exception:
                pass
        return False


def pid_lock_release(path: str) -> None:
    """Release the PID lock."""
    global _lock_fd
    if _lock_fd is not None:
        if IS_WINDOWS:
            try:
                _win32_unlock(_lock_fd.fileno())
            except Exception:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
        try:
            _lock_fd.close()
        except Exception:
            pass
        _lock_fd = None
    try:
        os.unlink(path)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# File locking (cross-platform)
# ---------------------------------------------------------------------------

def file_lock_exclusive(fd: int) -> None:
    """Acquire an exclusive (write) lock on a file descriptor. Blocks."""
    if IS_WINDOWS:
        _win32_lock(fd, exclusive=True, blocking=True)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)


def file_lock_shared(fd: int) -> None:
    """Acquire a shared (read) lock on a file descriptor. Blocks."""
    if IS_WINDOWS:
        _win32_lock(fd, exclusive=False, blocking=True)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_SH)


def file_lock_exclusive_nb(fd: int) -> None:
    """Try to acquire an exclusive lock, non-blocking. Raises OSError on failure."""
    if IS_WINDOWS:
        _win32_lock(fd, exclusive=True, blocking=False)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def file_unlock(fd: int) -> None:
    """Release a file lock."""
    if IS_WINDOWS:
        _win32_unlock(fd)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def pid_is_alive(pid: int) -> bool:
    """Return whether a PID appears alive without exposing os.kill to callers."""

    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Windows file locking via LockFileEx / UnlockFileEx (ctypes)
#
# msvcrt.locking() is a *byte-range* lock that fails on empty files (0 bytes).
# LockFileEx locks a range that can extend beyond the current file size,
# which makes it work identically to fcntl.flock() on Unix.
# ---------------------------------------------------------------------------

# Per-fd OVERLAPPED storage so unlock can find the right structure.
_win32_overlapped: dict = {}


_OVERLAPPED_CLS = None  # cached once per process


def _win32_overlapped_class():
    """Return the portable OVERLAPPED ctypes Structure (cached).

    ``wintypes.ULONG_PTR`` is absent on some Python/Windows builds, so we use
    ``ctypes.c_void_p`` which is pointer-width on all architectures (4 bytes on
    32-bit, 8 bytes on 64-bit) — exactly what ``ULONG_PTR`` is.

    The class is created once and reused so that lock/unlock share the same
    ``ctypes.POINTER(OVERLAPPED)`` type — ctypes rejects pointer arguments whose
    underlying Structure class object differs even if the layout is identical.
    """
    global _OVERLAPPED_CLS
    if _OVERLAPPED_CLS is not None:
        return _OVERLAPPED_CLS

    import ctypes
    from ctypes import wintypes

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _OVERLAPPED_CLS = OVERLAPPED
    return OVERLAPPED


def _win32_lock(fd: int, *, exclusive: bool = True, blocking: bool = True) -> None:
    """Lock a file descriptor using Win32 LockFileEx. Works on empty files."""
    import ctypes
    import msvcrt as _msvcrt
    from ctypes import wintypes

    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002

    OVERLAPPED = _win32_overlapped_class()

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(OVERLAPPED),
    ]
    kernel32.LockFileEx.restype = wintypes.BOOL

    hfile = _msvcrt.get_osfhandle(fd)
    flags = 0
    if exclusive:
        flags |= _LOCKFILE_EXCLUSIVE_LOCK
    if not blocking:
        flags |= _LOCKFILE_FAIL_IMMEDIATELY

    ov = OVERLAPPED()
    # Lock a huge range starting at offset 0 — standard Win32 "whole file" pattern.
    if not kernel32.LockFileEx(hfile, flags, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(ov)):
        err = ctypes.get_last_error()
        raise OSError(f"LockFileEx failed (error {err})")

    _win32_overlapped[fd] = (hfile, ov)


def _win32_unlock(fd: int) -> None:
    """Unlock a file descriptor previously locked by _win32_lock."""
    import ctypes
    from ctypes import wintypes

    entry = _win32_overlapped.pop(fd, None)
    if entry is None:
        return

    OVERLAPPED = _win32_overlapped_class()

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.UnlockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(OVERLAPPED),
    ]
    kernel32.UnlockFileEx.restype = wintypes.BOOL

    hfile, ov = entry
    try:
        kernel32.UnlockFileEx(hfile, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(ov))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

def kill_process_tree(proc: subprocess.Popen) -> None:
    """Force-kill a subprocess and its entire process tree."""
    if IS_WINDOWS:
        try:
            _hidden_run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass
    else:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def terminate_process_tree(proc: subprocess.Popen) -> None:
    """Gracefully terminate a subprocess and its process tree."""
    if IS_WINDOWS:
        proc.terminate()
    else:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def force_kill_pid(pid: int) -> None:
    """Force-kill a single process by PID."""
    if IS_WINDOWS:
        try:
            _hidden_run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def kill_pid_tree(pid: int) -> None:
    """Force-kill a process and ALL its descendants (recursive).

    On Windows: taskkill /F /T handles the entire tree natively.
    On Unix: walks the process tree via pgrep -P, then SIGKILL bottom-up.
    """
    if IS_WINDOWS:
        try:
            _hidden_run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass
        return

    descendants: list[int] = []
    _collect_descendants(pid, descendants)
    for dpid in reversed(descendants):
        try:
            os.kill(dpid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _collect_descendants(pid: int, result: list[int]) -> None:
    """Recursively collect all descendant PIDs via pgrep."""
    try:
        out = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True, text=True, timeout=3,
        )
        for line in out.stdout.strip().splitlines():
            line = line.strip()
            if line:
                child_pid = int(line)
                _collect_descendants(child_pid, result)
                result.append(child_pid)
    except Exception:
        pass


def kill_process_on_port(port: int) -> None:
    """Kill any process listening on the given TCP port."""
    try:
        if IS_WINDOWS:
            res = _hidden_run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=5,
            )
            for line in res.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.strip().split()
                    if parts:
                        try:
                            pid = int(parts[-1])
                            if pid != os.getpid():
                                _hidden_run(
                                    ["taskkill", "/F", "/PID", str(pid)],
                                    capture_output=True,
                                )
                        except (ValueError, ProcessLookupError, PermissionError):
                            pass
        else:
            res = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}"],
                capture_output=True, text=True, timeout=5,
            )
            for pid_str in res.stdout.strip().split():
                try:
                    pid = int(pid_str)
                    if pid != os.getpid():
                        os.kill(pid, 9)
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Node.js download
# ---------------------------------------------------------------------------

def node_download_info(version: str) -> tuple[str, str, str]:
    """Return (url, extracted_dir_name, archive_type) for Node.js download.

    archive_type is 'zip' for Windows, 'tar.gz' otherwise.
    """
    arch = platform.machine()
    if IS_WINDOWS:
        na = "x64"
        name = f"node-{version}-win-{na}"
        return f"https://nodejs.org/dist/{version}/{name}.zip", name, "zip"
    elif IS_MACOS:
        na = "arm64" if arch == "arm64" else "x64"
        name = f"node-{version}-darwin-{na}"
        return f"https://nodejs.org/dist/{version}/{name}.tar.gz", name, "tar.gz"
    else:
        na = "arm64" if arch == "aarch64" else "x64"
        name = f"node-{version}-linux-{na}"
        return f"https://nodejs.org/dist/{version}/{name}.tar.gz", name, "tar.gz"


# ---------------------------------------------------------------------------
# System profiling helpers
# ---------------------------------------------------------------------------

def get_system_memory() -> str:
    """Return total system memory as a human-readable string."""
    os_name = platform.system()
    try:
        if os_name == "Darwin":
            mem_bytes = int(subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"],
            ).strip())
            return f"{mem_bytes / (1024**3):.1f} GB"
        elif os_name == "Linux":
            out = subprocess.check_output(
                ["awk", '/MemTotal/ {print $2/1024/1024 " GB"}', "/proc/meminfo"],
            ).strip().decode()
            return out
        elif os_name == "Windows":
            out = _hidden_run(
                ["wmic", "ComputerSystem", "get", "TotalPhysicalMemory", "/value"],
                capture_output=True, text=True, timeout=10, check=True,
            ).stdout.strip()
            for line in out.splitlines():
                if "=" in line:
                    mem_bytes = int(line.split("=")[1])
                    return f"{mem_bytes / (1024**3):.1f} GB"
    except Exception:
        pass
    return "Unknown"


def get_cpu_info() -> str:
    """Return CPU model string."""
    os_name = platform.system()
    try:
        if os_name == "Darwin":
            return subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
            ).strip().decode()
        elif os_name == "Windows":
            out = _hidden_run(
                ["wmic", "cpu", "get", "Name", "/value"],
                capture_output=True, text=True, timeout=10, check=True,
            ).stdout.strip()
            for line in out.splitlines():
                if "=" in line:
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return platform.processor()


# ---------------------------------------------------------------------------
# Process session isolation
# ---------------------------------------------------------------------------

def create_new_session() -> None:
    """Create a new process session (Unix: setsid). No-op on Windows."""
    if not IS_WINDOWS:
        os.setsid()


def subprocess_new_group_kwargs() -> dict:
    """Return subprocess kwargs for process-group / session isolation.

    On Windows: CREATE_NEW_PROCESS_GROUP so the subprocess tree can be
    terminated via GenerateConsoleCtrlEvent or taskkill /T.
    On Unix: start_new_session=True creates a new session (setsid) so
    the entire tree can be killed via os.killpg().
    """
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def subprocess_hidden_kwargs() -> dict:
    """Return subprocess kwargs to suppress console windows on Windows.

    On non-Windows this returns an empty dict (no-op).
    """
    if IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def merge_hidden_kwargs(kwargs: dict) -> dict:
    """Return a copy of *kwargs* with platform hidden-window flags merged in.

    Merges ``creationflags`` via bitwise OR on Windows so that any flags the
    caller already set are preserved.  On non-Windows the dict is returned
    unchanged (a shallow copy is always returned).
    """
    hidden = subprocess_hidden_kwargs()
    if not hidden:
        return dict(kwargs)
    result = dict(kwargs)
    result["creationflags"] = result.get("creationflags", 0) | hidden.get("creationflags", 0)
    return result


