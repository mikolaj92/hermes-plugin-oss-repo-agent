"""Diagnostic entrypoint for the retired aggregate auto-worker path."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from lokay.config import load_config
from lokay.registry import PROCESS_IDS
from lokay.runtime import ensure_fala_paths
from lokay.tick_common import add_common_flags, resolve_dry_run


async def run_all(*, db_path: Path, config: Any, dry_run: bool, limit: int = 10) -> dict[str, Any]:
    """Fail closed: aggregate auto_worker/tick_all must never invoke Fala."""
    del db_path, config, dry_run, limit
    raise RuntimeError(
        "aggregate auto_worker activation is retired; use lokay.process "
        f"with canonical path IDs {PROCESS_IDS}"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="lokay-tick-all",
        description=(
            "Retired aggregate auto-worker entrypoint. "
            "Use lokay.process with canonical process path IDs."
        ),
    )
    add_common_flags(p)
    p.add_argument("--limit", type=int, default=10)
    args = p.parse_args(argv)
    dry = resolve_dry_run(args)
    if dry == 2:
        return 2
    # Keep flag parsing/load side effects for CLI compatibility diagnostics.
    load_config(args.config)
    ensure_fala_paths(Path(args.db) if args.db else None)
    message = (
        "aggregate auto_worker activation is retired; use lokay.process "
        f"with canonical path IDs {PROCESS_IDS}"
    )
    if args.json:
        print(
            json.dumps(
                {
                    "error": message,
                    "path_id": None,
                    "status": "failed",
                    "any_failed": True,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
