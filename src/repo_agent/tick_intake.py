from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from repo_agent.config import load_config
from repo_agent.flows.intake import intake_result_to_dict, run_intake_flow
from repo_agent.runtime import ensure_fala_paths


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="repo-agent-tick-intake",
        description=(
            "Fala-orchestrated intake tick: poll → claim → kanban. "
            "Default is dry-run (no GitHub/Kanban mutations)."
        ),
    )
    p.add_argument(
        "--config",
        default=None,
        help="Path to config.toml (default: ~/.hermes/oss-repo-agent/config.toml)",
    )
    p.add_argument(
        "--db",
        default=None,
        help="Fala SQLite path (default: ~/.hermes/oss-repo-agent/fala/state.sqlite)",
    )
    p.add_argument("--limit", type=int, default=10, help="Max open issues per repo to scan")
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Force dry-run (no mutations)",
    )
    p.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Allow mutations (claim + kanban create)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Print full JSON result",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dry_run and args.live:
        print("error: --dry-run and --live are mutually exclusive", file=sys.stderr)
        return 2

    cfg = load_config(args.config)
    dry_run: bool | None
    if args.live:
        dry_run = False
    elif args.dry_run:
        dry_run = True
    else:
        # Prefer explicit dry-run unless config mode is live *and* operator passed nothing.
        # Safety: default dry-run when neither flag set.
        dry_run = True

    db_path, _ = ensure_fala_paths(Path(args.db) if args.db else None)
    result = asyncio.run(
        run_intake_flow(
            db_path=db_path,
            config=cfg,
            dry_run=dry_run,
            limit=args.limit,
        )
    )

    if args.json:
        print(json.dumps(intake_result_to_dict(result), indent=2, sort_keys=True, default=str))
    else:
        print(f"run_id={result.run_id}")
        print(f"dry_run={result.dry_run} ticks={result.ticks} stopped={result.stopped_reason}")
        print(f"summary={json.dumps(result.summary, default=str)}")
        for proc in result.processes:
            step = proc.get("step_id") or "?"
            status = proc.get("status")
            out = proc.get("output") or {}
            brief = {
                k: out.get(k)
                for k in (
                    "status",
                    "reason",
                    "eligible_count",
                    "selected",
                    "mutated",
                    "error",
                )
                if k in out
            }
            print(f"  step={step} status={status} {json.dumps(brief, default=str)}")
        if result.failed:
            print(f"FAILED_STEPS={len(result.failed)}", file=sys.stderr)
            return 1
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
