"""cleanup correlation path: closed issue + no open PR → drop worktree/branch/claim."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from repo_agent.flows.runtime import FailurePolicy, run_repo_agent_path
from fala.models import CorrelationPathSpec
from fala.runtime_backend import Run, RuntimeBackendService

from repo_agent.config import AgentConfig, ConfigError, load_config
from repo_agent.flows.common import PathRunResult, effector, process_summary

CLEANUP_PATH = CorrelationPathSpec(
    id="cleanup",
    title="Worktree cleanup (parse → closed → no PR → remove → delete branch → release claim)",
    effectors=[
        effector(
            "parse_issue_from_branch",
            "repo_agent.steps.cleanup.parse_issue_from_branch",
        ),
        effector(
            "check_issue_closed",
            "repo_agent.steps.cleanup.check_issue_closed",
            conduction=["parse_issue_from_branch"],
        ),
        effector(
            "check_no_open_pr",
            "repo_agent.steps.cleanup.check_no_open_pr_for_branch",
            conduction=["parse_issue_from_branch"],
        ),
        effector(
            "remove_worktree",
            "repo_agent.steps.cleanup.remove_worktree",
            conduction=["check_issue_closed", "check_no_open_pr", "parse_issue_from_branch"],
        ),
        effector(
            "delete_local_fix_branch",
            "repo_agent.steps.cleanup.delete_local_fix_branch",
            conduction=["remove_worktree", "parse_issue_from_branch"],
        ),
        effector(
            "release_active_issue_claim",
            "repo_agent.steps.cleanup.release_active_issue_claim",
            conduction=[
                "remove_worktree",
                "parse_issue_from_branch",
                "check_issue_closed",
            ],
        ),
    ],
)

CLEANUP_FAILURE_POLICIES = {
    "parse_issue_from_branch": FailurePolicy.terminal,
    "check_issue_closed": FailurePolicy.retryable_read,
    "check_no_open_pr": FailurePolicy.retryable_read,
    "remove_worktree": FailurePolicy.terminal,
    "delete_local_fix_branch": FailurePolicy.terminal,
    "release_active_issue_claim": FailurePolicy.terminal,
}
CLEANUP_MAX_ATTEMPTS = {
    "parse_issue_from_branch": 1, "check_issue_closed": 3,
    "check_no_open_pr": 3, "remove_worktree": 1,
    "delete_local_fix_branch": 1, "release_active_issue_claim": 1,
}


async def run_cleanup_flow(
    *,
    db_path: Path,
    config: AgentConfig | None = None,
    dry_run: bool | None = None,
    repo: str | None = None,
    branch: str | None = None,
    clone_path: str | None = None,
    worktree_path: str | None = None,
    claim_path: str | None = None,
    run_id: str | None = None,
    worker_id: str = "repo-agent:tick-cleanup",
    max_ticks: int = 20,
) -> PathRunResult:
    """Run cleanup path on Fala 0.2.x."""
    cfg = config or load_config()
    is_dry = True if dry_run is None else dry_run
    if dry_run is False and not cfg.live:
        raise ConfigError("live execution requires config mode='live'")
    if dry_run is None and cfg.live:
        is_dry = False

    resolved_repo = repo or (cfg.repos[0].repo if cfg.repos else "")
    resolved_clone = clone_path or (cfg.repos[0].clone_path if cfg.repos else "")
    resolved_branch = branch or ""
    claim = claim_path or cfg.paths.active_issue
    wt = worktree_path or ""
    if not wt and resolved_branch:
        import re
        safe = re.sub(r"[^a-zA-Z0-9._/-]+", "-", resolved_branch)
        wt = str(Path(cfg.paths.worktree_root) / safe)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rid = run_id or f"cleanup-{stamp}-{uuid.uuid4().hex[:8]}"
    step_config: dict[str, Any] = {
        "repo": resolved_repo,
        "branch": resolved_branch,
        "clone_path": resolved_clone,
        "worktree_path": wt,
        "active_issue_path": claim,
        "gh_cli": cfg.gh_cli,
        "dry_run": is_dry,
        "require_safe": True,
        "blocked_label": cfg.labels.blocked,
        "executor_command": cfg.executor.command,
        "executor_model": cfg.executor.model,
        "executor_thinking": cfg.executor.thinking,
        "executor_timeout_seconds": cfg.executor.timeout_seconds,
    }

    service = RuntimeBackendService.sqlite(db_path)
    result = await run_repo_agent_path(
        service,
        run=Run(
            id=rid,
            title=f"cleanup dry_run={is_dry} {resolved_repo} {resolved_branch}",
            metadata={
                "correlation_path": CLEANUP_PATH.id,
                "dry_run": is_dry,
                "repo": resolved_repo,
                "branch": resolved_branch,
                "plugin": "oss-repo-agent",
            },
        ),
        correlation_path=CLEANUP_PATH,
        worker_id=worker_id,
        correlation_path_id=f"{rid}:{CLEANUP_PATH.id}",
        effector_inputs={
            "parse_issue_from_branch": {"branch": resolved_branch, "dry_run": is_dry},
            "check_issue_closed": {"repo": resolved_repo, "dry_run": is_dry},
            "check_no_open_pr": {"repo": resolved_repo, "branch": resolved_branch, "dry_run": is_dry},
            "remove_worktree": {"clone_path": resolved_clone, "worktree_path": wt, "dry_run": is_dry, "require_safe": True},
            "delete_local_fix_branch": {"clone_path": resolved_clone, "branch": resolved_branch, "dry_run": is_dry},
            "release_active_issue_claim": {"claim_path": claim, "repo": resolved_repo, "dry_run": is_dry},
        },
        effector_configs={e.id: step_config for e in CLEANUP_PATH.effectors},
        failure_policy_by_effector=CLEANUP_FAILURE_POLICIES,
        max_attempts_by_effector=CLEANUP_MAX_ATTEMPTS,
        retry_backoff_seconds=cfg.executor.retry_backoff_seconds,
        max_ticks=max_ticks,
        lease_seconds=300.0,
        actor=worker_id,
    )

    processes = list(result.processes or [])
    summaries = [process_summary(p) for p in processes]
    by_step = {s["step_id"]: s for s in summaries if s.get("step_id")}
    status = (
        result.status.value if hasattr(result.status, "value") else str(result.status)
    )
    parse_out = (by_step.get("parse_issue_from_branch") or {}).get("output", {})
    all_succeeded = bool(summaries) and all(s.get("status") == "succeeded" for s in summaries)
    has_terminal_failure = status in {"failed", "cancelled", "timed_out"} or bool(result.outcome.failed)
    is_noop = parse_out.get("status") == "noop" and all_succeeded and not has_terminal_failure
    path_status = "noop" if is_noop else status
    path_stopped_reason = str(parse_out.get("reason") or "no_branch") if is_noop else result.outcome.stopped_reason
    summary = {
        "repo": resolved_repo,
        "branch": resolved_branch,
        "closed": (by_step.get("check_issue_closed") or {}).get("output", {}).get(
            "closed"
        ),
        "safe_to_cleanup": (by_step.get("check_no_open_pr") or {}).get("output", {}).get(
            "safe_to_cleanup"
        ),
        "remove_status": (by_step.get("remove_worktree") or {}).get("output", {}).get(
            "status"
        ),
        "failed_steps": [s["step_id"] for s in summaries if s.get("status") in {"failed", "cancelled", "timed_out"}],
        "run_status": status,
    }

    return PathRunResult(
        run_id=rid,
        path_id=CLEANUP_PATH.id,
        dry_run=is_dry,
        ticks=result.outcome.ticks,
        stopped_reason=path_stopped_reason,
        completed=[process_summary(p) for p in result.outcome.completed],
        failed=[process_summary(p) for p in result.outcome.failed],
        processes=summaries,
        summary=summary,
        status=path_status,
    )
