"""PR triage package path: decide + merge/comment/repair branches in one host run."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from repo_agent.config import AgentConfig, ConfigError, load_config
from repo_agent.flows.common import PathRunResult, process_summary, process_values
from repo_agent.flows.runtime import run_package_path_async

PATH_ID = "pr_triage"
PACKAGE_PATH = Path(__file__).resolve().parents[3] / "fala-package.toml"
_TERMINAL_FAILURES = frozenset({"failed", "cancelled", "timed_out"})
_IDLE_REASONS = frozenset(
    {
        "no_open_prs",
        "no_repositories",
        "no_selected_pr",
        "not_selected",
        "skip",
    }
)


def _policy(cfg: AgentConfig) -> dict[str, Any]:
    return {
        "automerge": cfg.automation.automerge,
        "require_human_approval": cfg.automation.require_human_approval,
        "require_checks": cfg.automation.require_checks,
        "require_test_evidence": cfg.automation.require_test_evidence,
        "merge_method": cfg.automation.merge_method,
    }


def _resolve_repo_context(cfg: AgentConfig, repo: str | None) -> tuple[dict[str, Any] | None, str | None]:
    candidates = [entry for entry in cfg.repos if not repo or entry.repo == repo]
    if not candidates:
        return None, "repository_context_not_found"
    if len(candidates) != 1:
        return None, "ambiguous_repository_context"
    entry = candidates[0]
    return {
        "repo": entry.repo,
        "board": entry.board,
        "clone_path": entry.clone_path,
        "priority": entry.priority,
        "policy": _policy(cfg),
    }, None


def _step_config(cfg: AgentConfig, *, is_dry: bool, **extra: Any) -> dict[str, Any]:
    return {
        "assignee": cfg.assignee,
        "gh_cli": cfg.gh_cli,
        "branch_prefix": cfg.branch_prefix,
        "base_branch": cfg.base_branch,
        "automerge": cfg.automation.automerge,
        "require_human_approval": cfg.automation.require_human_approval,
        "require_checks": cfg.automation.require_checks,
        "require_test_evidence": cfg.automation.require_test_evidence,
        "fixer_assignee": cfg.automation.fixer_assignee,
        "merge_method": cfg.automation.merge_method,
        "executor_enabled": cfg.executor.enabled,
        "executor_command": cfg.executor.command,
        "executor_model": cfg.executor.model,
        "model": cfg.executor.model,
        "thinking": cfg.executor.thinking,
        "timeout_seconds": cfg.executor.timeout_seconds,
        "worktree_root": cfg.paths.worktree_root,
        "dispatch_receipts": cfg.paths.dispatch_receipts,
        "merge_receipts": cfg.paths.merge_receipts,
        "active_issue": cfg.paths.active_issue,
        "dry_run": is_dry,
        **extra,
    }


def _resolve_dry_run(cfg: AgentConfig, dry_run: bool | None) -> bool:
    if dry_run is False and not cfg.live:
        raise ConfigError("live execution requires config mode='live'")
    if dry_run is None:
        return not cfg.live
    return bool(dry_run)


def _by_step(summaries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(item["step_id"]): item
        for item in summaries
        if item.get("step_id")
    }


def _output_of(by_step: dict[str, dict[str, Any]], *step_ids: str) -> dict[str, Any]:
    for step_id in step_ids:
        values = process_values(by_step.get(step_id) or {})
        if values:
            return values
    return {}


def _failed_steps(summaries: list[dict[str, Any]]) -> list[str]:
    steps: list[str] = []
    for item in summaries:
        if str(item.get("status") or "") not in _TERMINAL_FAILURES:
            continue
        step_id = item.get("step_id")
        if step_id and str(step_id) not in steps:
            steps.append(str(step_id))
    return steps


def _normalize_status(
    *,
    run_status: str,
    summaries: list[dict[str, Any]],
    decide_out: dict[str, Any],
    list_out: dict[str, Any],
) -> tuple[str, str]:
    status = str(run_status or "")
    failed = _failed_steps(summaries)
    if failed:
        if status in _TERMINAL_FAILURES:
            return status, status
        return "failed", "failed"
    list_status = str(list_out.get("status") or "")
    decide_status = str(decide_out.get("status") or "")
    action = str(decide_out.get("action") or "")
    reason = str(decide_out.get("reason") or list_out.get("reason") or "")
    idle = list_status == "noop" or decide_status == "noop" or action in {"", "skip"} or reason in _IDLE_REASONS
    if idle and status in {"", "completed", "succeeded"}:
        return "idle", reason or "idle"
    return status or "completed", status or "completed"


async def run_pr_triage_decide(
    *,
    db_path: Path,
    config: AgentConfig | None = None,
    dry_run: bool | None = None,
    repo: str | None = None,
    pr_number: int | None = None,
    run_id: str | None = None,
    limit: int = 30,
    worker_id: str = "repo-agent:tick-triage",
    max_ticks: int = 40,
) -> PathRunResult:
    """Run the pr_triage package path once (decide + gated branch effectors)."""
    cfg = config or load_config()
    is_dry = _resolve_dry_run(cfg, dry_run)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rid = run_id or f"pr-triage-{stamp}-{uuid.uuid4().hex[:8]}"
    context, context_error = _resolve_repo_context(cfg, repo)
    if context is None:
        reason = "no_repositories" if not cfg.repos else (context_error or "repository_context_not_found")
        return PathRunResult(
            run_id=rid, path_id=PATH_ID, dry_run=is_dry, ticks=0,
            stopped_reason=reason,
            summary={"run_status": "failed" if cfg.repos else "idle", "repo": repo or "", "pr_number": pr_number, "action": "skip", "reason": reason, "failed_steps": [], "worked": False},
            status="failed" if cfg.repos else "idle", action="skip",
        )
    resolved_repo = str(context["repo"])
    board = str(context["board"])
    clone_path = str(context["clone_path"])
    receipt = str(Path(cfg.paths.merge_receipts) / f"merge-{resolved_repo.replace('/', '_')}-{pr_number or 'auto'}-{rid}.json")
    step_config = _step_config(cfg, is_dry=is_dry, **context, worktree_root=cfg.paths.worktree_root, receipt_path=receipt)


    candidate = str(cfg.raw.get("candidate") or "")
    dry_input = {"dry_run": is_dry, "run_id": rid, "path_id": PATH_ID, **({"candidate": candidate} if candidate else {}), **context}
    list_input: dict[str, Any] = {
        **dry_input,
        "limit": limit,
    }
    load_input: dict[str, Any] = dict(dry_input)
    effector_inputs: dict[str, dict[str, Any]] = {
        "list_ai_fix_prs": list_input,
        "load_pr_fields": load_input,
        "evaluate_checks": {
            "dry_run": is_dry,
            "require_checks": step_config["require_checks"],
        },
        "evaluate_test_evidence": {
            "dry_run": is_dry,
            "require_test_evidence": step_config["require_test_evidence"],
        },
        "decide_triage_action": {
            "dry_run": is_dry,
            "automerge": step_config["automerge"],
            "branch_prefix": step_config["branch_prefix"],
            "base_branch": step_config["base_branch"],
            "require_human_approval": step_config["require_human_approval"],
        },
        "claim_pr": {**dry_input, "repo": resolved_repo, **({"number": pr_number} if pr_number else {})},
        "merge": {**dry_input, "repo": resolved_repo, **({"number": pr_number} if pr_number else {})},
        "write_merge_receipt": {**dry_input, "receipt_path": receipt},
        "close_linked_issue": {**dry_input, "repo": resolved_repo},
        "comment_pr": {**dry_input, "repo": resolved_repo, **({"number": pr_number} if pr_number else {})},
        "create_review_fix_task": {
            **dry_input,
            "repo": resolved_repo,
            "board": board,
            **({"number": pr_number} if pr_number else {}),
        },
        "build_repair_prompt": dry_input,
        "repair_prepare_worktree": {
            **dry_input,
            "clone_path": clone_path,
            "worktree_root": step_config["worktree_root"],
            "base_branch": step_config["base_branch"],
        },
        "repair_run_omp": dry_input,
        "repair_push_branch": dry_input,
    }

    host = await run_package_path_async(
        db_path=db_path,
        package_path=PACKAGE_PATH,
        path_id=PATH_ID,
        run_id=rid,
        inputs=step_config,
        effector_inputs=effector_inputs,
        effector_configs={
            step_id: step_config
            for step_id in (
                "list_ai_fix_prs",
                "load_pr_fields",
                "evaluate_checks",
                "evaluate_test_evidence",
                "decide_triage_action",
                "claim_pr",
                "merge",
                "write_merge_receipt",
                "close_linked_issue",
                "comment_pr",
                "create_review_fix_task",
                "build_repair_prompt",
                "repair_prepare_worktree",
                "repair_run_omp",
                "repair_push_branch",
            )
        },
        max_ticks=max_ticks,
        worker_id=worker_id,
    )

    summaries = [process_summary(process) for process in host.processes]
    by_step = _by_step(summaries)
    decide_out = _output_of(by_step, "decide_triage_action")
    list_out = _output_of(by_step, "list_ai_fix_prs")
    load_out = _output_of(by_step, "load_pr_fields")
    action = decide_out.get("action")
    pr_obj = load_out.get("pr") if isinstance(load_out.get("pr"), dict) else {}
    resolved_number = load_out.get("number") or pr_obj.get("number") or pr_number
    status, stopped_reason = _normalize_status(
        run_status=host.run_status,
        summaries=summaries,
        decide_out=decide_out,
        list_out=list_out,
    )
    failed_steps = _failed_steps(summaries)
    worked = bool(
        action
        and action != "skip"
        and status not in {"idle", *_TERMINAL_FAILURES}
        and any(
            bool(process_values(item).get("mutated"))
            or str(item.get("status") or "") == "succeeded"
            and str(process_values(item).get("status") or "") not in {"noop", "planned", ""}
            for item in summaries
            if item.get("step_id")
            in {
                "claim_pr",
                "merge",
                "write_merge_receipt",
                "close_linked_issue",
                "comment_pr",
                "create_review_fix_task",
                "repair_prepare_worktree",
                "repair_run_omp",
                "repair_push_branch",
            }
        )
    )
    summary = {
        "repo": resolved_repo,
        "action": action,
        "reason": decide_out.get("reason") or list_out.get("reason"),
        "pr_number": resolved_number,
        "pr": load_out.get("pr"),
        "failed_steps": failed_steps,
        "run_status": status,
        "worked": worked,
        "replayed": host.replayed,
    }
    return PathRunResult(
        run_id=host.run_id,
        path_id=PATH_ID,
        dry_run=is_dry,
        ticks=host.ticks,
        stopped_reason=stopped_reason,
        completed=[process_summary(process) for process in host.completed],
        failed=[process_summary(process) for process in host.failed],
        processes=summaries,
        summary=summary,
        status=status,
        action=str(action) if action else None,
    )


async def run_triage_flow(
    *,
    db_path: Path,
    config: AgentConfig | None = None,
    dry_run: bool | None = None,
    repo: str | None = None,
    pr_number: int | None = None,
    limit: int = 30,
    run_id: str | None = None,
    worker_id: str = "repo-agent:tick-triage",
    max_ticks: int = 40,
) -> PathRunResult:
    """Public triage flow: one pr_triage package path invocation."""
    return await run_pr_triage_decide(
        db_path=db_path,
        config=config,
        dry_run=dry_run,
        repo=repo,
        pr_number=pr_number,
        limit=limit,
        run_id=run_id,
        worker_id=worker_id,
        max_ticks=max_ticks,
    )
