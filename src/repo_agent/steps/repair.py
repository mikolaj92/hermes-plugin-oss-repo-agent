"""Mega-atomic effectors: PR repair domain."""

from __future__ import annotations

from fala.adapters import EffectorRunRequest, EffectorRunResult

from repo_agent.adapters_cli import CommandError, hermes_kanban_json, run_cmd
from repo_agent.envelope import (
    cfg_of,
    cond_blob,
    dry_run_flag,
    fail,
    input_of,
    ok,
    planned,
)
from repo_agent.steps.issue_to_pr import resolve_kanban_task_id_after_create


def build_repair_prompt(request: EffectorRunRequest) -> EffectorRunResult:
    """Pure: build OMP prompt from PR checks/review context."""
    data = input_of(request)
    loaded = cond_blob(request, "load_pr_fields")
    decide = cond_blob(request, "decide_triage_action", "decide")
    checks = cond_blob(request, "evaluate_checks", "checks")
    pr = data.get("pr") or loaded.get("pr") or {}
    failures = data.get("failures") or checks.get("failures") or []
    reason = str(
        data.get("reason") or decide.get("reason") or "repair"
    )
    number = pr.get("number") or data.get("number") or loaded.get("number")
    title = pr.get("title") or ""
    body = (
        f"Repair PR #{number}: {title}\n"
        f"Reason: {reason}\n"
        f"Failing checks: {', '.join(failures) if failures else 'n/a'}\n"
        f"Update the branch to fix CI/merge issues. Keep scope minimal.\n"
        f"Do not force-push. Do not merge.\n"
    )
    return ok(
        status="built",
        prompt=body,
        reason=reason,
        pr_number=number,
        branch=pr.get("headRefName") if isinstance(pr, dict) else None,
    )


def create_review_fix_task(request: EffectorRunRequest) -> EffectorRunResult:
    """Create Kanban [fix-pr-review] task for a PR."""
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    loaded = cond_blob(request, "load_pr_fields", "decide_triage_action")
    decide = cond_blob(request, "decide_triage_action", "decide")
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
    if not board or not repo or not number:
        return fail("missing_board_repo_or_number")
    title = f"[fix-pr-review] {repo} PR#{number}: {reason}"
    branch = pr.get("headRefName") or ""
    body = (
        f"Repository: {repo}\n"
        f"PR: #{number}\n"
        f"Reason: {reason}\n"
        f"Branch: {branch}\n"
        f"Idempotency-Key: fix-pr-review:{repo}:{number}:{reason}\n"
    )
    if dry:
        return planned(board=board, title=title, assignee=assignee)
    try:
        tasks = hermes_kanban_json(
            ["--board", board, "list", "--json", "--sort", "created-desc"]
        )
    except CommandError as exc:
        return fail("kanban_list_failed", error=str(exc))
    for t in tasks if isinstance(tasks, list) else []:
        tt = str(t.get("title") or "")
        if f"PR#{number}" in tt and tt.startswith("[fix-pr-review]") and str(t.get("status")) != "done":
            return ok(
                status="exists",
                task_id=t.get("id") or t.get("task_id"),
                title=tt,
                mutated=False,
            )
    try:
        proc = run_cmd(
            [
                "hermes",
                "kanban",
                "--board",
                board,
                "create", title,
                "--body",
                body,
                "--assignee",
                assignee,
            ],
            timeout=90,
        )
    except CommandError as exc:
        return fail("create_failed", error=str(exc))
    task_id = resolve_kanban_task_id_after_create(
        board=board, task_title=title, stdout=proc.stdout or ""
    )
    if not task_id:
        return fail(
            "created_but_unresolved_task_id",
            title=title,
            board=board,
            stdout=(proc.stdout or "")[-300:],
            mutated=True,
        )
    return ok(
        status="created",
        title=title,
        board=board,
        task_id=task_id,
        stdout=(proc.stdout or "")[-300:],
        mutated=True,
    )


def block_kanban_task(request: EffectorRunRequest) -> EffectorRunResult:
    """Mark Kanban task blocked with a reason."""
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    board = str(data.get("board") or cfg.get("board") or "")
    task_id = str(data.get("task_id") or "")
    reason = str(data.get("reason") or "blocked")
    if not board or not task_id:
        return fail("missing_board_or_task_id")
    if dry:
        return planned(board=board, task_id=task_id, reason=reason)
    try:
        run_cmd(
            [
                "hermes",
                "kanban",
                "--board",
                board,
                "block",
                task_id,
                "--reason",
                reason,
            ],
            timeout=60,
        )
    except CommandError as exc:
        return fail("block_failed", error=str(exc))
    return ok(status="blocked", task_id=task_id, mutated=True)
