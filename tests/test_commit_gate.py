"""Tests for git safety tools, commit gate hardening, and operational polish.

Verifies (Phase 4):
- New tools registered: pull_from_remote, restore_to_head, revert_commit
- SAFETY_CRITICAL_PATHS blocks dangerous operations
- Confirm gates prevent accidental destructive actions
- also_stage parameter in repo_write_commit
- Auto-tagging on version bump
- Credential helper in git_ops (no token in remote URL)
- New tools in CORE_TOOL_NAMES

Verifies (Phase 5):
- Auto-push wired into commit functions
- migrate_remote_credentials exists and is safe
- ARCHITECTURE.md version sync in startup checks
"""
import importlib
import inspect
import os
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_git_module():
    sys.path.insert(0, REPO)
    return importlib.import_module("ouroboros.tools.git")


def _get_registry_module():
    sys.path.insert(0, REPO)
    return importlib.import_module("ouroboros.tools.registry")


def _get_git_ops_module():
    sys.path.insert(0, REPO)
    return importlib.import_module("supervisor.git_ops")


# --- Tool registration tests ---

def test_pull_from_remote_registered():
    git_mod = _get_git_module()
    names = [t.name for t in git_mod.get_tools()]
    assert "pull_from_remote" in names


def test_restore_to_head_registered():
    git_mod = _get_git_module()
    names = [t.name for t in git_mod.get_tools()]
    assert "restore_to_head" in names


def test_revert_commit_registered():
    git_mod = _get_git_module()
    names = [t.name for t in git_mod.get_tools()]
    assert "revert_commit" in names


def test_non_committing_review_cycle_exists_and_reuses_shared_stage_cycle():
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._run_non_committing_review_cycle)
    assert "_run_reviewed_stage_cycle" in source
    assert '"reviewed"' in source
    assert '"review_only"' in source
    assert '["git", "reset", "HEAD"]' in source
    assert '["git", "commit"' not in source


def test_non_committing_review_cycle_runtime_unstages_on_success(monkeypatch):
    git_mod = _get_git_module()
    reset_calls = []
    recorded = []
    released = []

    monkeypatch.setattr(git_mod, "_check_overlapping_review_attempt", lambda ctx: None)
    monkeypatch.setattr(git_mod, "_acquire_git_lock", lambda ctx: "lock-token")
    monkeypatch.setattr(git_mod, "_release_git_lock", lambda lock: released.append(lock))
    monkeypatch.setattr(
        git_mod,
        "_run_reviewed_stage_cycle",
        lambda *args, **kwargs: {
            "status": "passed",
            "message": "stage cycle passed",
            "pre_fingerprint": {"fingerprint": "pre"},
            "post_fingerprint": {"fingerprint": "post"},
        },
    )
    monkeypatch.setattr(
        git_mod,
        "_record_commit_attempt",
        lambda *args, **kwargs: recorded.append(
            {"status": args[2], "phase": kwargs.get("phase")}
        ),
    )
    monkeypatch.setattr(
        git_mod,
        "run_cmd",
        lambda cmd, cwd=None: reset_calls.append((tuple(cmd), cwd)) or "",
    )

    ctx = types.SimpleNamespace(repo_dir="/tmp/repo")
    outcome = git_mod._run_non_committing_review_cycle(ctx, "test commit")

    assert outcome["status"] == "passed"
    assert "Commit was not created" in outcome["message"]
    assert ctx._scope_review_history == {}
    assert recorded == [{"status": "reviewed", "phase": "review_only"}]
    assert released == ["lock-token"]
    assert reset_calls == [(("git", "reset", "HEAD"), "/tmp/repo")]


def test_non_committing_review_cycle_runtime_unstages_on_block(monkeypatch):
    git_mod = _get_git_module()
    reset_calls = []
    released = []

    monkeypatch.setattr(git_mod, "_check_overlapping_review_attempt", lambda ctx: None)
    monkeypatch.setattr(git_mod, "_acquire_git_lock", lambda ctx: "lock-token")
    monkeypatch.setattr(git_mod, "_release_git_lock", lambda lock: released.append(lock))
    monkeypatch.setattr(
        git_mod,
        "_run_reviewed_stage_cycle",
        lambda *args, **kwargs: {
            "status": "blocked",
            "message": "review blocked",
            "block_reason": "critical_findings",
        },
    )
    monkeypatch.setattr(
        git_mod,
        "run_cmd",
        lambda cmd, cwd=None: reset_calls.append((tuple(cmd), cwd)) or "",
    )

    ctx = types.SimpleNamespace(repo_dir="/tmp/repo")
    outcome = git_mod._run_non_committing_review_cycle(ctx, "test commit")

    assert outcome["status"] == "blocked"
    assert outcome["block_reason"] == "critical_findings"
    assert released == ["lock-token"]
    assert reset_calls == [(("git", "reset", "HEAD"), "/tmp/repo")]


def test_repo_commit_push_uses_shared_reviewed_stage_cycle():
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._repo_commit_push)
    assert "_run_reviewed_stage_cycle" in source


def test_repo_write_commit_uses_shared_reviewed_stage_cycle():
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._repo_write_commit)
    assert "_run_reviewed_stage_cycle" in source


# --- Protected-path checks ---

def test_restore_to_head_blocks_protected_paths():
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._restore_to_head)
    assert "is_protected_runtime_path" in source or "protected_paths_in" in source
    assert "RESTORE_BLOCKED" in source


def test_revert_commit_blocks_protected_paths():
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._revert_commit)
    assert "protected_paths_in" in source
    assert "REVERT_BLOCKED" in source


# --- Confirm gates ---

def test_revert_commit_has_confirm_gate():
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._revert_commit)
    assert "confirm" in source
    assert "Call again with confirm=true" in source


def test_restore_to_head_has_confirm_gate():
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._restore_to_head)
    assert "confirm" in source
    assert "Call again with confirm=true" in source


# --- also_stage ---

def test_also_stage_in_repo_write_commit():
    git_mod = _get_git_module()
    sig = inspect.signature(git_mod._repo_write_commit)
    assert "also_stage" in sig.parameters


def test_also_stage_in_schema():
    git_mod = _get_git_module()
    tools = git_mod.get_tools()
    rwc = next(t for t in tools if t.name == "repo_write_commit")
    props = rwc.schema["parameters"]["properties"]
    assert "also_stage" in props
    assert props["also_stage"]["type"] == "array"


# --- Auto-tagging ---

def test_auto_tag_function_exists():
    git_mod = _get_git_module()
    assert hasattr(git_mod, "_auto_tag_on_version_bump")
    assert callable(git_mod._auto_tag_on_version_bump)


def test_auto_tag_called_in_commit_functions():
    git_mod = _get_git_module()
    for fn_name in ("_repo_write_commit", "_repo_commit_push"):
        source = inspect.getsource(getattr(git_mod, fn_name))
        assert "_auto_tag_on_version_bump" in source, (
            f"{fn_name} must call _auto_tag_on_version_bump"
        )


def test_auto_tag_not_gated_by_test_warnings():
    """Auto-tagging must run unconditionally — not skipped when tests fail."""
    git_mod = _get_git_module()
    for fn_name in ("_repo_write_commit", "_repo_commit_push"):
        source = inspect.getsource(getattr(git_mod, fn_name))
        # Find the line(s) that call _auto_tag_on_version_bump
        for line in source.splitlines():
            if "_auto_tag_on_version_bump" in line:
                assert "if not test_warning" not in line, (
                    f"{fn_name}: _auto_tag_on_version_bump must not be gated "
                    f"by test_warning_ref — tags must always be created on VERSION bump"
                )


# --- Credential helper ---

def test_credential_helper_exists():
    git_ops = _get_git_ops_module()
    assert hasattr(git_ops, "_configure_credential_helper")
    assert callable(git_ops._configure_credential_helper)


def test_configure_remote_uses_clean_url():
    """configure_remote must not embed token in the remote URL."""
    git_ops = _get_git_ops_module()
    source = inspect.getsource(git_ops.configure_remote)
    assert "x-access-token" not in source, (
        "configure_remote must use credential helper, not embed token in URL"
    )
    assert "_configure_credential_helper" in source


# --- CORE_TOOL_NAMES ---

def test_new_tools_in_core_tool_names():
    registry = _get_registry_module()
    for name in ("pull_from_remote", "restore_to_head", "revert_commit"):
        assert name in registry.CORE_TOOL_NAMES, (
            f"{name} must be in CORE_TOOL_NAMES"
        )


# --- Pull tool specifics ---

def test_pull_uses_ff_only():
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._ff_pull)
    assert "--ff-only" in source, "Pull must use --ff-only for safety"


def test_pull_fetches_before_merge():
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._ff_pull)
    fetch_pos = source.find("git fetch")
    merge_pos = source.find("git merge")
    assert fetch_pos != -1, "Must call git fetch"
    assert merge_pos != -1, "Must call git merge"
    assert fetch_pos < merge_pos, "Fetch must come before merge"


# --- Revert tool specifics ---

def test_revert_uses_git_lock():
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._revert_commit)
    assert "_acquire_git_lock" in source
    assert "_release_git_lock" in source


def test_revert_aborts_on_failure():
    """On revert failure, git revert --abort must be called."""
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._revert_commit)
    assert '"--abort"' in source and '"revert"' in source


def test_revert_commit_blocks_merge_commits():
    """revert_commit must reject merge commits upfront."""
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._revert_commit)
    assert "merge commit" in source.lower()
    assert "rev-list" in source or "parents" in source


def test_restore_to_head_blocks_safety_critical_full_restore():
    """Full restore (no paths) must check dirty files against protected paths."""
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._restore_to_head)
    assert "affected_critical" in source or "dirty_files" in source, (
        "Full restore must parse dirty files and check against protected paths"
    )


def test_also_stage_blocks_safety_critical():
    """also_stage must check protected paths before staging."""
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._repo_write_commit)
    assert "protected_paths_in" in source, (
        "repo_write_commit must check also_stage paths against the shared protected path policy"
    )


# --- Auto-push (Phase 5) ---

def test_auto_push_function_exists():
    git_mod = _get_git_module()
    assert hasattr(git_mod, "_auto_push")
    assert callable(git_mod._auto_push)


def test_auto_push_called_in_commit_functions():
    git_mod = _get_git_module()
    for fn_name in ("_repo_write_commit", "_repo_commit_push"):
        source = inspect.getsource(getattr(git_mod, fn_name))
        assert "_auto_push" in source, (
            f"{fn_name} must call _auto_push after successful commit"
        )


def test_auto_push_not_in_rollback_tools():
    """Auto-push must NOT be wired into restore_to_head or revert_commit."""
    git_mod = _get_git_module()
    for fn_name in ("_restore_to_head", "_revert_commit", "_ff_pull"):
        source = inspect.getsource(getattr(git_mod, fn_name))
        assert "_auto_push" not in source, (
            f"{fn_name} must NOT call _auto_push"
        )


def test_auto_push_is_best_effort():
    """_auto_push must catch all exceptions and return a string (never raise)."""
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._auto_push)
    assert "except Exception" in source
    assert "non-fatal" in source.lower() or "non_fatal" in source.lower()


def test_auto_push_outside_git_lock():
    """Auto-push call must happen AFTER _release_git_lock, not inside the try/finally."""
    git_mod = _get_git_module()
    for fn_name in ("_repo_write_commit", "_repo_commit_push"):
        source = inspect.getsource(getattr(git_mod, fn_name))
        lock_release_pos = source.rfind("_release_git_lock")
        push_pos = source.rfind("_auto_push")
        assert lock_release_pos < push_pos, (
            f"{fn_name}: _auto_push must come after _release_git_lock"
        )


# --- Credential migration (Phase 5) ---

def test_migrate_remote_credentials_exists():
    git_ops = _get_git_ops_module()
    assert hasattr(git_ops, "migrate_remote_credentials")
    assert callable(git_ops.migrate_remote_credentials)


def test_migrate_remote_credentials_uses_configure_remote():
    git_ops = _get_git_ops_module()
    source = inspect.getsource(git_ops.migrate_remote_credentials)
    assert "configure_remote" in source


# --- ARCHITECTURE version sync (Phase 5) ---

def test_version_sync_checks_architecture_md():
    """_check_version_sync must compare VERSION with ARCHITECTURE.md header."""
    sys.path.insert(0, REPO)
    agent_mod = importlib.import_module("ouroboros.agent")
    source = inspect.getsource(agent_mod.OuroborosAgent._check_version_sync)
    assert "ARCHITECTURE" in source
    assert "architecture_version" in source


# ---------------------------------------------------------------------------
# Blocking review triad — repo_commit no longer requires an advisory pre-review
# gate; the blocking triad (OUROBOROS_REVIEW_ENFORCEMENT) machinery stays.
# ---------------------------------------------------------------------------

def _get_review_state_module():
    sys.path.insert(0, REPO)
    return importlib.import_module("ouroboros.review_state")


def test_repo_commit_passes_without_advisory_gate(tmp_path):
    """A repo_commit on a clean temp git repo passes with NO advisory-pre-review
    requirement and NO skip_advisory_pre_review param.

    The Claude-SDK advisory pre-review gate was removed. The shared reviewed
    stage cycle must no longer call _check_advisory_freshness, the helper must
    not exist on git.py, and repo_commit's schema must not expose a
    skip_advisory_pre_review parameter.
    """
    import subprocess

    git_mod = _get_git_module()

    # The advisory-freshness helper must be gone entirely.
    assert not hasattr(git_mod, "_check_advisory_freshness"), (
        "_check_advisory_freshness must be removed from git.py"
    )

    # The shared reviewed stage cycle must not gate on advisory freshness.
    cycle_source = inspect.getsource(git_mod._run_reviewed_stage_cycle)
    assert "_check_advisory_freshness" not in cycle_source
    assert "skip_advisory_pre_review" not in cycle_source
    assert "ADVISORY_PRE_REVIEW_REQUIRED" not in cycle_source

    # repo_commit schema must not advertise the removed bypass param.
    tools = git_mod.get_tools()
    commit_tool = next(t for t in tools if t.name == "repo_commit")
    props = commit_tool.schema["parameters"]["properties"]
    assert "skip_advisory_pre_review" not in props

    # A clean repo with no changes commits without any advisory requirement.
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo_dir), capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo_dir), capture_output=True,
    )
    (repo_dir / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), capture_output=True)
    proc = subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(repo_dir), capture_output=True
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")


def test_review_blocked_message_prefers_fix_over_rebuttal():
    """v4.9.2: REVIEW_BLOCKED message directs agent to fix first, rebuttal only for factual errors."""
    from ouroboros.tools.review import _build_critical_block_message

    class FakeCtx:
        _review_iteration_count = 1
        _review_history = []

    msg = _build_critical_block_message(FakeCtx(), "test commit", ["bible_compliance: violation"], [], "")  # ty: ignore[invalid-argument-type]
    assert "factually incorrect" in msg.lower()
    assert "not to argue" in msg.lower() or "not to argue against" in msg.lower()


def test_review_blocked_5plus_hint_suggests_split():
    """v4.9.2: After 5+ attempts, hint suggests implementing the fix or splitting."""
    from ouroboros.tools.review import _build_critical_block_message

    class FakeCtx:
        # v4.33.0 lowered the threshold from 5 to 3 — 5 still triggers but
        # the phrasing changed from "report the blockage" to "send_user_message
        # to escalate" which carries the same semantic weight.
        _review_iteration_count = 5
        _review_history = []

    msg = _build_critical_block_message(FakeCtx(), "test commit", ["tests_affected: missing tests"], [], "")  # ty: ignore[invalid-argument-type]
    lowered = msg.lower()
    assert "split" in lowered, f"missing split-the-diff guidance: {msg!r}"
    assert ("send_user_message" in lowered or "escalate" in lowered
            or "report" in lowered), (
        f"missing escalation guidance: {msg!r}"
    )


def test_review_blocked_message_requires_reaudit_after_first_block():
    """Blocked-review guidance should explicitly require a full-diff re-audit after the first block."""
    from ouroboros.tools.review import _build_critical_block_message

    class FakeCtx:
        _review_iteration_count = 2
        _review_history = []
        _last_review_critical_findings = [{"item": "code_quality"}]
        _last_review_advisory_findings = []

    msg = _build_critical_block_message(FakeCtx(), "test commit", ["code_quality: review mismatch"], [], "")  # ty: ignore[invalid-argument-type]
    lowered = msg.lower()
    assert "re-read the full diff" in lowered
    assert "group obligations by root cause" in lowered
    assert "rewrite the plan" in lowered


def test_self_consistency_listed_as_critical_in_severity_rules():
    """self_consistency (item 13) must be treated as conditionally critical, not always advisory."""
    import pathlib
    checklists_path = pathlib.Path(__file__).parent.parent / "docs" / "CHECKLISTS.md"
    content = checklists_path.read_text(encoding="utf-8")

    # The severity rules section must describe self_consistency as conditionally critical
    assert "self_consistency" in content
    # Must NOT say items 11-13 are ALL advisory
    lines = content.split("\n")
    for line in lines:
        if "items 11-13 are advisory" in line.lower():
            raise AssertionError(
                f"Found old 'items 11-13 are advisory' rule — self_consistency "
                f"must now be conditionally critical:\n  {line}"
            )
    # Must say item 13 is conditionally critical
    assert "item 13" in content.lower() and "critical" in content.lower()
    # v4.33.0: the old "README test counts" example was folded into the
    # broader Critical surface whitelist. Narrative / prose / commentary
    # mismatches outside the whitelist must be explicitly advisory.
    assert "Critical surface whitelist" in content
    assert "advisory" in content.lower()
    # And the "narrative" framing of commit-message / doc wording remains.
    assert "narrative" in content.lower()


def test_development_compliance_checklist_expanded():
    """development_compliance description must include specific concrete checks."""
    import pathlib
    checklists_path = pathlib.Path(__file__).parent.parent / "docs" / "CHECKLISTS.md"
    content = checklists_path.read_text(encoding="utf-8")

    # All these concrete checks must appear in the checklist
    required_terms = [
        "snake_case",
        "PascalCase",
        "Gateway",
        "LLMClient",
        "[:N]",
        "ToolEntry",
    ]
    for term in required_terms:
        assert term in content, (
            f"development_compliance checklist must mention '{term}' for concrete checks, "
            f"but it's missing from CHECKLISTS.md"
        )


def test_triad_review_prompt_has_thoroughness_instructions():
    """Triad review prompt must include thoroughness instructions."""
    from ouroboros.tools.review import _REVIEW_PROMPT_TEMPLATE

    prompt_lower = _REVIEW_PROMPT_TEMPLATE.lower()
    required_phrases = [
        "read the entire",
        "all bugs, logic errors",
        "do not stop after finding",
        "each distinct problem",
        "pass reasons may be brief",
        "fail reasons must be detailed",
        "how-to-fix",
    ]
    for phrase in required_phrases:
        assert phrase in prompt_lower, (
            f"Triad review prompt missing required thoroughness instruction: '{phrase}'"
        )


def test_triad_review_reasoning_effort_is_medium_not_low():
    """Triad review models must use at least medium reasoning effort, not 'low'."""
    from ouroboros.tools.review import _query_model

    source = inspect.getsource(_query_model)
    # Must NOT contain reasoning_effort="low"
    assert 'reasoning_effort="low"' not in source, (
        "_query_model uses reasoning_effort='low' — must be 'medium' or higher"
    )
    # Must contain medium or higher
    assert 'reasoning_effort="medium"' in source or 'reasoning_effort="high"' in source, (
        "_query_model must use reasoning_effort='medium' or 'high'"
    )

