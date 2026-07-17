"""CLI: repo-agent-tick-triage — decide then optional merge/comment/repair."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from repo_agent.config import load_config
from repo_agent.flows.triage import run_triage_with_router
from repo_agent.runtime import ensure_fala_paths
from repo_agent.tick_common import add_common_flags, print_path_result, resolve_dry_run


def build_parser():
    import argparse

    p = argparse.ArgumentParser(
        prog="repo-agent-tick-triage",
        description=(
            "Fala-orchestrated PR triage: decide action, then route to "
            "merge / comment_block / repair follow-up path. Default dry-run."
        ),
    )
    add_common_flags(p)
    p.add_argument("--repo", default=None, help="owner/name (default: first config repo)")
    p.add_argument("--pr", type=int, default=None, dest="pr_number", help="PR number")
    p.add_argument(
        "--decide-only",
        action="store_true",
        help="Only run decide path; do not execute follow-up",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dry = resolve_dry_run(args)
    if dry == 2:
        return 2
    cfg = load_config(args.config)
    db_path, _ = ensure_fala_paths(Path(args.db) if args.db else None)
    result = asyncio.run(
        run_triage_with_router(
            db_path=db_path,
            config=cfg,
            dry_run=bool(dry),
            repo=args.repo,
            pr_number=args.pr_number,
            execute_follow_up=not args.decide_only,
        )
    )
    return print_path_result(result, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
