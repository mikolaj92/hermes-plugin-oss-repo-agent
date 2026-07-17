"""issue_to_pr correlation path: Kanban task → worktree → OMP → PR."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fala.driver import run_correlation_path
from fala.models import CorrelationPathSpec
from fala.runtime_backend import Run, RuntimeBackendService

from repo_agent.config import AgentConfig, load_config
from repo_agent.flows.common import PathRunResult, effector, process_summary

ISSUE_TO_PR_PATH = CorrelationPathSpec(
    id="issue_to_pr",
    title="Kanban task → fix PR (load → parse → worktree → omp → push → PR)",
    effectors=[
        effector(
            "load_kanban_task",
            "repo_agent.steps.issue_to_pr.load_kanban_task",
        ),
        effector(
            "parse_issue_ref",
            "repo_agent.steps.issue_to_pr.parse_issue_ref_from_task",
            conduction=["load_kanban_task"],
        ),
        effector(
            "prepare_worktree",
            "repo_agent.steps.issue_to_pr.prepare_worktree",
            conduction=["parse_issue_ref"],
        ),
        effector(
            "run_omp",
            "repo_agent.steps.issue_to_pr.run_omp_worker",
            conduction=["prepare_worktree", "parse_issue_ref"],
        ),
        effector(
            "verify_branch",
            "repo_agent.steps.issue_to_pr.verify_branch_has_commits",
            conduction=["prepare_worktree", "run_omp"],
        ),
        effector(
            "push_branch",
            "repo_agent.steps.issue_to_pr.push_branch",
            conduction=["prepare_worktree", "verify_branch", "parse_issue_ref"],
        ),
        effector(
            "open_pull_request",
            "repo_agent.steps.issue_to_pr.open_pull_request",
            conduction=["parse_issue_ref", "prepare_worktree", "push_branch"],
        ),
        effector(
            "apply_pr_labels",
            "repo_agent.steps.issue_to_pr.apply_pr_labels",
            conduction=["open_pull_request", "parse_issue_ref"],
        ),
        effector(
            "write_dispatch_receipt",
            "repo_agent.steps.issue_to_pr.write_dispatch_receipt",
            conduction=[
                "parse_issue_ref",
                "prepare_worktree",
                "open_pull_request",
                "apply_pr_labels",
            ],
        ),
        effector(
            "complete_kanban_task",
            "repo_agent.steps.issue_to_pr.complete_kanban_task",
            conduction=["load_kanban_task", "parse_issue_ref", "write_dispatch_receipt"],
        ),
    ],
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
    """Run issue_to_pr on Fala 0.2.x via ``run_correlation_path``."""
    cfg = config or load_config()
    is_dry = True if dry_run is None else dry_run
    if dry_run is None and cfg.live:
        is_dry = False

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rid = run_id or f"issue-to-pr-{stamp}-{uuid.uuid4().hex[:8]}"

    # No explicit task means the path will poll Kanban; keep its no-op result.
    resolved_board = board or (cfg.repos[0].board if cfg.repos else "")
    resolved_clone = clone_path or (cfg.repos[0].clone_path if cfg.repos else "")
    wt_root = worktree_root or str(
        Path(cfg.raw.get("worktree_root") or "")
        if isinstance(cfg.raw, dict)
        else ""
    )
    if not wt_root:
        import os

        wt_root = os.environ.get(
            "HERMES_WORKTREE_ROOT",
            str(Path.home() / ".hermes" / "worktrees" / "repo-fixer"),
        )
    receipt = receipt_path or str(
        Path.home()
        / ".hermes"
        / "oss-repo-agent"
        / "receipts"
        / f"dispatch-{rid}.json"
    )

    raw = cfg.raw if isinstance(cfg.raw, dict) else {}
    automation = raw.get("automation") if isinstance(raw.get("automation"), dict) else {}
    step_config: dict[str, Any] = {
        "board": resolved_board,
        "clone_path": resolved_clone,
        "worktree_root": wt_root,
        "base_branch": cfg.base_branch,
        "branch_prefix": cfg.branch_prefix,
        "gh_cli": cfg.gh_cli,
        "assignee": cfg.assignee,
        "fixer_assignee": str(automation.get("fixer_assignee") or "repo-agent-fixer"),
        "model": "omniroute/omp/default",
        "dry_run": is_dry,
        "receipt_path": receipt,
    }

    dry_input = {"dry_run": is_dry}
    service = RuntimeBackendService.sqlite(db_path)
    result = await run_correlation_path(
        service,
        run=Run(
            id=rid,
            title=f"issue_to_pr dry_run={is_dry}",
            metadata={
                "correlation_path": ISSUE_TO_PR_PATH.id,
                "dry_run": is_dry,
                "plugin": "oss-repo-agent",
                "board": resolved_board,
                "task_id": task_id,
            },
        ),
        correlation_path=ISSUE_TO_PR_PATH,
        worker_id=worker_id,
        correlation_path_id=f"{rid}:{ISSUE_TO_PR_PATH.id}",
        effector_inputs={
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
            "verify_branch": {
                **dry_input,
                "clone_path": resolved_clone,
                "base_branch": cfg.base_branch,
            },
            "push_branch": dry_input,
            "open_pull_request": {
                **dry_input,
                "base_branch": cfg.base_branch,
            },
            "apply_pr_labels": dry_input,
            "write_dispatch_receipt": {
                **dry_input,
                "receipt_path": receipt,
            },
            "complete_kanban_task": {
                **dry_input,
                "board": resolved_board,
                "result": "dispatched via Fala issue_to_pr",
            },
        },
        effector_configs={eid: step_config for eid in path_effector_ids()},
        max_ticks=max_ticks,
        lease_seconds=600.0,
        actor=worker_id,
    )

    processes = list(result.processes or [])
    summaries = [process_summary(p) for p in processes]
    by_step = {s["step_id"]: s for s in summaries if s.get("step_id")}
    status = (
        result.status.value if hasattr(result.status, "value") else str(result.status)
    )
    summary = {
        "board": resolved_board,
        "task_id": task_id,
        "load_status": (by_step.get("load_kanban_task") or {}).get("output", {}).get(
            "status"
        ),
        "parse_status": (by_step.get("parse_issue_ref") or {}).get("output", {}).get(
            "status"
        ),
        "pr_status": (by_step.get("open_pull_request") or {}).get("output", {}).get(
            "status"
        ),
        "failed_steps": [s["step_id"] for s in summaries if s.get("status") == "failed"],
        "run_status": status,
        "repos": list(_repo_map(cfg)),
    }

    return PathRunResult(
        run_id=rid,
        path_id=ISSUE_TO_PR_PATH.id,
        dry_run=is_dry,
        ticks=result.outcome.ticks,
        stopped_reason=result.outcome.stopped_reason,
        completed=[process_summary(p) for p in result.outcome.completed],
        failed=[process_summary(p) for p in result.outcome.failed],
        processes=summaries,
        summary=summary,
        status=status,
    )


def path_effector_ids() -> list[str]:
    return [e.id for e in ISSUE_TO_PR_PATH.effectors]
