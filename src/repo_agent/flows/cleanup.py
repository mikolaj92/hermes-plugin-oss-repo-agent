"""Cleanup flow backed by the repository Fala package path."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from repo_agent.adapters_cli import CommandError
from repo_agent.adapters_git import branch_config_get

from repo_agent.config import AgentConfig, ConfigError, load_config
from repo_agent.flows.common import PathRunResult, process_summary, process_values
from repo_agent.flows.runtime import run_package_path_async
from repo_agent.steps.cleanup import write_cleanup_receipt


_PACKAGE_PATH = Path(__file__).resolve().parents[3] / "fala-package.toml"
_PROCESS_FAILURES = {"failed", "cancelled", "timed_out"}
_CLEANUP_EFFECTORS = (
    "parse_issue_from_branch",
    "check_issue_closed",
    "check_no_open_pr",
    "remove_worktree",
    "delete_local_fix_branch",
    "release_active_issue_claim",
    "write_cleanup_receipt",
)

def _resolve_repo_context(
    cfg: AgentConfig,
    *,
    repo: str | None,
    clone_path: str | None,
) -> tuple[Any | None, str | None]:
    candidates = list(cfg.repos)
    if repo:
        candidates = [entry for entry in candidates if entry.repo == repo]
    if clone_path:
        candidates = [entry for entry in candidates if entry.clone_path == clone_path]
    if not candidates:
        return None, "repository_context_not_found"
    if len(candidates) != 1:
        return None, "ambiguous_repository_context"
    return candidates[0], None
def _read_branch_provenance(clone_path: str, branch: str) -> tuple[dict[str, str] | None, str | None]:
    if not branch:
        return None, "missing_branch"
    values: dict[str, str] = {}
    for key in ("task", "issue", "receipt", "repo"):
        try:
            values[key] = branch_config_get(clone_path, branch, f"repo-agent-{key}").strip()
        except CommandError as exc:
            if exc.returncode == 1:
                values[key] = ""
            else:
                return None, "cleanup_provenance_read_failed"
    if not all(values.values()):
        return None, "cleanup_provenance_missing"
    return values, None



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
    task_id: str | None = None,
    issue: int | str | None = None,
    receipt_path: str | None = None,
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

    context, context_error = _resolve_repo_context(cfg, repo=repo, clone_path=clone_path)
    if context is None:
        reason = context_error or "repository_context_not_found"
        rid = run_id or f"cleanup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        return PathRunResult(
            run_id=rid,
            path_id="cleanup",
            dry_run=is_dry,
            ticks=0,
            stopped_reason=reason,
            summary={
                "run_status": "failed" if cfg.repos else "idle",
                "reason": reason,
                "repo": repo or "",
                "clone_path": clone_path or "",
                "failed_steps": [],
                "worked": False,
            },
            status="failed" if cfg.repos else "idle",
        )
    resolved_repo = context.repo
    resolved_board = context.board
    resolved_clone = context.clone_path
    resolved_branch = branch or ""
    resolved_claim = claim_path or cfg.paths.active_issue
    resolved_worktree = worktree_path or ""
    if not resolved_worktree and resolved_branch:
        safe = re.sub(r"[^a-zA-Z0-9._/-]+", "-", resolved_branch)
        resolved_worktree = str(Path(cfg.paths.worktree_root) / safe)

    ownership_receipt = ""
    resolved_task = str(task_id or "").strip()
    resolved_issue = str(issue if issue is not None else "").strip()
    if not is_dry:
        provenance, provenance_error = _read_branch_provenance(resolved_clone, resolved_branch)
        if provenance is None:
            return PathRunResult(
                run_id=run_id or f"cleanup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}",
                path_id="cleanup",
                dry_run=False,
                ticks=0,
                stopped_reason=provenance_error or "cleanup_provenance_missing",
                summary={"run_status": "failed", "reason": provenance_error or "cleanup_provenance_missing", "repo": resolved_repo, "branch": resolved_branch, "failed_steps": [], "worked": False},
                status="failed",
            )
        if provenance["repo"] != resolved_repo or (resolved_task and resolved_task != provenance["task"]) or (resolved_issue and resolved_issue != provenance["issue"]):
            return PathRunResult(
                run_id=run_id or f"cleanup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}",
                path_id="cleanup",
                dry_run=False,
                ticks=0,
                stopped_reason="cleanup_provenance_mismatch",
                summary={"run_status": "failed", "reason": "cleanup_provenance_mismatch", "repo": resolved_repo, "branch": resolved_branch, "failed_steps": [], "worked": False},
                status="failed",
            )
        resolved_task = provenance["task"]
        resolved_issue = provenance["issue"]
        ownership_receipt = provenance["receipt"]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rid = run_id or f"cleanup-{stamp}-{uuid.uuid4().hex[:8]}"
    resolved_receipt = receipt_path or str(Path(cfg.paths.dispatch_receipts) / f"cleanup-{rid}.json")
    step_config: dict[str, Any] = {
        "repo": resolved_repo,
        "board": resolved_board,
        "branch": resolved_branch,
        "clone_path": resolved_clone,
        "worktree_path": resolved_worktree,
        "active_issue_path": resolved_claim,
        "task_id": resolved_task,
        "issue": resolved_issue,
        "receipt_id": ownership_receipt,
        "receipt_path": resolved_receipt,
        "run_id": rid,
        "path_id": "cleanup",
        "gh_cli": cfg.gh_cli,
        "dry_run": is_dry,
        "require_safe": True,
        "blocked_label": cfg.labels.blocked,
        "executor_command": cfg.executor.command,
        "executor_model": cfg.executor.model,
        "executor_thinking": cfg.executor.thinking,
        "executor_timeout_seconds": cfg.executor.timeout_seconds,
    }
    common = {"dry_run": is_dry, "repo": resolved_repo, "board": resolved_board, "task_id": resolved_task, "issue": resolved_issue, "receipt_id": ownership_receipt, "receipt_path": resolved_receipt, "run_id": rid, "path_id": "cleanup"}
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
        "write_cleanup_receipt": {**common, "branch": resolved_branch, "receipt_path": resolved_receipt},
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
    outputs = [process_values(summary) for summary in summaries]
    worked = any(bool(output.get("mutated")) for output in outputs)
    receipt_output = process_values(by_step.get("write_cleanup_receipt") or {})
    if worked and receipt_output.get("status") not in {"written", "exists"}:
        conduction: dict[str, dict[str, Any]] = {}
        for step in _CLEANUP_EFFECTORS[:-1]:
            process = by_step.get(step) or {}
            values = process_values(process)
            conduction[step] = values or {
                "ok": False,
                "status": str(process.get("status") or "cancelled"),
                "mutated": False,
                "reason": str(process.get("error") or "upstream_cancelled"),
            }
        fallback = write_cleanup_receipt({
            "input": {
                **common,
                "branch": resolved_branch,
                "clone_path": resolved_clone,
                "worktree_path": resolved_worktree,
                "receipt_path": resolved_receipt,
                "process_id": f"{rid}:write_cleanup_receipt:fallback",
                "conduction": conduction,
            },
            "config": step_config,
            "run_id": rid,
            "path_id": "cleanup",
            "process_id": f"{rid}:write_cleanup_receipt:fallback",
        })
        summaries.append({"id": f"{rid}:write_cleanup_receipt:fallback", "step_id": "write_cleanup_receipt", "status": "completed" if fallback.get("ok") else "failed", "attempt": 1, "max_attempts": 1, "output": fallback, "error": None if fallback.get("ok") else fallback.get("reason")})
        by_step["write_cleanup_receipt"] = summaries[-1]
        outputs.append(fallback)
    parse_output = process_values(by_step.get("parse_issue_from_branch") or {})
    run_status = str(result.run_status)
    terminal_failures = [summary for summary in summaries if summary.get("status") in _PROCESS_FAILURES]
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
