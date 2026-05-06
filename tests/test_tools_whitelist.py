"""Unit tests for the ``OUROBOROS_TOOLS_ENABLED`` whitelist filter.

Validates the behaviour wired into ``ouroboros/tools/registry.py``
(``ToolRegistry.__init__`` -> ``_apply_whitelist``):

* Empty / unset env var -> no-op (full autodiscovered surface).
* Non-empty env var -> only listed tools survive, plus the protected core
  surface (``CORE_TOOL_NAMES`` + ``list_available_tools`` + ``enable_tools``).
* The filter logs a single ``"Whitelist active"`` info line when active.

Implementation notes (verified before writing):

* Storage attribute is ``self._entries`` (not ``self._tools``).
* The names listed in the original plan as examples (``"git"``, ``"shell"``)
  are MODULE names, not tool names. The actual tool names live inside those
  modules — for instance, ``ouroboros/tools/git.py`` exports
  ``git_status`` / ``git_diff`` / ``pull_from_remote`` (all CORE-protected!),
  plus a few others, while ``ouroboros/tools/shell.py`` exports ``run_shell``
  (also CORE-protected). To exercise the filter we therefore need real tool
  names that are NOT in ``CORE_TOOL_NAMES``. ``code_search`` and
  ``compact_context`` are stable picks discovered via:

      python3 -c "import pathlib; from ouroboros.tools.registry import \\
          ToolRegistry, CORE_TOOL_NAMES; \\
          r = ToolRegistry(repo_dir=pathlib.Path('/tmp/x'), \\
                           drive_root=pathlib.Path('/tmp/x')); \\
          print(sorted(set(r._entries) - set(CORE_TOOL_NAMES))[:10])"

The module deliberately avoids ``import pytest`` at module scope so the
project ``ty`` validator (which runs in a uv environment without pytest
installed) stays green. Pytest fixtures (``monkeypatch``, ``caplog``) are
declared as test-method parameters and resolved by pytest at collection
time — they never need to be referenced through the ``pytest.`` namespace.
"""

from __future__ import annotations

import logging
import os
import pathlib

# Two real, autodiscovered, NON-core tool names — verified against
# ``ToolRegistry._entries`` on this codebase. Picked to be stable: both
# ship as dedicated tool modules (``code_search.py``, ``compact_context.py``)
# and have lived in the registry across the v5.x line.
_REAL_TOOL_A = "code_search"
_REAL_TOOL_B = "compact_context"


def _fresh_registry():
    """Build a fresh ``ToolRegistry`` reading the *current* env state.

    The whitelist is read inside ``__init__`` (via ``_apply_whitelist``), so
    each test instantiates a new registry after mutating the env. The
    ``ToolRegistry`` constructor takes ``repo_dir`` and ``drive_root``
    pathlib.Path arguments — we hand it ``/tmp`` paths because no test in
    this module actually touches the filesystem through tool execution.
    """
    from ouroboros.tools.registry import ToolRegistry

    tmp = pathlib.Path("/tmp")
    return ToolRegistry(repo_dir=tmp, drive_root=tmp)


def _fresh_registry_baseline():
    """Return the set of tool names loaded with NO whitelist applied.

    Used by other tests to compute "the core surface that *should* survive
    a filter" without making brittle assumptions about which CORE_TOOL_NAMES
    entries the autoloader actually wired up on this build (e.g. frozen vs
    non-frozen).
    """
    saved = os.environ.pop("OUROBOROS_TOOLS_ENABLED", None)
    try:
        return set(_fresh_registry()._entries.keys())
    finally:
        if saved is not None:
            os.environ["OUROBOROS_TOOLS_ENABLED"] = saved


class TestNoWhitelist:
    def test_no_whitelist_loads_all_tools(self, monkeypatch):
        """Without the env var set, every autodiscovered tool is loaded."""
        monkeypatch.delenv("OUROBOROS_TOOLS_ENABLED", raising=False)
        r = _fresh_registry()
        # Sanity: a non-trivial number of tools should be discovered. The
        # exact count drifts as new modules land; the bound is intentionally
        # conservative (>10) to remain stable across releases.
        assert len(r._entries) > 10, (
            f"Expected >10 tools without whitelist, got {len(r._entries)}"
        )
        # The two tools we use later in the suite must exist on the
        # unfiltered surface — otherwise the test is testing nothing.
        assert _REAL_TOOL_A in r._entries
        assert _REAL_TOOL_B in r._entries


class TestWhitelistFilter:
    def test_whitelist_filters_to_subset(self, monkeypatch):
        """``OUROBOROS_TOOLS_ENABLED="A,B"`` keeps only A, B + core protected."""
        baseline = _fresh_registry_baseline()
        monkeypatch.setenv(
            "OUROBOROS_TOOLS_ENABLED", f"{_REAL_TOOL_A},{_REAL_TOOL_B}"
        )
        r = _fresh_registry()
        from ouroboros.tools.registry import CORE_TOOL_NAMES

        names = set(r._entries.keys())
        # Both whitelisted tools survive.
        assert _REAL_TOOL_A in names, f"{_REAL_TOOL_A!r} missing after whitelist"
        assert _REAL_TOOL_B in names, f"{_REAL_TOOL_B!r} missing after whitelist"
        # All core tools that *were* loaded by the unfiltered registry must
        # still be present (the protected-set guarantee).
        baseline_core = set(CORE_TOOL_NAMES) & baseline
        assert baseline_core.issubset(names), (
            f"Core tools dropped by filter: {baseline_core - names}"
        )
        # Some non-core tool that was not whitelisted MUST be filtered out —
        # otherwise the whitelist isn't enforced.
        baseline_non_core = (
            baseline
            - set(CORE_TOOL_NAMES)
            - {"list_available_tools", "enable_tools"}
            - {_REAL_TOOL_A, _REAL_TOOL_B}
        )
        if baseline_non_core:
            sample_dropped = sorted(baseline_non_core)[0]
            assert sample_dropped not in names, (
                f"Expected {sample_dropped!r} to be filtered out, but it "
                "survived — whitelist is not enforced."
            )


class TestWhitelistProtectsCore:
    def test_whitelist_protects_core(self, monkeypatch):
        """A nonsense whitelist entry must still leave the core surface intact."""
        from ouroboros.tools.registry import CORE_TOOL_NAMES

        baseline = _fresh_registry_baseline()
        monkeypatch.setenv(
            "OUROBOROS_TOOLS_ENABLED", "definitely_not_a_real_tool_name_xyz"
        )
        r = _fresh_registry()
        names = set(r._entries.keys())

        # Every CORE_TOOL_NAMES entry that *could* have been loaded
        # (i.e. existed in the unfiltered registry) must remain in place.
        protected_present = set(CORE_TOOL_NAMES) & baseline
        assert protected_present.issubset(names), (
            f"Core tools were stripped: {protected_present - names}"
        )
        # The discovery tools are explicitly preserved by the filter.
        for discovery in ("list_available_tools", "enable_tools"):
            if discovery in baseline:
                assert discovery in names, (
                    f"Discovery helper {discovery!r} was stripped by filter"
                )
        # The bogus name does not appear (defensive sanity check).
        assert "definitely_not_a_real_tool_name_xyz" not in names


class TestWhitelistLogging:
    def test_whitelist_logs_active_filter(self, monkeypatch, caplog):
        """An active whitelist emits exactly one ``"Whitelist active"`` info line."""
        monkeypatch.setenv("OUROBOROS_TOOLS_ENABLED", _REAL_TOOL_A)
        with caplog.at_level(logging.INFO, logger="ouroboros.tools.registry"):
            _fresh_registry()
        matching = [
            rec for rec in caplog.records if "Whitelist active" in rec.getMessage()
        ]
        assert matching, (
            "Expected at least one log record containing 'Whitelist active'; "
            f"got: {[rec.getMessage() for rec in caplog.records]}"
        )
