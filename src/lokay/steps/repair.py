"""Mega-atomic effectors: PR repair domain."""

from __future__ import annotations

from lokay.adapters_cli import CommandError, hermes_kanban_json, run_cmd
from lokay.envelope import (
    Request,
    Result,
    cfg_of,
    cond_blob,
    dry_run_flag,
    fail,
    input_of,
    noop,
    ok,
    planned,
    upstream_noop,
)


def _repair_decision_gate(request: Request) -> Result | None:
    """No-op unless decide_triage_action selected repair."""
    decide = cond_blob(request, "decide_triage_action", "decide", "triage_decide_triage_action")
    if not decide and "action" not in input_of(request):
        return None
    if decide.get("ok") is False or str(decide.get("status") or "") in {
        "failed",
        "cancelled",
        "timed_out",
    }:
        return fail(
            "upstream_failed",
            failure_class="terminal",
            retry_safe=False,
            upstream=decide,
            worked=False,
        )
    if decide.get("status") == "noop":
        return noop(
            str(decide.get("reason") or "no_selected_pr"),
            action=decide.get("action"),
            worked=False,
        )
    action = str(input_of(request).get("action") or decide.get("action") or "")
    if action == "repair":
        return None
    if not action or action == "skip":
        return noop(
            str(decide.get("reason") or "not_selected"),
            action=action or "skip",
            worked=False,
        )
    return noop(
        "not_selected",
        action=action,
        expected=["repair"],
        decide_reason=decide.get("reason"),
        worked=False,
    )


_COMPLETED_TASK_STATUSES = {"done", "completed", "archived"}


def _task_body(task: object) -> str:
    if not isinstance(task, dict):
        return ""
    return str(task.get("body") or task.get("description") or "")


def _task_id(task: object) -> object:
    if not isinstance(task, dict):
        return None
    return task.get("id") or task.get("task_id")


def _task_status(task: object) -> str:
    if not isinstance(task, dict):
        return ""
    return str(task.get("status") or task.get("state") or "").strip().lower()


def _tasks_with_marker(tasks: object, marker: str) -> list[dict[str, object]] | None:
    """Return exact Idempotency-Key body matches, or ``None`` if malformed."""
    import re

    if not isinstance(tasks, list) or any(not isinstance(task, dict) for task in tasks):
        return None
    marker_line = re.compile(r"(?m)^Idempotency-Key:\s*" + re.escape(marker) + r"\s*$")
    return [task for task in tasks if marker_line.search(_task_body(task))]

def _reconcile_marker_read(
    *, board: str, marker: str, title: str
) -> tuple[str, dict[str, object] | None, str | None]:
    """Re-list a board and classify a stable-marker read-back.

    A single marker match is safe to reuse, including a completed task.  No
    match is the only state in which an ambiguous create may be retried;
    malformed reads and duplicate/conflicting matches fail closed.
    """
    try:
        tasks = hermes_kanban_json(
            ["--board", board, "list", "--json", "--sort", "created-desc"]
        )
    except CommandError as exc:
        return "read_failed", None, str(exc)
    matches = _tasks_with_marker(tasks, marker)
    if matches is None:
        return "malformed", None, "kanban list returned non-list JSON"
    if len(matches) > 1:
        return "conflict", None, f"found {len(matches)} tasks with marker {marker}"
    if not matches:
        return "absent", None, None
    match = matches[0]
    match_title = str(match.get("title") or "")
    if match_title and match_title != title:
        return "conflict", None, "stable marker matched a task with a conflicting title"
    return "found", match, None


def _existing_task_result(
    *, board: str, marker: str, task: dict[str, object], title: str
) -> Result:
    task_id = _task_id(task)
    if task_id is None or not str(task_id).strip():
        return fail(
            "invalid_kanban_task_id",
            failure_class="terminal",
            retry_safe=False,
            board=board,
            idempotency_key=marker,
        )
    status = "already_completed" if _task_status(task) in _COMPLETED_TASK_STATUSES else "exists"
    return ok(
        status=status,
        board=board,
        task_id=task_id,
        title=str(task.get("title") or title),
        idempotency_key=marker,
        mutated=False,
    )

def build_repair_prompt(request: Request) -> Result:
    """Pure: build OMP prompt from PR checks/review context."""
    gated = _repair_decision_gate(request)
    if gated is not None:
        return gated
    data = input_of(request)
    decide = cond_blob(request, "decide_triage_action", "decide", "triage_decide_triage_action")
    checks = cond_blob(request, "evaluate_checks", "checks", "triage_evaluate_checks")
    loaded = cond_blob(request, "load_pr_fields", "triage_load_pr_fields")
    created = cond_blob(request, "create_review_task", "create_fix_task")
    pr = data.get("pr") or loaded.get("pr") or {}
    failures = data.get("failures") or checks.get("failures") or []
    reason = str(data.get("reason") or decide.get("reason") or "repair")
    number = pr.get("number") or data.get("number") or loaded.get("number")
    title = pr.get("title") or ""
    body = (
        f"Repair PR #{number}: {title}\n"
        f"Reason: {reason}\n"
        f"Failing checks: {', '.join(failures) if failures else 'n/a'}\n"
        "Update the branch to fix CI/merge issues. Keep scope minimal.\n"
        "Do not force-push. Do not merge.\n"
    )
    task_id = created.get("task_id") or data.get("task_id")
    repo = data.get("repo") or loaded.get("repo")
    linked = pr.get("linkedIssue") if isinstance(pr, dict) else None
    issue = data.get("issue") or loaded.get("issue") or (linked.get("number") if isinstance(linked, dict) else linked) or number
    return ok(
        status="built",
        prompt=body,
        reason=reason,
        pr_number=number,
        branch=pr.get("headRefName") if isinstance(pr, dict) else None,
        **({"task_id": task_id} if task_id else {}),
        **({"repo": repo} if repo else {}),
        **({"issue": issue} if issue else {}),
    )






# Atomic repair-chain handlers share the Kanban/read primitives with dispatch.
from lokay.steps.issue_to_pr import (
    _atomic_terminal,
    _reconcile_kanban_marker,
)

def read_review_tasks(request: Request) -> Result:
    terminal = _atomic_terminal(request, "read_review_tasks", "decide_triage_action", "verify_merge_receipt")
    if terminal: return terminal
    idle = upstream_noop(request, "decide_triage_action", "verify_merge_receipt")
    if idle: return noop(str(idle.get("reason") or "no_selected_pr"), operation="read_review_tasks")
    data, cfg = input_of(request), cfg_of(request); board = str(data.get("board") or cfg.get("board") or "")
    if not board: return fail("missing_board", failure_class="terminal", retry_safe=False, operation="read_review_tasks")
    try: tasks = hermes_kanban_json(["--board", board, "list", "--json", "--sort", "created-desc"])
    except CommandError as exc: return fail("kanban_list_failed", failure_class="retryable_read", retry_safe=True, operation="read_review_tasks", error=str(exc), board=board)
    if not isinstance(tasks, list) or any(not isinstance(t, dict) for t in tasks): return fail("invalid_kanban_json", failure_class="terminal", retry_safe=False, operation="read_review_tasks")
    return ok(status="read", operation="read_review_tasks", board=board, tasks=tasks)

def find_review_marker(request: Request) -> Result:
    terminal = _atomic_terminal(request, "find_review_marker", "read_review_tasks")
    if terminal: return terminal
    idle = upstream_noop(request, "read_review_tasks")
    if idle: return noop(str(idle.get("reason") or "no_selected_pr"), operation="find_review_marker")
    data = input_of(request); rows = data.get("tasks") or cond_blob(request, "read_review_tasks").get("tasks") or []; repo, number = str(data.get("repo") or ""), str(data.get("pr_number") or data.get("number") or ""); marker = str(data.get("idempotency_key") or f"fix-pr-review:{repo}:{number}")
    if not isinstance(rows, list): return fail("missing_review_rows", failure_class="terminal", retry_safe=False, operation="find_review_marker")
    matches = [t for t in rows if marker in str(t.get("body") or t.get("description") or "")]
    if len(matches) > 1: return fail("ambiguous_review_task", failure_class="terminal", retry_safe=False, operation="find_review_marker", matches=matches)
    return ok(status="found" if matches else "absent", operation="find_review_marker", marker=marker, task=matches[0] if matches else None)

def create_review_task(request: Request) -> Result:
    gated = _repair_decision_gate(request)
    if gated is not None: return gated
    terminal = _atomic_terminal(request, "create_review_task", "find_review_marker")
    if terminal: return terminal
    idle = upstream_noop(request, "find_review_marker")
    if idle: return noop(str(idle.get("reason") or "no_selected_pr"), operation="create_review_task")
    data, cfg = input_of(request), cfg_of(request); board, repo, number = str(data.get("board") or cfg.get("board") or ""), str(data.get("repo") or ""), str(data.get("pr_number") or data.get("number") or ""); reason = str(data.get("reason") or "checks_failed"); marker = str(data.get("idempotency_key") or f"fix-pr-review:{repo}:{number}"); title = str(data.get("title") or f"[fix-pr-review] {repo}#{number}: {reason}")
    if not board or not repo or not number: return fail("missing_board_repo_or_number", failure_class="terminal", retry_safe=False, operation="create_review_task", idempotency_key=marker)
    if dry_run_flag(request): return planned(operation="create_review_task", board=board, title=title, idempotency_key=marker)
    body = str(data.get("body") or f"Repository: {repo}\nPR: #{number}\nReason: {reason}\nIdempotency-Key: {marker}\n")
    try: proc = run_cmd(["hermes", "kanban", "--board", board, "create", "--body", body, "--assignee", str(cfg.get("fixer_assignee") or "lokay-fixer"), "--idempotency-key", marker, title], timeout=90)
    except CommandError as exc: return fail("review_task_create_failed", failure_class="reconcile_then_retry", retry_safe=False, operation="create_review_task", error=str(exc), mutated=True)
    return ok(status="created", operation="create_review_task", board=board, title=title, marker=marker, stdout=(proc.stdout or "")[-400:], mutated=True)

def reconcile_review_task(request: Request) -> Result:
    terminal = _atomic_terminal(request, "reconcile_review_task", "create_review_task")
    if terminal: return terminal
    idle = upstream_noop(request, "create_review_task")
    if idle: return noop(str(idle.get("reason") or "no_selected_pr"), operation="reconcile_review_task")
    return _reconcile_kanban_marker(request, "reconcile_review_task", "create_review_task", "fix-pr-review")

def read_task_for_block(request: Request) -> Result:
    terminal = _atomic_terminal(request, "read_task_for_block", "build_repair_prompt")
    if terminal: return terminal
    idle = upstream_noop(request, "build_repair_prompt")
    if idle: return noop(str(idle.get("reason") or "no_selected_pr"), operation="read_task_for_block")
    data, cfg = input_of(request), cfg_of(request); board, task_id = str(data.get("board") or cfg.get("board") or ""), str(data.get("task_id") or "")
    if not board or not task_id: return fail("missing_board_or_task_id", failure_class="terminal", retry_safe=False, operation="read_task_for_block")
    try: tasks = hermes_kanban_json(["--board", board, "list", "--json", "--sort", "created-desc"])
    except CommandError as exc: return fail("kanban_list_failed", failure_class="retryable_read", retry_safe=True, operation="read_task_for_block", error=str(exc))
    if not isinstance(tasks, list) or any(not isinstance(t, dict) for t in tasks): return fail("invalid_kanban_json", failure_class="terminal", retry_safe=False, operation="read_task_for_block")
    matches = [t for t in tasks if str(t.get("id") or t.get("task_id") or "") == task_id]
    if len(matches) != 1: return fail("task_not_found" if not matches else "ambiguous_task", failure_class="terminal", retry_safe=False, operation="read_task_for_block", task_id=task_id)
    return ok(status="read", operation="read_task_for_block", board=board, task=matches[0], task_id=task_id)

def decide_task_block(request: Request) -> Result:
    terminal = _atomic_terminal(request, "decide_task_block", "read_task_for_block")
    if terminal: return terminal
    idle = upstream_noop(request, "read_task_for_block")
    if idle: return noop(str(idle.get("reason") or "no_selected_pr"), operation="decide_task_block")
    task = input_of(request).get("task") or cond_blob(request, "read_task_for_block").get("task") or {}; state = _task_status(task)
    if state == "blocked": return ok(status="already_blocked", operation="decide_task_block", should_block=False)
    if state in _COMPLETED_TASK_STATUSES: return ok(status="already_completed", operation="decide_task_block", should_block=False)
    return ok(status="should_block", operation="decide_task_block", should_block=True)

def block_task(request: Request) -> Result:
    terminal = _atomic_terminal(request, "block_task", "decide_task_block")
    if terminal: return terminal
    idle = upstream_noop(request, "decide_task_block")
    if idle: return noop(str(idle.get("reason") or "no_selected_pr"), operation="block_task")
    data, cfg = input_of(request), cfg_of(request); board, task_id, reason = str(data.get("board") or cfg.get("board") or ""), str(data.get("task_id") or ""), str(data.get("reason") or "blocked"); decision = cond_blob(request, "decide_task_block")
    if decision.get("should_block") is False: return ok(status=decision.get("status") or "already_blocked", operation="block_task", mutated=False)
    if not board or not task_id: return fail("missing_board_or_task_id", failure_class="terminal", retry_safe=False, operation="block_task")
    if dry_run_flag(request): return planned(operation="block_task", board=board, task_id=task_id, reason=reason)
    try: run_cmd(["hermes", "kanban", "--board", board, "block", task_id, "--reason", reason], timeout=60)
    except CommandError as exc: return fail("block_failed", failure_class="reconcile_then_retry", retry_safe=False, operation="block_task", error=str(exc), mutated=True)
    return ok(status="blocked", operation="block_task", board=board, task_id=task_id, mutated=True)

def verify_task_blocked(request: Request) -> Result:
    terminal = _atomic_terminal(request, "verify_task_blocked", "block_task")
    if terminal: return terminal
    idle = upstream_noop(request, "block_task")
    if idle: return noop(str(idle.get("reason") or "no_selected_pr"), operation="verify_task_blocked")
    read = read_task_for_block(request)
    if read.get("ok") is False: return read
    state = _task_status(read["task"])
    if state not in {"blocked", *_COMPLETED_TASK_STATUSES}: return fail("block_not_confirmed", failure_class="reconcile_then_retry", retry_safe=False, operation="verify_task_blocked", task_id=read["task_id"], state=state, mutated=True)
    return ok(status="verified", operation="verify_task_blocked", task_id=read["task_id"], blocked=state == "blocked")
