"""CLI: repo-agent-tick-cleanup — Fala cleanup path for worktrees/branches."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from repo_agent.config import load_config
from repo_agent.flows.cleanup import run_cleanup_flow
from repo_agent.flows.common import PathRunResult
from repo_agent.steps.cleanup_reconcile import reconcile_no_target_cleanup
from repo_agent.runtime import ensure_fala_paths
from repo_agent.tick_common import add_common_flags, print_path_result, resolve_dry_run


def build_parser():
    import argparse

    p = argparse.ArgumentParser(
        prog="repo-agent-tick-cleanup",
        description=(
            "Fala-orchestrated cleanup tick: parse branch → ensure issue closed "
            "and no open PR → remove worktree → delete local branch → release claim. "
            "Default dry-run."
        ),
    )
    add_common_flags(p)
    p.add_argument("--repo", default=None, help="owner/name")
    p.add_argument("--branch", required=True, help="ai/fix/... branch to clean")
    p.add_argument("--clone-path", default=None, help="Clone path override")
    p.add_argument("--worktree-path", default=None, help="Worktree path override")
    p.add_argument("--claim-path", default=None, help="active-issue claim JSON path")
    p.add_argument("--reconcile-no-target", action="store_true", help="Prove an exact already-absent target and retain its remote branch")
    p.add_argument("--issue", type=int)
    p.add_argument("--pr-number", type=int)
    p.add_argument("--task-id")
    p.add_argument("--task-receipt-path")
    p.add_argument("--merge-receipt-path")
    p.add_argument("--receipt-path")
    p.add_argument("--base-sha")
    p.add_argument("--head-oid")
    p.add_argument("--merge-oid")
    p.add_argument("--origin-main-sha")
    p.add_argument("--authorize-remote-retention", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dry = resolve_dry_run(args)
    if dry == 2:
        return 2
    cfg = load_config(args.config)
    db_path, _ = ensure_fala_paths(Path(args.db) if args.db else None)
    if args.reconcile_no_target:
        context = next((entry for entry in cfg.repos if (not args.repo or entry.repo == args.repo) and (not args.clone_path or entry.clone_path == str(Path(args.clone_path).expanduser().absolute()))), None)
        if context is None:
            result = PathRunResult(run_id="cleanup-reconcile", path_id="cleanup_reconcile", dry_run=bool(dry), ticks=0, stopped_reason="repository_context_not_found", summary={"run_status": "failed"}, status="failed")
        else:
            worktree_path = args.worktree_path or str(Path(cfg.paths.worktree_root) / args.branch)
            output = reconcile_no_target_cleanup({"input": {
                "repo": context.repo, "issue": args.issue, "pr_number": args.pr_number, "task_id": args.task_id,
                "branch": args.branch, "clone_path": context.clone_path, "worktree_path": worktree_path,
                "claim_path": args.claim_path or cfg.paths.active_issue, "task_receipt_path": args.task_receipt_path,
                "merge_receipt_path": args.merge_receipt_path, "receipt_path": args.receipt_path, "db_path": str(db_path),
                "base_sha": args.base_sha, "head_oid": args.head_oid, "merge_oid": args.merge_oid,
                "origin_main_sha": args.origin_main_sha, "remote_retention_authorized": args.authorize_remote_retention,
                "dry_run": bool(dry),
            }, "config": {
                "repo": context.repo, "clone_path": context.clone_path, "worktree_root": cfg.paths.worktree_root,
                "claim_root": cfg.paths.active_issue, "db_path": str(db_path),
                "task_receipt_root": cfg.paths.task_receipts,
                "merge_receipt_root": cfg.paths.merge_receipts,
                "cleanup_receipt_root": str(Path(cfg.paths.merge_receipts) / "cleanup-outcomes"),
            }})
            failed = output.get("ok") is not True
            result = PathRunResult(run_id="cleanup-reconcile", path_id="cleanup_reconcile", dry_run=bool(dry), ticks=1, stopped_reason=str(output.get("reason") or output.get("status")), failed=[output] if failed else [], processes=[{"step_id": "reconcile_no_target_cleanup", "status": "failed" if failed else "succeeded", "output": output}], summary={"run_status": "failed" if failed else "completed", "outcome": output.get("status")}, status="failed" if failed else "completed")
    else:
        result = asyncio.run(run_cleanup_flow(db_path=db_path, config=cfg, dry_run=bool(dry), repo=args.repo, branch=args.branch, clone_path=args.clone_path, worktree_path=args.worktree_path, claim_path=args.claim_path))
    return print_path_result(result, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
