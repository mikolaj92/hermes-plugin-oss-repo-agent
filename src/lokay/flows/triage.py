"""PR triage package path: decide action only; repair lives on pr_repair."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lokay.config import AgentConfig, ConfigError, load_config
from lokay.flows.common import PathRunResult, process_summary, process_values
from lokay.flows.runtime import run_package_path_async

PATH_ID = "pr_triage"
PACKAGE_PATH = Path(__file__).resolve().parents[3] / "fala-package.toml"
_PR_TRIAGE_EFFECTORS = (
    "read_open_prs",
    "filter_fix_prs",
    "select_fix_pr",
    "load_pr_fields",
    "evaluate_checks",
    "evaluate_test_evidence",
    "decide_triage_action",
)
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
    entry = min(candidates, key=lambda item: (int(item.priority), str(item.repo)))
    return {
        "repo": entry.repo,
        "board": entry.board,
        "clone_path": entry.clone_path,
        "priority": entry.priority,
        "policy": _policy(cfg),
        "repos": [
            {"repo": item.repo, "board": item.board, "clone_path": item.clone_path, "priority": item.priority}
            for item in sorted(candidates, key=lambda item: (int(item.priority), str(item.repo)))
        ],
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
        "task_receipts": cfg.paths.task_receipts,
        "active_issue": cfg.paths.active_issue,
        "dry_run": is_dry,
        "live": not is_dry,
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
    worker_id: str = "lokay:tick-triage",
    max_ticks: int = 40,
) -> PathRunResult:
    """Run the pr_triage package path once (decide only)."""
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
    step_config = _step_config(
        cfg,
        is_dry=is_dry,
        **context,
    )
    candidate = str(cfg.raw.get("candidate") or cfg.raw.get("candidate_id") or "")
    candidate_sha = str(cfg.raw.get("candidate_sha") or candidate)
    head_sha = str(cfg.raw.get("head_sha") or cfg.raw.get("verified_head") or cfg.raw.get("head_oid") or "")
    check_run_id = str(cfg.raw.get("check_run_id") or "")
    configured_repos = [
        {"repo": entry.repo, "board": entry.board, "clone_path": entry.clone_path, "priority": entry.priority}
        for entry in cfg.repos
    ]
    dry_input = {
        "dry_run": is_dry,
        "live": not is_dry,
        "run_id": rid,
        "path_id": PATH_ID,
        "repos": configured_repos,
        "executor_enabled": cfg.executor.enabled,
        "executor_command": cfg.executor.command,
        "executor_model": cfg.executor.model,
        "model": cfg.executor.model,
        "thinking": cfg.executor.thinking,
        "timeout_seconds": cfg.executor.timeout_seconds,
        **({"candidate": candidate} if candidate else {}),
        **({"candidate_id": candidate} if candidate else {}),
        **({"candidate_sha": candidate_sha} if candidate_sha else {}),
        **({"head_sha": head_sha, "verified_head": head_sha} if head_sha else {}),
        **({"check_run_id": check_run_id} if check_run_id else {}),
    }
    if repo is not None:
        dry_input.update(context)
    list_input: dict[str, Any] = {**dry_input, "limit": limit}
    load_input: dict[str, Any] = dict(dry_input)
    effector_inputs: dict[str, dict[str, Any]] = {
        "read_open_prs": list_input,
        "filter_fix_prs": dict(dry_input),
        "select_fix_pr": dict(dry_input),
        "load_pr_fields": load_input,
        "evaluate_checks": {**dry_input, "require_checks": step_config["require_checks"]},
        "evaluate_test_evidence": {**dry_input, "require_test_evidence": step_config["require_test_evidence"]},
        "decide_triage_action": {
            **dry_input,
            "automerge": step_config["automerge"],
            "branch_prefix": step_config["branch_prefix"],
            "base_branch": step_config["base_branch"],
            "require_human_approval": step_config["require_human_approval"],
        },
    }
    effector_configs = {step_id: step_config for step_id in _PR_TRIAGE_EFFECTORS}

    host = await run_package_path_async(
        db_path=db_path,
        package_path=PACKAGE_PATH,
        path_id=PATH_ID,
        run_id=rid,
        inputs=step_config,
        effector_inputs=effector_inputs,
        effector_configs=effector_configs,
        max_ticks=max_ticks,
        worker_id=worker_id,
    )

    summaries = [process_summary(process) for process in host.processes]
    by_step = _by_step(summaries)
    decide_out = _output_of(by_step, "decide_triage_action")
    list_out = _output_of(by_step, "read_open_prs")
    load_out = _output_of(by_step, "load_pr_fields")
    action = decide_out.get("action")
    pr_obj = load_out.get("pr") if isinstance(load_out.get("pr"), dict) else {}
    resolved_number = load_out.get("number") or pr_obj.get("number") or pr_number
    status, stopped_reason = _normalize_status(run_status=host.run_status, summaries=summaries, decide_out=decide_out, list_out=list_out)
    failed_steps = _failed_steps(summaries)
    worked = bool(
        action
        and action != "skip"
        and status not in {"idle", *_TERMINAL_FAILURES}
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
    worker_id: str = "lokay:tick-triage",
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
