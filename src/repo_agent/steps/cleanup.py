"""Mega-atomic effectors: cleanup domain."""

from __future__ import annotations

import re
from pathlib import Path

from fala.adapters import EffectorRunRequest, EffectorRunResult

from repo_agent.adapters_cli import CommandError, hermes_kanban_json, run_cmd
from repo_agent.adapters_git import (
    branch_exists,
    delete_local_branch,
    parse_worktree_porcelain,
    worktree_list,
    worktree_remove,
)

from repo_agent.envelope import (
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


def _task_marker_matches(task: object, marker: str) -> bool:
    if not isinstance(task, dict):
        return False
    body = str(task.get("body") or task.get("description") or "")
    return bool(re.search(r"(?m)^Idempotency-Key:\s*" + re.escape(marker) + r"\s*$", body))


def _task_id(task: dict[str, object]) -> object:
    return task.get("id") or task.get("task_id")


def parse_issue_from_branch(request: EffectorRunRequest) -> EffectorRunResult:
    """Pure: extract issue number from ai/fix/<n>-... branch name."""
    data = input_of(request)
    branch = str(data.get("branch") or cfg_of(request).get("branch") or "")
    if not branch:
        return noop("no_branch", branch=branch)
    m = re.search(r"(?:^|/)ai/fix/(\d+)", branch)
    if not m:
        m = re.search(r"/(\d+)(?:-|$)", branch)
    if not m:
        return fail("unparseable_branch", failure_class="terminal", retry_safe=False, branch=branch, idempotency_key=f"cleanup:branch:{branch}:parse")
    return ok(status="parsed", issue=int(m.group(1)), branch=branch)


def check_issue_closed(request: EffectorRunRequest) -> EffectorRunResult:
    """Read GitHub issue state."""
    import json

    data = input_of(request)
    cfg = cfg_of(request)
    parsed = cond_blob(request, "parse_issue_from_branch", "parse")
    upstream = upstream_noop(request, "parse_issue_from_branch")
    if upstream:
        return noop(str(upstream.get("reason") or "no_branch"), **{k: v for k, v in upstream.items() if k not in {"status", "ok", "mutated", "reason", "dry_run"}})
    repo = str(data.get("repo") or cfg.get("repo") or "")
    issue = int(data.get("issue") or parsed.get("issue") or 0)
    gh = str(cfg.get("gh_cli") or "gh")
    if not repo or not issue:
        return fail("missing_repo_or_issue", failure_class="terminal", retry_safe=False, repo=repo, issue=issue, idempotency_key=f"cleanup:issue:{repo}:{issue}:check-closed")
    try:
        proc = run_cmd(
            [gh, "issue", "view", str(issue), "--repo", repo, "--json", "state"],
            timeout=60,
        )
        raw = (proc.stdout or "").strip()
        if not raw:
            raise ValueError("blank issue state read-back")
        payload = json.loads(raw)
        if not isinstance(payload, dict) or str(payload.get("state") or "").upper() not in {"OPEN", "CLOSED"}:
            raise ValueError("invalid issue state read-back")
        state = str(payload["state"]).upper()
    except CommandError as exc:
        return fail("issue_view_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), repo=repo, issue=issue, idempotency_key=f"cleanup:issue:{repo}:{issue}:check-closed")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("invalid_issue_readback", failure_class="terminal", retry_safe=False, error=str(exc), repo=repo, issue=issue, idempotency_key=f"cleanup:issue:{repo}:{issue}:check-closed")
    return ok(status="checked", state=state, closed=state == "CLOSED", repo=repo, issue=issue)


def check_no_open_pr_for_branch(request: EffectorRunRequest) -> EffectorRunResult:
    """True when no open PR exists for head branch."""
    import json

    data = input_of(request)
    cfg = cfg_of(request)
    upstream = upstream_noop(request, "parse_issue_from_branch")
    if upstream:
        return noop(str(upstream.get("reason") or "no_branch"), **{k: v for k, v in upstream.items() if k not in {"status", "ok", "mutated", "reason", "dry_run"}})
    parsed = cond_blob(request, "parse_issue_from_branch", "parse")
    repo = str(data.get("repo") or cfg.get("repo") or "")
    branch = str(data.get("branch") or parsed.get("branch") or cfg.get("branch") or "")
    gh = str(cfg.get("gh_cli") or "gh")
    if not repo or not branch:
        return fail("missing_repo_or_branch", failure_class="terminal", retry_safe=False, repo=repo, branch=branch, idempotency_key=f"cleanup:pr:{repo}:{branch}:check-open")
    try:
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
                "number",
            ],
            timeout=60,
        )
        raw = (proc.stdout or "").strip()
        if not raw:
            raise ValueError("blank PR list read-back")
        prs = json.loads(raw)
        if not isinstance(prs, list) or any(not isinstance(pr, dict) for pr in prs):
            raise ValueError("invalid PR list read-back")
    except CommandError as exc:
        return fail("pr_list_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), repo=repo, branch=branch, idempotency_key=f"cleanup:pr:{repo}:{branch}:check-open")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("invalid_pr_list", failure_class="terminal", retry_safe=False, error=str(exc), repo=repo, branch=branch, idempotency_key=f"cleanup:pr:{repo}:{branch}:check-open")
    return ok(
        status="checked",
        open_count=len(prs),
        safe_to_cleanup=len(prs) == 0,
        prs=prs,
    )


def remove_worktree(request: EffectorRunRequest) -> EffectorRunResult:
    """Remove one git worktree path (force optional)."""
    data = input_of(request)
    dry = dry_run_flag(request)
    cfg = cfg_of(request)
    closed_blob = cond_blob(request, "check_issue_closed")
    open_pr_blob = cond_blob(request, "check_no_open_pr", "check_no_open_pr_for_branch")
    require_safe = bool(data.get("require_safe", cfg.get("require_safe", True)))
    cleanup_key = f"cleanup:worktree:{data.get('clone_path') or cfg.get('clone_path') or ''}:{data.get('worktree_path') or cfg.get('worktree_path') or ''}:remove"
    for guard_name, guard in (("check_issue_closed", closed_blob), ("check_no_open_pr", open_pr_blob)):
        if not guard or guard.get("status") == "failed":
            guard_class = str(guard.get("failure_class") or "terminal") if isinstance(guard, dict) else "terminal"
            guard_retry_safe = bool(guard.get("retry_safe", False)) if isinstance(guard, dict) else False
            return fail("cleanup_guard_failed", failure_class=guard_class, retry_safe=guard_retry_safe, guard=guard_name, guard_output=guard, clone_path=str(data.get("clone_path") or cfg.get("clone_path") or ""), worktree_path=str(data.get("worktree_path") or cfg.get("worktree_path") or ""), idempotency_key=cleanup_key)
    upstream = upstream_noop(
        request, "check_issue_closed", "check_no_open_pr", "parse_issue_from_branch"
    )
    if upstream:
        return noop(str(upstream.get("reason") or "cleanup_not_ready"))
    if require_safe and (closed_blob or open_pr_blob):
        if closed_blob and closed_blob.get("closed") is False:
            return noop(
                "issue_still_open",
                closed=False,
                issue=closed_blob.get("issue"),
            )
        if open_pr_blob and open_pr_blob.get("safe_to_cleanup") is False:
            return noop(
                "open_pr_exists",
                safe_to_cleanup=False,
                open_count=open_pr_blob.get("open_count"),
            )
    clone_path = str(data.get("clone_path") or cfg.get("clone_path") or "")
    worktree_path = str(
        data.get("worktree_path") or cfg.get("worktree_path") or ""
    )
    force = bool(data.get("force", False))
    if not clone_path or not worktree_path:
        return fail("missing_clone_or_worktree", failure_class="terminal", retry_safe=False, clone_path=clone_path, worktree_path=worktree_path, idempotency_key=f"cleanup:worktree:{clone_path}:{worktree_path}:remove")
    worktree = Path(worktree_path)
    if dry:
        return planned(clone_path=clone_path, worktree_path=worktree_path, force=force)
    if not worktree.exists():
        return fail("worktree_missing", failure_class="terminal", retry_safe=False, clone_path=clone_path, worktree_path=worktree_path, branch=str(data.get("branch") or cfg.get("branch") or ""), idempotency_key=f"cleanup:worktree:{clone_path}:{worktree_path}:remove")
    # Never remove an arbitrary directory: prove git owns this path and branch.
    expected_branch = str(data.get("branch") or cfg.get("branch") or "")
    try:
        rows = parse_worktree_porcelain(worktree_list(clone_path))
    except CommandError as exc:
        return fail(
            "worktree_provenance_read_failed",
            failure_class="retryable_read",
            retry_safe=True,
            mutated=False,
            error=str(exc),
            clone_path=clone_path,
            worktree_path=worktree_path,
            branch=expected_branch,
            idempotency_key=cleanup_key,
        )
    matches = [row for row in rows if str(Path(row.get("path") or "")).resolve() == str(worktree.resolve())]
    if not matches or (expected_branch and matches[0].get("branch") != expected_branch):
        return fail(
            "worktree_provenance_mismatch",
            failure_class="terminal",
            retry_safe=False,
            clone_path=clone_path,
            worktree_path=worktree_path,
            branch=expected_branch,
            actual_branch=(matches[0].get("branch") if matches else ""),
            idempotency_key=cleanup_key,
        )
    try:
        worktree_remove(clone_path, worktree_path, force=force)
    except CommandError as exc:
        try:
            remaining = parse_worktree_porcelain(worktree_list(clone_path))
        except CommandError:
            remaining = matches
        if not any(str(Path(row.get("path") or "")).resolve() == str(worktree.resolve()) for row in remaining):
            return ok(status="already_absent", clone_path=clone_path, worktree_path=worktree_path, branch=expected_branch, idempotency_key=f"cleanup:worktree:{clone_path}:{worktree_path}:remove", mutated=True, reconciled=True)
        return fail("remove_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), clone_path=clone_path, worktree_path=worktree_path, branch=expected_branch, idempotency_key=f"cleanup:worktree:{clone_path}:{worktree_path}:remove", mutated=True)
    return ok(status="removed", clone_path=clone_path, worktree_path=worktree_path, branch=expected_branch, idempotency_key=f"cleanup:worktree:{clone_path}:{worktree_path}:remove", mutated=True, retry_safe=True)


def delete_local_fix_branch(request: EffectorRunRequest) -> EffectorRunResult:
    """Delete local branch only (never remote)."""
    data = input_of(request)
    dry = dry_run_flag(request)
    cfg = cfg_of(request)
    parsed = cond_blob(request, "parse_issue_from_branch", "parse")
    removed = cond_blob(request, "remove_worktree")
    if removed.get("status") in {"noop", "failed"}:
        return noop("skipped_after_remove_guard", upstream_reason=removed.get("reason"), upstream_status=removed.get("status"))
    clone_path = str(data.get("clone_path") or cfg.get("clone_path") or "")
    branch = str(
        data.get("branch") or parsed.get("branch") or cfg.get("branch") or ""
    )
    force = bool(data.get("force", True))
    if not clone_path or not branch:
        return fail("missing_clone_or_branch", failure_class="terminal", retry_safe=False, clone_path=clone_path, branch=branch, idempotency_key=f"cleanup:branch:{clone_path}:{branch}:delete")
    if dry:
        return planned(clone_path=clone_path, branch=branch, force=force)
    key = f"branch:{clone_path}:{branch}:delete"
    try:
        delete_local_branch(clone_path, branch, force=force)
    except CommandError as exc:
        # A failed delete is ambiguous; absence alone is not proof of a
        # successful mutation without a receipt/provenance record.
        return fail(
            "delete_failed",
            failure_class="reconcile_then_retry",
            retry_safe=False,
            error=str(exc),
            idempotency_key=key,
            mutated=True,
        )
    try:
        if branch_exists(clone_path, branch):
            return fail("delete_not_confirmed", failure_class="reconcile_then_retry", retry_safe=False, branch=branch, idempotency_key=key, mutated=True)
    except Exception as exc:
        return fail("delete_readback_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), idempotency_key=key, mutated=True)
    return ok(status="deleted", branch=branch, idempotency_key=key, mutated=True, retry_safe=True)


def release_active_issue_claim(request: EffectorRunRequest) -> EffectorRunResult:
    """Remove active-issue claim file only after exact repo/issue read-back."""
    import json
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    parsed = cond_blob(request, "parse_issue_from_branch", "parse")
    closed = cond_blob(request, "check_issue_closed")
    removed = cond_blob(request, "remove_worktree")
    if removed.get("status") in {"noop", "failed"}:
        return noop("skipped_after_remove_guard", upstream_reason=removed.get("reason"), upstream_status=removed.get("status"))
    claim_path = str(data.get("claim_path") or cfg.get("active_issue_path") or "")
    repo = str(data.get("repo") or closed.get("repo") or cfg.get("repo") or "")
    issue = str(data.get("issue") or parsed.get("issue") or closed.get("issue") or "")
    key = f"cleanup:claim:{claim_path}:{repo}:{issue}:release"
    if not claim_path:
        return fail("missing_claim_path", failure_class="terminal", retry_safe=False, claim_path=claim_path, repo=repo, issue=issue, idempotency_key=key)
    if not repo or not issue:
        return fail("missing_claim_provenance", failure_class="terminal", retry_safe=False, claim_path=claim_path, repo=repo, issue=issue, idempotency_key=key)
    p = Path(claim_path)
    if not p.exists():
        return ok(status="absent", claim_path=claim_path, mutated=False)
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return fail("claim_corrupt", failure_class="terminal", retry_safe=False, error=str(exc), claim_path=claim_path, repo=repo, issue=issue, idempotency_key=key)
    if str(payload.get("repo") or "") != repo:
        return fail("claim_repo_mismatch", failure_class="terminal", retry_safe=False, claim_path=claim_path, repo=repo, issue=issue, payload=payload, idempotency_key=key)
    if str(payload.get("issue") or payload.get("number") or "") != issue:
        return fail("claim_issue_mismatch", failure_class="terminal", retry_safe=False, claim_path=claim_path, repo=repo, issue=issue, payload=payload, idempotency_key=key)
    if dry:
        return planned(claim_path=claim_path, payload=payload)
    try:
        p.unlink()
    except OSError as exc:
        if not p.exists():
            return ok(status="absent", claim_path=claim_path, repo=repo, issue=issue, idempotency_key=key, mutated=True, reconciled=True)
    if p.exists():
        return fail("unlink_not_confirmed", failure_class="reconcile_then_retry", retry_safe=False, claim_path=claim_path, repo=repo, issue=issue, idempotency_key=key, mutated=True)
    return ok(status="released", claim_path=claim_path, repo=repo, issue=issue, idempotency_key=key, mutated=True, retry_safe=True)


def create_maintenance_task(request: EffectorRunRequest) -> EffectorRunResult:
    """Create one Kanban maintenance task per deterministic repo/PR marker."""
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    board = str(data.get("board") or cfg.get("board") or "")
    repo = str(data.get("repo") or cfg.get("repo") or "")
    pr = str(data.get("pr_number") or data.get("number") or "")
    path = str(data.get("worktree_path") or "")
    reason = str(data.get("reason") or "dirty_worktree")
    assignee = str(cfg.get("kanban_intake_assignee") or "repo-agent-intake")
    if not board:
        return fail("missing_board", failure_class="terminal", retry_safe=False, board=board, repo=repo, worktree_path=path, pr_number=pr, idempotency_key=f"maintenance:{repo or path}:pr:{pr or 'none'}")
    if not repo and not path:
        return fail("missing_maintenance_provenance", failure_class="terminal", retry_safe=False, board=board, repo=repo, worktree_path=path, pr_number=pr, idempotency_key=f"maintenance:{repo or path}:pr:{pr or 'none'}")
    marker = f"maintenance:{repo or path}:pr:{pr or 'none'}"
    title = f"[maintenance] dirty worktree: {path or repo or reason}"
    body = f"Path: {path}\nRepository: {repo}\nPR: {pr}\nReason: {reason}\nIdempotency-Key: {marker}\n"
    if dry:
        return planned(board=board, title=title, assignee=assignee, idempotency_key=marker)
    try:
        tasks = hermes_kanban_json(["--board", board, "list", "--json", "--sort", "created-desc"])
    except CommandError as exc:
        return fail("kanban_list_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), idempotency_key=marker)
    if not isinstance(tasks, list) or any(not isinstance(task, dict) for task in tasks):
        return fail("invalid_kanban_readback", failure_class="terminal", retry_safe=False, idempotency_key=marker)
    matches = [task for task in tasks if _task_marker_matches(task, marker)]
    if len(matches) > 1:
        return fail("ambiguous_kanban_task", failure_class="terminal", retry_safe=False, idempotency_key=marker, task_ids=[_task_id(task) for task in matches])
    if matches:
        task = matches[0]
        status = str(task.get("status") or task.get("state") or "").strip().lower()
        return ok(status="already_completed" if status in {"done", "completed", "archived"} else "exists", board=board, task_id=_task_id(task), title=task.get("title"), idempotency_key=marker, mutated=False)
    try:
        proc = run_cmd(["hermes", "kanban", "--board", board, "create", "--title", title, "--body", body, "--assignee", assignee, "--idempotency-key", marker], timeout=90)
    except CommandError as exc:
        return fail("create_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), board=board, repo=repo, worktree_path=path, pr_number=pr, title=title, idempotency_key=marker, mutated=True)
    try:
        after = hermes_kanban_json(["--board", board, "list", "--json", "--sort", "created-desc"])
    except CommandError as exc:
        return fail("create_readback_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), board=board, repo=repo, worktree_path=path, pr_number=pr, title=title, idempotency_key=marker, mutated=True)
    if not isinstance(after, list) or any(not isinstance(task, dict) for task in after):
        return fail("invalid_create_readback", failure_class="terminal", retry_safe=False, board=board, repo=repo, worktree_path=path, pr_number=pr, title=title, idempotency_key=marker, mutated=True)
    matches = [task for task in after if _task_marker_matches(task, marker)]
    if len(matches) != 1:
        return fail("created_but_unresolved_task_id", failure_class="terminal", retry_safe=False, board=board, repo=repo, worktree_path=path, pr_number=pr, title=title, idempotency_key=marker, match_count=len(matches), mutated=True)
    task = matches[0]
    return ok(status="created", board=board, task_id=_task_id(task), title=title, idempotency_key=marker, stdout=proc.stdout[-300:], mutated=True)
