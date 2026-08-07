from __future__ import annotations

from pathlib import Path

from lokay.config import AgentConfig
from lokay.flows.common import PathRunResult
from lokay.registry import PROCESS_IDS


async def run_intake_flow(
    *,
    db_path: Path,
    config: AgentConfig | None = None,
    dry_run: bool | None = None,
    limit: int = 10,
    run_id: str | None = None,
    worker_id: str = "lokay:tick-intake",
    max_ticks: int = 20,
) -> PathRunResult:
    """Diagnostic wrapper for retired aggregate intake.

    Production work uses the twelve canonical process path IDs via
    ``lokay.process``. Aggregate ``issue_intake`` must never be invoked.
    """
    del db_path, config, dry_run, limit, run_id, worker_id, max_ticks
    raise RuntimeError(
        "aggregate issue_intake activation is retired; use lokay.process "
        f"with canonical path IDs {PROCESS_IDS}"
    )
