"""Mega-atomic effectors: issue → PR domain."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from dataclasses import dataclass

from fala.adapters import EffectorRunRequest, EffectorRunResult

from repo_agent.adapters_cli import CommandError, hermes_kanban_json, run_cmd
from repo_agent.adapters_git import (
    branch_exists,
    git,
    is_dirty,
    parse_worktree_porcelain,
    push_branch as git_push_branch,
    rev_parse,
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


def resolve_kanban_task_id_after_create(
    *,
    board: str,
    task_title: str,
    stdout: str = "",
) -> str | None:
    """Backward-compatible task-id-only wrapper around typed read-back."""
    return resolve_kanban_task_id_after_create_result(
        board=board, task_title=task_title, stdout=stdout
    ).task_id


def load_kanban_task(request: EffectorRunRequest) -> EffectorRunResult:
    """Read one Kanban task by id (or first ready [fix-pr]/[issue])."""
    data = input_of(request)
    cfg = cfg_of(request)
    board = str(data.get("board") or cfg.get("board") or "")
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
    if task_id and not matches:
        return fail("task_not_found", failure_class="terminal", retry_safe=False, task_id=task_id, board=board)
    if task_id and matches:
        return ok(status="loaded", task=matches[0], board=board)
    # pick first ready-ish fix-pr / issue
    for t in tasks:
        if str(t.get("status") or "") in ("done", "archived"):
            continue
        title = str(t.get("title") or "")
        if title.startswith(("[fix-pr]", "[issue]", "[fix-pr-review]")):
            return ok(status="loaded", task=t, board=board)
    return noop("no_ready_task", board=board)


def parse_issue_ref_from_task(request: EffectorRunRequest) -> EffectorRunResult:
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
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", title.lower())[:40].strip("-")
    branch_prefix = str(cfg_of(request).get("branch_prefix") or "ai/fix")
    branch = f"{branch_prefix}/{issue}-{slug or 'task'}"
    return ok(
        status="parsed",
        repo=repo,
        issue=issue,
        branch=branch,
        task_id=task.get("id") or task.get("task_id"),
        task_title=title,
    )


def create_fix_pr_task(request: EffectorRunRequest) -> EffectorRunResult:
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
    return ok(
        status="created",
        title=task_title,
        board=board,
        task_id=task_id,
        stdout=(proc.stdout or "")[-400:],
        mutated=True,
    )


def complete_kanban_task(request: EffectorRunRequest) -> EffectorRunResult:
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
    upstream = upstream_noop(request, "load_kanban_task", "parse_issue_ref", "write_dispatch_receipt")
    if upstream:
        return noop(str(upstream.get("reason") or "no_ready_task"))
    result_text = str(data.get("result") or "completed")
    if not board or not task_id:
        return fail("missing_board_or_task_id", failure_class="terminal", retry_safe=False)
    if dry:
        return planned(board=board, task_id=task_id, result=result_text)
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
        return ok(status="already_completed", board=board, task_id=task_id, mutated=False)
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
    return ok(status="completed", board=board, task_id=task_id, result=result_text, idempotency_key=f"kanban:{board}:{task_id}:complete", mutated=True)


def refresh_clone_base(request: EffectorRunRequest) -> EffectorRunResult:
    """Fetch origin and ensure clone is a git worktree root."""
    data = input_of(request)
    dry = dry_run_flag(request)
    clone_path = str(
        data.get("clone_path")
        or cond_get(request, "clone_path", "parse_issue_ref", "load_kanban_task")
        or cfg_of(request).get("clone_path")
        or ""
    )
    base_branch = str(data.get("base_branch") or cfg_of(request).get("base_branch") or "main")
    if not clone_path:
        return fail("missing_clone_path", failure_class="terminal", retry_safe=False)
    if dry:
        return planned(clone_path=clone_path, base_branch=base_branch, actions=["fetch", "checkout"])
    if not Path(clone_path, ".git").exists() and not Path(clone_path, ".git").is_file():
        if not Path(clone_path).exists():
            return fail("clone_missing", failure_class="terminal", retry_safe=False, clone_path=clone_path)
    try:
        git(["fetch", "origin", "--prune"], cwd=clone_path)
    except CommandError as exc:
        return fail("refresh_fetch_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), clone_path=clone_path, base_branch=base_branch, idempotency_key=f"git:{clone_path}:fetch-origin", mutated=True)
    try:
        git(["rev-parse", "--verify", f"origin/{base_branch}"], cwd=clone_path)
    except CommandError as exc:
        return fail("refresh_rev_parse_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), clone_path=clone_path, base_branch=base_branch, idempotency_key=f"git:{clone_path}:fetch-origin", mutated=True)
    return ok(status="refreshed", clone_path=clone_path, base_branch=base_branch, mutated=True)


def prepare_worktree(request: EffectorRunRequest) -> EffectorRunResult:
    """Create or reattach a controlled worktree for branch under worktree_root."""
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    upstream = upstream_noop(request, "parse_issue_ref")
    if upstream:
        return noop(str(upstream.get("reason") or "no_ready_task"))
    clone_path = str(
        data.get("clone_path")
        or cond_get(
            request,
            "clone_path",
            "refresh_clone_base",
            "parse_issue_ref",
            "load_kanban_task",
        )
        or cfg.get("clone_path")
        or ""
    )
    branch = str(
        data.get("branch")
        or cond_get(
            request,
            "branch",
            "parse_issue_ref",
            "load_pr_fields",
            "build_repair_prompt",
        )
        or ""
    )
    # PR head branch from triage load
    if not branch:
        pr = cond_get(request, "pr", "load_pr_fields")
        if isinstance(pr, dict):
            branch = str(pr.get("headRefName") or "")
    worktree_root = str(data.get("worktree_root") or cfg.get("worktree_root") or "")
    base_branch = str(data.get("base_branch") or cfg.get("base_branch") or "main")
    if not clone_path or not branch or not worktree_root:
        return fail("missing_clone_branch_or_root", failure_class="terminal", retry_safe=False)
    safe = re.sub(r"[^a-zA-Z0-9._/-]+", "-", branch)
    path = str(Path(worktree_root) / safe)
    if dry:
        return planned(
            clone_path=clone_path,
            branch=branch,
            worktree_path=path,
            create_branch=True,
        )
    Path(worktree_root).mkdir(parents=True, exist_ok=True)
    if Path(path).exists():
        try:
            rows = parse_worktree_porcelain(worktree_list(clone_path))
        except CommandError as exc:
            return fail("worktree_list_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), worktree_path=path, branch=branch, mutated=False)
        matching = [row for row in rows if str(row.get("path") or "") == str(Path(path))]
        actual_branch = str(matching[0].get("branch") or "").removeprefix("refs/heads/") if matching else ""
        if actual_branch != branch:
            return fail("worktree_provenance_mismatch", failure_class="terminal", retry_safe=False, worktree_path=path, branch=branch, actual_branch=actual_branch, mutated=False)
        if is_dirty(path):
            return fail("worktree_dirty", failure_class="terminal", retry_safe=False, worktree_path=path, branch=branch, mutated=False)
        try:
            head = rev_parse(path)
        except CommandError as exc:
            return fail("worktree_rev_parse_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), worktree_path=path, branch=branch, mutated=False)
        return ok(status="reused", worktree_path=path, branch=branch, head=head, mutated=False)
    try:
        exists = branch_exists(clone_path, branch)
    except CommandError as exc:
        return fail("branch_exists_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), clone_path=clone_path, branch=branch, mutated=False)
    try:
        if exists:
            worktree_add(clone_path, path, branch, create_branch=False)
        else:
            git(["branch", branch, f"origin/{base_branch}"], cwd=clone_path)
            worktree_add(clone_path, path, branch, create_branch=False)
    except CommandError as exc:
        return fail("worktree_prepare_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), worktree_path=path, branch=branch, mutated=True)
    try:
        head = rev_parse(path)
    except CommandError as exc:
        return fail("worktree_rev_parse_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), worktree_path=path, branch=branch, mutated=True)
    return ok(status="prepared", worktree_path=path, branch=branch, head=head, mutated=True)


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

def run_omp_worker(request: EffectorRunRequest) -> EffectorRunResult:
    """Single OMP invocation in a worktree (no PR open, no labels)."""
    data = input_of(request)
    cfg = cfg_of(request)
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
    model = str(data.get("model") or cfg.get("model") or "omniroute/omp/default")
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
        out = run_omp(prompt=prompt, cwd=worktree_path, model=model, timeout=timeout, dry_run=dry)
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


def verify_branch_has_commits(request: EffectorRunRequest) -> EffectorRunResult:
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


def open_pull_request(request: EffectorRunRequest) -> EffectorRunResult:
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


def apply_pr_labels(request: EffectorRunRequest) -> EffectorRunResult:
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


def write_dispatch_receipt(request: EffectorRunRequest) -> EffectorRunResult:
    """Write a receipt once, using durable atomic no-clobber publication."""
    data = input_of(request)
    dry = dry_run_flag(request)
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
    if not path:
        return fail("missing_receipt_path", failure_class="terminal", retry_safe=False)
    p = Path(path)

    def existing_result() -> EffectorRunResult | None:
        if not p.exists():
            return None
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return fail("receipt_conflict", failure_class="terminal", retry_safe=False, error=str(exc), receipt_path=path)
        if existing == payload:
            return ok(status="exists", receipt_path=path, payload=payload, mutated=False)
        return fail("receipt_conflict", failure_class="terminal", retry_safe=False, receipt_path=path)

    prior = existing_result()
    if prior is not None:
        return prior
    if dry:
        return planned(receipt_path=path, payload=payload)

    tmp_path: Path | None = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
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
        return fail("receipt_write_failed", failure_class="terminal", retry_safe=False, error=str(exc), receipt_path=path, mutated=True)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
    return ok(status="written", receipt_path=path, payload=payload, mutated=True)


def check_worktree_dirty(request: EffectorRunRequest) -> EffectorRunResult:
    """Atomic read: is worktree dirty (status --porcelain non-empty)?"""
    data = input_of(request)
    worktree_path = str(data.get("worktree_path") or "")
    if not worktree_path:
        return fail("missing_worktree_path", failure_class="terminal", retry_safe=False, mutated=False)
    if not Path(worktree_path).exists():
        return fail("worktree_missing", failure_class="terminal", retry_safe=False, worktree_path=worktree_path, mutated=False)
    dirty = is_dirty(worktree_path)
    return ok(status="checked", worktree_path=worktree_path, dirty=dirty)


def list_controlled_worktrees(request: EffectorRunRequest) -> EffectorRunResult:
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


def push_branch(request: EffectorRunRequest) -> EffectorRunResult:
    """Atomic push with local/remote OID reconciliation and no force push."""
    data = input_of(request)
    dry = dry_run_flag(request)
    prep = cond_blob(request, "prepare_worktree")
    parsed = cond_blob(request, "parse_issue_ref", "parse_issue_ref_from_task")
    worktree_path = str(data.get("worktree_path") or prep.get("worktree_path") or "")
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


def apply_issue_labels(request: EffectorRunRequest) -> EffectorRunResult:
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
