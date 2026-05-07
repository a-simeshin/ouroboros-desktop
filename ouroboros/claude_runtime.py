"""Claude Agent SDK runtime validation (extracted from launcher_bootstrap)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

_CLAUDE_SDK_BASELINE = "claude-agent-sdk>=0.1.60"
_CLAUDE_SDK_MIN_VERSION = "0.1.60"


@dataclass(frozen=True)
class ClaudeRuntimeContext:
    embedded_python: str
    hidden_run: Callable[..., Any]
    log: Any


class _RuntimeContextLike(Protocol):
    embedded_python: str
    hidden_run: Callable[..., Any]
    log: Any


def _version_tuple(v: str) -> tuple:
    """Parse a PEP 440-ish version string into a comparable tuple.

    Strips any post/pre/dev suffix after the first non-numeric component.
    ``"0.1.60" -> (0, 1, 60)``, ``"0.1.60.post1" -> (0, 1, 60)``.
    Returns ``(0,)`` on parse failure (treat as "very old, needs upgrade").
    """
    if not v:
        return (0,)
    parts: list[int] = []
    for p in v.split("."):
        digits = ""
        for ch in p:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else (0,)


def verify_claude_runtime(context: _RuntimeContextLike) -> bool:
    """Ensure the Claude runtime baseline is present in the app-managed interpreter.

    Checks that ``claude-agent-sdk`` is importable, its installed version meets
    ``_CLAUDE_SDK_MIN_VERSION``, and its bundled CLI binary exists. If any
    check fails, attempts a repair install. Returns True on success.

    Version check prevents a silent gap where an older installed SDK
    (e.g. 0.1.50 on an upgraded install) still imports and has the CLI
    binary present, but pre-dates Opus 4.7 adaptive thinking support.
    """
    import sys as _sys
    cli_name = "claude.exe" if _sys.platform == "win32" else "claude"
    try:
        result = context.hidden_run(
            [context.embedded_python, "-c",
             "import claude_agent_sdk; "
             "import importlib.metadata as _m; "
             "from pathlib import Path; "
             f"cli = Path(claude_agent_sdk.__file__).parent / '_bundled' / '{cli_name}'; "
             "ver = _m.version('claude-agent-sdk'); "
             "print('ok|' + ver if cli.exists() else 'no_cli|' + ver)"],
            capture_output=True, text=True, timeout=30,
        )
        stdout = (result.stdout or "").strip()
        if result.returncode == 0 and stdout.startswith("ok|"):
            installed = stdout.split("|", 1)[1]
            if _version_tuple(installed) >= _version_tuple(_CLAUDE_SDK_MIN_VERSION):
                context.log.info(
                    "Claude runtime verified: SDK %s >= %s, bundled CLI present.",
                    installed, _CLAUDE_SDK_MIN_VERSION,
                )
                return True
            context.log.warning(
                "Claude runtime SDK %s is below baseline %s — repairing.",
                installed, _CLAUDE_SDK_MIN_VERSION,
            )
        else:
            context.log.warning("Claude runtime check: %s (exit %d)", stdout, result.returncode)
    except Exception as exc:
        context.log.warning("Claude runtime probe failed: %s", exc)

    context.log.info("Repairing Claude runtime baseline...")
    try:
        repair = context.hidden_run(
            [context.embedded_python, "-m", "pip", "install", "--upgrade", _CLAUDE_SDK_BASELINE],
            timeout=120,
            capture_output=True,
        )
        if repair.returncode != 0:
            context.log.warning("Claude runtime repair pip returned exit %d", repair.returncode)
            return False
        context.log.info("Claude runtime repair install complete.")
        return True
    except Exception as exc:
        context.log.warning("Claude runtime repair failed: %s", exc)
        return False
