"""CLI: repo-agent-tick-cleanup — Fala cleanup path for worktrees/branches."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from repo_agent.config import load_config
from repo_agent.flows.cleanup import run_cleanup_flow
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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dry = resolve_dry_run(args)
    if dry == 2:
        return 2
    cfg = load_config(args.config)
    db_path, _ = ensure_fala_paths(Path(args.db) if args.db else None)
    result = asyncio.run(
        run_cleanup_flow(
            db_path=db_path,
            config=cfg,
            dry_run=bool(dry),
            repo=args.repo,
            branch=args.branch,
            clone_path=args.clone_path,
            worktree_path=args.worktree_path,
            claim_path=args.claim_path,
        )
    )
    return print_path_result(result, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
