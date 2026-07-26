"""CLI: lokay-tick-cleanup — Fala cleanup path for worktrees/branches."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from lokay.config import load_config
from lokay.flows.cleanup import run_cleanup_flow, run_cleanup_reconcile_flow
from lokay.runtime import ensure_fala_paths
from lokay.tick_common import add_common_flags, print_path_result, resolve_dry_run


def build_parser():
    import argparse

    p = argparse.ArgumentParser(
        prog="lokay-tick-cleanup",
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
        result = asyncio.run(
            run_cleanup_reconcile_flow(
                db_path=db_path,
                config=cfg,
                dry_run=bool(dry),
                branch=args.branch,
                repo=args.repo,
                clone_path=args.clone_path,
                worktree_path=args.worktree_path,
                claim_path=args.claim_path,
                issue=args.issue,
                pr_number=args.pr_number,
                task_id=args.task_id,
                task_receipt_path=args.task_receipt_path,
                merge_receipt_path=args.merge_receipt_path,
                receipt_path=args.receipt_path,
                base_sha=args.base_sha,
                head_oid=args.head_oid,
                merge_oid=args.merge_oid,
                origin_main_sha=args.origin_main_sha,
                authorize_remote_retention=args.authorize_remote_retention,
            )
        )
    else:
        result = asyncio.run(run_cleanup_flow(db_path=db_path, config=cfg, dry_run=bool(dry), repo=args.repo, branch=args.branch, clone_path=args.clone_path, worktree_path=args.worktree_path, claim_path=args.claim_path))
    return print_path_result(result, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
