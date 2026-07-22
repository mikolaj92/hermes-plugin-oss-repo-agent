"""issue_to_pr package path: Kanban task → worktree → OMP → PR."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from repo_agent.config import AgentConfig, ConfigError, load_config
from repo_agent.flows.common import PathRunResult, process_summary, process_values
from repo_agent.flows.runtime import run_package_path_async


_PATH_ID = "issue_to_pr"
_EFFECTOR_IDS = (
    "load_kanban_task",
    "parse_issue_ref",
    "prepare_worktree",
    "run_omp",
    "verify_branch",
    "push_branch",
    "open_pull_request",
    "apply_pr_labels",
    "write_dispatch_receipt",
    "complete_kanban_task",
)


def _repo_map(cfg: AgentConfig) -> dict[str, dict[str, Any]]:
    return {
        r.repo: {
            "repo": r.repo,
            "board": r.board,
            "clone_path": r.clone_path,
            "priority": r.priority,
        }
        for r in cfg.repos
    }


def _package_path() -> Path:
    return Path(__file__).resolve().parents[3] / "fala-package.toml"


async def run_issue_to_pr_flow(
    *,
    db_path: Path,
    config: AgentConfig | None = None,
    dry_run: bool | None = None,
    board: str | None = None,
    task_id: str | None = None,
    clone_path: str | None = None,
    worktree_root: str | None = None,
    receipt_path: str | None = None,
    run_id: str | None = None,
    worker_id: str = "repo-agent:tick-dispatch",
    max_ticks: int = 40,
) -> PathRunResult:
    """Run issue_to_pr once through the Fala 0.7 package host facade."""
    cfg = config or load_config()
    if dry_run is False and not cfg.live:
        raise ConfigError("live execution requires config mode='live'")
    is_dry = True if dry_run is None else dry_run
    if dry_run is None and cfg.live:
        is_dry = False
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rid = run_id or f"issue-to-pr-{stamp}-{uuid.uuid4().hex[:8]}"
    if not cfg.repos and not board and not task_id:
        return PathRunResult(
            run_id=rid,
            path_id=_PATH_ID,
            dry_run=is_dry,
            ticks=0,
            stopped_reason="no_repositories",
            summary={
                "run_status": "idle",
                "repos": [],
                "board": "",
                "task_id": None,
                "failed_steps": [],
                "worked": False,
            },
            status="idle",
        )

    resolved_board = board or (cfg.repos[0].board if cfg.repos else "")
    resolved_clone = clone_path or (cfg.repos[0].clone_path if cfg.repos else "")
    wt_root = worktree_root or cfg.paths.worktree_root
    if not wt_root:
        wt_root = os.environ.get(
            "HERMES_WORKTREE_ROOT",
            str(Path.home() / ".hermes" / "worktrees" / "repo-fixer"),
        )
    receipt = receipt_path or str(Path(cfg.paths.dispatch_receipts) / f"dispatch-{rid}.json")
    step_config: dict[str, Any] = {
        "board": resolved_board,
        "clone_path": resolved_clone,
        "worktree_root": wt_root,
        "base_branch": cfg.base_branch,
        "branch_prefix": cfg.branch_prefix,
        "gh_cli": cfg.gh_cli,
        "assignee": cfg.assignee,
        "fixer_assignee": cfg.automation.fixer_assignee,
        "model": cfg.executor.model,
        "thinking": cfg.executor.thinking,
        "command": cfg.executor.command,
        "timeout_seconds": cfg.executor.timeout_seconds,
        "executor_enabled": cfg.executor.enabled,
        "dry_run": is_dry,
        "receipt_path": receipt,
        "pr_opened_label": cfg.labels.pr_opened,
        "generated_label": cfg.labels.generated,
    }
    dry_input = {"dry_run": is_dry}
    effector_inputs: dict[str, dict[str, Any]] = {
        "load_kanban_task": {
            **dry_input,
            "board": resolved_board,
            **({"task_id": task_id} if task_id else {}),
        },
        "parse_issue_ref": dry_input,
        "prepare_worktree": {
            **dry_input,
            "clone_path": resolved_clone,
            "worktree_root": wt_root,
            "base_branch": cfg.base_branch,
        },
        "run_omp": dry_input,
        "verify_branch": {**dry_input, "clone_path": resolved_clone, "base_branch": cfg.base_branch},
        "push_branch": dry_input,
        "open_pull_request": {**dry_input, "base_branch": cfg.base_branch},
        "apply_pr_labels": dry_input,
        "write_dispatch_receipt": {**dry_input, "receipt_path": receipt},
        "complete_kanban_task": {
            **dry_input,
            "board": resolved_board,
            "result": "dispatched via issue_to_pr",
        },
    }
    result = await run_package_path_async(
        db_path=db_path,
        package_path=_package_path(),
        path_id=_PATH_ID,
        run_id=rid,
        inputs={"dry_run": is_dry},
        effector_inputs=effector_inputs,
        effector_configs={eid: step_config for eid in _EFFECTOR_IDS},
        max_ticks=max_ticks,
        worker_id=worker_id,
    )

    processes = [process_summary(process) for process in result.processes]
    by_step = {item["step_id"]: item for item in processes if item.get("step_id")}
    load_output = process_values(by_step.get("load_kanban_task") or {})
    pr_output = process_values(by_step.get("open_pull_request") or {})
    idle = load_output.get("status") == "noop" and load_output.get("reason") == "no_ready_task"
    status = "idle" if idle and result.run_status == "completed" else result.run_status
    summary = {
        "board": resolved_board,
        "task_id": task_id,
        "load_status": load_output.get("status"),
        "parse_status": (by_step.get("parse_issue_ref") or {}).get("output", {}).get("status"),
        "pr_status": pr_output.get("status"),
        "pr_number": pr_output.get("number"),
        "pr_url": pr_output.get("url"),
        "worked": bool(pr_output.get("number") and pr_output.get("url")),
        "outcome": "idle" if idle else status,
        "failed_steps": [item["step_id"] for item in processes if item.get("status") in {"failed", "cancelled", "timed_out"}],
        "run_status": result.run_status,
        "repos": list(_repo_map(cfg)),
    }
    return PathRunResult(
        run_id=rid,
        path_id=_PATH_ID,
        dry_run=is_dry,
        ticks=result.ticks,
        stopped_reason="idle" if idle else result.run_status,
        completed=[process_summary(process) for process in result.completed],
        failed=[process_summary(process) for process in result.failed],
        processes=processes,
        summary=summary,
        status=status,
    )
