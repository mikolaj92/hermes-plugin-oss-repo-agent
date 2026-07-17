"""Mega-atomic effectors: cleanup domain."""

from __future__ import annotations

import re
from pathlib import Path

from fala.adapters import EffectorRunRequest, EffectorRunResult

from repo_agent.adapters_cli import CommandError, run_cmd
from repo_agent.adapters_git import delete_local_branch, worktree_remove
from repo_agent.envelope import (
    cfg_of,
    cond_blob,
    dry_run_flag,
    fail,
    input_of,
    noop,
    ok,
    planned,
)


def parse_issue_from_branch(request: EffectorRunRequest) -> EffectorRunResult:
    """Pure: extract issue number from ai/fix/<n>-... branch name."""
    data = input_of(request)
    branch = str(data.get("branch") or cfg_of(request).get("branch") or "")
    m = re.search(r"(?:^|/)ai/fix/(\d+)", branch)
    if not m:
        m = re.search(r"/(\d+)(?:-|$)", branch)
    if not m:
        return fail("unparseable_branch", branch=branch)
    return ok(status="parsed", issue=int(m.group(1)), branch=branch)


def check_issue_closed(request: EffectorRunRequest) -> EffectorRunResult:
    """Read GitHub issue state."""
    import json

    data = input_of(request)
    cfg = cfg_of(request)
    parsed = cond_blob(request, "parse_issue_from_branch", "parse")
    repo = str(data.get("repo") or cfg.get("repo") or "")
    issue = int(data.get("issue") or parsed.get("issue") or 0)
    gh = str(cfg.get("gh_cli") or "gh")
    if not repo or not issue:
        return fail("missing_repo_or_issue")
    try:
        proc = run_cmd(
            [gh, "issue", "view", str(issue), "--repo", repo, "--json", "state"],
            timeout=60,
        )
        state = str(json.loads(proc.stdout or "{}").get("state") or "UNKNOWN").upper()
    except (CommandError, json.JSONDecodeError) as exc:
        return fail("issue_view_failed", error=str(exc))
    return ok(status="checked", state=state, closed=state == "CLOSED", repo=repo, issue=issue)


def check_no_open_pr_for_branch(request: EffectorRunRequest) -> EffectorRunResult:
    """True when no open PR exists for head branch."""
    import json

    data = input_of(request)
    cfg = cfg_of(request)
    parsed = cond_blob(request, "parse_issue_from_branch", "parse")
    repo = str(data.get("repo") or cfg.get("repo") or "")
    branch = str(data.get("branch") or parsed.get("branch") or cfg.get("branch") or "")
    gh = str(cfg.get("gh_cli") or "gh")
    if not repo or not branch:
        return fail("missing_repo_or_branch")
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
        prs = json.loads(proc.stdout or "[]")
    except (CommandError, json.JSONDecodeError) as exc:
        return fail("pr_list_failed", error=str(exc))
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
        return fail("missing_clone_or_worktree")
    if dry:
        return planned(clone_path=clone_path, worktree_path=worktree_path, force=force)
    if not Path(worktree_path).exists():
        return ok(status="already_absent", worktree_path=worktree_path, mutated=False)
    try:
        worktree_remove(clone_path, worktree_path, force=force)
    except CommandError as exc:
        return fail("remove_failed", error=str(exc))
    return ok(status="removed", worktree_path=worktree_path, mutated=True)


def delete_local_fix_branch(request: EffectorRunRequest) -> EffectorRunResult:
    """Delete local branch only (never remote)."""
    data = input_of(request)
    dry = dry_run_flag(request)
    cfg = cfg_of(request)
    parsed = cond_blob(request, "parse_issue_from_branch", "parse")
    removed = cond_blob(request, "remove_worktree")
    if removed.get("status") == "noop" or removed.get("reason") in (
        "issue_still_open",
        "open_pr_exists",
    ):
        return noop(
            "skipped_after_remove_guard",
            upstream_reason=removed.get("reason"),
        )
    clone_path = str(data.get("clone_path") or cfg.get("clone_path") or "")
    branch = str(
        data.get("branch") or parsed.get("branch") or cfg.get("branch") or ""
    )
    force = bool(data.get("force", True))
    if not clone_path or not branch:
        return fail("missing_clone_or_branch")
    if dry:
        return planned(clone_path=clone_path, branch=branch, force=force)
    try:
        delete_local_branch(clone_path, branch, force=force)
    except CommandError as exc:
        return fail("delete_failed", error=str(exc))
    return ok(status="deleted", branch=branch, mutated=True)


def release_active_issue_claim(request: EffectorRunRequest) -> EffectorRunResult:
    """Remove active-issue claim file if it matches repo#issue."""
    import json
    from pathlib import Path

    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    parsed = cond_blob(request, "parse_issue_from_branch", "parse")
    closed = cond_blob(request, "check_issue_closed")
    removed = cond_blob(request, "remove_worktree")
    if removed.get("status") == "noop" or removed.get("reason") in (
        "issue_still_open",
        "open_pr_exists",
    ):
        return noop(
            "skipped_after_remove_guard",
            upstream_reason=removed.get("reason"),
        )
    claim_path = str(
        data.get("claim_path")
        or cfg.get("active_issue_path")
        or ""
    )
    repo = str(data.get("repo") or closed.get("repo") or cfg.get("repo") or "")
    issue = str(
        data.get("issue")
        or parsed.get("issue")
        or closed.get("issue")
        or ""
    )
    if not claim_path:
        return fail("missing_claim_path")
    p = Path(claim_path)
    if not p.exists():
        return ok(status="absent", claim_path=claim_path, mutated=False)
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return fail("claim_corrupt", error=str(exc))
    if repo and str(payload.get("repo") or "") not in ("", repo):
        return fail("claim_repo_mismatch", payload=payload)
    if issue and str(payload.get("issue") or payload.get("number") or "") not in ("", issue):
        return fail("claim_issue_mismatch", payload=payload)
    if dry:
        return planned(claim_path=claim_path, payload=payload)
    try:
        p.unlink()
    except OSError as exc:
        return fail("unlink_failed", error=str(exc))
    return ok(status="released", claim_path=claim_path, mutated=True)


def create_maintenance_task(request: EffectorRunRequest) -> EffectorRunResult:
    """Create Kanban [maintenance] task for dirty worktree human follow-up."""
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    board = str(data.get("board") or cfg.get("board") or "")
    path = str(data.get("worktree_path") or "")
    reason = str(data.get("reason") or "dirty_worktree")
    assignee = str(cfg.get("kanban_intake_assignee") or "repo-agent-intake")
    if not board:
        return fail("missing_board")
    title = f"[maintenance] dirty worktree: {path or reason}"
    body = f"Path: {path}\nReason: {reason}\n"
    if dry:
        return planned(board=board, title=title, assignee=assignee)
    try:
        proc = run_cmd(
            [
                "hermes",
                "kanban",
                "--board",
                board,
                "create",
                "--title",
                title,
                "--body",
                body,
                "--assignee",
                assignee,
            ],
            timeout=90,
        )
    except CommandError as exc:
        return fail("create_failed", error=str(exc))
    return ok(status="created", title=title, stdout=proc.stdout[-300:], mutated=True)
