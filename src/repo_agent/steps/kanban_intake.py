from __future__ import annotations

import re
from typing import Any

from repo_agent.envelope import Request, Result

from repo_agent.adapters_cli import CommandError, hermes_kanban_json, run_cmd
from repo_agent.envelope import (
    cfg_of,
    conduction_of,
    dry_run_flag,
    fail,
    input_of,
    noop,
    ok,
    planned,
)


def _task_marker(task: object) -> str:
    if not isinstance(task, dict):
        return ""
    body = str(task.get("body") or task.get("description") or "")
    match = re.search(r"(?m)^Idempotency-Key:\s*([^\s]+)\s*$", body)
    return match.group(1) if match else ""


_COMPLETED_TASK_STATUSES = {"done", "completed", "archived"}


def _task_matches(task: dict[str, Any], marker: str) -> bool:
    """Match only the authoritative, exact idempotency marker in the body."""
    return _task_marker(task) == marker


def _task_id(task: dict[str, Any]) -> object:
    return task.get("id") or task.get("task_id")


def ensure_kanban_intake(request: Request) -> Result:
    """Atomic: ensure one Kanban [issue] task for claimed issue."""
    data = input_of(request)
    cond = conduction_of(request)
    claim = dict(data.get("claim") or cond.get("claim") or cond.get("claim_github_issue") or cond.get("intake_claim") or {})
    poll = dict(data.get("poll") or cond.get("poll") or cond.get("intake_poll") or {})
    decide = dict(data.get("decide_issue_action") or data.get("decide") or cond.get("decide_issue_action") or cond.get("decide") or cond.get("intake_decide_issue_action") or {})
    decide_action = str(data.get("action") or decide.get("action") or "")
    selected = data.get("selected") if "selected" in data else (claim.get("selected") or poll.get("selected"))
    dry_run = dry_run_flag(request, default=bool(claim.get("dry_run", True)))
    cfg = cfg_of(request)
    assignee = str(cfg.get("kanban_intake_assignee") or "repo-agent-intake")
    if decide_action in {"reject_comment", "skip"}:
        return noop(
            str(decide.get("reason") or f"issue_{decide_action}"),
            dry_run=dry_run,
            decide_action=decide_action,
        )
    if not selected or claim.get("status") == "noop":
        return noop(claim.get("reason") or "no_selected_issue", dry_run=dry_run)
    if claim.get("status") == "failed":
        return fail(
            "claim_failed",
            failure_class=str(claim.get("failure_class") or "terminal"),
            retry_safe=bool(claim.get("retry_safe", False)),
            claim=claim,
            dry_run=dry_run,
        )
    repo = str(selected["repo"])
    number = int(selected.get("number") or selected.get("issue") or 0)
    board = str(selected.get("board") or "")
    title = str(selected.get("title") or "")
    url = str(selected.get("url") or "")
    labels = selected.get("labels") or []
    key = f"github-issue:{repo}:{number}"
    task_title = f"[issue] {repo}#{number}: {title}"
    body = (
        f"GitHub issue: {url}\nRepository: {repo}\nIssue: #{number}\n"
        f"Labels at intake: {', '.join(labels) if labels else 'none'}\n\n"
        f"Intake via Fala effector ensure_kanban_intake.\nIdempotency-Key: {key}\n"
    )
    planned_spec = {"board": board, "title": task_title, "assignee": assignee, "idempotency_key": key}
    if dry_run:
        return planned(selected=selected, planned=planned_spec)
    if not board:
        return fail("missing_board", failure_class="terminal", retry_safe=False, selected=selected, idempotency_key=key)
    try:
        tasks = hermes_kanban_json(["--board", board, "list", "--json", "--sort", "created-desc"])
    except CommandError as exc:
        return fail("kanban_list_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), selected=selected, idempotency_key=key)
    if not isinstance(tasks, list) or any(not isinstance(task, dict) for task in tasks):
        return fail("invalid_kanban_readback", failure_class="terminal", retry_safe=False, selected=selected, idempotency_key=key)
    matches = [task for task in tasks if _task_matches(task, key)]
    if len(matches) > 1:
        return fail("ambiguous_kanban_task", failure_class="terminal", retry_safe=False, selected=selected, idempotency_key=key, task_ids=[_task_id(task) for task in matches])
    if matches:
        task = matches[0]
        task_id = _task_id(task)
        if task_id is None or not str(task_id).strip():
            return fail("invalid_kanban_task_id", failure_class="terminal", retry_safe=False, selected=selected, idempotency_key=key)
        status = str(task.get("status") or task.get("state") or "").strip().lower()
        return ok(status="already_completed" if status in _COMPLETED_TASK_STATUSES else "exists", selected=selected, task_id=task_id, task_title=task.get("title"), idempotency_key=key, mutated=False)
    try:
        proc = run_cmd(
            [
                "hermes", "kanban", "--board", board, "create",
                "--title", task_title, "--body", body, "--assignee", assignee,
                "--idempotency-key", key,
            ],
            timeout=90,
        )
    except CommandError as exc:
        # A create timeout may have committed remotely; the next runner must
        # reconcile by listing the stable idempotency key before retrying.
        try:
            recovery = hermes_kanban_json(["--board", board, "list", "--json", "--sort", "created-desc"])
        except CommandError:
            recovery = None
        if isinstance(recovery, list) and all(isinstance(task, dict) for task in recovery):
            recovered = [task for task in recovery if _task_matches(task, key)]
            if len(recovered) == 1:
                task = recovered[0]
                task_id = _task_id(task)
                if task_id is None or not str(task_id).strip():
                    return fail("invalid_kanban_task_id", failure_class="terminal", retry_safe=False, selected=selected, idempotency_key=key, mutated=True)
                return ok(status="reconciled", selected=selected, task_id=task_id, task_title=task.get("title"), idempotency_key=key, mutated=True, reconciled=True)
        return fail(
            "kanban_create_failed",
            failure_class="reconcile_then_retry",
            retry_safe=False,
            error=str(exc),
            stderr=exc.stderr[-500:],
            planned=planned_spec,
            idempotency_key=key,
            mutated=True,
        )
    try:
        after = hermes_kanban_json(["--board", board, "list", "--json", "--sort", "created-desc"])
    except CommandError as exc:
        return fail(
            "kanban_create_readback_failed",
            failure_class="reconcile_then_retry",
            retry_safe=False,
            error=str(exc),
            planned=planned_spec,
            idempotency_key=key,
            mutated=True,
        )
    if not isinstance(after, list) or any(not isinstance(task, dict) for task in after):
        return fail("invalid_kanban_create_readback", failure_class="terminal", retry_safe=False, planned=planned_spec, idempotency_key=key, mutated=True)
    matches = [task for task in after if _task_matches(task, key)]
    if len(matches) != 1:
        return fail(
            "kanban_create_readback_ambiguous",
            failure_class="reconcile_then_retry",
            retry_safe=False,
            planned=planned_spec,
            idempotency_key=key,
            match_count=len(matches),
            mutated=True,
        )
    task = matches[0]
    task_id = _task_id(task)
    if task_id is None or not str(task_id).strip():
        return fail("invalid_kanban_task_id", failure_class="terminal", retry_safe=False, planned=planned_spec, idempotency_key=key, mutated=True)
    return ok(
        status="created",
        selected=selected,
        planned=planned_spec,
        idempotency_key=key,
        task_id=task_id,
        task_title=task.get("title"),
        stdout=proc.stdout[-500:],
        mutated=True,
    )
