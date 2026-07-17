"""CLI: repo-agent-tick-dispatch — Fala issue_to_pr path."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from repo_agent.config import load_config
from repo_agent.flows.issue_to_pr import run_issue_to_pr_flow
from repo_agent.runtime import ensure_fala_paths
from repo_agent.tick_common import add_common_flags, print_path_result, resolve_dry_run


def build_parser():
    import argparse

    p = argparse.ArgumentParser(
        prog="repo-agent-tick-dispatch",
        description=(
            "Fala-orchestrated issue→PR dispatch tick. "
            "Default is dry-run (no git/gh/kanban mutations)."
        ),
    )
    add_common_flags(p)
    p.add_argument("--board", default=None, help="Kanban board id")
    p.add_argument("--task-id", default=None, help="Specific Kanban task id")
    p.add_argument("--clone-path", default=None, help="Override clone path")
    p.add_argument("--worktree-root", default=None, help="Worktree root directory")
    p.add_argument("--receipt-path", default=None, help="Dispatch receipt JSON path")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dry = resolve_dry_run(args)
    if dry == 2:
        return 2
    cfg = load_config(args.config)
    db_path, _ = ensure_fala_paths(Path(args.db) if args.db else None)
    result = asyncio.run(
        run_issue_to_pr_flow(
            db_path=db_path,
            config=cfg,
            dry_run=bool(dry),
            board=args.board,
            task_id=args.task_id,
            clone_path=args.clone_path,
            worktree_root=args.worktree_root,
            receipt_path=args.receipt_path,
        )
    )
    return print_path_result(result, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
