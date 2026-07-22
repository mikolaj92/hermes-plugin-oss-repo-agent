from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from repo_agent.config import AgentConfig, ConfigError, load_config
from repo_agent.flows.common import PathRunResult, process_summary, process_values
from repo_agent.flows.runtime import run_package_path_async

async def run_intake_flow(
    *,
    db_path: Path,
    config: AgentConfig | None = None,
    dry_run: bool | None = None,
    limit: int = 10,
    run_id: str | None = None,
    worker_id: str = "repo-agent:tick-intake",
    max_ticks: int = 20,
) -> PathRunResult:
    """Run the issue-intake package path once and return journal evidence."""
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
        "max_active_issues": cfg.automation.max_active_issues,
        "active_issue_path": cfg.paths.active_issue,
        "paths": {"active_issue": cfg.paths.active_issue},
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
            "repo": entry.repo,
            "board": entry.board,
            "clone_path": entry.clone_path,
            "priority": entry.priority,
        }
        for entry in cfg.repos
    ]
    dry_input = {"dry_run": is_dry}
    result = await run_package_path_async(
        db_path=db_path,
        package_path=Path(__file__).resolve().parents[3] / "fala-package.toml",
        path_id="issue_intake",
        run_id=rid,
        inputs={"repos": repos, "limit": limit, **dry_input},
        effector_inputs={
            "poll": {"repos": repos, "limit": limit, **dry_input},
            "decide_issue_action": dry_input,
            "comment_issue": dry_input,
            "claim": dry_input,
            "kanban": dry_input,
        },
        effector_configs={step: step_config for step in ("poll", "decide_issue_action", "comment_issue", "claim", "kanban")},
        max_ticks=max_ticks,
        worker_id=worker_id,
    )

    processes = [process_summary(process) for process in result.processes]
    by_step = {item["step_id"]: item for item in processes if item.get("step_id")}
    poll_out = process_values(by_step.get("poll") or {})
    decide_out = process_values(by_step.get("decide_issue_action") or {})
    comment_out = process_values(by_step.get("comment_issue") or {})
    claim_out = process_values(by_step.get("claim") or {})
    kanban_out = process_values(by_step.get("kanban") or {})
    failed_steps = [
        item["step_id"]
        for item in processes
        if item.get("status") in {"failed", "cancelled", "timed_out"}
    ]
    raw_status = result.run_status
    worked = bool(
        poll_out.get("selected")
        or comment_out.get("mutated")
        or claim_out.get("mutated")
        or kanban_out.get("mutated")
    )
    status = "idle" if raw_status == "completed" and not failed_steps and not worked else raw_status
    stopped_reason = "failed" if failed_steps else ("worked" if worked else "idle")
    summary = {
        "eligible_count": poll_out.get("eligible_count", 0),
        "selected": poll_out.get("selected"),
        "issue_action": decide_out.get("action"),
        "issue_reason": decide_out.get("reason"),
        "comment_status": comment_out.get("status"),
        "claim_status": claim_out.get("status"),
        "kanban_status": kanban_out.get("status"),
        "worked": worked,
        "failed_steps": failed_steps,
        "run_status": status,
    }
    return PathRunResult(
        run_id=rid,
        path_id="issue_intake",
        dry_run=is_dry,
        ticks=result.ticks,
        stopped_reason=stopped_reason,
        completed=[process_summary(process) for process in result.completed],
        failed=[process_summary(process) for process in result.failed],
        processes=processes,
        summary=summary,
        status=status,
    )
