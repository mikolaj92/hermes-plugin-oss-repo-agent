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

from repo_agent.config import load_config
from repo_agent.flows.common import PathRunResult, path_result_to_dict, process_summary, process_values
from repo_agent.flows.runtime import run_package_path_async
from repo_agent.runtime import ensure_fala_paths
from repo_agent.tick_common import add_common_flags, resolve_dry_run


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
        "dry_run": dry_run,
        **extra,
    }


def _prefixed_inputs(cfg: Any, *, dry_run: bool, limit: int) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    repos = [
        {"repo": r.repo, "board": r.board, "clone_path": r.clone_path, "priority": r.priority}
        for r in cfg.repos
    ]
    repo = cfg.repos[0].repo if cfg.repos else ""
    board = cfg.repos[0].board if cfg.repos else ""
    clone_path = cfg.repos[0].clone_path if cfg.repos else ""
    receipt = str(Path(cfg.paths.dispatch_receipts) / "auto-worker-dispatch.json")
    merge_receipt = str(Path(cfg.paths.merge_receipts) / "auto-worker-merge.json")
    common = {"dry_run": dry_run, "repo": repo, "board": board}
    inputs: dict[str, dict[str, Any]] = {
        "intake_poll": {"repos": repos, "limit": limit, "dry_run": dry_run},
        "intake_decide_issue_action": {"dry_run": dry_run},
        "intake_comment_issue": {"dry_run": dry_run},
        "intake_claim": {"dry_run": dry_run},
        "intake_kanban": {"dry_run": dry_run},
        "dispatch_load_kanban_task": {**common},
        "dispatch_parse_issue_ref": {"dry_run": dry_run},
        "dispatch_prepare_worktree": {"dry_run": dry_run, "clone_path": clone_path, "worktree_root": cfg.paths.worktree_root, "base_branch": cfg.base_branch},
        "dispatch_run_omp": {"dry_run": dry_run},
        "dispatch_verify_branch": {"dry_run": dry_run, "clone_path": clone_path, "base_branch": cfg.base_branch},
        "dispatch_push_branch": {"dry_run": dry_run},
        "dispatch_open_pull_request": {"dry_run": dry_run, "base_branch": cfg.base_branch},
        "dispatch_apply_pr_labels": {"dry_run": dry_run},
        "dispatch_write_dispatch_receipt": {"dry_run": dry_run, "receipt_path": receipt},
        "dispatch_complete_kanban_task": {"dry_run": dry_run, "board": board, "result": "dispatched via auto_worker"},
        "triage_list_ai_fix_prs": {"repo": repo, "dry_run": dry_run, "limit": limit},
        "triage_load_pr_fields": {"repo": repo, "dry_run": dry_run},
        "triage_evaluate_checks": {"dry_run": dry_run, "require_checks": cfg.automation.require_checks},
        "triage_evaluate_test_evidence": {"dry_run": dry_run, "require_test_evidence": cfg.automation.require_test_evidence},
        "triage_decide_triage_action": {"dry_run": dry_run, "automerge": cfg.automation.automerge, "branch_prefix": cfg.branch_prefix, "base_branch": cfg.base_branch},
        "triage_claim_pr": {**common},
        "triage_merge": {**common},
        "triage_write_merge_receipt": {"dry_run": dry_run, "receipt_path": merge_receipt},
        "triage_close_linked_issue": {"dry_run": dry_run, "repo": repo},
        "triage_comment_pr": {**common},
        "triage_create_review_fix_task": {**common},
        "triage_build_repair_prompt": {"dry_run": dry_run},
        "triage_repair_prepare_worktree": {"dry_run": dry_run, "clone_path": clone_path, "worktree_root": cfg.paths.worktree_root, "base_branch": cfg.base_branch},
        "triage_repair_run_omp": {"dry_run": dry_run},
        "triage_repair_push_branch": {"dry_run": dry_run},
        "cleanup_parse_issue_from_branch": {**common, "branch": ""},
        "cleanup_check_issue_closed": common.copy(),
        "cleanup_check_no_open_pr": {**common, "branch": ""},
        "cleanup_remove_worktree": {**common, "clone_path": clone_path, "worktree_path": "", "require_safe": True},
        "cleanup_delete_local_fix_branch": {**common, "clone_path": clone_path, "branch": ""},
        "cleanup_release_active_issue_claim": {**common, "claim_path": cfg.paths.active_issue},
    }
    config = _step_config(cfg, dry_run=dry_run, repo=repo, board=board, clone_path=clone_path, receipt_path=receipt, merge_receipt_path=merge_receipt)
    return {"dry_run": dry_run, "repos": repos, "limit": limit}, {key: {**config, **value} for key, value in inputs.items()}


async def run_all(*, db_path: Path, config: Any, dry_run: bool, limit: int = 10) -> dict[str, Any]:
    inputs, effector_inputs = _prefixed_inputs(config, dry_run=dry_run, limit=limit)
    run_id = f"auto-worker-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    host = await run_package_path_async(
        db_path=db_path,
        package_path=_PACKAGE_PATH,
        path_id="auto_worker",
        run_id=run_id,
        inputs=inputs,
        effector_inputs=effector_inputs,
        run_metadata={"mode": "dry-run" if dry_run else "live"},
        max_ticks=40,
        worker_id="repo-agent:tick-all",
    )
    processes = [process_summary(process) for process in host.processes]
    failed = [item for item in processes if item.get("status") in _TERMINAL_FAILURES]
    waiting = [item for item in processes if item.get("status") in _WAITING]
    outputs = [process_values(item) for item in processes]
    worked = any(
        bool(output.get("mutated") or output.get("selected"))
        or output.get("status") == "planned"
        or output.get("action") not in {None, "", "skip"}
        for output in outputs
    )
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
    p = argparse.ArgumentParser(prog="repo-agent-tick-all", description="One full auto-worker cycle (Fala path, dry-run by default)")
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
