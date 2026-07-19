from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from repo_agent.flows.runtime import FailurePolicy, run_repo_agent_path
from fala.models import CorrelationPathSpec
from fala.runtime_backend import Run, RuntimeBackendService

from repo_agent.config import AgentConfig, ConfigError, load_config
from repo_agent.flows.common import effector, process_summary


# Fala 0.2.x correlation path: poll → direction decide → reject comment → claim → kanban
INTAKE_PATH = CorrelationPathSpec(
    id="issue_intake",
    title="GitHub issue intake (poll → direction → comment → claim → kanban)",
    effectors=[
        effector("poll", "repo_agent.steps.poll.poll_eligible_issues"),
        effector(
            "decide_issue_action",
            "repo_agent.steps.issue_direction.decide_issue_action",
            conduction=["poll"],
        ),
        effector(
            "comment_issue",
            "repo_agent.steps.issue_direction.comment_issue_once",
            conduction=["poll", "decide_issue_action"],
        ),
        effector(
            "claim",
            "repo_agent.steps.claim.claim_github_issue",
            conduction=["poll", "decide_issue_action"],
        ),
        effector(
            "kanban",
            "repo_agent.steps.kanban_intake.ensure_kanban_intake",
            conduction=["claim", "decide_issue_action"],
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
    """Run intake with outcome-aware Fala lifecycle and explicit retry policy."""
    cfg = config or load_config()
    if dry_run is False and not cfg.live:
        raise ConfigError("live execution requires config mode='live'")
    is_dry = True if dry_run is None else dry_run
    if dry_run is None and cfg.live:
        is_dry = False

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rid = run_id or f"intake-{stamp}-{uuid.uuid4().hex[:8]}"

    step_config = {
        "assignee": cfg.assignee,
        "kanban_intake_assignee": cfg.kanban_intake_assignee,
        "ready_label": cfg.labels.ready,
        "in_progress_label": cfg.labels.in_progress,
        "blocked_label": cfg.labels.blocked,
        "pr_opened_label": cfg.labels.pr_opened,
        "generated_label": cfg.labels.generated,
        "gh_cli": cfg.gh_cli,
        "limit": limit,
        "dry_run": is_dry,
        "executor_enabled": cfg.executor.enabled,
        "executor_command": cfg.executor.command,
        "executor_model": cfg.executor.model,
        "executor_thinking": cfg.executor.thinking,
        "executor_timeout_seconds": cfg.executor.timeout_seconds,
        "repo_goal": cfg.direction.repo_goal,
        "direction_require_keywords": list(cfg.direction.require_keywords),
        "direction_deny_keywords": list(cfg.direction.deny_keywords),
        "direction_reject_labels": list(cfg.direction.reject_labels),
        "direction_min_goal_overlap": cfg.direction.min_goal_overlap,
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
    result = await run_repo_agent_path(
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
            "decide_issue_action": {"dry_run": is_dry},
            "comment_issue": {"dry_run": is_dry},
            "claim": {"dry_run": is_dry},
            "kanban": {"dry_run": is_dry},
        },
        effector_configs={
            "poll": step_config,
            "decide_issue_action": step_config,
            "comment_issue": step_config,
            "claim": step_config,
            "kanban": step_config,
        },
        failure_policy_by_effector={
            "poll": FailurePolicy.retryable_read,
            "decide_issue_action": FailurePolicy.terminal,
            "comment_issue": FailurePolicy.reconcile_then_retry,
            "claim": FailurePolicy.terminal,
            "kanban": FailurePolicy.terminal,
        },
        max_attempts_by_effector={
            "poll": 3,
            "decide_issue_action": 1,
            "comment_issue": 3,
            "claim": 1,
            "kanban": 1,
        },
        retry_backoff_seconds=cfg.executor.retry_backoff_seconds,
        max_ticks=max_ticks,
        lease_seconds=300.0,
        actor=worker_id,
    )

    processes = list(result.processes or [])
    summaries = [process_summary(p) for p in processes]
    by_step = {s["step_id"]: s for s in summaries if s.get("step_id")}
    poll_out = (by_step.get("poll") or {}).get("output") or {}
    decide_out = (by_step.get("decide_issue_action") or {}).get("output") or {}
    comment_out = (by_step.get("comment_issue") or {}).get("output") or {}
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
        "issue_action": decide_out.get("action"),
        "issue_reason": decide_out.get("reason"),
        "comment_status": comment_out.get("status"),
        "claim_status": claim_out.get("status"),
        "kanban_status": kanban_out.get("status"),
        "failed_steps": [s["step_id"] for s in summaries if s.get("status") in {"failed", "cancelled", "timed_out"}],
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
