"""Cleanup flow backed by the repository Fala package path."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from repo_agent.config import AgentConfig, ConfigError, load_config
from repo_agent.flows.common import PathRunResult, process_summary, process_values
from repo_agent.flows.runtime import run_package_path_async


_PACKAGE_PATH = Path(__file__).resolve().parents[3] / "fala-package.toml"
_PROCESS_FAILURES = {"failed", "cancelled", "timed_out"}
_CLEANUP_EFFECTORS = (
    "parse_issue_from_branch",
    "check_issue_closed",
    "check_no_open_pr",
    "remove_worktree",
    "delete_local_fix_branch",
    "release_active_issue_claim",
)


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
    """Run exactly one cleanup package path invocation."""
    cfg = config or load_config()
    is_dry = True if dry_run is None else dry_run
    if dry_run is False and not cfg.live:
        raise ConfigError("live execution requires config mode='live'")
    if dry_run is None and cfg.live:
        is_dry = False

    resolved_repo = repo or (cfg.repos[0].repo if cfg.repos else "")
    resolved_board = cfg.repos[0].board if cfg.repos else ""
    resolved_clone = clone_path or (cfg.repos[0].clone_path if cfg.repos else "")
    resolved_branch = branch or ""
    resolved_claim = claim_path or cfg.paths.active_issue
    resolved_worktree = worktree_path or ""
    if not resolved_worktree and resolved_branch:
        safe = re.sub(r"[^a-zA-Z0-9._/-]+", "-", resolved_branch)
        resolved_worktree = str(Path(cfg.paths.worktree_root) / safe)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rid = run_id or f"cleanup-{stamp}-{uuid.uuid4().hex[:8]}"
    step_config: dict[str, Any] = {
        "repo": resolved_repo,
        "board": resolved_board,
        "branch": resolved_branch,
        "clone_path": resolved_clone,
        "worktree_path": resolved_worktree,
        "active_issue_path": resolved_claim,
        "gh_cli": cfg.gh_cli,
        "dry_run": is_dry,
        "require_safe": True,
        "blocked_label": cfg.labels.blocked,
        "executor_command": cfg.executor.command,
        "executor_model": cfg.executor.model,
        "executor_thinking": cfg.executor.thinking,
        "executor_timeout_seconds": cfg.executor.timeout_seconds,
    }
    common = {"dry_run": is_dry, "repo": resolved_repo, "board": resolved_board}
    effector_inputs: dict[str, dict[str, Any]] = {
        "parse_issue_from_branch": {**common, "branch": resolved_branch},
        "check_issue_closed": common.copy(),
        "check_no_open_pr": {**common, "branch": resolved_branch},
        "remove_worktree": {
            **common,
            "clone_path": resolved_clone,
            "worktree_path": resolved_worktree,
            "require_safe": True,
        },
        "delete_local_fix_branch": {
            **common,
            "clone_path": resolved_clone,
            "branch": resolved_branch,
        },
        "release_active_issue_claim": {**common, "claim_path": resolved_claim},
    }
    result = await run_package_path_async(
        db_path=db_path,
        package_path=_PACKAGE_PATH,
        path_id="cleanup",
        run_id=rid,
        effector_inputs=effector_inputs,
        effector_configs={effector: step_config for effector in _CLEANUP_EFFECTORS},
        max_ticks=max_ticks,
        worker_id=worker_id,
    )

    processes = list(result.processes)
    summaries = [process_summary(process) for process in processes]
    by_step = {summary["step_id"]: summary for summary in summaries if summary.get("step_id")}
    parse_output = process_values(by_step.get("parse_issue_from_branch") or {})
    run_status = str(result.run_status)
    terminal_failures = [summary for summary in summaries if summary.get("status") in _PROCESS_FAILURES]
    outputs = [process_values(summary) for summary in summaries]
    worked = any(bool(output.get("mutated")) for output in outputs)
    idle = (
        parse_output.get("status") == "noop"
        and parse_output.get("reason") == "no_branch"
        and not any(summary.get("status") in {"failed", "timed_out"} for summary in summaries)
        and not worked
    ) or (
        not terminal_failures
        and not worked
        and any(output.get("status") in {"noop", "planned"} for output in outputs)
    )
    status = "idle" if idle else run_status
    summary = {
        "repo": resolved_repo,
        "branch": resolved_branch,
        "closed": (by_step.get("check_issue_closed") or {}).get("output", {}).get("closed"),
        "safe_to_cleanup": (by_step.get("check_no_open_pr") or {}).get("output", {}).get("safe_to_cleanup"),
        "remove_status": (by_step.get("remove_worktree") or {}).get("output", {}).get("status"),
        "delete_status": (by_step.get("delete_local_fix_branch") or {}).get("output", {}).get("status"),
        "release_status": (by_step.get("release_active_issue_claim") or {}).get("output", {}).get("status"),
        "failed_steps": [failure["step_id"] for failure in terminal_failures],
        "worked": worked,
        "run_status": status,
    }
    return PathRunResult(
        run_id=rid,
        path_id="cleanup",
        dry_run=is_dry,
        ticks=result.ticks,
        stopped_reason="no_branch" if idle else run_status,
        completed=[process_summary(process) for process in result.completed],
        failed=[process_summary(process) for process in result.failed],
        processes=summaries,
        summary=summary,
        status=status,
    )
