"""PR triage correlation paths + action router (merge | comment_block | repair)."""

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

# Decide path only — tick routes follow-up paths based on action.
PR_TRIAGE_PATH = CorrelationPathSpec(
    id="pr_triage",
    title="PR triage decide (list → load → checks → evidence → decide)",
    effectors=[
        effector(
            "list_ai_fix_prs",
            "repo_agent.steps.triage.list_ai_fix_prs",
        ),
        effector(
            "load_pr_fields",
            "repo_agent.steps.triage.load_pr_fields",
            conduction=["list_ai_fix_prs"],
        ),
        effector(
            "evaluate_checks",
            "repo_agent.steps.triage.evaluate_checks",
            conduction=["load_pr_fields"],
        ),
        effector(
            "evaluate_test_evidence",
            "repo_agent.steps.triage.evaluate_test_evidence",
            conduction=["load_pr_fields"],
        ),
        effector(
            "decide_triage_action",
            "repo_agent.steps.triage.decide_triage_action",
            conduction=[
                "load_pr_fields",
                "evaluate_checks",
                "evaluate_test_evidence",
            ],
        ),
    ],
)

PR_MERGE_PATH = CorrelationPathSpec(
    id="pr_merge",
    title="PR merge (claim → merge → receipt → close issue)",
    effectors=[
        effector(
            "claim_pr",
            "repo_agent.steps.triage.claim_pr_assignee",
        ),
        effector(
            "merge",
            "repo_agent.steps.triage.merge_pull_request",
            conduction=["claim_pr"],
        ),
        effector(
            "write_merge_receipt",
            "repo_agent.steps.triage.write_merge_receipt",
            conduction=["claim_pr", "merge"],
        ),
        effector(
            "close_linked_issue",
            "repo_agent.steps.triage.close_linked_issue",
            conduction=["merge", "claim_pr"],
        ),
    ],
)

PR_COMMENT_PATH = CorrelationPathSpec(
    id="pr_comment_block",
    title="PR comment block (single comment)",
    effectors=[
        effector(
            "comment_pr",
            "repo_agent.steps.triage.comment_pr_once",
        ),
    ],
)

PR_REPAIR_PATH = CorrelationPathSpec(
    id="pr_repair",
    title="PR repair (review task → worktree → prompt → omp → push)",
    effectors=[
        effector(
            "create_review_fix_task",
            "repo_agent.steps.repair.create_review_fix_task",
        ),
        effector(
            "build_repair_prompt",
            "repo_agent.steps.repair.build_repair_prompt",
            conduction=["create_review_fix_task"],
        ),
        effector(
            "prepare_worktree",
            "repo_agent.steps.issue_to_pr.prepare_worktree",
            conduction=["build_repair_prompt"],
        ),
        effector(
            "run_omp",
            "repo_agent.steps.issue_to_pr.run_omp_worker",
            conduction=["prepare_worktree", "build_repair_prompt"],
        ),
        effector(
            "push_branch",
            "repo_agent.steps.issue_to_pr.push_branch",
            conduction=["prepare_worktree", "run_omp"],
        ),
    ],
)


def _step_config(cfg: AgentConfig, *, is_dry: bool, **extra: Any) -> dict[str, Any]:
    raw = cfg.raw if isinstance(cfg.raw, dict) else {}
    automation = raw.get("automation") if isinstance(raw.get("automation"), dict) else {}
    return {
        "assignee": cfg.assignee,
        "gh_cli": cfg.gh_cli,
        "branch_prefix": cfg.branch_prefix,
        "base_branch": cfg.base_branch,
        "automerge": bool(automation.get("automerge", False)),
        "require_test_evidence": bool(automation.get("require_test_evidence", False)),
        "fixer_assignee": str(automation.get("fixer_assignee") or "repo-agent-fixer"),
        "merge_method": str(automation.get("merge_method") or "merge"),
        "dry_run": is_dry,
        **extra,
    }


async def run_pr_triage_decide(
    *,
    db_path: Path,
    config: AgentConfig | None = None,
    dry_run: bool | None = None,
    repo: str | None = None,
    pr_number: int | None = None,
    run_id: str | None = None,
    worker_id: str = "repo-agent:tick-triage",
    max_ticks: int = 20,
) -> PathRunResult:
    """Run pr_triage decide path only (no mutations beyond dry_run flags)."""
    cfg = config or load_config()
    is_dry = True if dry_run is None else dry_run
    if dry_run is None and cfg.live:
        is_dry = False

    resolved_repo = repo or (cfg.repos[0].repo if cfg.repos else "")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rid = run_id or f"pr-triage-{stamp}-{uuid.uuid4().hex[:8]}"
    step_config = _step_config(cfg, is_dry=is_dry, repo=resolved_repo)

    service = RuntimeBackendService.sqlite(db_path)
    result = await run_correlation_path(
        service,
        run=Run(
            id=rid,
            title=f"pr_triage dry_run={is_dry} repo={resolved_repo}",
            metadata={
                "correlation_path": PR_TRIAGE_PATH.id,
                "dry_run": is_dry,
                "repo": resolved_repo,
                "plugin": "oss-repo-agent",
            },
        ),
        correlation_path=PR_TRIAGE_PATH,
        worker_id=worker_id,
        correlation_path_id=f"{rid}:{PR_TRIAGE_PATH.id}",
        effector_inputs={
            "list_ai_fix_prs": {
                "repo": resolved_repo,
                "dry_run": is_dry,
                "limit": 30,
            },
            "load_pr_fields": {
                "repo": resolved_repo,
                "dry_run": is_dry,
                **({"number": pr_number} if pr_number else {}),
            },
            "evaluate_checks": {"dry_run": is_dry},
            "evaluate_test_evidence": {"dry_run": is_dry},
            "decide_triage_action": {
                "dry_run": is_dry,
                "automerge": step_config["automerge"],
            },
        },
        effector_configs={e.id: step_config for e in PR_TRIAGE_PATH.effectors},
        max_ticks=max_ticks,
        lease_seconds=300.0,
        actor=worker_id,
    )

    processes = list(result.processes or [])
    summaries = [process_summary(p) for p in processes]
    by_step = {s["step_id"]: s for s in summaries if s.get("step_id")}
    decide_out = (by_step.get("decide_triage_action") or {}).get("output") or {}
    load_out = (by_step.get("load_pr_fields") or {}).get("output") or {}
    status = (
        result.status.value if hasattr(result.status, "value") else str(result.status)
    )
    action = decide_out.get("action")
    pr_obj = load_out.get("pr") if isinstance(load_out.get("pr"), dict) else {}
    pr_number = load_out.get("number") or pr_obj.get("number")
    summary = {
        "repo": resolved_repo,
        "action": action,
        "reason": decide_out.get("reason"),
        "pr_number": pr_number,
        "pr": load_out.get("pr"),
        "failed_steps": [s["step_id"] for s in summaries if s.get("status") == "failed"],
        "run_status": status,
    }

    return PathRunResult(
        run_id=rid,
        path_id=PR_TRIAGE_PATH.id,
        dry_run=is_dry,
        ticks=result.outcome.ticks,
        stopped_reason=result.outcome.stopped_reason,
        completed=[process_summary(p) for p in result.outcome.completed],
        failed=[process_summary(p) for p in result.outcome.failed],
        processes=summaries,
        summary=summary,
        status=status,
        action=str(action) if action else None,
    )


async def run_follow_up_path(
    *,
    action: str,
    db_path: Path,
    config: AgentConfig | None = None,
    dry_run: bool = True,
    repo: str,
    pr: dict[str, Any] | None = None,
    number: int | None = None,
    board: str | None = None,
    clone_path: str | None = None,
    worktree_root: str | None = None,
    receipt_path: str | None = None,
    reason: str | None = None,
    worker_id: str = "repo-agent:tick-triage-followup",
    max_ticks: int = 30,
) -> PathRunResult | None:
    """Run merge / comment_block / repair path after decide. skip → None."""
    if action in (None, "skip", ""):
        return None

    cfg = config or load_config()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pr = pr or {}
    number = int(number or pr.get("number") or 0)
    board = board or (cfg.repos[0].board if cfg.repos else "")
    clone_path = clone_path or (cfg.repos[0].clone_path if cfg.repos else "")
    import os

    wt_root = worktree_root or os.environ.get(
        "HERMES_WORKTREE_ROOT",
        str(Path.home() / ".hermes" / "worktrees" / "repo-fixer"),
    )
    receipt = receipt_path or str(
        Path.home()
        / ".hermes"
        / "oss-repo-agent"
        / "receipts"
        / f"merge-{repo.replace('/', '_')}-{number}-{stamp}.json"
    )
    step_config = _step_config(
        cfg,
        is_dry=dry_run,
        repo=repo,
        board=board,
        clone_path=clone_path,
        worktree_root=wt_root,
        receipt_path=receipt,
    )
    branch = str(pr.get("headRefName") or "")
    service = RuntimeBackendService.sqlite(db_path)

    if action == "merge":
        path = PR_MERGE_PATH
        rid = f"pr-merge-{stamp}-{uuid.uuid4().hex[:8]}"
        effector_inputs = {
            "claim_pr": {
                "dry_run": dry_run,
                "repo": repo,
                "number": number,
                "pr": pr,
            },
            "merge": {
                "dry_run": dry_run,
                "repo": repo,
                "number": number,
                "pr": pr,
                "head_oid": pr.get("headRefOid"),
            },
            "write_merge_receipt": {
                "dry_run": dry_run,
                "receipt_path": receipt,
            },
            "close_linked_issue": {
                "dry_run": dry_run,
                "repo": repo,
                "pr": pr,
            },
        }
    elif action == "comment_block":
        path = PR_COMMENT_PATH
        rid = f"pr-comment-{stamp}-{uuid.uuid4().hex[:8]}"
        effector_inputs = {
            "comment_pr": {
                "dry_run": dry_run,
                "repo": repo,
                "number": number,
                "pr": pr,
                "body": (
                    f"repo-agent triage: blocked ({reason or 'policy'}). "
                    f"Please address and re-request review."
                ),
            },
        }
    elif action == "repair":
        path = PR_REPAIR_PATH
        rid = f"pr-repair-{stamp}-{uuid.uuid4().hex[:8]}"
        effector_inputs = {
            "create_review_fix_task": {
                "dry_run": dry_run,
                "repo": repo,
                "number": number,
                "board": board,
                "pr": pr,
                "reason": reason or "checks_not_green",
            },
            "build_repair_prompt": {
                "dry_run": dry_run,
                "pr": pr,
                "reason": reason or "checks_not_green",
            },
            "prepare_worktree": {
                "dry_run": dry_run,
                "clone_path": clone_path,
                "worktree_root": wt_root,
                "branch": branch,
                "base_branch": cfg.base_branch,
            },
            "run_omp": {"dry_run": dry_run},
            "push_branch": {
                "dry_run": dry_run,
                "branch": branch,
            },
        }
    else:
        return None

    result = await run_correlation_path(
        service,
        run=Run(
            id=rid,
            title=f"{path.id} dry_run={dry_run} {repo}#{number}",
            metadata={
                "correlation_path": path.id,
                "dry_run": dry_run,
                "repo": repo,
                "number": number,
                "action": action,
                "plugin": "oss-repo-agent",
            },
        ),
        correlation_path=path,
        worker_id=worker_id,
        correlation_path_id=f"{rid}:{path.id}",
        effector_inputs=effector_inputs,
        effector_configs={e.id: step_config for e in path.effectors},
        max_ticks=max_ticks,
        lease_seconds=600.0,
        actor=worker_id,
    )

    processes = list(result.processes or [])
    summaries = [process_summary(p) for p in processes]
    status = (
        result.status.value if hasattr(result.status, "value") else str(result.status)
    )
    return PathRunResult(
        run_id=rid,
        path_id=path.id,
        dry_run=dry_run,
        ticks=result.outcome.ticks,
        stopped_reason=result.outcome.stopped_reason,
        completed=[process_summary(p) for p in result.outcome.completed],
        failed=[process_summary(p) for p in result.outcome.failed],
        processes=summaries,
        summary={
            "action": action,
            "repo": repo,
            "number": number,
            "run_status": status,
            "failed_steps": [
                s["step_id"] for s in summaries if s.get("status") == "failed"
            ],
        },
        status=status,
        action=action,
    )


async def run_triage_with_router(
    *,
    db_path: Path,
    config: AgentConfig | None = None,
    dry_run: bool | None = None,
    repo: str | None = None,
    pr_number: int | None = None,
    execute_follow_up: bool = True,
    worker_id: str = "repo-agent:tick-triage",
) -> PathRunResult:
    """Decide then optionally run merge/comment/repair follow-up path."""
    decide = await run_pr_triage_decide(
        db_path=db_path,
        config=config,
        dry_run=dry_run,
        repo=repo,
        pr_number=pr_number,
        worker_id=worker_id,
    )
    action = decide.action or "skip"
    if not execute_follow_up or action == "skip":
        return decide

    cfg = config or load_config()
    resolved_repo = (
        repo
        or decide.summary.get("repo")
        or (cfg.repos[0].repo if cfg.repos else "")
    )
    # Match clone/board for this repo
    board = cfg.repos[0].board if cfg.repos else ""
    clone_path = cfg.repos[0].clone_path if cfg.repos else ""
    for r in cfg.repos:
        if r.repo == resolved_repo:
            board = r.board
            clone_path = r.clone_path
            break

    follow = await run_follow_up_path(
        action=action,
        db_path=db_path,
        config=cfg,
        dry_run=decide.dry_run,
        repo=str(resolved_repo),
        pr=decide.summary.get("pr") if isinstance(decide.summary.get("pr"), dict) else {},
        number=int(decide.summary.get("pr_number") or pr_number or 0) or None,
        board=board,
        clone_path=clone_path,
        reason=str(decide.summary.get("reason") or ""),
        worker_id=f"{worker_id}:followup",
    )
    if follow is None:
        return decide

    decide.follow_up = follow.to_dict()
    decide.summary["follow_up_path"] = follow.path_id
    decide.summary["follow_up_status"] = follow.status
    decide.summary["follow_up_failed"] = follow.summary.get("failed_steps")
    if follow.failed:
        decide.failed = list(decide.failed) + list(follow.failed)
    return decide
