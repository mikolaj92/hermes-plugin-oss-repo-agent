from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from lokay.config import load_config
from lokay.flows.intake import run_intake_flow
from lokay.runtime import ensure_fala_paths
from lokay.tick_common import add_common_flags, print_path_result, resolve_dry_run

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lokay-tick-intake",
        description=(
            "Retired aggregate intake entrypoint. "
            "Use lokay.process with canonical process path IDs."
        ),
    )
    add_common_flags(p)
    p.add_argument("--limit", type=int, default=10, help="Max open issues per repo to scan")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dry = resolve_dry_run(args)
    if dry == 2:
        return 2
    cfg = load_config(args.config)
    db_path, _ = ensure_fala_paths(Path(args.db) if args.db else None)
    result = asyncio.run(
        run_intake_flow(
            db_path=db_path,
            config=cfg,
            dry_run=bool(dry),
            limit=args.limit,
        )
    )
    return print_path_result(result, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
