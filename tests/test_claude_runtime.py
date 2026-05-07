"""Phase 1 regression tests for ``ouroboros.claude_runtime``.

The module was extracted from ``launcher_bootstrap`` as part of the
"remove PyInstaller / launcher pipeline" effort. These tests pin:

- the PEP 440-ish parser ``_version_tuple`` (happy path + invalid input);
- the runtime probe ``verify_claude_runtime`` (baseline ok / below baseline /
  missing SDK paths);
- the ``ClaudeRuntimeContext`` dataclass shape (no embedded-bundle fields
  remain after the launcher excision).
"""

from __future__ import annotations

import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from ouroboros.claude_runtime import (
    ClaudeRuntimeContext,
    _version_tuple,
    verify_claude_runtime,
)


def _make_log() -> Any:
    return types.SimpleNamespace(
        info=lambda *_a, **_k: None,
        warning=lambda *_a, **_k: None,
    )


def _make_ctx(hidden_run, log: Any | None = None) -> ClaudeRuntimeContext:
    return ClaudeRuntimeContext(
        embedded_python="python",
        hidden_run=hidden_run,
        log=log or _make_log(),
    )


# ---------------------------------------------------------------------------
# _version_tuple
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0.1.60", (0, 1, 60)),
        ("0.2.0a1", (0, 2, 0)),
        ("1.0.0", (1, 0, 0)),
    ],
)
def test_version_tuple_parses_pep440(raw, expected):
    assert _version_tuple(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", (0,)),
        ("not-a-version", (0,)),
    ],
)
def test_version_tuple_handles_invalid_input(raw, expected):
    assert _version_tuple(raw) == expected


# ---------------------------------------------------------------------------
# verify_claude_runtime
# ---------------------------------------------------------------------------


def test_verify_claude_runtime_passes_when_sdk_meets_baseline(mocker):
    """First probe reports ok with a version >= baseline — no repair fires."""
    hidden_run = MagicMock()
    hidden_run.return_value = types.SimpleNamespace(
        returncode=0, stdout="ok|0.1.60", stderr=""
    )
    ctx = _make_ctx(hidden_run)
    assert verify_claude_runtime(ctx) is True
    # Only the probe should have been called — no pip install repair.
    assert hidden_run.call_count == 1


def test_verify_claude_runtime_fails_when_sdk_below_baseline(mocker):
    """Below-baseline probe triggers pip-install repair; success returns True."""
    probe_result = types.SimpleNamespace(returncode=0, stdout="ok|0.1.50", stderr="")
    repair_result = types.SimpleNamespace(returncode=0, stdout="", stderr="")
    hidden_run = MagicMock(side_effect=[probe_result, repair_result])
    ctx = _make_ctx(hidden_run)
    assert verify_claude_runtime(ctx) is True
    assert hidden_run.call_count == 2
    # Second call must be the pip install repair invocation.
    second_args, _second_kwargs = hidden_run.call_args_list[1]
    cmd = second_args[0]
    assert "pip" in cmd and "install" in cmd
    assert any("claude-agent-sdk" in part for part in cmd)


def test_verify_claude_runtime_handles_missing_sdk(mocker):
    """Probe raising / non-ok stdout still triggers the pip repair path."""
    repair_result = types.SimpleNamespace(returncode=0, stdout="", stderr="")
    hidden_run = MagicMock(side_effect=[Exception("import failed"), repair_result])
    ctx = _make_ctx(hidden_run)
    assert verify_claude_runtime(ctx) is True
    assert hidden_run.call_count == 2
    second_args, _ = hidden_run.call_args_list[1]
    cmd = second_args[0]
    assert "pip" in cmd and "install" in cmd


def test_verify_claude_runtime_returns_false_when_pip_repair_fails(mocker):
    """If the pip-install repair exits non-zero, the function must return False."""
    probe_result = types.SimpleNamespace(returncode=0, stdout="ok|0.1.50", stderr="")
    repair_result = types.SimpleNamespace(returncode=1, stdout="", stderr="boom")
    hidden_run = MagicMock(side_effect=[probe_result, repair_result])
    ctx = _make_ctx(hidden_run)
    assert verify_claude_runtime(ctx) is False


# ---------------------------------------------------------------------------
# ClaudeRuntimeContext dataclass shape
# ---------------------------------------------------------------------------


def test_claude_runtime_context_no_bundle_fields():
    """The dataclass must expose exactly the three fields documented in
    the new module: ``embedded_python``, ``hidden_run``, ``log``. No
    leftover ``bundle_root`` / ``embedded_bin`` / ``runtime_root`` from
    the old launcher_bootstrap shape may sneak back in.
    """
    fields = set(ClaudeRuntimeContext.__dataclass_fields__.keys())
    assert fields == {"embedded_python", "hidden_run", "log"}
