from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fala.driver import run_correlation_path
from fala.models import CorrelationPathSpec
from fala.runtime_backend import Run, RuntimeBackendService

from repo_agent.config import AgentConfig, load_config
from repo_agent.flows.common import effector, process_summary


# Fala 0.2.x correlation path: poll → claim → kanban
INTAKE_PATH = CorrelationPathSpec(
    id="issue_intake",
    title="GitHub issue intake (poll → claim → kanban)",
    effectors=[
        effector("poll", "repo_agent.steps.poll.poll_eligible_issues"),
        effector(
            "claim",
            "repo_agent.steps.claim.claim_github_issue",
            conduction=["poll"],
        ),
        effector(
            "kanban",
            "repo_agent.steps.kanban_intake.ensure_kanban_intake",
            conduction=["claim"],
        ),
    ],
)

# Back-compat alias for imports/docs
INTAKE_FLOW = INTAKE_PATH


@dataclass
class IntakeRunResult:
    run_id: str
    dry_run: bool
    ticks: int
    stopped_reason: str
    completed: list[dict[str, Any]]
    failed: list[dict[str, Any]]
    processes: list[dict[str, Any]]
    summary: dict[str, Any]
    fala_version: str = "0.2.1"
    status: str = ""


async def run_intake_flow(
    *,
    db_path: Path,
    config: AgentConfig | None = None,
    dry_run: bool | None = None,
    limit: int = 10,
    run_id: str | None = None,
    worker_id: str = "repo-agent:tick-intake",
    max_ticks: int = 20,
) -> IntakeRunResult:
    """Run intake on Fala 0.2.x via ``run_correlation_path``.

    Fala owns create_run, instantiate_correlation_path, claim/execute/advance,
    and terminal run status. We only supply the path + effector inputs.
    """
    cfg = config or load_config()
    is_dry = True if dry_run is None else dry_run
    if dry_run is None and cfg.live:
        is_dry = False

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rid = run_id or f"intake-{stamp}-{uuid.uuid4().hex[:8]}"

    step_config = {
        "assignee": cfg.assignee,
        "kanban_intake_assignee": cfg.kanban_intake_assignee,
        "ready_label": cfg.ready_label,
        "in_progress_label": "ai:in-progress",
        "gh_cli": cfg.gh_cli,
        "limit": limit,
        "dry_run": is_dry,
    }
    repos = [
        {
            "repo": r.repo,
            "board": r.board,
            "clone_path": r.clone_path,
            "priority": r.priority,
        }
        for r in cfg.repos
    ]

    service = RuntimeBackendService.sqlite(db_path)
    result = await run_correlation_path(
        service,
        run=Run(
            id=rid,
            title=f"issue intake dry_run={is_dry}",
            metadata={
                "correlation_path": INTAKE_PATH.id,
                "dry_run": is_dry,
                "repos": [r.repo for r in cfg.repos],
                "plugin": "oss-repo-agent",
            },
        ),
        correlation_path=INTAKE_PATH,
        worker_id=worker_id,
        correlation_path_id=f"{rid}:{INTAKE_PATH.id}",
        # Note: "config"/"adapter"/"conduction" are reserved in effector_inputs.
        # Agent settings go through effector_configs → request.config.
        effector_inputs={
            "poll": {
                "repos": repos,
                "limit": limit,
                "dry_run": is_dry,
            },
            "claim": {"dry_run": is_dry},
            "kanban": {"dry_run": is_dry},
        },
        effector_configs={
            "poll": step_config,
            "claim": step_config,
            "kanban": step_config,
        },
        max_ticks=max_ticks,
        lease_seconds=300.0,
        actor=worker_id,
    )

    processes = list(result.processes or [])
    summaries = [process_summary(p) for p in processes]
    by_step = {s["step_id"]: s for s in summaries if s.get("step_id")}
    poll_out = (by_step.get("poll") or {}).get("output") or {}
    claim_out = (by_step.get("claim") or {}).get("output") or {}
    kanban_out = (by_step.get("kanban") or {}).get("output") or {}

    status = (
        result.status.value
        if hasattr(result.status, "value")
        else str(result.status)
    )
    summary = {
        "eligible_count": poll_out.get("eligible_count", 0),
        "selected": poll_out.get("selected"),
        "claim_status": claim_out.get("status"),
        "kanban_status": kanban_out.get("status"),
        "failed_steps": [s["step_id"] for s in summaries if s.get("status") == "failed"],
        "run_status": status,
    }

    return IntakeRunResult(
        run_id=rid,
        dry_run=is_dry,
        ticks=result.outcome.ticks,
        stopped_reason=result.outcome.stopped_reason,
        completed=[process_summary(p) for p in result.outcome.completed],
        failed=[process_summary(p) for p in result.outcome.failed],
        processes=summaries,
        summary=summary,
        fala_version="0.2.1",
        status=status,
    )


def intake_result_to_dict(result: IntakeRunResult) -> dict[str, Any]:
    return asdict(result)
