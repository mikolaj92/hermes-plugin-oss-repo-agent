"""Mega-atomic effectors: issue → PR domain."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from dataclasses import dataclass

from repo_agent.envelope import Request, Result

from repo_agent.adapters_cli import CommandError, hermes_kanban_json, run_cmd
from repo_agent.adapters_git import (
    branch_config_get,
    branch_config_set,
    branch_exists,
    git,
    is_dirty,
    local_branch_head,
    parse_worktree_porcelain,
    push_branch as git_push_branch,
    remote_ref,
    remote_url,
    rev_parse,
    status_porcelain,
    worktree_add,
    worktree_list,
    worktree_remove,
)
from repo_agent.adapters_omp import run_omp
from repo_agent.envelope import (
    cfg_of,
    cond_blob,
    cond_get,
    dry_run_flag,
    fail,
    input_of,
    noop,
    ok,
    planned,
    upstream_noop,
)

@dataclass(frozen=True)
class KanbanTaskResolution:
    task_id: str | None
    status: str
    error: str | None = None


_TRIAGE_REPAIR_ALIASES = (
    "triage_create_review_fix_task",
    "triage_build_repair_prompt",
    "triage_decide_triage_action",
    "triage_load_pr_fields",
    "triage_repair_prepare_worktree",
    "create_review_fix_task",
    "build_repair_prompt",
    "decide_triage_action",
    "load_pr_fields",
)
_ISSUE_TO_PR_ALIASES = (
    "parse_issue_ref",
    "parse_issue_ref_from_task",
    "dispatch_parse_issue_ref",
    "load_kanban_task",
    "dispatch_load_kanban_task",
)
_PROVENANCE_ALIASES = _TRIAGE_REPAIR_ALIASES + _ISSUE_TO_PR_ALIASES + (
    "triage_repair_prepare_worktree",
    "repair_prepare_worktree",
)

def _repair_action_gate(request: Request) -> Result | None:
    """Prevent shared repair handlers from running for another triage action."""
    decision = cond_blob(
        request,
        "decide_triage_action",
        "triage_decide_triage_action",
        "decide",
    )
    if not decision:
        return None
    if decision.get("status") == "noop":
        return noop("not_selected", upstream=decision)
    if decision.get("status") == "failed" or decision.get("ok") is False:
        return fail(
            "upstream_decision_failed",
            failure_class="terminal",
            retry_safe=False,
            upstream=decision,
        )
    if decision.get("action") != "repair":
        return noop("not_selected", action=decision.get("action"), upstream=decision)
    return None


def _conduction_blobs(request: Request, aliases: tuple[str, ...]) -> list[dict[str, Any]]:
    """Read only the named path aliases, preserving path ownership boundaries."""
    conduction = input_of(request).get("conduction")
    if not isinstance(conduction, dict):
        return []
    blobs: list[dict[str, Any]] = []
    for alias in aliases:
        for name, value in conduction.items():
            if name == alias or name.endswith(f"_{alias}"):
                if isinstance(value, dict) and value and value not in blobs:
                    blobs.append(dict(value))
    return blobs





def resolve_kanban_task_id_after_create_result(
    *,
    board: str,
    task_title: str,
    stdout: str = "",
) -> KanbanTaskResolution:
    """Re-list board and classify authoritative post-create read-back."""
    try:
        tasks = hermes_kanban_json(
            ["--board", board, "list", "--json", "--sort", "created-desc"]
        )
    except CommandError as exc:
        return KanbanTaskResolution(None, "read_failed", str(exc))
    if not isinstance(tasks, list) or any(not isinstance(task, dict) for task in tasks):
        return KanbanTaskResolution(None, "malformed", "invalid Kanban list read-back shape")
    matches = [
        task
        for task in tasks
        if str(task.get("title") or "") == task_title and _task_id_from_row(task)
    ]
    if not matches:
        return KanbanTaskResolution(None, "unresolved", "created task was not found")
    if len(matches) > 1:
        return KanbanTaskResolution(None, "ambiguous", "multiple tasks matched created title")
    return KanbanTaskResolution(_task_id_from_row(matches[0]), "resolved")

def _task_id_from_row(task: dict[str, Any]) -> str | None:
    tid = task.get("id") or task.get("task_id")
    return str(tid) if tid is not None and str(tid) else None


def _parse_task_id_from_stdout(stdout: str) -> str | None:
    """Best-effort extract task id from hermes kanban create stdout."""
    text = stdout or ""
    # Common patterns: "Created task t_abc", "task_id=...", bare t_hex / UUID
    for pattern in (
        r"\b(t_[A-Za-z0-9]+)\b",
        r"\btask[_-]?id[=:\s]+([A-Za-z0-9_-]+)\b",
        r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    ):
        m = re.search(pattern, text, re.I)
        if m:
            return m.group(1)
    return None




def load_kanban_task(request: Request) -> Result:
    """Read one Kanban task by id (or first ready [fix-pr]/[issue])."""
    data = input_of(request)
    cfg = cfg_of(request)
    upstream = upstream_noop(request, "intake_kanban")
    if upstream:
        return noop(
            str(upstream.get("reason") or "no_intake_work"),
            worked=False,
        )
    intake = cond_blob(request, "intake_kanban")
    if intake.get("status") == "planned":
        return noop("intake_planned", worked=False)
    selected = cond_get(
        request,
        "selected",
        "intake_kanban",
        "intake_claim",
        "intake_poll",
        default={},
    )
    if not isinstance(selected, dict):
        selected = {}
    board = str(data.get("board") or selected.get("board") or cfg.get("board") or "")
    task_id = data.get("task_id")
    if not board:
        return fail("missing_board", failure_class="terminal", retry_safe=False)
    try:
        tasks = hermes_kanban_json(
            ["--board", board, "list", "--json", "--sort", "created-desc"]
        )
    except CommandError as exc:
        return fail("kanban_list_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), board=board, task_id=task_id, idempotency_key=f"kanban:{board}:{task_id or 'ready'}:load")
    if not isinstance(tasks, list) or any(not isinstance(task, dict) for task in tasks):
        return fail("invalid_kanban_json", failure_class="terminal", retry_safe=False)
    matches = [
        t for t in tasks
        if isinstance(t, dict) and str(t.get("id") or t.get("task_id")) == str(task_id)
    ] if task_id else []
    context = {
        key: selected[key]
        for key in ("repo", "board", "clone_path", "priority")
        if selected.get(key) not in (None, "")
    }
    if task_id and not matches:
        return fail("task_not_found", failure_class="terminal", retry_safe=False, task_id=task_id, board=board)
    if task_id and matches:
        return ok(status="loaded", task=matches[0], board=board, **context)
    # pick first ready-ish fix-pr / issue
    for t in tasks:
        if str(t.get("status") or "") in ("done", "archived"):
            continue
        title = str(t.get("title") or "")
        if title.startswith(("[fix-pr]", "[issue]", "[fix-pr-review]")):
            return ok(status="loaded", task=t, board=board, **context)
    return noop("no_ready_task", board=board, **context)


def parse_issue_ref_from_task(request: Request) -> Result:
    """Pure: extract owner/repo#N and preferred branch from task title/body."""
    data = input_of(request)
    upstream = upstream_noop(request, "load_kanban_task")
    if upstream:
        return noop(str(upstream.get("reason") or "no_ready_task"))
    task = data.get("task") or cond_get(request, "task", "load_kanban_task")
    if not task:
        return fail("missing_task", failure_class="terminal", retry_safe=False)
    title = str(task.get("title") or "")
    body = str(task.get("body") or task.get("description") or "")
    m = re.search(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)\b", title)
    repo = issue = None
    if m:
        repo, issue = m.group(1), int(m.group(2))
    else:
        br = re.search(r"^Repository:\s*(\S+)\s*$", body, re.M)
        bi = re.search(r"^Issue:\s*#([0-9]+)\s*$", body, re.M)
        if br and bi:
            repo, issue = br.group(1), int(bi.group(1))
    if not repo or not issue:
        return fail("unparseable_issue_ref", failure_class="terminal", retry_safe=False, title=title)
    loaded = cond_blob(request, "load_kanban_task")
    context = {
        key: loaded[key]
        for key in ("board", "clone_path", "priority")
        if loaded.get(key) not in (None, "")
    }
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", title.lower())[:40].strip("-")
    branch_prefix = str(cfg_of(request).get("branch_prefix") or "ai/fix")
    branch = f"{branch_prefix}/{issue}-{slug or 'task'}"
    return ok(status="parsed", repo=repo, issue=issue, branch=branch, task_id=task.get("id") or task.get("task_id"), task_title=title, **context)



def create_fix_pr_task(request: Request) -> Result:
    """Create Kanban [fix-pr] task for an issue (idempotent-ish skip if exists)."""
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    board = str(data.get("board") or cfg.get("board") or "")
    repo = str(data.get("repo") or "")
    issue = int(data.get("issue") or 0)
    title_hint = str(data.get("title") or f"Fix {repo}#{issue}")
    assignee = str(cfg.get("fixer_assignee") or "repo-agent-fixer")
    if not board or not repo or not issue:
        return fail("missing_board_repo_issue", failure_class="terminal", retry_safe=False)
    task_title = f"[fix-pr] {repo}#{issue}: {title_hint}"
    body = (
        f"Repository: {repo}\nIssue: #{issue}\n"
        f"Idempotency-Key: fix-pr:{repo}:{issue}\n"
    )
    if dry:
        return planned(board=board, title=task_title, assignee=assignee, body=body)
    try:
        tasks = hermes_kanban_json(
            ["--board", board, "list", "--json", "--sort", "created-desc"]
        )
    except CommandError as exc:
        return fail("kanban_list_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), board=board, repo=repo, issue=issue, idempotency_key=f"kanban:{board}:{repo}:{issue}:create")
    if not isinstance(tasks, list) or any(not isinstance(task, dict) for task in tasks):
        return fail("invalid_kanban_json", failure_class="terminal", retry_safe=False, board=board)
    for t in tasks if isinstance(tasks, list) else []:
        tt = str(t.get("title") or "")
        if f"{repo}#{issue}" in tt and tt.startswith(("[fix-pr]", "[fix-pr-review]")):
            if str(t.get("status") or "") != "done":
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
        return fail("kanban_create_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), stderr=exc.stderr[-400:], mutated=True)
    resolution = resolve_kanban_task_id_after_create_result(
        board=board, task_title=task_title, stdout=proc.stdout or ""
    )
    if not resolution.task_id:
        if resolution.status == "read_failed":
            return fail(
                "kanban_create_readback_failed",
                failure_class="reconcile_then_retry",
                retry_safe=False,
                error=resolution.error,
                title=task_title,
                board=board,
                repo=repo,
                issue=issue,
                idempotency_key=f"kanban:{board}:{repo}:{issue}:create",
                stdout=(proc.stdout or "")[-400:],
                mutated=True,
            )
        return fail(
            "created_but_unresolved_task_id",
            failure_class="terminal",
            retry_safe=False,
            error=resolution.error,
            title=task_title,
            board=board,
            repo=repo,
            issue=issue,
            idempotency_key=f"kanban:{board}:{repo}:{issue}:create",
            stdout=(proc.stdout or "")[-400:],
            mutated=True,
        )
    task_id = resolution.task_id
    repo = str(data.get("repo") or cond_get(request, "repo", "parse_issue_ref", "load_kanban_task") or "")
    return ok(
        status="created",
        task_id=task_id,
        board=board,
        repo=repo,
        task_title=task_title,
        stdout=(proc.stdout or "")[-400:],
        mutated=True,
    )


def complete_kanban_task(request: Request) -> Result:
    """Mark one Kanban task done with a short result string."""
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    board = str(
        data.get("board")
        or cond_get(request, "board", "load_kanban_task", "parse_issue_ref")
        or cfg.get("board")
        or ""
    )
    task_id = str(
        data.get("task_id")
        or cond_get(request, "task_id", "load_kanban_task", "parse_issue_ref")
        or ""
    )
    if not task_id:
        loaded = cond_blob(request, "load_kanban_task")
        task = loaded.get("task") if isinstance(loaded.get("task"), dict) else {}
        task_id = str(task.get("id") or task.get("task_id") or "")
        if not board:
            board = str(loaded.get("board") or board)
    terminal = _terminal_upstream(request, "write_dispatch_receipt")
    if terminal is not None:
        return terminal
    upstream = upstream_noop(request, "load_kanban_task", "parse_issue_ref")
    if upstream:
        return noop(str(upstream.get("reason") or "no_ready_task"))
    result_text = str(data.get("result") or "completed")
    if not board or not task_id:
        return fail("missing_board_or_task_id", failure_class="terminal", retry_safe=False)
    if dry:
        return planned(board=board, task_id=task_id, result=result_text, repo=str(data.get("repo") or cond_get(request, "repo", "parse_issue_ref", "load_kanban_task") or ""))
    try:
        current = hermes_kanban_json(["--board", board, "list", "--json", "--sort", "created-desc"])
    except CommandError as exc:
        return fail("kanban_list_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), board=board, task_id=task_id, idempotency_key=f"kanban:{board}:{task_id}:complete", mutated=False)
    if not isinstance(current, list) or any(not isinstance(task, dict) for task in current):
        return fail(
            "kanban_list_failed",
            failure_class="terminal",
            retry_safe=False,
            board=board,
            task_id=task_id,
            error="invalid Kanban list read-back shape",
        )
    matching = [task for task in current if str(task.get("id") or task.get("task_id") or "") == task_id]
    if not matching:
        return fail("task_not_found", failure_class="terminal", retry_safe=False, board=board, task_id=task_id)
    if str(matching[0].get("status") or "").lower() in {"done", "completed", "archived"}:
        return ok(status="already_completed", board=board, task_id=task_id, repo=str(data.get("repo") or cond_get(request, "repo", "parse_issue_ref", "load_kanban_task") or ""), mutated=False)
    try:
        run_cmd(
            [
                "hermes", "kanban", "--board", board, "complete", task_id,
                "--result", result_text, "--summary", result_text,
            ],
            timeout=60,
        )
    except CommandError as exc:
        return fail(
            "complete_failed",
            failure_class="reconcile_then_retry",
            retry_safe=False,
            error=str(exc),
            stderr=(exc.stderr or "")[-400:],
            board=board,
            task_id=task_id,
            result=result_text,
            idempotency_key=f"kanban:{board}:{task_id}:complete",
            mutated=True,
        )
    return ok(status="completed", board=board, task_id=task_id, repo=str(data.get("repo") or cond_get(request, "repo", "parse_issue_ref", "load_kanban_task") or ""), result=result_text, idempotency_key=f"kanban:{board}:{task_id}:complete", mutated=True)


def refresh_clone_base(request: Request) -> Result:
    """Fetch origin only from a clean clone and read back exact base ref."""
    data = input_of(request)
    dry = dry_run_flag(request)
    clone_path = str(data.get("clone_path") or cond_get(request, "clone_path", "parse_issue_ref", "load_kanban_task") or cfg_of(request).get("clone_path") or "")
    base_branch = str(data.get("base_branch") or cfg_of(request).get("base_branch") or "main")
    if not clone_path:
        return fail("missing_clone_path", failure_class="terminal", retry_safe=False)
    if dry:
        return planned(clone_path=clone_path, base_branch=base_branch, actions=["fetch", "read_origin_ref"])
    clone = Path(clone_path)
    if not (clone / ".git").exists():
        return fail("clone_missing", failure_class="terminal", retry_safe=False, clone_path=clone_path, mutated=False)
    try:
        status = status_porcelain(clone)
        if status.strip():
            return fail("clone_dirty", failure_class="terminal", retry_safe=False, clone_path=clone_path, clone_status=status, mutated=False)
        origin = remote_url(clone)
        if not origin.strip():
            return fail("origin_missing", failure_class="terminal", retry_safe=False, clone_path=clone_path, mutated=False)
        git(["fetch", "origin", "--prune"], cwd=clone)
        base_head = remote_ref(clone, "origin", base_branch)
    except CommandError as exc:
        return fail("refresh_clone_check_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), clone_path=clone_path, base_branch=base_branch, mutated=False)
    return ok(status="refreshed", clone_path=clone_path, base_branch=base_branch, base_head=base_head, origin=origin, mutated=True)


def _identity_values(
    request: Request,
    keys: tuple[str, ...],
    aliases: tuple[str, ...] = _PROVENANCE_ALIASES,
) -> list[str]:
    """Collect every non-empty identity value available before this effector."""
    values: list[str] = []

    def add(source: Any) -> None:
        if not isinstance(source, dict):
            return
        for key in keys:
            value = source.get(key)
            if key in {"task_id", "task"} and isinstance(value, dict):
                value = value.get("id") or value.get("task_id")
            if value is None or not str(value).strip():
                continue
            normalized = str(value).strip()
            if normalized not in values:
                values.append(normalized)

    add(input_of(request))
    add(cfg_of(request))
    for source in _conduction_blobs(request, aliases):
        add(source)
    return values


def _worktree_provenance(request: Request, branch: str) -> dict[str, str]:
    """Resolve one complete ownership tuple from this path's conduction."""
    task_values = _identity_values(request, ("task_id", "task"))
    issue_values = _identity_values(request, ("issue",))
    receipt_values = _identity_values(request, ("receipt_id", "receipt_path"))
    repo_values = _identity_values(request, ("repo",))
    return {
        "task_id": task_values[0] if len(task_values) == 1 else "",
        "issue": issue_values[0] if len(issue_values) == 1 else "",
        "receipt": receipt_values[0] if len(receipt_values) == 1 else "",
        "repo": repo_values[0] if len(repo_values) == 1 else "",
        "branch": branch,
    }


def _worktree_provenance_error(request: Request, provenance: dict[str, str]) -> dict[str, Any]:
    fields = {
        "task_id": ("task_id", "task"),
        "issue": ("issue",),
        "receipt": ("receipt_id", "receipt_path"),
        "repo": ("repo",),
    }
    missing = [key for key, aliases in fields.items() if not _identity_values(request, aliases)]
    conflicts = {
        key: values
        for key, aliases in fields.items()
        if len(values := _identity_values(request, aliases)) > 1
    }
    if not provenance.get("branch"):
        missing.append("branch")
    return {"missing": missing, "conflicts": conflicts}


def _worktree_branch(request: Request) -> tuple[str, list[str]]:
    """Resolve branch from current input/config and upstream conduction."""
    aliases = (
        "triage_build_repair_prompt", "build_repair_prompt",
        "triage_repair_prepare_worktree", "repair_prepare_worktree",
        "dispatch_parse_issue_ref", "parse_issue_ref", "parse_issue_ref_from_task",
        "load_pr_fields", "triage_load_pr_fields",
    )
    values = _identity_values(request, ("branch",), aliases=aliases)
    for blob in _conduction_blobs(request, aliases):
        pr = blob.get("pr")
        candidates = [pr.get("headRefName")] if isinstance(pr, dict) else []
        candidates.append(blob.get("headRefName"))
        for candidate in candidates:
            value = str(candidate or "").strip()
            if value and value not in values:
                values.append(value)
    return (values[0] if len(values) == 1 else ""), values


def _branch_provenance(clone_path: str, branch: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for key in ("task", "issue", "receipt", "repo", "base"):
        try:
            values[key] = branch_config_get(clone_path, branch, f"repo-agent-{key}").strip()
        except CommandError:
            values[key] = ""
    return values

def _provenance_matches(expected: dict[str, str], actual: dict[str, str]) -> bool:
    """Accept reuse only when every recorded ownership field matches exactly."""
    return all(
        expected.get(expected_key, "")
        and expected.get(expected_key, "") == actual.get(actual_key, "")
        for expected_key, actual_key in (
            ("task_id", "task"),
            ("issue", "issue"),
            ("receipt", "receipt"),
            ("repo", "repo"),
        )
    )




def prepare_worktree(request: Request) -> Result:
    """Create/reuse a worktree only after clone, ref, and ownership read-backs."""
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    upstream = upstream_noop(
        request,
        "parse_issue_ref",
        "dispatch_parse_issue_ref",
        "triage_load_pr_fields",
        "load_pr_fields",
        "triage_build_repair_prompt",
        "build_repair_prompt",
    )
    if upstream:
        return noop(str(upstream.get("reason") or "no_ready_task"))
    clone_path = str(data.get("clone_path") or cond_get(request, "clone_path", "refresh_clone_base", "parse_issue_ref", "dispatch_parse_issue_ref", "load_kanban_task", "dispatch_load_kanban_task", "triage_load_pr_fields", "load_pr_fields") or cfg.get("clone_path") or "")
    branch, branch_values = _worktree_branch(request)
    if not branch and branch_values:
        branch = branch_values[0]
    if dry and not branch:
        branch = str(data.get("branch") or "").strip()
    worktree_root = str(data.get("worktree_root") or cfg.get("worktree_root") or "")
    base_branch = str(data.get("base_branch") or cfg.get("base_branch") or "main")
    expected_head = str(data.get("expected_head") or cond_get(request, "expected_head", "prepare_worktree", "repair_prepare_worktree", "triage_repair_prepare_worktree", "write_dispatch_receipt", "dispatch_write_dispatch_receipt") or "").strip()
    if not clone_path or not branch or not worktree_root:
        return fail("missing_clone_branch_or_root", failure_class="terminal", retry_safe=False)
    safe = re.sub(r"[^a-zA-Z0-9._/-]+", "-", branch)
    path = Path(worktree_root) / safe
    provenance = _worktree_provenance(request, branch)
    provenance_error = _worktree_provenance_error(request, provenance)
    if not dry and (provenance_error["missing"] or provenance_error["conflicts"] or len(branch_values) != 1):
        return fail(
            "conflicting_worktree_provenance" if provenance_error["conflicts"] or len(branch_values) != 1 else "missing_worktree_provenance",
            failure_class="terminal",
            retry_safe=False,
            branch=branch,
            branch_values=branch_values,
            provenance=provenance,
            **provenance_error,
            mutated=False,
        )
    if dry:
        return planned(clone_path=clone_path, branch=branch, worktree_path=str(path), base_branch=base_branch, create_branch=True, provenance=provenance)

    clone = Path(clone_path)
    if not (clone / ".git").exists():
        return fail("clone_missing", failure_class="terminal", retry_safe=False, clone_path=clone_path, mutated=False)
    # Read all clone state before any fetch, branch, or worktree mutation.
    try:
        clone_status = status_porcelain(clone)
        if clone_status.strip():
            return fail("clone_dirty", failure_class="terminal", retry_safe=False, clone_status=clone_status, clone_path=clone_path, mutated=False)
        origin = remote_url(clone)
        if not origin.strip():
            return fail("origin_missing", failure_class="terminal", retry_safe=False, clone_path=clone_path, mutated=False)
        git(["fetch", "origin", "--prune"], cwd=clone)
        base_head = remote_ref(clone, "origin", base_branch)
    except CommandError as exc:
        return fail("clone_ref_check_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), clone_path=clone_path, base_branch=base_branch, mutated=False)
    try:
        rows = parse_worktree_porcelain(worktree_list(clone_path))
    except CommandError as exc:
        return fail("worktree_list_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), worktree_path=str(path), branch=branch, mutated=False)
    resolved_path = str(path.resolve())
    matches = [row for row in rows if str(Path(row.get("path") or "").resolve()) == resolved_path]
    if matches:
        row = matches[0]
        actual_branch = str(row.get("branch") or "")
        if row.get("locked") or actual_branch != branch:
            return fail("worktree_provenance_mismatch", failure_class="terminal", retry_safe=False, worktree_path=str(path), branch=branch, actual_branch=actual_branch, locked=bool(row.get("locked")), mutated=False)
        if is_dirty(str(path)):
            return fail("worktree_dirty", failure_class="terminal", retry_safe=False, worktree_path=str(path), branch=branch, mutated=False)
        actual = _branch_provenance(clone_path, branch)
        if not _provenance_matches(provenance, actual):
            return fail("foreign_worktree_ownership", failure_class="terminal", retry_safe=False, worktree_path=str(path), branch=branch, provenance=provenance, actual_provenance=actual, mutated=False)
        try:
            head = rev_parse(str(path))
        except CommandError as exc:
            return fail("worktree_rev_parse_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), worktree_path=str(path), branch=branch, mutated=False)
        if expected_head and head != expected_head:
            return fail("worktree_head_mismatch", failure_class="terminal", retry_safe=False, expected_head=expected_head, head=head, mutated=False)
        return ok(status="reused", clone_path=clone_path, worktree_path=str(path), branch=branch, head=head, base_head=base_head, origin=origin, provenance=provenance, mutated=False)
    if path.exists():
        return fail("worktree_path_collision", failure_class="terminal", retry_safe=False, worktree_path=str(path), branch=branch, mutated=False)

    try:
        exists = branch_exists(clone_path, branch)
        if exists:
            actual = _branch_provenance(clone_path, branch)
            if not _provenance_matches(provenance, actual):
                return fail("foreign_branch_ownership", failure_class="terminal", retry_safe=False, branch=branch, provenance=provenance, actual_provenance=actual, mutated=False)
            current_head = local_branch_head(clone_path, branch)
            if expected_head and current_head != expected_head:
                return fail("branch_head_mismatch", failure_class="terminal", retry_safe=False, branch=branch, expected_head=expected_head, head=current_head, mutated=False)
            if not expected_head and current_head != base_head:
                return fail("branch_stale", failure_class="terminal", retry_safe=False, branch=branch, head=current_head, base_head=base_head, mutated=False)
            worktree_add(clone_path, str(path), branch, create_branch=False)
        else:
            git(["branch", branch, base_head], cwd=clone)
            for key, value in (("task", provenance["task_id"]), ("issue", provenance["issue"]), ("receipt", provenance["receipt"]), ("repo", provenance["repo"]), ("base", base_head)):
                if value:
                    branch_config_set(clone_path, branch, f"repo-agent-{key}", value)
            worktree_add(clone_path, str(path), branch, create_branch=False)
    except CommandError as exc:
        return fail("worktree_prepare_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), worktree_path=str(path), branch=branch, mutated=True)
    try:
        head = rev_parse(str(path))
    except CommandError as exc:
        return fail("worktree_rev_parse_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), worktree_path=str(path), branch=branch, mutated=True)
    if head != base_head:
        return fail("worktree_base_mismatch", failure_class="terminal", retry_safe=False, head=head, base_head=base_head, worktree_path=str(path), branch=branch, mutated=True)
    return ok(status="prepared", clone_path=clone_path, worktree_path=str(path), branch=branch, head=head, base_head=base_head, origin=origin, provenance=provenance, mutated=True)


def _omp_diff_paths(worktree_path: str) -> list[str]:
    """Return changed paths reported by git, including untracked files."""
    status = git(
        ["status", "--porcelain=v1", "--untracked-files=all"], cwd=worktree_path
    )
    paths: list[str] = []
    for line in status.splitlines():
        value = line[3:] if len(line) >= 3 else line
        if " -> " in value:
            value = value.rsplit(" -> ", 1)[-1]
        if value:
            paths.append(value)
    for args in (["diff", "--name-only", "HEAD"], ["diff", "--cached", "--name-only"]):
        paths.extend(p for p in git(args, cwd=worktree_path).splitlines() if p)
    return paths


def _escaped_omp_paths(worktree_path: str, paths: list[str]) -> list[str]:
    root = Path(worktree_path).resolve()
    escaped: list[str] = []
    for value in paths:
        path = Path(value)
        candidate = path.resolve() if path.is_absolute() else (root / path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            escaped.append(value)
    return escaped

def run_omp_worker(request: Request) -> Result:
    """Single OMP invocation in a worktree (no PR open, no labels)."""
    data = input_of(request)
    cfg = cfg_of(request)
    gated = _repair_action_gate(request)
    if gated is not None:
        return gated
    dry = dry_run_flag(request)
    upstream = upstream_noop(request, "prepare_worktree", "parse_issue_ref")
    if upstream:
        return noop(str(upstream.get("reason") or "no_ready_task"))
    worktree_path = str(data.get("worktree_path") or cond_get(request, "worktree_path", "prepare_worktree") or "")
    prompt = str(data.get("prompt") or cond_get(request, "prompt", "build_repair_prompt") or "")
    if not prompt:
        parsed = cond_blob(request, "parse_issue_ref", "parse_issue_ref_from_task")
        if parsed.get("repo") and parsed.get("issue"):
            prompt = (f"Implement a minimal fix for {parsed['repo']}#{parsed['issue']}.\n"
                      f"Branch: {parsed.get('branch') or ''}\n"
                      f"Task: {parsed.get('task_title') or ''}\n"
                      "Keep scope tight. Do not force-push. Do not open/merge PRs.\n")
    command = str(data.get("command") or cfg.get("executor_command") or "omp")
    model = str(data.get("model") or cfg.get("model") or "omniroute/omp/default")
    thinking = str(data.get("thinking") or cfg.get("thinking") or "medium")
    timeout = float(data.get("timeout_seconds") or cfg.get("timeout_seconds") or 1800)

    intended_branch = str(data.get("branch") or cond_get(request, "branch", "prepare_worktree") or cond_get(request, "branch", "parse_issue_ref") or "")
    clone_path = str(data.get("clone_path") or cond_get(request, "clone_path", "refresh_clone_base", "prepare_worktree") or cfg.get("clone_path") or "")
    base_branch = str(data.get("base_branch") or cfg.get("base_branch") or "main")
    if not worktree_path or not prompt:
        return fail("missing_worktree_or_prompt", failure_class="terminal", retry_safe=False)
    if not dry and not bool(data.get("executor_enabled", cfg.get("executor_enabled", True))):
        return fail("executor_disabled", failure_class="terminal", retry_safe=False)
    if not dry:
        try:
            if git(["rev-parse", "--is-inside-work-tree"], cwd=worktree_path) != "true":
                return fail("omp_worktree_invalid", failure_class="terminal", retry_safe=False)
            top_level = git(["rev-parse", "--show-toplevel"], cwd=worktree_path)
            if not top_level or Path(top_level).resolve() != Path(worktree_path).resolve():
                return fail("omp_worktree_confinement", failure_class="terminal", retry_safe=False, top_level=top_level)
            if not intended_branch:
                return fail("omp_postcondition_failed", failure_class="terminal", retry_safe=False, detail="missing_intended_branch")
            pre_branch = git(["branch", "--show-current"], cwd=worktree_path)
            pre_head = rev_parse(worktree_path)
            base = rev_parse(clone_path or worktree_path, f"origin/{base_branch}")
            if pre_branch != intended_branch:
                return fail("omp_branch_mismatch", failure_class="terminal", retry_safe=False, expected_branch=intended_branch, actual_branch=pre_branch)
        except CommandError as exc:
            return fail("omp_worktree_invalid", failure_class="terminal", retry_safe=False, error=str(exc))
    try:
        out = run_omp(
            prompt=prompt,
            cwd=worktree_path,
            command=command,
            model=model,
            thinking=thinking,
            timeout=timeout,
            dry_run=dry,
        )

    except CommandError as exc:
        return fail("omp_failed", failure_class="terminal", retry_safe=False, error=str(exc), stderr=exc.stderr[-500:], mutated=True)
    if dry:
        return planned(worktree_path=worktree_path, model=model, omp=out, prompt_len=out.get("prompt_len"))
    try:
        post_top_level = git(["rev-parse", "--show-toplevel"], cwd=worktree_path)
        if not post_top_level or Path(post_top_level).resolve() != Path(worktree_path).resolve():
            return fail("omp_worktree_confinement", failure_class="terminal", retry_safe=False, top_level=post_top_level)
        post_branch = git(["branch", "--show-current"], cwd=worktree_path)
        if post_branch != intended_branch:
            return fail("omp_branch_mismatch", failure_class="terminal", retry_safe=False, expected_branch=intended_branch, actual_branch=post_branch)
        post_head = rev_parse(worktree_path)
        if post_head == pre_head:
            return fail("omp_head_unchanged", failure_class="terminal", retry_safe=False, head=post_head, pre_head=pre_head)
        if post_head == base:
            return fail("omp_no_new_commits", failure_class="terminal", retry_safe=False, head=post_head, base=base)
        escaped = _escaped_omp_paths(worktree_path, _omp_diff_paths(worktree_path))
        if escaped:
            return fail("omp_diff_path_escape", failure_class="terminal", retry_safe=False, paths=escaped)
    except CommandError as exc:
        return fail("omp_postcondition_failed", failure_class="terminal", retry_safe=False, error=str(exc))
    except (OSError, ValueError) as exc:
        return fail("omp_postcondition_failed", failure_class="terminal", retry_safe=False, error=str(exc))
    return ok(status="omp_finished", worktree_path=worktree_path, model=model, omp=out, pre_head=pre_head, head=post_head, base=base, branch=post_branch, mutated=True)


def verify_branch_has_commits(request: Request) -> Result:
    """Check worktree HEAD differs from base_branch without reading planned paths."""
    upstream = upstream_noop(request, "prepare_worktree", "run_omp")
    if upstream:
        return noop(str(upstream.get("reason") or "no_ready_task"))
    data = input_of(request)
    worktree_path = str(
        data.get("worktree_path")
        or cond_get(request, "worktree_path", "prepare_worktree", "run_omp")
        or ""
    )
    clone_path = str(
        data.get("clone_path")
        or cond_get(request, "clone_path", "refresh_clone_base", "prepare_worktree")
        or cfg_of(request).get("clone_path")
        or ""
    )
    base_branch = str(data.get("base_branch") or cfg_of(request).get("base_branch") or "main")
    if not worktree_path:
        return fail("missing_worktree_path", failure_class="terminal", retry_safe=False)
    if dry_run_flag(request):
        return planned(worktree_path=worktree_path, clone_path=clone_path, base_branch=base_branch)
    try:
        head = rev_parse(worktree_path)
        base = rev_parse(clone_path or worktree_path, f"origin/{base_branch}")
    except CommandError as exc:
        return fail("rev_parse_failed", failure_class="retryable_read", retry_safe=True, error=str(exc))
    if head == base:
        return fail("no_new_commits", failure_class="terminal", retry_safe=False, head=head, base=base)
    return ok(status="has_commits", head=head, base=base)


def open_pull_request(request: Request) -> Result:
    """Open one PR for branch → base (gh pr create)."""
    import json

    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    parsed = cond_blob(request, "parse_issue_ref", "parse_issue_ref_from_task")
    upstream = upstream_noop(request, "prepare_worktree", "verify_branch", "parse_issue_ref")
    if upstream:
        return noop(str(upstream.get("reason") or "no_ready_task"))
    prep = cond_blob(request, "prepare_worktree")
    repo = str(data.get("repo") or parsed.get("repo") or "")
    branch = str(data.get("branch") or prep.get("branch") or parsed.get("branch") or "")
    base = str(data.get("base_branch") or cfg.get("base_branch") or "main")
    issue = parsed.get("issue") or data.get("issue")
    title = str(data.get("title") or (f"fix: {repo}#{issue}" if repo and issue else f"fix: {branch}"))
    body = str(
        data.get("body")
        or (
            f"Automated fix for {repo}#{issue} via repo-agent.\n\nCloses #{issue}\n"
            if issue
            else "Automated fix via repo-agent."
        )
    )
    if not repo or not branch:
        return fail("missing_repo_or_branch", failure_class="terminal", retry_safe=False)
    gh = str(cfg.get("gh_cli") or "gh")
    if dry:
        return planned(repo=repo, branch=branch, base=base, title=title)

    def list_open() -> list[dict[str, Any]]:
        proc = run_cmd(
            [gh, "pr", "list", "--repo", repo, "--head", branch, "--base", base, "--state", "open", "--json", "number,url,baseRefName,headRefName"],
        )
        listed = json.loads(proc.stdout or "[]")
        if not isinstance(listed, list) or any(not isinstance(row, dict) for row in listed):
            raise ValueError("invalid pull request list")
        return listed

    try:
        existing = list_open()
    except CommandError as exc:
        return fail("pr_create_failed", failure_class="retryable_read", retry_safe=True, repo=repo, branch=branch, base=base, idempotency_key=f"pr:{repo}:head:{branch}:base:{base}:create", error=str(exc))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return fail("invalid_pr_list", failure_class="terminal", retry_safe=False, error=str(exc))
    if len(existing) > 1:
        return fail("ambiguous_existing_prs", failure_class="terminal", retry_safe=False, repo=repo, branch=branch, prs=existing)
    if existing:
        pr = existing[0]
        return ok(status="exists", repo=repo, branch=branch, base=base, number=pr.get("number"), url=pr.get("url"), idempotency_key=f"pr:{repo}:head:{branch}:base:{base}:create", mutated=False)

    try:
        proc = run_cmd(
            [gh, "pr", "create", "--repo", repo, "--base", base, "--head", branch, "--title", title, "--body", body],
            timeout=120,
        )
    except CommandError as exc:
        try:
            reconciled = list_open()
        except (CommandError, json.JSONDecodeError, TypeError, ValueError):
            reconciled = []
        if len(reconciled) == 1:
            pr = reconciled[0]
            return ok(status="exists", repo=repo, branch=branch, base=base, number=pr.get("number"), url=pr.get("url"), idempotency_key=f"pr:{repo}:head:{branch}:base:{base}:create", reconciled=True, mutated=True)
        if len(reconciled) > 1:
            return fail("ambiguous_pr_create", failure_class="terminal", retry_safe=False, repo=repo, branch=branch, base=base, prs=reconciled, idempotency_key=f"pr:{repo}:head:{branch}:base:{base}:create", mutated=True)
        return fail("pr_create_unresolved", failure_class="reconcile_then_retry", retry_safe=False, repo=repo, branch=branch, base=base, idempotency_key=f"pr:{repo}:head:{branch}:base:{base}:create", error=str(exc), mutated=True)

    try:
        listed = list_open()
    except CommandError as exc:
        return fail("pr_created_but_unresolved", failure_class="reconcile_then_retry", retry_safe=False, repo=repo, branch=branch, base=base, idempotency_key=f"pr:{repo}:head:{branch}:base:{base}:create", error=str(exc), mutated=True)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return fail("pr_created_but_unresolved", failure_class="terminal", retry_safe=False, repo=repo, branch=branch, base=base, idempotency_key=f"pr:{repo}:head:{branch}:base:{base}:create", error=str(exc), mutated=True)
    if len(listed) != 1:
        if len(listed) > 1:
            return fail("ambiguous_pr_create", failure_class="terminal", retry_safe=False, repo=repo, branch=branch, base=base, prs=listed, idempotency_key=f"pr:{repo}:head:{branch}:base:{base}:create", mutated=True)
        return fail("pr_created_but_unresolved", failure_class="reconcile_then_retry", retry_safe=False, repo=repo, branch=branch, base=base, prs=listed, idempotency_key=f"pr:{repo}:head:{branch}:base:{base}:create", mutated=True)
    pr = listed[0]
    number = pr.get("number")
    if not number:
        return fail("pr_created_but_unresolved", failure_class="reconcile_then_retry", retry_safe=False, repo=repo, branch=branch, base=base, idempotency_key=f"pr:{repo}:head:{branch}:base:{base}:create", mutated=True)
    return ok(status="created", repo=repo, branch=branch, base=base, number=number, url=pr.get("url"), idempotency_key=f"pr:{repo}:head:{branch}:base:{base}:create", mutated=True)


def apply_pr_labels(request: Request) -> Result:
    """Add labels to an existing PR (e.g. ai:generated, ai:pr-opened)."""
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    upstream = upstream_noop(request, "open_pull_request", "parse_issue_ref")
    if upstream:
        return noop(str(upstream.get("reason") or "no_ready_task"))
    opened = cond_blob(request, "open_pull_request", "open_pr")
    parsed = cond_blob(request, "parse_issue_ref", "parse_issue_ref_from_task")
    repo = str(data.get("repo") or opened.get("repo") or parsed.get("repo") or "")
    number = int(
        data.get("number")
        or data.get("pr_number")
        or opened.get("number")
        or 0
    )
    labels = data.get("labels") or ["ai:generated", "ai:pr-opened"]
    gh = str(cfg.get("gh_cli") or "gh")
    if not repo or not number:
        return fail("missing_repo_or_number", failure_class="terminal", retry_safe=False)
    if dry:
        return planned(repo=repo, number=number, labels=list(labels))
    applied = []
    label_key = f"pr:{repo}:{number}:labels:{','.join(sorted(str(label) for label in labels))}"
    for label in labels:
        try:
            run_cmd(
                [
                    gh,
                    "pr",
                    "edit",
                    str(number),
                    "--repo",
                    repo,
                    "--add-label",
                    str(label),
                ],
                timeout=60,
            )
            applied.append({"label": label, "ok": True})
        except CommandError as exc:
            applied.append({"label": label, "ok": False, "error": exc.stderr[-200:]})
    if not any(a["ok"] for a in applied):
        return fail("all_labels_failed", failure_class="reconcile_then_retry", retry_safe=False, repo=repo, number=number, labels=list(labels), actions=applied, idempotency_key=label_key, mutated=True)
    if any(not a["ok"] for a in applied):
        return fail("partial_labels_failed", failure_class="reconcile_then_retry", retry_safe=False, repo=repo, number=number, labels=list(labels), actions=applied, idempotency_key=label_key, mutated=True)
    return ok(status="labeled", repo=repo, number=number, labels=list(labels), actions=applied, idempotency_key=label_key, mutated=True)


_UPSTREAM_TERMINAL = {"failed", "cancelled", "timed_out"}


def _receipt_metadata(request: Request, payload: dict[str, Any], *, entity: dict[str, Any]) -> dict[str, Any]:
    data = input_of(request)
    cfg = cfg_of(request)
    def first(key: str, default: Any = "") -> Any:
        value = data.get(key)
        if value in (None, ""):
            value = cfg.get(key)
        if value in (None, ""):
            value = request.get(key)
        if value in (None, ""):
            value = payload.get(key, default)
        return value
    run_id = first("run_id")
    path_id = first("path_id")
    process_id = first("process_id")
    candidate = first("candidate")
    timestamp = first("timestamp", payload.get("timestamp", "unspecified"))
    if not any((run_id, path_id, process_id, candidate, cfg, entity)):
        return {}
    return {
        "run_id": str(run_id),
        "path_id": str(path_id),
        "process_id": str(process_id),
        "candidate": candidate,
        "config": dict(cfg),
        "entity": dict(entity),
        "timestamp": str(timestamp),
    }


def _terminal_upstream(request: Request, *effector_ids: str) -> Result | None:
    for effector_id in effector_ids:
        blob = cond_blob(request, effector_id)
        if blob and (blob.get("ok") is False or blob.get("status") in _UPSTREAM_TERMINAL):
            return fail("upstream_failed", failure_class="terminal", retry_safe=False, upstream=blob, mutated=False)
    return None


def write_dispatch_receipt(request: Request) -> Result:
    """Write a receipt once, using durable atomic no-clobber publication."""
    data = input_of(request)
    dry = dry_run_flag(request)
    terminal = _terminal_upstream(
        request, "parse_issue_ref", "prepare_worktree", "open_pull_request", "apply_pr_labels"
    )
    if terminal is not None:
        return terminal
    upstream = upstream_noop(
        request, "parse_issue_ref", "prepare_worktree", "open_pull_request", "apply_pr_labels"
    )
    if upstream:
        return noop(str(upstream.get("reason") or "no_ready_task"))
    path = str(data.get("receipt_path") or cfg_of(request).get("receipt_path") or "")
    payload = data.get("payload")
    if not isinstance(payload, dict) or not payload:
        parsed = cond_blob(request, "parse_issue_ref", "parse_issue_ref_from_task")
        opened = cond_blob(request, "open_pull_request", "open_pr")
        prep = cond_blob(request, "prepare_worktree")
        payload = {
            "phase": "DISPATCHED",
            "repo": parsed.get("repo") or opened.get("repo"),
            "issue": parsed.get("issue"),
            "branch": prep.get("branch") or parsed.get("branch"),
            "pr_number": opened.get("number"),
            "pr_url": opened.get("url"),
            "worktree_path": prep.get("worktree_path"),
            "dry_run": dry,
        }
    else:
        payload = dict(payload)
    parsed = cond_blob(request, "parse_issue_ref", "parse_issue_ref_from_task")
    opened = cond_blob(request, "open_pull_request", "open_pr")
    prep = cond_blob(request, "prepare_worktree")
    entity = {
        "repo": payload.get("repo") or parsed.get("repo") or opened.get("repo"),
        "issue": payload.get("issue") or parsed.get("issue"),
        "branch": payload.get("branch") or prep.get("branch") or parsed.get("branch"),
        "pr_number": payload.get("pr_number") or opened.get("number"),
    }
    payload.update(_receipt_metadata(request, payload, entity=entity))
    if not path:
        return fail("missing_receipt_path", failure_class="terminal", retry_safe=False)
    p = Path(path)

    def existing_result() -> Result | None:
        if not p.exists():
            return None
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return fail("receipt_conflict", failure_class="terminal", retry_safe=False, error=str(exc), receipt_path=path)
        if existing == payload:
            try:
                dir_fd = os.open(str(p.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError as exc:
                return fail("receipt_durability_unconfirmed", failure_class="terminal", retry_safe=False, error=str(exc), receipt_path=path)
            return ok(status="exists", receipt_path=path, payload=payload, mutated=False)
        return fail("receipt_conflict", failure_class="terminal", retry_safe=False, receipt_path=path)

    prior = existing_result()
    if prior is not None:
        return prior
    if dry:
        return planned(receipt_path=path, payload=payload)

    tmp_path: Path | None = None
    published_identity: tuple[int, int] | None = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
            published = os.fstat(fh.fileno())
            published_identity = (published.st_dev, published.st_ino)
        try:
            os.link(tmp_path, p)
        except FileExistsError:
            prior = existing_result()
            if prior is not None:
                return prior
            return fail("receipt_conflict", failure_class="terminal", retry_safe=False, receipt_path=path)
        os.unlink(tmp_path)
        tmp_path = None
        dir_fd = os.open(str(p.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        if not p.is_file() or json.loads(p.read_text(encoding="utf-8")) != payload:
            raise ValueError("receipt read-back mismatch")
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        rollback_error: Exception | None = None
        if published_identity is not None:
            try:
                current = p.stat()
                if (current.st_dev, current.st_ino) == published_identity:
                    os.unlink(p)
                dir_fd = os.open(str(p.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception as rollback_exc:
                rollback_error = rollback_exc
        error = str(exc)
        if rollback_error is not None:
            error = f"{error}; receipt rollback durability unconfirmed: {rollback_error}"
        return fail("receipt_write_failed", failure_class="terminal", retry_safe=False, error=error, receipt_path=path, mutated=True)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
    return ok(status="written", receipt_path=path, payload=payload, mutated=True)


def check_worktree_dirty(request: Request) -> Result:
    """Atomic read: is worktree dirty (status --porcelain non-empty)?"""
    data = input_of(request)
    worktree_path = str(data.get("worktree_path") or "")
    if not worktree_path:
        return fail("missing_worktree_path", failure_class="terminal", retry_safe=False, mutated=False)
    if not Path(worktree_path).exists():
        return fail("worktree_missing", failure_class="terminal", retry_safe=False, worktree_path=worktree_path, mutated=False)
    dirty = is_dirty(worktree_path)
    return ok(status="checked", worktree_path=worktree_path, dirty=dirty)


def list_controlled_worktrees(request: Request) -> Result:
    """List git worktrees under clone; optionally filter by worktree_root prefix."""
    data = input_of(request)
    cfg = cfg_of(request)
    clone_path = str(data.get("clone_path") or "")
    worktree_root = str(data.get("worktree_root") or cfg.get("worktree_root") or "")
    if not clone_path:
        return fail("missing_clone_path", failure_class="terminal", retry_safe=False, mutated=False)
    try:
        text = worktree_list(clone_path)
    except CommandError as exc:
        return fail("worktree_list_failed", failure_class="terminal", retry_safe=False, error=str(exc), mutated=False)
    rows = parse_worktree_porcelain(text)
    if worktree_root:
        root = str(Path(worktree_root).resolve()) if Path(worktree_root).exists() else worktree_root
        filtered = []
        for row in rows:
            p = row.get("path") or ""
            if p == clone_path:
                continue
            if p.startswith(worktree_root) or p.startswith(root):
                filtered.append(row)
        rows = filtered
    return ok(status="listed", clone_path=clone_path, count=len(rows), worktrees=rows)


def push_branch(request: Request) -> Result:
    """Atomic push with local/remote OID reconciliation and no force push."""
    data = input_of(request)
    dry = dry_run_flag(request)
    prep = cond_blob(request, "prepare_worktree")
    parsed = cond_blob(request, "parse_issue_ref", "parse_issue_ref_from_task")
    worktree_path = str(data.get("worktree_path") or prep.get("worktree_path") or "")
    gated = _repair_action_gate(request)
    if gated is not None:
        return gated
    upstream = upstream_noop(request, "prepare_worktree", "verify_branch", "parse_issue_ref")
    if upstream:
        return noop(str(upstream.get("reason") or "no_ready_task"))
    branch = str(data.get("branch") or prep.get("branch") or parsed.get("branch") or "")
    if not worktree_path or not branch:
        return fail("missing_worktree_or_branch", failure_class="terminal", retry_safe=False)
    if dry:
        return planned(worktree_path=worktree_path, branch=branch, remote="origin")
    local_oid = ""
    remote_oid = ""
    out = ""
    push_key = f"push:origin:{branch}:unknown"
    try:
        # Real worktrees are always verified. Test/planning adapters may pass a
        # non-existent synthetic path, for which no local OID is observable.
        if Path(worktree_path).exists():
            local_oid = rev_parse(worktree_path)
            push_key = f"push:origin:{branch}:{local_oid or 'unknown'}"
            out = git_push_branch(worktree_path, branch, set_upstream=True)
            remote_line = git(["ls-remote", "origin", f"refs/heads/{branch}"], cwd=worktree_path)
            remote_oid = (remote_line.split()[0] if remote_line.split() else "")
            if not remote_oid or remote_oid != local_oid:
                return fail(
                    "push_readback_mismatch",
                    failure_class="terminal",
                    retry_safe=False,
                    worktree_path=worktree_path,
                    branch=branch,
                    local_oid=local_oid,
                    remote_oid=remote_oid,
                    idempotency_key=push_key,
                    mutated=True,
                )
        else:
            return fail(
                "worktree_missing",
                failure_class="terminal",
                retry_safe=False,
                worktree_path=worktree_path,
                branch=branch,
                idempotency_key=push_key,
                mutated=False,
            )
    except CommandError as exc:
        return fail("push_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), stderr=(exc.stderr or "")[-500:], worktree_path=worktree_path, branch=branch, local_oid=local_oid, remote_oid=remote_oid, idempotency_key=push_key, mutated=True)
    return ok(
        status="pushed",
        worktree_path=worktree_path,
        branch=branch,
        remote="origin",
        local_oid=local_oid,
        remote_oid=remote_oid,
        idempotency_key=push_key,
        stdout_tail=(out or "")[-400:],
        mutated=True,
    )


def apply_issue_labels(request: Request) -> Result:
    """Atomic: add labels to a GitHub issue (e.g. ai:in-progress finish/block)."""
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    repo = str(data.get("repo") or "")
    issue = int(data.get("issue") or data.get("number") or 0)
    labels = data.get("labels") or []
    gh = str(cfg.get("gh_cli") or "gh")
    if not repo or not issue:
        return fail("missing_repo_or_issue", failure_class="terminal", retry_safe=False)
    if not labels:
        return fail("missing_labels", failure_class="terminal", retry_safe=False)
    if dry:
        return planned(repo=repo, issue=issue, labels=list(labels))
    applied = []
    for label in labels:
        try:
            run_cmd(
                [
                    gh,
                    "issue",
                    "edit",
                    str(issue),
                    "--repo",
                    repo,
                    "--add-label",
                    str(label),
                ],
                timeout=60,
            )
            applied.append({"label": label, "ok": True})
        except CommandError as exc:
            applied.append({"label": label, "ok": False, "error": exc.stderr[-200:]})
    if not any(a["ok"] for a in applied):
        return fail("all_labels_failed", failure_class="terminal", retry_safe=False, repo=repo, issue=issue, labels=list(labels), actions=applied, idempotency_key=f"issue:{repo}:{issue}:labels", mutated=bool(applied))
    if any(not a["ok"] for a in applied):
        return fail("partial_labels_failed", failure_class="terminal", retry_safe=False, repo=repo, issue=issue, labels=list(labels), actions=applied, idempotency_key=f"issue:{repo}:{issue}:labels", mutated=True)
    return ok(status="labeled", repo=repo, issue=issue, labels=list(labels), actions=applied, idempotency_key=f"issue:{repo}:{issue}:labels", mutated=True)
