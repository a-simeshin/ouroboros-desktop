"""Structured review-evidence collection for summaries, reflections, and UX."""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict


def collect_review_evidence(
    drive_root: Any,
    *,
    task_id: str = "",
    repo_dir: Any = None,
    max_attempts: int = 3,
    max_runs: int = 3,
    max_obligations: int | None = None,
    max_continuations: int = 3,
) -> Dict[str, Any]:
    from ouroboros.review_state import (
        _LEGACY_CURRENT_REPO_KEY,
        load_state,
        make_repo_key,
    )
    from ouroboros.task_continuation import list_review_continuations

    drive_root_path = pathlib.Path(drive_root)
    repo_dir_path = pathlib.Path(repo_dir) if repo_dir else None
    repo_key = make_repo_key(repo_dir_path) if repo_dir_path else ""

    state = load_state(drive_root_path)
    all_attempts = list(state.attempts or [])

    if task_id:
        scoped_attempts = state.filter_attempts(task_id=task_id)
    elif repo_key:
        scoped_attempts = state.filter_attempts(repo_key=repo_key)
    else:
        scoped_attempts = all_attempts

    open_obligations = state.get_open_obligations(repo_key=repo_key or None)
    open_debts = state.get_open_commit_readiness_debts(repo_key=repo_key or None)
    continuations, corrupt = list_review_continuations(drive_root_path)
    if task_id:
        scoped_continuations = [item for item in continuations if item.task_id == task_id]
    elif repo_key:
        scoped_continuations = [
            item for item in continuations
            if item.repo_key in ("", repo_key, _LEGACY_CURRENT_REPO_KEY)
        ]
    else:
        scoped_continuations = continuations
    scoped_continuations.sort(key=lambda item: str(item.updated_ts or item.created_ts or ""), reverse=True)

    evidence = {
        "task_id": task_id,
        "repo_key": repo_key,
        "recent_attempts": [_attempt_to_dict(item) for item in (scoped_attempts[-max_attempts:] if max_attempts > 0 else [])],
        "omitted_attempts": max(0, len(scoped_attempts) - max_attempts) if max_attempts > 0 else len(scoped_attempts),
        "open_obligations": [_obligation_to_dict(item) for item in (open_obligations[:max_obligations] if max_obligations is not None else open_obligations)],
        "omitted_obligations": max(0, len(open_obligations) - max_obligations) if max_obligations is not None else 0,
        "commit_readiness_debts": [_debt_to_dict(item) for item in open_debts],
        "continuations": [_continuation_to_dict(item) for item in scoped_continuations[:max_continuations]],
        "omitted_continuations": max(0, len(scoped_continuations) - max_continuations),
        "corrupt_continuations": [str(item) for item in corrupt[:3]],
        "omitted_corrupt": max(0, len(corrupt) - 3),
    }
    evidence["has_evidence"] = any([
        evidence["recent_attempts"],
        evidence["open_obligations"],
        evidence["commit_readiness_debts"],
        evidence["continuations"],
        evidence["corrupt_continuations"],
        # Omission counters signal truncated evidence even when visible lists are empty
        evidence["omitted_attempts"] > 0,
        evidence["omitted_obligations"] > 0,
        evidence["omitted_continuations"] > 0,
        evidence["omitted_corrupt"] > 0,
    ])
    return evidence


def format_review_evidence_for_prompt(
    evidence: Dict[str, Any],
    *,
    max_chars: int = 0,
    **_kwargs,
) -> str:
    """Format review evidence as JSON for prompt injection.

    When *max_chars* is 0 (default) the full JSON is returned — no truncation.
    Callers that inject evidence into bounded prompts (summaries, reflections)
    can pass a positive *max_chars* to get an explicit omission note instead
    of silent clipping.
    """
    if not evidence or not evidence.get("has_evidence"):
        return "(no structured review evidence)"
    full = json.dumps(evidence, ensure_ascii=False, indent=2)
    if max_chars > 0 and len(full) > max_chars:
        return full[:max_chars] + f"\n⚠️ OMISSION NOTE: review evidence truncated at {max_chars} chars; original length {len(full)}"
    return full


def _attempt_to_dict(item: Any) -> Dict[str, Any]:
    return {
        "ts": str(getattr(item, "ts", "") or ""),
        "tool_name": str(getattr(item, "tool_name", "") or ""),
        "attempt": int(getattr(item, "attempt", 0) or 0),
        "status": str(getattr(item, "status", "") or ""),
        "phase": str(getattr(item, "phase", "") or ""),
        "block_reason": str(getattr(item, "block_reason", "") or ""),
        "late_result_pending": bool(getattr(item, "late_result_pending", False)),
        "critical_findings": list(getattr(item, "critical_findings", []) or []),
        "advisory_findings": list(getattr(item, "advisory_findings", []) or []),
        "readiness_warnings": [str(x) for x in (getattr(item, "readiness_warnings", []) or [])],
        "obligation_ids": [str(x) for x in (getattr(item, "obligation_ids", []) or [])],
        "degraded_reasons": [str(x) for x in (getattr(item, "degraded_reasons", []) or [])],
        "triad_models": [str(x) for x in (getattr(item, "triad_models", []) or [])],
        "scope_model": str(getattr(item, "scope_model", "") or ""),
        "duration_sec": float(getattr(item, "duration_sec", 0.0) or 0.0),
        "triad_raw_results": list(getattr(item, "triad_raw_results", []) or []),
        "scope_raw_result": dict(getattr(item, "scope_raw_result", {}) or {}),
    }


def _obligation_to_dict(item: Any) -> Dict[str, Any]:
    return {
        "obligation_id": str(getattr(item, "obligation_id", "") or ""),
        "fingerprint": str(getattr(item, "fingerprint", "") or ""),
        "item": str(getattr(item, "item", "") or ""),
        "severity": str(getattr(item, "severity", "") or ""),
        "reason": str(getattr(item, "reason", "") or ""),
        "status": str(getattr(item, "status", "") or ""),
        "created_ts": str(getattr(item, "created_ts", "") or ""),
        "updated_ts": str(getattr(item, "updated_ts", "") or ""),
    }


def _continuation_to_dict(item: Any) -> Dict[str, Any]:
    return {
        "task_id": str(getattr(item, "task_id", "") or ""),
        "source": str(getattr(item, "source", "") or ""),
        "stage": str(getattr(item, "stage", "") or ""),
        "tool_name": str(getattr(item, "tool_name", "") or ""),
        "attempt": int(getattr(item, "attempt", 0) or 0),
        "block_reason": str(getattr(item, "block_reason", "") or ""),
        "critical_findings": list(getattr(item, "critical_findings", []) or []),
        "advisory_findings": list(getattr(item, "advisory_findings", []) or []),
        "readiness_warnings": [str(x) for x in (getattr(item, "readiness_warnings", []) or [])],
        "updated_ts": str(getattr(item, "updated_ts", "") or ""),
    }


def _debt_to_dict(item: Any) -> Dict[str, Any]:
    return {
        "debt_id": str(getattr(item, "debt_id", "") or ""),
        "category": str(getattr(item, "category", "") or ""),
        "title": str(getattr(item, "title", "") or ""),
        "summary": str(getattr(item, "summary", "") or ""),
        "status": str(getattr(item, "status", "") or ""),
        "severity": str(getattr(item, "severity", "") or ""),
        "source": str(getattr(item, "source", "") or ""),
        "repo_key": str(getattr(item, "repo_key", "") or ""),
        "source_obligation_ids": [str(x) for x in (getattr(item, "source_obligation_ids", []) or [])],
        "evidence": [str(x) for x in (getattr(item, "evidence", []) or [])],
        "updated_at": str(getattr(item, "updated_at", "") or ""),
    }
