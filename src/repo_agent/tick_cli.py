"""Shared CLI helpers for auto-worker ticks."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable


def add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", default=None, help="config.toml path")
    p.add_argument("--db", default=None, help="Fala SQLite path")
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Force dry-run (default when neither --live nor --dry-run)",
    )
    p.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Allow mutations",
    )
    p.add_argument("--json", action="store_true", help="Full JSON result")


def resolve_dry_run(args: argparse.Namespace) -> bool:
    if args.dry_run and args.live:
        raise SystemExit("error: --dry-run and --live are mutually exclusive")
    if args.live:
        return False
    return True  # default safe


def print_path_result(result: Any, *, as_json: bool) -> int:
    if as_json:
        from repo_agent.flows.common import path_result_to_dict

        print(json.dumps(path_result_to_dict(result), indent=2, sort_keys=True, default=str))
    else:
        print(f"run_id={result.run_id} path={getattr(result, 'path_id', '?')}")
        print(
            f"dry_run={result.dry_run} ticks={result.ticks} "
            f"stopped={result.stopped_reason} status={result.status}"
        )
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
                    "action",
                    "number",
                    "task_id",
                    "mutated",
                    "ok",
                )
                if k in out
            }
            print(f"  step={step} status={status} {json.dumps(brief, default=str)}")
    return 1 if result.failed else 0
