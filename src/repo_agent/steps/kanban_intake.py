from __future__ import annotations

import re
from typing import Any

from fala.adapters import EffectorRunRequest, EffectorRunResult

from repo_agent.adapters_cli import CommandError, hermes_kanban_json, run_cmd
from repo_agent.envelope import (
    cfg_of,
    conduction_of,
    dry_run_flag,
    fail,
    noop,
    ok,
    planned,
)


def _task_matches(task: dict[str, Any], repo: str, issue: int) -> bool:
    if str(task.get("status") or "") == "done":
        return False
    title = str(task.get("title") or "")
    if not title.startswith(("[issue]", "[fix-pr]", "[fix-pr-review]")):
        return False
    body = str(task.get("body") or task.get("description") or "")
    m = re.search(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)\b", title)
    if m and m.group(1) == repo and int(m.group(2)) == issue:
        return True
    body_repo = re.search(r"^Repository:\s*(\S+)\s*$", body, re.MULTILINE)
    body_issue = re.search(r"^Issue:\s*#([0-9]+)\s*$", body, re.MULTILINE)
    if (
        body_repo
        and body_issue
        and body_repo.group(1) == repo
        and int(body_issue.group(1)) == issue
    ):
        return True
    return False


def ensure_kanban_intake(request: EffectorRunRequest) -> EffectorRunResult:
    """Atomic: ensure one Kanban [issue] task for claimed issue."""
    cond = conduction_of(request)
    claim = dict(cond.get("claim") or cond.get("claim_github_issue") or {})
    poll = dict(cond.get("poll") or {})
    selected = claim.get("selected") or poll.get("selected")
    dry_run = dry_run_flag(request, default=bool(claim.get("dry_run", True)))
    cfg = cfg_of(request)
    assignee = str(cfg.get("kanban_intake_assignee") or "repo-agent-intake")

    if not selected or claim.get("status") == "noop":
        return noop(claim.get("reason") or "no_selected_issue", dry_run=dry_run)

    if claim.get("status") == "failed":
        return fail("claim_failed", claim=claim, dry_run=dry_run)

    repo = str(selected["repo"])
    number = int(selected.get("number") or selected.get("issue") or 0)
    board = str(selected.get("board") or "")
    title = str(selected.get("title") or "")
    url = str(selected.get("url") or "")
    labels = selected.get("labels") or []
    task_title = f"[issue] {repo}#{number}: {title}"
    body = (
        f"GitHub issue: {url}\n"
        f"Repository: {repo}\n"
        f"Issue: #{number}\n"
        f"Labels at intake: {', '.join(labels) if labels else 'none'}\n"
        f"\n"
        f"Intake via Fala effector ensure_kanban_intake.\n"
        f"Idempotency-Key: github-issue:{repo}:{number}\n"
    )
    planned_spec = {
        "board": board,
        "title": task_title,
        "assignee": assignee,
        "idempotency_key": f"github-issue:{repo}:{number}",
    }

    if dry_run:
        return planned(selected=selected, planned=planned_spec)

    if not board:
        return fail("missing_board", selected=selected)

    try:
        tasks = hermes_kanban_json(
            ["--board", board, "list", "--json", "--sort", "created-desc"]
        )
    except CommandError as exc:
        return fail("kanban_list_failed", error=str(exc), selected=selected)

    if not isinstance(tasks, list):
        tasks = []

    for task in tasks:
        if isinstance(task, dict) and _task_matches(task, repo, number):
            return ok(
                status="exists",
                selected=selected,
                task_id=task.get("id") or task.get("task_id"),
                task_title=task.get("title"),
                mutated=False,
            )

    try:
        proc = run_cmd(
            [
                "hermes",
                "kanban",
                "--board",
                board,
                "create",
                "--title",
                task_title,
                "--body",
                body,
                "--assignee",
                assignee,
            ],
            timeout=90,
        )
    except CommandError as exc:
        return fail(
            "kanban_create_failed",
            error=str(exc),
            stderr=exc.stderr[-500:],
            planned=planned_spec,
        )

    return ok(
        status="created",
        selected=selected,
        planned=planned_spec,
        stdout=proc.stdout[-500:],
        mutated=True,
    )
