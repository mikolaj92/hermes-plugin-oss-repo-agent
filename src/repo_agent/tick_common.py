"""Shared CLI helpers for repo-agent tick entrypoints."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from repo_agent.flows.common import PathRunResult


def add_common_flags(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
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
        help="Allow mutations",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Print full JSON result",
    )
    return p


def resolve_dry_run(args: argparse.Namespace) -> bool | int:
    """Return dry_run bool, or 2 if --dry-run and --live conflict."""
    if args.dry_run and args.live:
        print("error: --dry-run and --live are mutually exclusive", file=sys.stderr)
        return 2
    if args.live:
        return False
    return True  # default safe


def print_path_result(result: PathRunResult, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str))
    else:
        print(f"run_id={result.run_id}")
        print(f"path_id={result.path_id}")
        print(
            f"dry_run={result.dry_run} ticks={result.ticks} "
            f"stopped={result.stopped_reason} status={result.status}"
        )
        if result.action:
            print(f"action={result.action}")
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
                    "mutated",
                    "ok",
                    "number",
                    "error",
                )
                if k in out
            }
            print(f"  step={step} status={status} {json.dumps(brief, default=str)}")
        if result.follow_up:
            fu = result.follow_up
            print(
                f"follow_up path={fu.get('path_id')} status={fu.get('status')} "
                f"ticks={fu.get('ticks')}"
            )
        if result.failed:
            print(f"FAILED_STEPS={len(result.failed)}", file=sys.stderr)
    return 1 if result.failed else 0
