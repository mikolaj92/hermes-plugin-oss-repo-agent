"""Run all auto-worker ticks once: intake → dispatch → triage → cleanup."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from repo_agent.config import load_config
from repo_agent.flows.cleanup import run_cleanup_flow
from repo_agent.flows.common import path_result_to_dict
from repo_agent.flows.intake import intake_result_to_dict, run_intake_flow
from repo_agent.flows.issue_to_pr import run_issue_to_pr_flow
from repo_agent.flows.triage import run_triage_flow
from repo_agent.runtime import ensure_fala_paths
from repo_agent.tick_cli import add_common_args, resolve_dry_run


async def run_all(*, db_path: Path, config, dry_run: bool, limit: int = 10) -> dict:
    intake = await run_intake_flow(
        db_path=db_path, config=config, dry_run=dry_run, limit=limit
    )
    dispatch = await run_issue_to_pr_flow(
        db_path=db_path, config=config, dry_run=dry_run
    )
    triage = await run_triage_flow(
        db_path=db_path, config=config, dry_run=dry_run, limit=limit
    )
    cleanup = await run_cleanup_flow(
        db_path=db_path, config=config, dry_run=dry_run
    )
    return {
        "dry_run": dry_run,
        "intake": intake_result_to_dict(intake),
        "dispatch": path_result_to_dict(dispatch),
        "triage": path_result_to_dict(triage),
        "cleanup": path_result_to_dict(cleanup),
        "any_failed": bool(
            intake.failed or dispatch.failed or triage.failed or cleanup.failed
        ),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="repo-agent-tick-all",
        description="One full auto-worker cycle (Fala paths, dry-run by default)",
    )
    add_common_args(p)
    p.add_argument("--limit", type=int, default=10)
    args = p.parse_args(argv)
    dry = resolve_dry_run(args)
    cfg = load_config(args.config)
    db_path, _ = ensure_fala_paths(Path(args.db) if args.db else None)
    result = asyncio.run(
        run_all(db_path=db_path, config=cfg, dry_run=dry, limit=args.limit)
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print(f"dry_run={result['dry_run']} any_failed={result['any_failed']}")
        for name in ("intake", "dispatch", "triage", "cleanup"):
            block = result[name]
            print(
                f"  {name}: run_id={block.get('run_id')} "
                f"ticks={block.get('ticks')} status={block.get('status')} "
                f"summary={json.dumps(block.get('summary'), default=str)}"
            )
    return 1 if result["any_failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
