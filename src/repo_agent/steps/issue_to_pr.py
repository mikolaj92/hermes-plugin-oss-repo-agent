"""Mega-atomic effectors: issue → PR domain."""

from __future__ import annotations

import json
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


def _pr_list_readback(gh: str, repo: str, branch: str, base: str, *, require_one: bool = True) -> list[dict[str, Any]]:
    proc = run_cmd(
        [gh, "pr", "list", "--repo", repo, "--head", branch, "--state", "open", "--json", "number,url,headRefName,baseRefName"],
        timeout=60,
    )
    text = (proc.stdout or "").strip()
    if not text:
        return []
    rows = json.loads(text)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("PR readback was malformed")
    exact = [
        row
        for row in rows
        if str(row.get("headRefName") or "") in {"", branch}
        and (not row.get("baseRefName") or str(row.get("baseRefName")) == base)
    ]
    if len(exact) > 1 or (require_one and len(exact) != 1):
        raise ValueError(f"expected exactly one matching open PR, found {len(exact)}")
    if not exact:
        return []
    number = exact[0].get("number")
    if isinstance(number, bool) or number is None or not str(number).strip():
        raise ValueError("PR readback number was blank")
    return exact
def open_pull_request(request: EffectorRunRequest) -> EffectorRunResult:
    """Open one PR for branch → base and verify authoritative state."""
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
    body = str(data.get("body") or (f"Automated fix for {repo}#{issue} via repo-agent.\n\nCloses #{issue}\n" if issue else "Automated fix via repo-agent."))
    gh = str(cfg.get("gh_cli") or "gh")
    if not repo or not branch:
        return fail("missing_repo_or_branch", failure_class="terminal", retry_safe=False)
    if not base.strip():
        return fail("missing_base_branch", failure_class="terminal", retry_safe=False)
    if dry:
        return planned(repo=repo, branch=branch, base=base, title=title)
    try:
        existing = _pr_list_readback(gh, repo, branch, base, require_one=False)
        if existing:
            pr = existing[0]
            return ok(status="exists", repo=repo, branch=branch, base=base, number=int(pr["number"]), url=str(pr.get("url") or ""), mutated=False)
    except CommandError as exc:
        return fail("pr_discovery_failed", error=str(exc), failure_class="terminal", retry_safe=False)
    except (ValueError, json.JSONDecodeError, TypeError, OverflowError) as exc:
        return fail("pr_discovery_ambiguous", error=str(exc), failure_class="terminal", retry_safe=False)
    try:
        proc = run_cmd([gh, "pr", "create", "--repo", repo, "--base", base, "--head", branch, "--title", title, "--body", body], timeout=120)
    except CommandError as exc:
        return fail("pr_create_failed", error=str(exc), stderr=exc.stderr[-500:], mutated=True, failure_class="reconcile_then_retry", retry_safe=False)
    url = (proc.stdout or "").strip().splitlines()[-1].strip() if proc.stdout else ""
    try:
        exact = _pr_list_readback(gh, repo, branch, base)
    except CommandError as exc:
        return fail("pr_created_readback_failed", error=str(exc), repo=repo, branch=branch, base=base, url=url or None, mutated=True, failure_class="reconcile_then_retry", retry_safe=False)
    except (ValueError, json.JSONDecodeError, TypeError, OverflowError) as exc:
        return fail("pr_created_readback_ambiguous", error=str(exc), repo=repo, branch=branch, base=base, url=url or None, mutated=True, failure_class="terminal", retry_safe=False)
    pr = exact[0]
    return ok(status="created", repo=repo, branch=branch, base=base, number=int(pr["number"]), url=str(pr.get("url") or url), mutated=True)


def _label_names(value: Any) -> set[str]:
    if not isinstance(value, list):
        raise ValueError("labels readback was malformed")
    result: set[str] = set()
    for item in value:
        name = str(item.get("name") or "").strip() if isinstance(item, dict) else str(item or "").strip()
        if not name:
            raise ValueError("labels readback was malformed")
        result.add(name)
    return result


def _pr_labels(gh: str, repo: str, number: int) -> set[str]:
    proc = run_cmd([gh, "pr", "view", str(number), "--repo", repo, "--json", "labels"], timeout=60)
    text = (proc.stdout or "").strip()
    if not text:
        return set()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("PR labels readback was malformed")
    return _label_names(value.get("labels"))


def apply_pr_labels(request: EffectorRunRequest) -> EffectorRunResult:
    """Add only missing labels and require authoritative post-readback."""
    data = input_of(request); cfg = cfg_of(request); dry = dry_run_flag(request)
    upstream = upstream_noop(request, "open_pull_request", "parse_issue_ref")
    if upstream:
        return noop(str(upstream.get("reason") or "no_ready_task"))
    opened = cond_blob(request, "open_pull_request", "open_pr"); parsed = cond_blob(request, "parse_issue_ref", "parse_issue_ref_from_task")
    repo = str(data.get("repo") or opened.get("repo") or parsed.get("repo") or "")
    number = int(data.get("number") or data.get("pr_number") or opened.get("number") or 0)
    labels = [str(label) for label in (data.get("labels") or ["ai:generated", "ai:pr-opened"])]
    gh = str(cfg.get("gh_cli") or "gh")
    if not repo or not number:
        return fail("missing_repo_or_number", failure_class="terminal", retry_safe=False)
    if dry:
        return planned(repo=repo, number=number, labels=labels)
    missing: list[str] = []
    actions: list[dict[str, Any]] = []
    try:
        before = _pr_labels(gh, repo, number)
        missing = [label for label in labels if label not in before]
        for label in missing:
            try:
                run_cmd([gh, "pr", "edit", str(number), "--repo", repo, "--add-label", label], timeout=60)
                actions.append({"label": label, "ok": True})
            except CommandError as exc:
                actions.append({"label": label, "ok": False, "error": exc.stderr[-200:]})
        after = _pr_labels(gh, repo, number)
        if after and not set(labels).issubset(after):
            return fail("labels_readback_incomplete", repo=repo, number=number, labels=labels, observed_labels=sorted(after), actions=actions, mutated=bool(missing), failure_class="reconcile_then_retry" if missing else "terminal", retry_safe=False)
    except CommandError as exc:
        return fail("labels_readback_failed", error=str(exc), repo=repo, number=number, mutated=bool(missing), failure_class="reconcile_then_retry", retry_safe=False)
    except (ValueError, json.JSONDecodeError, TypeError) as exc:
        return fail("labels_readback_ambiguous", error=str(exc), repo=repo, number=number, mutated=bool(missing), failure_class="terminal", retry_safe=False)
    return ok(status="labeled", repo=repo, number=number, actions=actions, labels=labels, mutated=bool(missing))


def write_dispatch_receipt(request: EffectorRunRequest) -> EffectorRunResult:
    """Create dispatch receipt exclusively; identical payload is idempotent."""
    import os
    data = input_of(request); dry = dry_run_flag(request)
    upstream = upstream_noop(request, "parse_issue_ref", "prepare_worktree", "open_pull_request", "apply_pr_labels")
    if upstream:
        return noop(str(upstream.get("reason") or "no_ready_task"))
    path = str(data.get("receipt_path") or cfg_of(request).get("receipt_path") or "")
    payload = data.get("payload")
    if not isinstance(payload, dict) or not payload:
        parsed = cond_blob(request, "parse_issue_ref", "parse_issue_ref_from_task"); opened = cond_blob(request, "open_pull_request", "open_pr"); prep = cond_blob(request, "prepare_worktree")
        payload = {"phase": "DISPATCHED", "repo": parsed.get("repo") or opened.get("repo"), "issue": parsed.get("issue"), "branch": prep.get("branch") or parsed.get("branch"), "pr_number": opened.get("number"), "pr_url": opened.get("url"), "worktree_path": prep.get("worktree_path"), "dry_run": dry}
    if not path:
        return fail("missing_receipt_path", failure_class="terminal", retry_safe=False)
    if dry:
        return planned(receipt_path=path, payload=payload)
    text = json.dumps(payload, indent=2, sort_keys=True)
    try:
        p = Path(path); p.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = p.read_text(encoding="utf-8")
            if existing != text and existing != text + "\n":
                return fail("receipt_conflict", receipt_path=path, failure_class="terminal", retry_safe=False)
            return ok(status="exists", receipt_path=path, mutated=False)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text); fh.flush(); os.fsync(fh.fileno())
    except (OSError, UnicodeError) as exc:
        return fail("receipt_write_failed", error=str(exc), receipt_path=path, failure_class="terminal", retry_safe=False)
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
    """Add only missing issue labels and require authoritative post-readback."""
    data = input_of(request); cfg = cfg_of(request); dry = dry_run_flag(request)
    repo = str(data.get("repo") or ""); issue = int(data.get("issue") or data.get("number") or 0)
    labels = [str(label) for label in (data.get("labels") or [])]; gh = str(cfg.get("gh_cli") or "gh")
    if not repo or not issue:
        return fail("missing_repo_or_issue", failure_class="terminal", retry_safe=False)
    if not labels:
        return fail("missing_labels", failure_class="terminal", retry_safe=False)
    if dry:
        return planned(repo=repo, issue=issue, labels=labels)
    missing: list[str] = []; actions: list[dict[str, Any]] = []
    try:
        proc = run_cmd([gh, "issue", "view", str(issue), "--repo", repo, "--json", "labels"], timeout=60)
        text = (proc.stdout or "").strip()
        if not text: raise ValueError("issue labels readback was blank")
        value = json.loads(text)
        if not isinstance(value, dict): raise ValueError("issue labels readback was malformed")
        before = _label_names(value.get("labels")); missing = [label for label in labels if label not in before]
        for label in missing:
            try:
                run_cmd([gh, "issue", "edit", str(issue), "--repo", repo, "--add-label", label], timeout=60); actions.append({"label": label, "ok": True})
            except CommandError as exc: actions.append({"label": label, "ok": False, "error": exc.stderr[-200:]})
        proc = run_cmd([gh, "issue", "view", str(issue), "--repo", repo, "--json", "labels"], timeout=60)
        text = (proc.stdout or "").strip()
        if not text: raise ValueError("issue labels post-readback was blank")
        value = json.loads(text)
        if not isinstance(value, dict): raise ValueError("issue labels post-readback was malformed")
        after = _label_names(value.get("labels"))
        if after and not set(labels).issubset(after):
            return fail("labels_readback_incomplete", repo=repo, issue=issue, labels=labels, observed_labels=sorted(after), actions=actions, mutated=bool(missing), failure_class="reconcile_then_retry" if missing else "terminal", retry_safe=False)
    except CommandError as exc:
        return fail("labels_readback_failed", error=str(exc), repo=repo, issue=issue, mutated=bool(missing), failure_class="reconcile_then_retry", retry_safe=False)
    except (ValueError, json.JSONDecodeError, TypeError) as exc:
        return fail("labels_readback_ambiguous", error=str(exc), repo=repo, issue=issue, mutated=bool(missing), failure_class="terminal", retry_safe=False)
    return ok(status="labeled", repo=repo, issue=issue, actions=actions, labels=labels, mutated=bool(missing))
