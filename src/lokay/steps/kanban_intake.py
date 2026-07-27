from __future__ import annotations

import re
from typing import Any

from lokay.envelope import Request, Result

from lokay.adapters_cli import CommandError, hermes_kanban_json, run_cmd
from lokay.envelope import (
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


def read_intake_tasks(request: Request) -> Result:
    """Read all Kanban tasks for one intake board."""
    from lokay.envelope import cond_blob, terminal_upstream
    terminal = terminal_upstream(request, "read_intake_tasks", "build_issue_claim_result")
    if terminal:
        return terminal
    data = input_of(request)
    cfg = cfg_of(request)
    claim = cond_blob(request, "build_issue_claim_result")
    if claim.get("status") == "noop":
        return noop(str(claim.get("reason") or "no_selected_issue"), dry_run=dry_run_flag(request), selected=claim.get("selected"))
    selected = data.get("selected") or claim.get("selected") or {}
    board = str(data.get("board") or (selected.get("board") if isinstance(selected, dict) else ""))
    if not board:
        return fail("missing_board", failure_class="terminal", retry_safe=False, board=board)
    try:
        tasks = hermes_kanban_json(["--board", board, "list", "--json", "--sort", "created-desc"])
    except CommandError as exc:
        return fail("kanban_list_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), board=board)
    if not isinstance(tasks, list) or any(not isinstance(task, dict) for task in tasks):
        return fail("invalid_kanban_readback", failure_class="terminal", retry_safe=False, board=board)
    return ok(status="intake_tasks_read", tasks=tasks, board=board, selected=selected, dry_run=dry_run_flag(request))


def find_intake_marker(request: Request) -> Result:
    """Purely find the exact idempotency marker in task rows."""
    from lokay.envelope import cond_blob, terminal_upstream
    terminal = terminal_upstream(request, "find_intake_marker", "read_intake_tasks")
    if terminal:
        return terminal
    data = input_of(request)
    read = cond_blob(request, "read_intake_tasks")
    if read.get("status") == "noop":
        return noop(str(read.get("reason") or "no_selected_issue"), dry_run=dry_run_flag(request), selected=read.get("selected"))
    tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else read.get("tasks", [])
    selected = data.get("selected") or read.get("selected") or {}
    repo = str((selected or {}).get("repo") or "")
    number = (selected or {}).get("number") or 0
    marker = str(data.get("idempotency_key") or f"github-issue:{repo}:{number}")
    if not isinstance(tasks, list) or any(not isinstance(x, dict) for x in tasks):
        return fail("invalid_kanban_tasks", failure_class="terminal", retry_safe=False, mutated=False)
    matches = [task for task in tasks if _task_matches(task, marker)]
    if len(matches) > 1:
        return fail("ambiguous_kanban_task", failure_class="terminal", retry_safe=False, task_ids=[_task_id(x) for x in matches], mutated=False)
    if matches:
        task = matches[0]
        task_id = _task_id(task)
        if task_id is None or not str(task_id).strip():
            return fail("invalid_kanban_task_id", failure_class="terminal", retry_safe=False, mutated=False)
        status = str(task.get("status") or task.get("state") or "").lower()
        return ok(status="intake_marker_found", found=True, task=task, task_id=task_id, already_completed=status in _COMPLETED_TASK_STATUSES, marker=marker, dry_run=dry_run_flag(request))
    return ok(status="intake_marker_absent", found=False, marker=marker, selected=selected, board=read.get("board"), dry_run=dry_run_flag(request))


def create_intake_task(request: Request) -> Result:
    """Create one Kanban intake task; reconciliation is separate."""
    from lokay.envelope import cond_blob, terminal_upstream
    terminal = terminal_upstream(request, "create_intake_task", "find_intake_marker")
    if terminal:
        return terminal
    data = input_of(request)
    cfg = cfg_of(request)
    found = cond_blob(request, "find_intake_marker")
    selected = data.get("selected") or found.get("selected") or {}
    board = str(data.get("board") or found.get("board") or (selected.get("board") if isinstance(selected, dict) else ""))
    marker = str(data.get("idempotency_key") or found.get("marker") or "")
    if found.get("status") == "noop":
        return noop(str(found.get("reason") or "no_selected_issue"), dry_run=dry_run_flag(request), selected=found.get("selected"))
    repo = str((selected or {}).get("repo") or "")
    number = (selected or {}).get("number") or 0
    title = str((selected or {}).get("title") or "")
    assignee = str(cfg.get("kanban_intake_assignee") or "lokay-intake")
    dry = dry_run_flag(request)
    if found.get("found"):
        return noop("already_exists", task_id=found.get("task_id"), marker=marker, dry_run=dry)
    if dry:
        return planned(board=board, title=f"[issue] {repo}#{number}: {title}", idempotency_key=marker, assignee=assignee)
    if not board:
        return fail("missing_board", failure_class="terminal", retry_safe=False, marker=marker)
    body = f"GitHub issue: {selected.get('url', '')}\nRepository: {repo}\nIssue: #{number}\n\nIdempotency-Key: {marker}\n"
    try:
        proc = run_cmd(["hermes", "kanban", "--board", board, "create", "--body", body, "--assignee", assignee, "--idempotency-key", marker, f"[issue] {repo}#{number}: {title}"], timeout=90)
    except CommandError as exc:
        return fail("kanban_create_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), mutated=True, board=board, marker=marker)
    return ok(status="intake_task_created", mutated=True, stdout=(getattr(proc, "stdout", "") or "")[-500:], board=board, marker=marker)


def reconcile_intake_task(request: Request) -> Result:
    """Re-read Kanban tasks and verify exactly one marker after creation."""
    from lokay.envelope import cond_blob, terminal_upstream
    terminal = terminal_upstream(request, "reconcile_intake_task", "create_intake_task")
    if terminal:
        return terminal
    data = input_of(request)
    created = cond_blob(request, "create_intake_task")
    board = str(data.get("board") or created.get("board") or "")
    marker = str(data.get("idempotency_key") or created.get("marker") or "")
    if created.get("status") == "noop":
        return noop(str(created.get("reason") or "no_selected_issue"), dry_run=dry_run_flag(request), marker=created.get("marker"))
    dry = dry_run_flag(request)
    if dry or created.get("status") == "noop":
        return ok(status="intake_reconciled", verified=False, mutated=False, dry_run=dry, marker=marker)
    try:
        tasks = hermes_kanban_json(["--board", board, "list", "--json", "--sort", "created-desc"])
    except CommandError as exc:
        return fail("kanban_reconcile_read_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), mutated=True, board=board, marker=marker)
    matches = [x for x in tasks if isinstance(x, dict) and _task_matches(x, marker)] if isinstance(tasks, list) else []
    if len(matches) != 1:
        return fail("kanban_reconcile_mismatch", failure_class="reconcile_then_retry", retry_safe=False, match_count=len(matches), mutated=True, board=board, marker=marker)
    task = matches[0]
    task_id = _task_id(task)
    if task_id is None or not str(task_id).strip():
        return fail("invalid_kanban_task_id", failure_class="terminal", retry_safe=False, mutated=True, board=board, marker=marker)
    return ok(status="intake_reconciled", verified=True, mutated=True, task_id=task_id, task_title=task.get("title"), board=board, marker=marker)
