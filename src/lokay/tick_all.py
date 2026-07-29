"""Run the combined auto-worker package path once."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lokay.config import load_config
from lokay.flows.common import PathRunResult, path_result_to_dict, process_summary, process_values
from lokay.flows.runtime import run_package_path_async
from lokay.runtime import ensure_fala_paths
from lokay.tick_common import add_common_flags, resolve_dry_run


_PACKAGE_PATH = Path(__file__).resolve().parents[2] / "fala-package.toml"
_TERMINAL_FAILURES = {"failed", "cancelled", "timed_out"}
_WAITING = {"waiting", "retry_wait", "running", "pending"}
_CANDIDATE_RE = re.compile(r"[0-9a-f]{64}\Z")


def _candidate_identity(cfg: Any) -> str:
    """Return only an explicitly identified immutable deployment candidate."""
    raw = getattr(cfg, "raw", {})
    if isinstance(raw, dict):
        for key in ("candidate", "candidate_id"):
            value = str(raw.get(key) or "").strip()
            if _CANDIDATE_RE.fullmatch(value):
                return value
    value = os.environ.get("FALA_CANDIDATE_ID", "").strip()
    if _CANDIDATE_RE.fullmatch(value):
        return value
    parts = Path(__file__).resolve().parts
    for index, part in enumerate(parts[:-1]):
        if part == "versions" and index + 1 < len(parts):
            value = parts[index + 1]
            if _CANDIDATE_RE.fullmatch(value) and parts[index + 2 : index + 5] == ("source", "project", "src"):
                return value
    return ""


def _step_config(cfg: Any, *, dry_run: bool, **extra: Any) -> dict[str, Any]:
    raw = getattr(cfg, "raw", {}) if isinstance(getattr(cfg, "raw", {}), dict) else {}
    paths = getattr(cfg, "paths", None)
    task_receipts = str(getattr(paths, "task_receipts", "") or "")
    active_issue = str(getattr(paths, "active_issue", "") or "")
    worktree_root = str(getattr(paths, "worktree_root", "") or "")
    dispatch_receipts = str(getattr(paths, "dispatch_receipts", "") or "")
    merge_receipts = str(getattr(paths, "merge_receipts", "") or "")
    return {
        "assignee": cfg.assignee,
        "kanban_intake_assignee": cfg.kanban_intake_assignee,
        "ready_label": cfg.labels.ready,
        "in_progress_label": cfg.labels.in_progress,
        "blocked_label": cfg.labels.blocked,
        "pr_opened_label": cfg.labels.pr_opened,
        "generated_label": cfg.labels.generated,
        "needs_feedback_label": cfg.labels.needs_feedback,
        "duplicate_label": cfg.labels.duplicate,
        "out_of_scope_label": cfg.labels.out_of_scope,
        "frozen_label": cfg.labels.frozen,
        "gh_cli": cfg.gh_cli,
        "triage_enabled": bool(cfg.triage.enabled),
        "triage_receipts": str(cfg.paths.triage_receipts),
        "triage_context_paths": list(cfg.triage.context_paths),
        "triage_context_max_bytes": cfg.triage.context_max_bytes,
        "auto_close_duplicates": cfg.triage.auto_close_duplicates,
        "auto_close_out_of_scope": cfg.triage.auto_close_out_of_scope,
        "repo_goal": cfg.direction.repo_goal,
        "direction_require_keywords": list(cfg.direction.require_keywords),
        "direction_deny_keywords": list(cfg.direction.deny_keywords),
        "direction_reject_labels": list(cfg.direction.reject_labels),
        "direction_min_goal_overlap": cfg.direction.min_goal_overlap,
        "base_branch": cfg.base_branch,
        "branch_prefix": cfg.branch_prefix,
        "automerge": cfg.automation.automerge,
        "require_human_approval": cfg.automation.require_human_approval,
        "require_checks": cfg.automation.require_checks,
        "require_test_evidence": cfg.automation.require_test_evidence,
        "fixer_assignee": cfg.automation.fixer_assignee,
        "merge_method": cfg.automation.merge_method,
        "executor_enabled": bool(cfg.executor.enabled),
        "executor_command": cfg.executor.command,
        "executor_model": cfg.executor.model,
        "model": cfg.executor.model,
        "thinking": cfg.executor.thinking,
        "timeout_seconds": cfg.executor.timeout_seconds,
        "live": not dry_run,
        "worktree_root": worktree_root,
        "dispatch_receipts": dispatch_receipts,
        "merge_receipts": merge_receipts,
        "task_receipts": task_receipts,
        "active_issue": active_issue,
        "active_issue_path": active_issue,
        "claim_root": active_issue,
        "repair_receipt_root": str(raw.get("repair_receipt_root") or task_receipts),
        "repair_state_root": str(raw.get("repair_state_root") or raw.get("repair_receipt_root") or task_receipts),
        "lifecycle_receipt_root": str(raw.get("lifecycle_receipt_root") or task_receipts),
        "attempt_recovery": raw.get("attempt_recovery") if isinstance(raw.get("attempt_recovery"), dict) and raw.get("attempt_recovery") else None,
        "repair_creation_recovery": raw.get("repair_creation_recovery") if isinstance(raw.get("repair_creation_recovery"), dict) and raw.get("repair_creation_recovery") else None,
        "db_path": str(extra.get("db_path") or ""),
        "paths": {
            "active_issue": active_issue,
            "worktree_root": worktree_root,
            "dispatch_receipts": dispatch_receipts,
            "merge_receipts": merge_receipts,
            "task_receipts": task_receipts,
            "triage_receipts": str(getattr(paths, "triage_receipts", "") or ""),
        },
        "max_active_issues": getattr(cfg.automation, "max_active_issues", 1),
        "repos": [
        {
            "repo": entry.repo,
            "board": entry.board,
            "clone_path": entry.clone_path,
            "priority": entry.priority,
            "triage_goal": cfg.effective_triage_goal(entry),
            "triage_context_paths": list(cfg.effective_triage_context_paths(entry)),
            "auto_close_duplicates": cfg.effective_auto_close_duplicates(entry),
            "auto_close_out_of_scope": cfg.effective_auto_close_out_of_scope(entry),
        }
        for entry in cfg.repos
        ],
        "dry_run": dry_run,
        **extra,
    }


def _prefixed_inputs(cfg: Any, *, dry_run: bool, limit: int, run_id: str = "", db_path: str = "") -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    repos = [
        {
            "repo": r.repo,
            "board": r.board,
            "clone_path": r.clone_path,
            "priority": r.priority,
            "triage_goal": cfg.effective_triage_goal(r),
            "triage_context_paths": list(cfg.effective_triage_context_paths(r)),
            "auto_close_duplicates": cfg.effective_auto_close_duplicates(r),
            "auto_close_out_of_scope": cfg.effective_auto_close_out_of_scope(r),
        }
        for r in cfg.repos
    ]
    suffix = run_id or ""
    receipt = str(Path(cfg.paths.dispatch_receipts) / f"auto-worker-dispatch-{suffix}.json")
    merge_receipt = str(Path(cfg.paths.merge_receipts) / f"auto-worker-merge-{suffix}.json")
    task_root = str(getattr(cfg.paths, "task_receipts", "") or "")
    triage_receipt_root = str(cfg.paths.triage_receipts)
    repair_receipt = str(Path(task_root) / f"auto-worker-repair-{suffix}.json")
    lifecycle_receipt = str(Path(task_root) / f"auto-worker-lifecycle-{suffix}.json")
    cleanup_receipt = str(Path(task_root) / f"auto-worker-cleanup-{suffix}.json")
    candidate = _candidate_identity(cfg)
    raw = getattr(cfg, "raw", {}) if isinstance(getattr(cfg, "raw", {}), dict) else {}
    executor_policy = raw.get("executor_policy") if isinstance(raw.get("executor_policy"), dict) else {}
    inputs: dict[str, dict[str, Any]] = {
        "intake_read_open_issues": {"repos": repos, "limit": limit},
        "intake_normalize_issue_rows": {}, "intake_filter_issue_eligibility": {}, "intake_select_issue_candidate": {},
        "intake_read_triage_receipt_index": {}, "intake_select_triage_candidate": {}, "intake_reserve_triage_run_budget": {},
        "intake_read_triage_issue_state": {}, "intake_read_triage_comments": {}, "intake_read_triage_repository_state": {},
        "intake_read_triage_canonical_issue": {},
        "intake_build_triage_context": {}, "intake_classify_triage_issue": {}, "intake_verify_triage_repository_unchanged": {},
        "intake_publish_triage_decision_receipt": {}, "intake_read_triage_labels": {}, "intake_decide_triage_mutation": {},
        "intake_ensure_triage_label": {}, "intake_publish_triage_mutation_authorization": {}, "intake_mutate_triage_issue_labels": {},
        "intake_post_triage_feedback": {}, "intake_verify_triage_feedback": {}, "intake_observe_triage_feedback": {},
        "intake_publish_triage_feedback_receipt": {}, "intake_publish_triage_mutation_verification": {},
        "intake_publish_triage_close_authorization": {}, "intake_close_triage_issue": {}, "intake_verify_triage_issue_closed": {},
        "intake_publish_triage_close_verification": {}, "intake_verify_triage_receipt": {}, "intake_build_triage_terminal": {},
        "intake_decide_issue_priority": {}, "intake_decide_issue_action": {}, "intake_read_issue_comments": {}, "intake_decide_issue_comment": {},
        "intake_post_issue_comment": {}, "intake_verify_issue_comment": {},
        "intake_reserve_claim_file": {"active_issue_path": cfg.paths.active_issue}, "intake_read_issue_claim_state": {},
        "intake_assign_issue": {}, "intake_add_issue_label": {"label": cfg.labels.in_progress}, "intake_verify_issue_claim": {}, "intake_build_issue_claim_result": {},
        "intake_read_intake_tasks": {}, "intake_find_intake_marker": {}, "intake_create_intake_task": {}, "intake_reconcile_intake_task": {},
        "dispatch_read_dispatch_tasks": {"repos": repos, "limit": limit}, "dispatch_select_dispatch_task": {}, "dispatch_parse_issue_ref_from_task": {},
        "dispatch_read_fix_tasks": {}, "dispatch_find_fix_task_marker": {}, "dispatch_create_fix_task": {}, "dispatch_reconcile_fix_task": {},
        "dispatch_read_clone_preconditions": {"worktree_root": cfg.paths.worktree_root, "base_branch": cfg.base_branch}, "dispatch_fetch_clone_origin": {},
        "dispatch_read_base_ref": {"base_branch": cfg.base_branch}, "dispatch_read_worktree_inventory": {"worktree_root": cfg.paths.worktree_root},
        "dispatch_read_branch_provenance": {}, "dispatch_create_local_branch": {"worktree_root": cfg.paths.worktree_root, "base_branch": cfg.base_branch, "receipt_path": receipt},
        "dispatch_write_branch_provenance": {"receipt_path": receipt}, "dispatch_add_worktree": {"worktree_root": cfg.paths.worktree_root},
        "dispatch_verify_worktree_head": {}, "dispatch_read_omp_preconditions": {}, "dispatch_invoke_omp": {}, "dispatch_verify_omp_postconditions": {},
        "dispatch_read_worktree_head": {}, "dispatch_read_base_head": {}, "dispatch_decide_branch_has_commits": {}, "dispatch_read_push_head": {},
        "dispatch_push_branch": {}, "dispatch_read_pushed_ref": {}, "dispatch_verify_push_oid": {}, "dispatch_read_open_pr_for_branch": {},
        "dispatch_decide_existing_pr": {}, "dispatch_create_pull_request": {"base_branch": cfg.base_branch}, "dispatch_reconcile_pull_request": {},
        "dispatch_normalize_pr_labels": {}, "dispatch_add_pr_label": {}, "dispatch_aggregate_pr_label_results": {}, "dispatch_add_issue_label": {},
        "dispatch_aggregate_issue_label_results": {}, "dispatch_build_dispatch_receipt": {"receipt_path": receipt},
        "dispatch_publish_dispatch_receipt": {"receipt_path": receipt}, "dispatch_verify_dispatch_receipt": {"receipt_path": receipt},
        "dispatch_read_task_for_completion": {}, "dispatch_decide_task_completion": {}, "dispatch_complete_task": {"result": "dispatched via auto_worker"}, "dispatch_verify_task_completed": {},
        "triage_read_open_prs": {"repos": repos, "limit": limit}, "triage_filter_fix_prs": {}, "triage_select_fix_pr": {}, "triage_load_pr_fields": {},
        "triage_evaluate_checks": {}, "triage_evaluate_test_evidence": {}, "triage_decide_triage_action": {}, "triage_read_pr_assignees": {},
        "triage_decide_pr_assignee": {}, "triage_assign_pr": {}, "triage_verify_pr_assignee": {}, "triage_read_pr_comments": {},
        "triage_decide_pr_comment": {}, "triage_post_pr_comment": {}, "triage_verify_pr_comment": {}, "triage_read_merge_preconditions": {},
        "triage_merge_pr": {}, "triage_read_merge_postcondition": {}, "triage_verify_merge_provenance": {}, "triage_verify_linked_merge_provenance": {},
        "triage_read_linked_issue_state": {}, "triage_close_linked_issue": {}, "triage_verify_linked_issue_closed": {},
        "triage_build_merge_receipt": {"receipt_path": merge_receipt}, "triage_read_receipt_merge_provenance": {},
        "triage_publish_merge_receipt": {"receipt_path": merge_receipt}, "triage_verify_merge_receipt": {"receipt_path": merge_receipt},
        "triage_read_review_tasks": {}, "triage_find_review_marker": {}, "triage_create_review_task": {}, "triage_reconcile_review_task": {},
        "triage_build_repair_prompt": {},
        # Repair attempt state is immutable and never shares dispatch/merge receipts.
        "triage_read_repair_attempt_state": {}, "triage_read_repair_completed_receipt": {}, "triage_read_repair_attempt_reconciliation": {}, "triage_read_repair_attempt_recovery_evidence": {},
        "triage_claim_repair_attempt_recovery": {}, "triage_verify_repair_attempt_recovery": {},
        "triage_read_repair_recovery_continuation_evidence": {}, "triage_claim_repair_recovery_continuation": {}, "triage_verify_repair_recovery_continuation": {},
        "triage_decide_repair_attempt": {}, "triage_read_repair_attempt_baseline": {}, "triage_reserve_repair_attempt": {}, "triage_verify_repair_attempt_reservation": {},
        "triage_read_repair_base_head": {}, "triage_decide_legacy_repair_head_refresh": {}, "triage_update_legacy_repair_pr_branch": {}, "triage_verify_legacy_repair_pr_head": {},
        "triage_read_repair_context": {}, "triage_read_repair_remote_head": {}, "triage_fetch_repair_remote_head": {}, "triage_verify_fetched_repair_remote_head": {},
        "triage_read_repair_worktree_inventory": {}, "triage_read_repair_branch_provenance": {},
        "triage_read_repair_worktree_cleanliness": {}, "triage_read_repair_remote_ancestry": {},
        "triage_decide_repair_worktree_fast_forward": {}, "triage_read_repair_worktree_branch_before_fast_forward": {}, "triage_read_repair_worktree_head_before_fast_forward": {}, "triage_read_repair_worktree_cleanliness_before_fast_forward": {}, "triage_decide_repair_worktree_fast_forward_execution": {}, "triage_fast_forward_repair_worktree": {},
        "triage_read_repair_creation_evidence": {},
        "triage_decide_repair_worktree_ownership": {}, "triage_create_repair_branch": {},
        "triage_write_repair_branch_provenance": {}, "triage_add_repair_worktree": {}, "triage_verify_repair_worktree": {},
        "triage_read_repair_omp_preconditions": {}, "triage_invoke_repair_omp": {},
        "triage_verify_repair_omp_postconditions": {}, "triage_read_repair_worktree_head": {},
        "triage_decide_repair_push": {}, "triage_push_repair_branch": {}, "triage_read_repair_pushed_ref": {},
        "triage_verify_repair_push_oid": {}, "triage_update_repair_branch_provenance": {}, "triage_verify_updated_repair_branch_provenance": {},
        "triage_read_existing_repair_pr": {}, "triage_verify_existing_repair_pr": {},
        "triage_build_repair_receipt": {"receipt_path": repair_receipt},
        "triage_publish_repair_receipt": {"receipt_path": repair_receipt}, "triage_verify_repair_receipt": {"receipt_path": repair_receipt},
        "lifecycle_read_lifecycle_github_state": {}, "lifecycle_read_lifecycle_local_evidence": {},
        "lifecycle_decide_lifecycle_transition": {}, "lifecycle_release_orphan_claim": {"claim_path": cfg.paths.active_issue},
        "lifecycle_verify_orphan_claim_release": {"claim_path": cfg.paths.active_issue},
        "aggregate_lane_results": {},
        "cleanup_resolve_cleanup_branch_source": {}, "cleanup_parse_cleanup_issue_number": {}, "cleanup_read_branch_ownership": {},
        "cleanup_derive_cleanup_paths": {"worktree_root": cfg.paths.worktree_root}, "cleanup_validate_cleanup_identity": {}, "cleanup_check_issue_closed": {},
        "cleanup_check_no_open_pr_for_branch": {}, "cleanup_verify_cleanup_guards": {"require_safe": True}, "cleanup_read_worktree_ownership": {},
        "cleanup_read_worktree_cleanliness": {}, "cleanup_remove_worktree": {"require_safe": True}, "cleanup_verify_worktree_absent": {},
        "cleanup_verify_branch_delete_guards": {}, "cleanup_read_local_branch_ownership": {}, "cleanup_delete_local_branch": {},
        "cleanup_verify_local_branch_absent": {}, "cleanup_verify_claim_release_evidence": {}, "cleanup_read_claim_identity": {"claim_path": cfg.paths.active_issue},
        "cleanup_release_claim_file": {"claim_path": cfg.paths.active_issue}, "cleanup_verify_claim_absent": {"claim_path": cfg.paths.active_issue},
        "cleanup_collect_cleanup_receipt_evidence": {"receipt_path": cleanup_receipt}, "cleanup_decide_cleanup_outcome": {},
        "cleanup_build_cleanup_receipt": {"receipt_path": cleanup_receipt}, "cleanup_publish_cleanup_receipt": {"receipt_path": cleanup_receipt},
        "cleanup_verify_cleanup_receipt": {"receipt_path": cleanup_receipt}, "cleanup_read_maintenance_tasks": {}, "cleanup_find_maintenance_marker": {},
        "cleanup_create_maintenance_task": {}, "cleanup_reconcile_maintenance_task": {},
    }
    config = _step_config(cfg, dry_run=dry_run, receipt_path=receipt, cleanup_receipt_path=cleanup_receipt,
        merge_receipt_path=merge_receipt, repair_receipt_path=repair_receipt, lifecycle_receipt_path=lifecycle_receipt,
        triage_receipts=triage_receipt_root, run_id=run_id, path_id="auto_worker", candidate=candidate, candidate_id=candidate, candidate_sha=candidate,
        executor_policy=executor_policy, db_path=db_path)
    base = {"dry_run": dry_run, "live": not dry_run, "repos": repos, "limit": limit, "run_id": run_id, "path_id": "auto_worker",
        "candidate": candidate, "candidate_id": candidate, "candidate_sha": candidate, "db_path": db_path}
    return base, {key: {**config, **value} for key, value in inputs.items()}

async def run_all(*, db_path: Path, config: Any, dry_run: bool, limit: int = 10) -> dict[str, Any]:
    run_id = f"auto-worker-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    inputs, effector_inputs = _prefixed_inputs(config, dry_run=dry_run, limit=limit, run_id=run_id, db_path=str(db_path))
    host = await run_package_path_async(
        db_path=db_path,
        package_path=_PACKAGE_PATH,
        path_id="auto_worker",
        run_id=run_id,
        inputs=inputs,
        effector_inputs=effector_inputs,
        run_metadata={"mode": "dry-run" if dry_run else "live"},
        max_ticks=256,
        worker_id="lokay:tick-all",
    )
    processes = [process_summary(process) for process in host.processes]
    failed = [item for item in processes if item.get("status") in _TERMINAL_FAILURES]
    waiting = [item for item in processes if item.get("status") in _WAITING]
    outputs = [process_values(item) for item in processes]
    worked = any(bool(output.get("mutated") or output.get("selected")) for output in outputs)

    status = "idle" if host.run_status == "completed" and not failed and not waiting and not worked else host.run_status
    result = PathRunResult(
        run_id=host.run_id,
        path_id="auto_worker",
        dry_run=dry_run,
        ticks=host.ticks,
        stopped_reason=status,
        completed=[process_summary(process) for process in host.completed],
        failed=failed,
        processes=processes,
        summary={"run_status": status, "worked": worked, "failed_steps": [p.get("id") for p in failed], "waiting_steps": [p.get("id") for p in waiting]},
        status=status,
    )
    payload = path_result_to_dict(result)
    payload["any_failed"] = bool(failed or waiting or host.run_status in _TERMINAL_FAILURES or host.run_status in _WAITING)
    return payload


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lokay-tick-all", description="One full auto-worker cycle (Fala path, dry-run by default)")
    add_common_flags(p)
    p.add_argument("--limit", type=int, default=10)
    args = p.parse_args(argv)
    dry = resolve_dry_run(args)
    if dry == 2:
        return 2
    cfg = load_config(args.config)
    db_path, _ = ensure_fala_paths(Path(args.db) if args.db else None)
    result = asyncio.run(run_all(db_path=db_path, config=cfg, dry_run=bool(dry), limit=args.limit))
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print(f"run_id={result['run_id']}")
        print(f"path_id={result['path_id']}")
        print(f"dry_run={result['dry_run']} ticks={result['ticks']} stopped={result['stopped_reason']} status={result['status']}")
        print(f"summary={json.dumps(result['summary'], default=str)}")
        for failure in result["failed"]:
            print(
                "FAILED_PROCESS="
                + json.dumps(
                    {
                        "id": failure.get("id"),
                        "status": failure.get("status"),
                        "attempt": failure.get("attempt"),
                        "error": failure.get("error"),
                    },
                    sort_keys=True,
                    default=str,
                ),
                file=sys.stderr,
            )
    return 1 if result["any_failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
