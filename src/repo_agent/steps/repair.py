"""Mega-atomic effectors: PR repair domain."""

from __future__ import annotations

from repo_agent.adapters_cli import CommandError, hermes_kanban_json, run_cmd
from repo_agent.envelope import (
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
    loaded = cond_blob(request, "load_pr_fields", "triage_load_pr_fields")
    created = cond_blob(request, "create_review_fix_task", "triage_create_review_fix_task")
    decide = cond_blob(request, "decide_triage_action", "decide", "triage_decide_triage_action")
    checks = cond_blob(request, "evaluate_checks", "checks", "triage_evaluate_checks")
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


def create_review_fix_task(request: Request) -> Result:
    """Create Kanban [fix-pr-review] task for a PR."""
    gated = _repair_decision_gate(request)
    if gated is not None:
        return gated
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    loaded = cond_blob(request, "load_pr_fields", "decide_triage_action", "triage_load_pr_fields", "triage_decide_triage_action")
    decide = cond_blob(request, "decide_triage_action", "decide", "triage_decide_triage_action")
    pr = data.get("pr") or loaded.get("pr") or {}
    board = str(data.get("board") or cfg.get("board") or "")
    repo = str(data.get("repo") or loaded.get("repo") or cfg.get("repo") or "")
    number = int(
        data.get("number")
        or data.get("pr_number")
        or loaded.get("number")
        or (pr.get("number") if isinstance(pr, dict) else 0)
        or 0
    )
    reason = str(data.get("reason") or decide.get("reason") or "checks_failed")
    assignee = str(cfg.get("fixer_assignee") or "repo-agent-fixer")
    marker = f"fix-pr-review:{repo}:{number}"
    if not board or not repo or not number:
        return fail(
            "missing_board_repo_or_number",
            failure_class="terminal",
            retry_safe=False,
            board=board,
            repo=repo,
            pr_number=number,
            idempotency_key=marker,
        )
    # Title uses owner/repo#PR so shell extract_records parses PR number
    # the same way as scripts/repo_pr_triage.sh review tasks.
    title = f"[fix-pr-review] {repo}#{number}: {reason}"
    body = (
        f"Repository: {repo}\nPR: #{number}\nReason: {reason}\n"
        f"Idempotency-Key: {marker}\n"
    )
    if dry:
        return planned(board=board, title=title, assignee=assignee, idempotency_key=marker)
    state, existing, error = _reconcile_marker_read(board=board, marker=marker, title=title)
    if state == "found" and existing is not None:
        return _existing_task_result(board=board, marker=marker, task=existing, title=title)
    if state == "read_failed":
        return fail(
            "kanban_list_failed",
            failure_class="retryable_read",
            retry_safe=True,
            error=error,
            board=board,
            repo=repo,
            pr_number=number,
            title=title,
            idempotency_key=marker,
        )
    if state == "malformed":
        return fail(
            "invalid_kanban_readback",
            failure_class="terminal",
            retry_safe=False,
            error=error,
            board=board,
            repo=repo,
            pr_number=number,
            title=title,
            idempotency_key=marker,
        )
    if state == "conflict":
        return fail(
            "ambiguous_kanban_task",
            failure_class="terminal",
            retry_safe=False,
            error=error,
            board=board,
            repo=repo,
            pr_number=number,
            title=title,
            idempotency_key=marker,
        )
    try:
        proc = run_cmd(
            [
                "hermes", "kanban", "--board", board, "create",
                "--title", title, "--body", body, "--assignee", assignee,
                "--idempotency-key", marker,
            ],
            timeout=90,
        )
    except CommandError as exc:
        state, existing, error = _reconcile_marker_read(board=board, marker=marker, title=title)
        if state == "found" and existing is not None:
            existing_result = _existing_task_result(board=board, marker=marker, task=existing, title=title)
            reconciled_output = dict(existing_result)
            reconciled_output["reconciled"] = True
            reconciled_output["mutated"] = True
            if reconciled_output.get("ok") is not False:
                reconciled_output["status"] = "reconciled"
            return reconciled_output
        return fail(
            "create_reconciliation_failed",
            failure_class="terminal",
            retry_safe=False,
            error=error or str(exc),
            board=board,
            repo=repo,
            pr_number=number,
            title=title,
            idempotency_key=marker,
            mutated=True,
        )
    state, existing, error = _reconcile_marker_read(board=board, marker=marker, title=title)
    if state == "found" and existing is not None:
        task_id = _task_id(existing)
        if task_id is None or not str(task_id).strip():
            return fail(
                "created_but_unresolved_task_id",
                failure_class="terminal",
                retry_safe=False,
                title=title,
                board=board,
                repo=repo,
                pr_number=number,
                error="stable marker matched a task with no id",
                stdout=(proc.stdout or "")[-300:],
                idempotency_key=marker,
                mutated=True,
            )
        return ok(status="created", title=title, board=board, task_id=task_id, stdout=(proc.stdout or "")[-300:], idempotency_key=marker, mutated=True)
    return fail(
        "created_but_unresolved_task_id",
        failure_class="terminal",
        retry_safe=False,
        title=title,
        board=board,
        repo=repo,
        pr_number=number,
        error=error or "stable marker missing after create",
        stdout=(proc.stdout or "")[-300:],
        idempotency_key=marker,
        mutated=True,
    )


def block_kanban_task(request: Request) -> Result:
    """Mark a task blocked, reconciling authoritative state before and after."""
    data = input_of(request)
    dry = dry_run_flag(request)
    cfg = cfg_of(request)
    board = str(data.get("board") or cfg.get("board") or "")
    task_id = str(data.get("task_id") or "")
    reason = str(data.get("reason") or "blocked")
    key = f"kanban:{board}:task:{task_id}:block"
    if not board or not task_id:
        return fail(
            "missing_board_or_task_id",
            failure_class="terminal",
            retry_safe=False,
            board=board,
            task_id=task_id,
            idempotency_key=key,
        )

    def read_state() -> tuple[dict[str, object] | None, str | None]:
        try:
            tasks = hermes_kanban_json(["--board", board, "list", "--json", "--sort", "created-desc"])
        except CommandError as exc:
            return None, str(exc)
        if not isinstance(tasks, list) or any(not isinstance(task, dict) for task in tasks):
            return None, "kanban list returned malformed JSON"
        matches = [task for task in tasks if str(_task_id(task) or "") == task_id]
        if len(matches) != 1:
            return None, "task missing or ambiguous"
        status = _task_status(matches[0])
        if not status:
            return None, "task state is blank"
        return matches[0], None

    if dry:
        return planned(board=board, task_id=task_id, reason=reason, idempotency_key=key)
    before, error = read_state()
    if error or before is None:
        return fail("invalid_kanban_task_readback", failure_class="terminal", retry_safe=False, error=error or "missing task", board=board, task_id=task_id, idempotency_key=key)
    before_status = _task_status(before)
    if before_status in {"blocked", *_COMPLETED_TASK_STATUSES}:
        return ok(status="already_blocked" if before_status == "blocked" else "already_completed", board=board, task_id=task_id, mutated=False)
    try:
        run_cmd(["hermes", "kanban", "--board", board, "block", task_id, "--reason", reason], timeout=60)
    except CommandError as exc:
        after, read_error = read_state()
        if after is not None and _task_status(after) in {"blocked", *_COMPLETED_TASK_STATUSES}:
            return ok(status="reconciled", board=board, task_id=task_id, mutated=True, reconciled=True)
        return fail("block_failed", failure_class="reconcile_then_retry", retry_safe=False, error=read_error or str(exc), board=board, task_id=task_id, idempotency_key=key, mutated=True)
    after, error = read_state()
    if error or after is None:
        return fail("block_readback_failed", failure_class="reconcile_then_retry", retry_safe=False, error=error or "missing task", board=board, task_id=task_id, idempotency_key=key, mutated=True)
    after_status = _task_status(after)
    if after_status not in {"blocked", *_COMPLETED_TASK_STATUSES}:
        return fail("block_not_confirmed", failure_class="reconcile_then_retry", retry_safe=False, board=board, task_id=task_id, idempotency_key=key, state=after_status, mutated=True)
    return ok(status="blocked" if after_status == "blocked" else "already_completed", board=board, task_id=task_id, mutated=True)
