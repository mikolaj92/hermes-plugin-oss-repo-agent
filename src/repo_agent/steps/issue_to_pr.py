"""Mega-atomic effectors: issue → PR domain."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

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
    """Re-list board and match exact title; fall back to stdout parse."""
    parsed = _parse_task_id_from_stdout(stdout)
    try:
        tasks = hermes_kanban_json(
            ["--board", board, "list", "--json", "--sort", "created-desc"]
        )
    except CommandError:
        return parsed
    if isinstance(tasks, list):
        for t in tasks:
            if not isinstance(t, dict):
                continue
            if str(t.get("title") or "") == task_title:
                tid = _task_id_from_row(t)
                if tid:
                    return tid
    return parsed


def load_kanban_task(request: EffectorRunRequest) -> EffectorRunResult:
    """Read one Kanban task by id (or first ready [fix-pr]/[issue])."""
    data = input_of(request)
    cfg = cfg_of(request)
    board = str(data.get("board") or cfg.get("board") or "")
    task_id = data.get("task_id")
    if not board:
        return fail("missing_board")
    try:
        tasks = hermes_kanban_json(
            ["--board", board, "list", "--json", "--sort", "created-desc"]
        )
    except CommandError as exc:
        return fail("kanban_list_failed", error=str(exc))
    if not isinstance(tasks, list):
        return fail("invalid_kanban_json")
    if task_id:
        for t in tasks:
            if str(t.get("id") or t.get("task_id")) == str(task_id):
                return ok(status="loaded", task=t, board=board)
        return fail("task_not_found", task_id=task_id, board=board)
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
        return fail("missing_task")
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
        return fail("unparseable_issue_ref", title=title)
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
        return fail("missing_board_repo_issue")
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
        return fail("kanban_list_failed", error=str(exc))
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
        return fail("kanban_create_failed", error=str(exc), stderr=exc.stderr[-400:])
    task_id = resolve_kanban_task_id_after_create(
        board=board, task_title=task_title, stdout=proc.stdout or ""
    )
    if not task_id:
        return fail(
            "created_but_unresolved_task_id",
            title=task_title,
            board=board,
            stdout=(proc.stdout or "")[-400:],
            mutated=True,
        )
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
        return fail("missing_board_or_task_id")
    if dry:
        return planned(board=board, task_id=task_id, result=result_text)
    try:
        run_cmd(
            [
                "hermes",
                "kanban",
                "--board",
                board,
                "complete",
                task_id,
                "--result",
                result_text,
                "--summary",
                result_text,
            ],
            timeout=60,
        )
    except CommandError as exc:
        return fail("complete_failed", error=str(exc))
    return ok(status="completed", board=board, task_id=task_id, mutated=True)


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
    base_branch = str(
        data.get("base_branch")
        or cfg_of(request).get("base_branch")
        or "main"
    )
    if not clone_path:
        return fail("missing_clone_path")
    if dry:
        return planned(clone_path=clone_path, base_branch=base_branch, actions=["fetch", "checkout"])
    if not Path(clone_path, ".git").exists() and not Path(clone_path, ".git").is_file():
        # bare .git file for worktree is ok; directory for normal clone
        if not Path(clone_path).exists():
            return fail("clone_missing", clone_path=clone_path)
    try:
        git(["fetch", "origin", "--prune"], cwd=clone_path)
        git(["rev-parse", "--verify", f"origin/{base_branch}"], cwd=clone_path)
    except CommandError as exc:
        return fail("refresh_failed", error=str(exc))
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
        return fail("missing_clone_branch_or_root")
    safe = re.sub(r"[^a-zA-Z0-9._/-]+", "-", branch)
    path = str(Path(worktree_root) / safe)
    if dry:
        return planned(
            clone_path=clone_path,
            branch=branch,
            worktree_path=path,
            create_branch=True,
        )
    try:
        Path(worktree_root).mkdir(parents=True, exist_ok=True)
        if Path(path).exists():
            # reuse
            head = rev_parse(path)
            return ok(
                status="reused",
                worktree_path=path,
                branch=branch,
                head=head,
                mutated=False,
            )
        exists = branch_exists(clone_path, branch)
        if exists:
            worktree_add(clone_path, path, branch, create_branch=False)
        else:
            # branch from origin/base
            git(["branch", branch, f"origin/{base_branch}"], cwd=clone_path)
            worktree_add(clone_path, path, branch, create_branch=False)
        head = rev_parse(path)
    except CommandError as exc:
        return fail("worktree_prepare_failed", error=str(exc), worktree_path=path)
    return ok(
        status="prepared",
        worktree_path=path,
        branch=branch,
        head=head,
        mutated=True,
    )


def run_omp_worker(request: EffectorRunRequest) -> EffectorRunResult:
    """Single OMP invocation in a worktree (no PR open, no labels)."""
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    upstream = upstream_noop(request, "prepare_worktree", "parse_issue_ref")
    if upstream:
        return noop(str(upstream.get("reason") or "no_ready_task"))
    worktree_path = str(
        data.get("worktree_path")
        or cond_get(request, "worktree_path", "prepare_worktree")
        or ""
    )
    prompt = str(
        data.get("prompt")
        or cond_get(request, "prompt", "build_repair_prompt")
        or ""
    )
    if not prompt:
        # issue_to_pr default prompt from parse_issue_ref
        parsed = cond_blob(request, "parse_issue_ref", "parse_issue_ref_from_task")
        if parsed.get("repo") and parsed.get("issue"):
            prompt = (
                f"Implement a minimal fix for {parsed['repo']}#{parsed['issue']}.\n"
                f"Branch: {parsed.get('branch') or ''}\n"
                f"Task: {parsed.get('task_title') or ''}\n"
                f"Keep scope tight. Do not force-push. Do not open/merge PRs.\n"
            )
    model = str(data.get("model") or cfg.get("model") or "omniroute/omp/default")
    timeout = float(data.get("timeout_seconds") or cfg.get("timeout_seconds") or 1800)
    if not worktree_path or not prompt:
        return fail("missing_worktree_or_prompt")
    try:
        out = run_omp(
            prompt=prompt,
            cwd=worktree_path,
            model=model,
            timeout=timeout,
            dry_run=dry,
        )
    except CommandError as exc:
        return fail("omp_failed", error=str(exc), stderr=exc.stderr[-500:])
    if dry:
        return planned(
            worktree_path=worktree_path,
            model=model,
            omp=out,
            prompt_len=out.get("prompt_len"),
        )
    return ok(
        status="omp_finished",
        worktree_path=worktree_path,
        model=model,
        omp=out,
        mutated=True,
    )


def verify_branch_has_commits(request: EffectorRunRequest) -> EffectorRunResult:
    """Pure-ish: check worktree HEAD differs from base_branch tip."""
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
    base_branch = str(
        data.get("base_branch") or cfg_of(request).get("base_branch") or "main"
    )
    if not worktree_path:
        return fail("missing_worktree_path")
    try:
        head = rev_parse(worktree_path)
        base = rev_parse(clone_path or worktree_path, f"origin/{base_branch}")
    except CommandError as exc:
        return fail("rev_parse_failed", error=str(exc))
    if head == base:
        return fail("no_new_commits", head=head, base=base)
    return ok(status="has_commits", head=head, base=base)


def open_pull_request(request: EffectorRunRequest) -> EffectorRunResult:
    """Open one PR for branch → base (gh pr create)."""
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
    title = str(
        data.get("title")
        or (f"fix: {repo}#{issue}" if repo and issue else f"fix: {branch}")
    )
    body = str(
        data.get("body")
        or (
            f"Automated fix for {repo}#{issue} via repo-agent.\n\n"
            f"Closes #{issue}\n"
            if issue
            else "Automated fix via repo-agent."
        )
    )
    gh = str(cfg.get("gh_cli") or "gh")
    if not repo or not branch:
        return fail("missing_repo_or_branch")
    if dry:
        return planned(repo=repo, branch=branch, base=base, title=title)
    try:
        # existing?
        proc = run_cmd(
            [
                gh,
                "pr",
                "list",
                "--repo",
                repo,
                "--head",
                branch,
                "--state",
                "open",
                "--json",
                "number,url",
            ],
            timeout=60,
        )
        import json

        existing = json.loads(proc.stdout or "[]")
        if existing:
            pr = existing[0]
            return ok(
                status="exists",
                number=pr.get("number"),
                url=pr.get("url"),
                mutated=False,
            )
        proc = run_cmd(
            [
                gh,
                "pr",
                "create",
                "--repo",
                repo,
                "--base",
                base,
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ],
            timeout=120,
        )
        url = (proc.stdout or "").strip().splitlines()[-1].strip() if proc.stdout else ""
        number: int | None = None
        # Prefer structured re-read so conduction can feed apply_pr_labels without re-listing world.
        try:
            view = run_cmd(
                [
                    gh,
                    "pr",
                    "list",
                    "--repo",
                    repo,
                    "--head",
                    branch,
                    "--state",
                    "open",
                    "--json",
                    "number,url",
                    "--limit",
                    "1",
                ],
                timeout=60,
            )
            listed = json.loads(view.stdout or "[]")
            if listed:
                number = int(listed[0].get("number") or 0) or None
                url = str(listed[0].get("url") or url)
        except (CommandError, json.JSONDecodeError, TypeError, ValueError):
            # Fallback: parse /pull/N from URL if present
            m = re.search(r"/pull/(\d+)", url)
            if m:
                number = int(m.group(1))
        if not number:
            return fail(
                "pr_created_but_unresolved",
                repo=repo,
                branch=branch,
                url=url or None,
                stdout=(proc.stdout or "")[-400:],
                mutated=True,
            )
    except CommandError as exc:
        return fail("pr_create_failed", error=str(exc), stderr=exc.stderr[-500:])
    return ok(
        status="created",
        repo=repo,
        branch=branch,
        number=number,
        url=url,
        mutated=True,
    )


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
        return fail("missing_repo_or_number")
    if dry:
        return planned(repo=repo, number=number, labels=list(labels))
    applied = []
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
        return fail("all_labels_failed", actions=applied)
    return ok(status="labeled", actions=applied, mutated=True)


def write_dispatch_receipt(request: EffectorRunRequest) -> EffectorRunResult:
    """Atomic write of a small JSON receipt for fix-pr work (fsync best-effort)."""
    import json
    import os

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
        return fail("missing_receipt_path")
    if dry:
        return planned(receipt_path=path, payload=payload)
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        text = json.dumps(payload, indent=2, sort_keys=True)
        tmp.write_text(text, encoding="utf-8")
        with tmp.open("r+b") as fh:
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(p)
    except OSError as exc:
        return fail("receipt_write_failed", error=str(exc), receipt_path=path)
    return ok(status="written", receipt_path=path, mutated=True)


def check_worktree_dirty(request: EffectorRunRequest) -> EffectorRunResult:
    """Atomic read: is worktree dirty (status --porcelain non-empty)?"""
    data = input_of(request)
    worktree_path = str(data.get("worktree_path") or "")
    if not worktree_path:
        return fail("missing_worktree_path")
    if not Path(worktree_path).exists():
        return fail("worktree_missing", worktree_path=worktree_path)
    dirty = is_dirty(worktree_path)
    return ok(status="checked", worktree_path=worktree_path, dirty=dirty)


def list_controlled_worktrees(request: EffectorRunRequest) -> EffectorRunResult:
    """List git worktrees under clone; optionally filter by worktree_root prefix."""
    data = input_of(request)
    cfg = cfg_of(request)
    clone_path = str(data.get("clone_path") or "")
    worktree_root = str(data.get("worktree_root") or cfg.get("worktree_root") or "")
    if not clone_path:
        return fail("missing_clone_path")
    try:
        text = worktree_list(clone_path)
    except CommandError as exc:
        return fail("worktree_list_failed", error=str(exc))
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
    """Atomic: git push -u origin <branch> from worktree (no force)."""
    data = input_of(request)
    dry = dry_run_flag(request)
    prep = cond_blob(request, "prepare_worktree")
    parsed = cond_blob(request, "parse_issue_ref", "parse_issue_ref_from_task")
    worktree_path = str(
        data.get("worktree_path") or prep.get("worktree_path") or ""
    )
    upstream = upstream_noop(request, "prepare_worktree", "verify_branch", "parse_issue_ref")
    if upstream:
        return noop(str(upstream.get("reason") or "no_ready_task"))
    branch = str(
        data.get("branch") or prep.get("branch") or parsed.get("branch") or ""
    )
    if not worktree_path or not branch:
        return fail("missing_worktree_or_branch")
    if dry:
        return planned(worktree_path=worktree_path, branch=branch, remote="origin")
    try:
        out = git_push_branch(worktree_path, branch, set_upstream=True)
    except CommandError as exc:
        return fail(
            "push_failed",
            error=str(exc),
            stderr=(exc.stderr or "")[-500:],
        )
    return ok(
        status="pushed",
        worktree_path=worktree_path,
        branch=branch,
        remote="origin",
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
        return fail("missing_repo_or_issue")
    if not labels:
        return fail("missing_labels")
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
        return fail("all_labels_failed", actions=applied)
    return ok(status="labeled", repo=repo, issue=issue, actions=applied, mutated=True)
