"""Run the combined auto-worker package path once."""

from __future__ import annotations

import argparse
import asyncio
import json
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


def _step_config(cfg: Any, *, dry_run: bool, **extra: Any) -> dict[str, Any]:
    return {
        "assignee": cfg.assignee,
        "kanban_intake_assignee": cfg.kanban_intake_assignee,
        "ready_label": cfg.labels.ready,
        "in_progress_label": cfg.labels.in_progress,
        "blocked_label": cfg.labels.blocked,
        "pr_opened_label": cfg.labels.pr_opened,
        "generated_label": cfg.labels.generated,
        "gh_cli": cfg.gh_cli,
        "base_branch": cfg.base_branch,
        "branch_prefix": cfg.branch_prefix,
        "automerge": cfg.automation.automerge,
        "require_human_approval": cfg.automation.require_human_approval,
        "require_checks": cfg.automation.require_checks,
        "require_test_evidence": cfg.automation.require_test_evidence,
        "fixer_assignee": cfg.automation.fixer_assignee,
        "merge_method": cfg.automation.merge_method,
        "executor_enabled": cfg.executor.enabled,
        "executor_command": cfg.executor.command,
        "executor_model": cfg.executor.model,
        "model": cfg.executor.model,
        "thinking": cfg.executor.thinking,
        "timeout_seconds": cfg.executor.timeout_seconds,
        "worktree_root": cfg.paths.worktree_root,
        "dispatch_receipts": cfg.paths.dispatch_receipts,
        "merge_receipts": cfg.paths.merge_receipts,
        "active_issue": cfg.paths.active_issue,
        "active_issue_path": cfg.paths.active_issue,
        "paths": {
            "active_issue": cfg.paths.active_issue,
            "worktree_root": cfg.paths.worktree_root,
            "dispatch_receipts": cfg.paths.dispatch_receipts,
            "merge_receipts": cfg.paths.merge_receipts,
            "task_receipts": getattr(cfg.paths, "task_receipts", None),
        },
        "max_active_issues": getattr(cfg.automation, "max_active_issues", 1),
        "repos": [
            {"repo": entry.repo, "board": entry.board, "clone_path": entry.clone_path, "priority": entry.priority}
            for entry in cfg.repos
        ],
        "dry_run": dry_run,
        **extra,
    }


def _prefixed_inputs(cfg: Any, *, dry_run: bool, limit: int, run_id: str = "") -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    repos = [
        {"repo": r.repo, "board": r.board, "clone_path": r.clone_path, "priority": r.priority}
        for r in cfg.repos
    ]
    suffix = run_id or "unidentified"
    receipt = str(Path(cfg.paths.dispatch_receipts) / f"auto-worker-dispatch-{suffix}.json")
    merge_receipt = str(Path(cfg.paths.merge_receipts) / f"auto-worker-merge-{suffix}.json")
    cleanup_receipt = str(Path(cfg.paths.dispatch_receipts) / f"auto-worker-cleanup-{suffix}.json")
    common = {"dry_run": dry_run, "run_id": run_id, "path_id": "auto_worker"}
    inputs: dict[str, dict[str, Any]] = {
        # Intake starts with the configured repository set; all later atoms
        # consume the selected repository through Fala conduction.
        "intake_read_open_issues": {"repos": repos, "limit": limit},
        "intake_normalize_issue_rows": {},
        "intake_filter_issue_eligibility": {},
        "intake_select_issue_candidate": {},
        "intake_decide_issue_action": {},
        "intake_read_issue_comments": {},
        "intake_decide_issue_comment": {},
        "intake_post_issue_comment": {},
        "intake_verify_issue_comment": {},
        "intake_reserve_claim_file": {"active_issue_path": cfg.paths.active_issue},
        "intake_read_issue_claim_state": {},
        "intake_assign_issue": {},
        "intake_add_issue_label": {},
        "intake_verify_issue_claim": {},
        "intake_build_issue_claim_result": {},
        "intake_read_intake_tasks": {},
        "intake_find_intake_marker": {},
        "intake_create_intake_task": {},
        "intake_reconcile_intake_task": {},
        # Dispatch reads the configured repository set, then routes the
        # selected board/clone/task through conduction into its atoms.
        "dispatch_read_dispatch_tasks": {"repos": repos, "limit": limit},
        "dispatch_select_dispatch_task": {},
        "dispatch_parse_issue_ref_from_task": {},
        "dispatch_read_fix_tasks": {},
        "dispatch_find_fix_task_marker": {},
        "dispatch_create_fix_task": {},
        "dispatch_reconcile_fix_task": {},
        "dispatch_read_clone_preconditions": {"worktree_root": cfg.paths.worktree_root, "base_branch": cfg.base_branch},
        "dispatch_fetch_clone_origin": {},
        "dispatch_read_base_ref": {"base_branch": cfg.base_branch},
        "dispatch_read_worktree_inventory": {"worktree_root": cfg.paths.worktree_root},
        "dispatch_read_branch_provenance": {},
        "dispatch_create_local_branch": {"worktree_root": cfg.paths.worktree_root, "base_branch": cfg.base_branch, "receipt_path": receipt},
        "dispatch_write_branch_provenance": {"receipt_path": receipt},
        "dispatch_add_worktree": {"worktree_root": cfg.paths.worktree_root},
        "dispatch_verify_worktree_head": {},
        "dispatch_read_omp_preconditions": {},
        "dispatch_invoke_omp": {},
        "dispatch_verify_omp_postconditions": {},
        "dispatch_read_worktree_head": {},
        "dispatch_read_base_head": {},
        "dispatch_decide_branch_has_commits": {},
        "dispatch_read_push_head": {},
        "dispatch_push_branch": {},
        "dispatch_read_pushed_ref": {},
        "dispatch_verify_push_oid": {},
        "dispatch_read_open_pr_for_branch": {},
        "dispatch_decide_existing_pr": {},
        "dispatch_create_pull_request": {"base_branch": cfg.base_branch},
        "dispatch_reconcile_pull_request": {},
        "dispatch_normalize_pr_labels": {},
        "dispatch_add_pr_label": {},
        "dispatch_aggregate_pr_label_results": {},
        "dispatch_add_issue_label": {},
        "dispatch_aggregate_issue_label_results": {},
        "dispatch_build_dispatch_receipt": {"receipt_path": receipt},
        "dispatch_publish_dispatch_receipt": {"receipt_path": receipt},
        "dispatch_verify_dispatch_receipt": {"receipt_path": receipt},
        "dispatch_read_task_for_completion": {},
        "dispatch_decide_task_completion": {},
        "dispatch_complete_task": {"result": "dispatched via auto_worker"},
        "dispatch_verify_task_completed": {},
        # Triage also starts from all configured repositories.  Its selected
        # PR context is then carried by conduction into merge/repair atoms.
        "triage_read_open_prs": {"repos": repos, "limit": limit},
        "triage_filter_fix_prs": {},
        "triage_select_fix_pr": {},
        "triage_load_pr_fields": {},
        "triage_evaluate_checks": {},
        "triage_evaluate_test_evidence": {},
        "triage_decide_triage_action": {},
        "triage_read_pr_assignees": {},
        "triage_decide_pr_assignee": {},
        "triage_assign_pr": {},
        "triage_verify_pr_assignee": {},
        "triage_read_pr_comments": {},
        "triage_decide_pr_comment": {},
        "triage_post_pr_comment": {},
        "triage_verify_pr_comment": {},
        "triage_read_merge_preconditions": {},
        "triage_merge_pr": {},
        "triage_read_merge_postcondition": {},
        "triage_verify_merge_provenance": {},
        "triage_verify_linked_merge_provenance": {},
        "triage_read_linked_issue_state": {},
        "triage_close_linked_issue": {},
        "triage_verify_linked_issue_closed": {},
        "triage_build_merge_receipt": {"receipt_path": merge_receipt},
        "triage_read_receipt_merge_provenance": {},
        "triage_publish_merge_receipt": {"receipt_path": merge_receipt},
        "triage_verify_merge_receipt": {"receipt_path": merge_receipt},
        "triage_read_review_tasks": {},
        "triage_find_review_marker": {},
        "triage_create_review_task": {},
        "triage_reconcile_review_task": {},
        "triage_build_repair_prompt": {},
        "triage_read_task_for_block": {},
        "triage_decide_task_block": {},
        "triage_block_task": {},
        "triage_verify_task_blocked": {},
        # Cleanup consumes branch/clone/worktree/receipt identity from
        # upstream conduction; explicit empty values avoid selecting a repo.
        "cleanup_resolve_cleanup_branch_source": {},
        "cleanup_parse_cleanup_issue_number": {},
        "cleanup_read_branch_ownership": {},
        "cleanup_derive_cleanup_paths": {"worktree_root": cfg.paths.worktree_root},
        "cleanup_validate_cleanup_identity": {},
        "cleanup_check_issue_closed": {},
        "cleanup_check_no_open_pr_for_branch": {},
        "cleanup_verify_cleanup_guards": {"require_safe": True},
        "cleanup_read_worktree_ownership": {},
        "cleanup_read_worktree_cleanliness": {},
        "cleanup_remove_worktree": {"require_safe": True},
        "cleanup_verify_worktree_absent": {},
        "cleanup_verify_branch_delete_guards": {},
        "cleanup_read_local_branch_ownership": {},
        "cleanup_delete_local_branch": {},
        "cleanup_verify_local_branch_absent": {},
        "cleanup_verify_claim_release_evidence": {},
        "cleanup_read_claim_identity": {"claim_path": cfg.paths.active_issue},
        "cleanup_release_claim_file": {"claim_path": cfg.paths.active_issue},
        "cleanup_verify_claim_absent": {"claim_path": cfg.paths.active_issue},
        "cleanup_collect_cleanup_receipt_evidence": {"receipt_path": cleanup_receipt},
        "cleanup_decide_cleanup_outcome": {},
        "cleanup_build_cleanup_receipt": {"receipt_path": cleanup_receipt},
        "cleanup_publish_cleanup_receipt": {"receipt_path": cleanup_receipt},
        "cleanup_verify_cleanup_receipt": {"receipt_path": cleanup_receipt},
        "cleanup_read_maintenance_tasks": {},
        "cleanup_find_maintenance_marker": {},
        "cleanup_create_maintenance_task": {},
        "cleanup_reconcile_maintenance_task": {},
    }
    config = _step_config(
        cfg,
        dry_run=dry_run,
        receipt_path=receipt,
        cleanup_receipt_path=cleanup_receipt,
        merge_receipt_path=merge_receipt,
        run_id=run_id,
        path_id="auto_worker",
    )
    return {"dry_run": dry_run, "repos": repos, "limit": limit, "run_id": run_id, "path_id": "auto_worker"}, {key: {**config, **value} for key, value in inputs.items()}

async def run_all(*, db_path: Path, config: Any, dry_run: bool, limit: int = 10) -> dict[str, Any]:
    run_id = f"auto-worker-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    inputs, effector_inputs = _prefixed_inputs(config, dry_run=dry_run, limit=limit, run_id=run_id)
    host = await run_package_path_async(
        db_path=db_path,
        package_path=_PACKAGE_PATH,
        path_id="auto_worker",
        run_id=run_id,
        inputs=inputs,
        effector_inputs=effector_inputs,
        run_metadata={"mode": "dry-run" if dry_run else "live"},
        max_ticks=160,
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
