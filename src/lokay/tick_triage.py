"""CLI: lokay-tick-triage — PR triage package path."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from lokay.config import load_config
from lokay.flows.triage import run_triage_flow
from lokay.runtime import ensure_fala_paths
from lokay.tick_common import add_common_flags, print_path_result, resolve_dry_run


def build_parser():
    p = argparse.ArgumentParser(
        prog="lokay-tick-triage",
        description="Fala-orchestrated PR triage package path. Default dry-run.",
    )
    add_common_flags(p)
    p.add_argument("--repo", default=None, help="owner/name (default: first config repo)")
    p.add_argument("--pr", type=int, default=None, dest="pr_number", help="PR number")
    p.add_argument("--limit", type=int, default=30, help="Max open PRs to scan")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dry = resolve_dry_run(args)
    if dry == 2:
        return 2
    cfg = load_config(args.config)
    db_path, _ = ensure_fala_paths(Path(args.db) if args.db else None)
    result = asyncio.run(
        run_triage_flow(
            db_path=db_path,
            config=cfg,
            dry_run=bool(dry),
            repo=args.repo,
            pr_number=args.pr_number,
            limit=args.limit,
        )
    )
    return print_path_result(result, as_json=args.json)




if __name__ == "__main__":
    raise SystemExit(main())
