"""Mega-atomic effectors: cleanup domain."""

from __future__ import annotations

from contextlib import contextmanager

import fcntl
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from repo_agent.envelope import Request, Result

from repo_agent.adapters_cli import CommandError, hermes_kanban_json, run_cmd
from repo_agent.adapters_git import (
    branch_config_get,
    branch_exists,
    delete_local_branch,
    is_dirty,
    local_branch_head,
    parse_worktree_porcelain,
    status_porcelain,
    worktree_list,
    worktree_remove,
)

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
_TERMINAL_PROCESS_STATUSES = {"failed", "cancelled", "timed_out"}



def _task_marker_matches(task: object, marker: str) -> bool:
    if not isinstance(task, dict):
        return False
    body = str(task.get("body") or task.get("description") or "")
    return bool(re.search(r"(?m)^Idempotency-Key:\s*" + re.escape(marker) + r"$", body))


def _cleanup_provenance(data: dict[str, object], cfg: dict[str, object], branch: str, conduction: dict[str, object] | None = None) -> dict[str, str]:
    conduction = conduction or {}
    parsed = next((conduction[key] for key in ("dispatch_parse_issue_ref", "parse_issue_ref") if isinstance(conduction.get(key), dict)), {})
    receipt = next((conduction[key] for key in ("dispatch_write_dispatch_receipt", "write_dispatch_receipt") if isinstance(conduction.get(key), dict)), {})
    return {
        "task": str(data.get("task_id") or cfg.get("task_id") or parsed.get("task_id") or "").strip(),
        "issue": str(data.get("issue") or cfg.get("issue") or parsed.get("issue") or "").strip(),
        "receipt": str(data.get("receipt_id") or cfg.get("receipt_id") or receipt.get("receipt_path") or data.get("receipt_path") or cfg.get("receipt_path") or "").strip(),
        "repo": str(data.get("repo") or cfg.get("repo") or parsed.get("repo") or "").strip(),
        "branch": branch,
    }


def _cleanup_owner_matches(clone_path: str, branch: str, expected: dict[str, str]) -> bool:
    keys = ("task", "issue", "receipt", "repo")
    if not all(expected.get(key) for key in keys):
        return False
    for key in keys:
        try:
            if branch_config_get(clone_path, branch, f"repo-agent-{key}").strip() != expected[key]:
                return False
        except CommandError:
            return False
    return True


def _task_id(task: dict[str, object]) -> object:
    return task.get("id") or task.get("task_id")


def parse_issue_from_branch(request: Request) -> Result:
    """Pure: extract issue number from ai/fix/<n>-... branch name."""
    data = input_of(request)
    context = cond_blob(request, "dispatch_parse_issue_ref", "dispatch_prepare_worktree", "dispatch_write_dispatch_receipt", "triage_load_pr_fields", "triage_repair_prepare_worktree", "triage_repair_push_branch", "repair_push_branch", "write_merge_receipt", "comment_pr")
    branch = str(data.get("branch") or cfg_of(request).get("branch") or context.get("branch") or "")
    if not branch:
        return noop("no_branch", branch=branch)
    m = re.search(r"(?:^|/)ai/fix/(\d+)", branch)
    if not m:
        m = re.search(r"/(\d+)(?:-|$)", branch)
    if not m:
        return fail("unparseable_branch", failure_class="terminal", retry_safe=False, branch=branch, idempotency_key=f"cleanup:branch:{branch}:parse")
    return ok(status="parsed", issue=int(m.group(1)), branch=branch, **{key: context[key] for key in ("repo", "clone_path", "worktree_path", "task_id", "receipt_path") if context.get(key) not in (None, "")})


def check_issue_closed(request: Request) -> Result:
    """Read GitHub issue state."""
    import json

    data = input_of(request)
    cfg = cfg_of(request)
    parsed = cond_blob(request, "parse_issue_from_branch", "parse", "cleanup_parse_issue_from_branch")
    upstream = upstream_noop(request, "parse_issue_from_branch", "cleanup_parse_issue_from_branch")
    if upstream:
        return noop(str(upstream.get("reason") or "no_branch"), **{k: v for k, v in upstream.items() if k not in {"status", "ok", "mutated", "reason", "dry_run"}})
    repo = str(data.get("repo") or parsed.get("repo") or cfg.get("repo") or "")
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


def check_no_open_pr_for_branch(request: Request) -> Result:
    """True when no open PR exists for head branch."""
    import json

    data = input_of(request)
    cfg = cfg_of(request)
    upstream = upstream_noop(request, "parse_issue_from_branch", "cleanup_parse_issue_from_branch")
    if upstream:
        return noop(str(upstream.get("reason") or "no_branch"), **{k: v for k, v in upstream.items() if k not in {"status", "ok", "mutated", "reason", "dry_run"}})
    parsed = cond_blob(request, "parse_issue_from_branch", "parse", "cleanup_parse_issue_from_branch")
    repo = str(data.get("repo") or parsed.get("repo") or cfg.get("repo") or "")
    branch = str(data.get("branch") or parsed.get("branch") or cfg.get("branch") or "")
    gh = str(cfg.get("gh_cli") or "gh")
    if not repo or not branch:
        return fail("missing_repo_or_branch", failure_class="terminal", retry_safe=False, repo=repo, branch=branch, idempotency_key=f"cleanup:pr:{repo}:{branch}:check-open")
    try:
        proc = run_cmd(
            [gh, "pr", "list", "--repo", repo, "--head", branch, "--state", "open", "--json", "number"],
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
    return ok(status="checked", open_count=len(prs), safe_to_cleanup=len(prs) == 0, prs=prs)


def remove_worktree(request: Request) -> Result:
    """Remove one git worktree path (force optional)."""
    data = input_of(request)
    dry = dry_run_flag(request)
    cfg = cfg_of(request)
    closed_blob = cond_blob(request, "check_issue_closed", "cleanup_check_issue_closed")
    open_pr_blob = cond_blob(request, "check_no_open_pr", "check_no_open_pr_for_branch", "cleanup_check_no_open_pr")
    require_safe = bool(data.get("require_safe", cfg.get("require_safe", True)))
    cleanup_key = f"cleanup:worktree:{data.get('clone_path') or cfg.get('clone_path') or ''}:{data.get('worktree_path') or cfg.get('worktree_path') or ''}:remove"
    for guard_name, guard in (("check_issue_closed", closed_blob), ("check_no_open_pr", open_pr_blob)):
        if not guard or guard.get("status") in _TERMINAL_PROCESS_STATUSES:
            guard_class = str(guard.get("failure_class") or "terminal") if isinstance(guard, dict) else "terminal"
            guard_retry_safe = bool(guard.get("retry_safe", False)) if isinstance(guard, dict) else False
            return fail("cleanup_guard_failed", failure_class=guard_class, retry_safe=guard_retry_safe, guard=guard_name, guard_output=guard, clone_path=str(data.get("clone_path") or cfg.get("clone_path") or ""), worktree_path=str(data.get("worktree_path") or cfg.get("worktree_path") or ""), idempotency_key=cleanup_key)
    upstream = upstream_noop(request, "check_issue_closed", "check_no_open_pr", "parse_issue_from_branch", "cleanup_check_issue_closed", "cleanup_check_no_open_pr", "cleanup_parse_issue_from_branch")
    if upstream:
        return noop(str(upstream.get("reason") or "cleanup_not_ready"))
    if require_safe and (closed_blob or open_pr_blob):
        if closed_blob and closed_blob.get("closed") is False:
            return noop("issue_still_open", closed=False, issue=closed_blob.get("issue"))
        if open_pr_blob and open_pr_blob.get("safe_to_cleanup") is False:
            return noop("open_pr_exists", safe_to_cleanup=False, open_count=open_pr_blob.get("open_count"))
    prepared = cond_blob(request, "dispatch_prepare_worktree", "prepare_worktree", "triage_repair_prepare_worktree")
    parsed = cond_blob(request, "dispatch_parse_issue_ref", "parse_issue_from_branch", "cleanup_parse_issue_from_branch")
    clone_path = str(data.get("clone_path") or cfg.get("clone_path") or prepared.get("clone_path") or parsed.get("clone_path") or "")
    worktree_path = str(data.get("worktree_path") or cfg.get("worktree_path") or prepared.get("worktree_path") or "")
    force = bool(data.get("force", False))
    if not clone_path or not worktree_path:
        return fail("missing_clone_or_worktree", failure_class="terminal", retry_safe=False, clone_path=clone_path, worktree_path=worktree_path, idempotency_key=cleanup_key)
    worktree = Path(worktree_path).resolve()
    if dry:
        return planned(clone_path=clone_path, worktree_path=str(worktree), force=force)
    if not worktree.exists():
        expected_branch = str(data.get("branch") or cfg.get("branch") or parsed.get("branch") or prepared.get("branch") or "")
        if not expected_branch:
            return fail("worktree_missing", failure_class="terminal", retry_safe=False, clone_path=clone_path, worktree_path=str(worktree), branch=expected_branch, idempotency_key=cleanup_key)
        expected = _cleanup_provenance(data, cfg, expected_branch, data.get("conduction") if isinstance(data.get("conduction"), dict) else None)
        try:
            if status_porcelain(clone_path).strip():
                return fail("clone_dirty", failure_class="terminal", retry_safe=False, clone_path=clone_path, worktree_path=str(worktree), mutated=False)
            if not branch_exists(clone_path, expected_branch) or not _cleanup_owner_matches(clone_path, expected_branch, expected):
                return fail("worktree_provenance_unavailable", failure_class="terminal", retry_safe=False, clone_path=clone_path, worktree_path=str(worktree), branch=expected_branch, mutated=False, idempotency_key=cleanup_key)
        except CommandError as exc:
            return fail("worktree_provenance_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), clone_path=clone_path, worktree_path=str(worktree), branch=expected_branch, mutated=False, idempotency_key=cleanup_key)
        return ok(status="already_absent", clone_path=clone_path, worktree_path=str(worktree), branch=expected_branch, idempotency_key=cleanup_key, mutated=False, retry_safe=True)
    expected_branch = str(data.get("branch") or cfg.get("branch") or parsed.get("branch") or prepared.get("branch") or "")
    expected = _cleanup_provenance(data, cfg, expected_branch, data.get("conduction") if isinstance(data.get("conduction"), dict) else None)
    try:
        if status_porcelain(clone_path).strip():
            return fail("clone_dirty", failure_class="terminal", retry_safe=False, clone_path=clone_path, mutated=False)
        rows = parse_worktree_porcelain(worktree_list(clone_path))
    except CommandError as exc:
        return fail("worktree_provenance_read_failed", failure_class="retryable_read", retry_safe=True, mutated=False, error=str(exc), clone_path=clone_path, worktree_path=str(worktree), branch=expected_branch, idempotency_key=cleanup_key)
    matches = [row for row in rows if str(Path(row.get("path") or "").resolve()) == str(worktree)]
    if not matches:
        return fail("worktree_provenance_mismatch", failure_class="terminal", retry_safe=False, clone_path=clone_path, worktree_path=str(worktree), branch=expected_branch, mutated=False, idempotency_key=cleanup_key)
    row = matches[0]
    if row.get("locked") or str(row.get("branch") or "") != expected_branch:
        return fail("foreign_worktree_ownership", failure_class="terminal", retry_safe=False, clone_path=clone_path, worktree_path=str(worktree), branch=expected_branch, actual_branch=row.get("branch"), locked=bool(row.get("locked")), mutated=False, idempotency_key=cleanup_key)
    if not _cleanup_owner_matches(clone_path, expected_branch, expected):
        return fail("foreign_worktree_ownership", failure_class="terminal", retry_safe=False, clone_path=clone_path, worktree_path=str(worktree), branch=expected_branch, mutated=False, idempotency_key=cleanup_key)
    if is_dirty(str(worktree)):
        return fail("worktree_dirty", failure_class="terminal", retry_safe=False, clone_path=clone_path, worktree_path=str(worktree), branch=expected_branch, mutated=False, idempotency_key=cleanup_key)
    try:
        worktree_remove(clone_path, str(worktree), force=force)
    except CommandError as exc:
        try:
            remaining = parse_worktree_porcelain(worktree_list(clone_path))
        except CommandError as readback_exc:
            return fail("remove_readback_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(readback_exc), remove_error=str(exc), clone_path=clone_path, worktree_path=str(worktree), branch=expected_branch, mutated=True, idempotency_key=cleanup_key)
        still_present = any(str(Path(row.get("path") or "").resolve()) == str(worktree) for row in remaining)
        if still_present:
            return fail("remove_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), clone_path=clone_path, worktree_path=str(worktree), branch=expected_branch, mutated=False, idempotency_key=cleanup_key)
        return ok(status="removed", clone_path=clone_path, worktree_path=str(worktree), branch=expected_branch, idempotency_key=cleanup_key, mutated=True, retry_safe=True, reconciled=True)
    try:
        remaining = parse_worktree_porcelain(worktree_list(clone_path))
    except CommandError as exc:
        return fail("remove_readback_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), clone_path=clone_path, worktree_path=str(worktree), branch=expected_branch, mutated=True, idempotency_key=cleanup_key)
    if any(str(Path(row.get("path") or "").resolve()) == str(worktree) for row in remaining):
        return fail("remove_not_confirmed", failure_class="reconcile_then_retry", retry_safe=False, clone_path=clone_path, worktree_path=str(worktree), branch=expected_branch, mutated=True, idempotency_key=cleanup_key)
    return ok(status="removed", clone_path=clone_path, worktree_path=str(worktree), branch=expected_branch, idempotency_key=cleanup_key, mutated=True, retry_safe=True)


def delete_local_fix_branch(request: Request) -> Result:
    """Delete local branch only (never remote)."""
    data = input_of(request)
    dry = dry_run_flag(request)
    cfg = cfg_of(request)
    parsed = cond_blob(request, "parse_issue_from_branch", "parse", "cleanup_parse_issue_from_branch")
    removed = cond_blob(request, "remove_worktree", "cleanup_remove_worktree")
    clone_path = str(data.get("clone_path") or parsed.get("clone_path") or cfg.get("clone_path") or "")
    branch = str(data.get("branch") or parsed.get("branch") or cfg.get("branch") or "")
    force = bool(data.get("force", True))
    if dry:
        return planned(clone_path=clone_path, branch=branch, force=force)
    if removed and removed.get("status") in {"noop", *_TERMINAL_PROCESS_STATUSES}:
        return noop("skipped_after_remove_guard", upstream_reason=removed.get("reason"), upstream_status=removed.get("status"))
    if removed and (removed.get("ok") is not True or removed.get("status") not in {"removed", "already_absent"}):
        return fail("remove_worktree_evidence_missing", failure_class="terminal", retry_safe=False, evidence=removed)
    if not removed and not all(data.get(key) for key in ("task_id", "issue", "receipt_path", "repo")):
        return fail("remove_worktree_evidence_missing", failure_class="terminal", retry_safe=False, evidence=removed)
    if not clone_path or not branch:
        return fail("missing_clone_or_branch", failure_class="terminal", retry_safe=False, clone_path=clone_path, branch=branch, idempotency_key=f"cleanup:branch:{clone_path}:{branch}:delete")
    key = f"branch:{clone_path}:{branch}:delete"
    expected = _cleanup_provenance(data, cfg, branch, data.get("conduction") if isinstance(data.get("conduction"), dict) else None)
    try:
        if not branch_exists(clone_path, branch):
            return ok(status="already_absent", clone_path=clone_path, branch=branch, idempotency_key=key, mutated=False)
        if not _cleanup_owner_matches(clone_path, branch, expected):
            return fail("foreign_branch_ownership", failure_class="terminal", retry_safe=False, clone_path=clone_path, branch=branch, mutated=False, idempotency_key=key)
    except CommandError as exc:
        return fail("branch_provenance_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), clone_path=clone_path, branch=branch, mutated=False, idempotency_key=key)
    try:
        delete_local_branch(clone_path, branch, force=force)
    except CommandError as exc:
        try:
            still_exists = branch_exists(clone_path, branch)
        except CommandError as readback_exc:
            return fail("delete_readback_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(readback_exc), delete_error=str(exc), branch=branch, idempotency_key=key, mutated=True)
        if still_exists:
            return fail("delete_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), branch=branch, idempotency_key=key, mutated=False)
        return ok(status="deleted", branch=branch, idempotency_key=key, mutated=True, retry_safe=True, reconciled=True)
    try:
        if branch_exists(clone_path, branch):
            return fail("delete_not_confirmed", failure_class="reconcile_then_retry", retry_safe=False, branch=branch, idempotency_key=key, mutated=True)
    except CommandError as exc:
        return fail("delete_readback_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), branch=branch, idempotency_key=key, mutated=True)
    return ok(status="deleted", branch=branch, idempotency_key=key, mutated=True, retry_safe=True)


def release_active_issue_claim(request: Request) -> Result:
    """Remove an active-issue claim only after every cleanup guard succeeds."""
    import json

    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    raw_conduction = data.get("conduction")
    if not isinstance(raw_conduction, dict) or not raw_conduction:
        return fail("cleanup_evidence_missing", failure_class="terminal", retry_safe=False)

    def evidence(*names: str) -> dict[str, object] | None:
        for name in names:
            blob = raw_conduction.get(name)
            if isinstance(blob, dict):
                return blob
        return None

    parsed = evidence("parse_issue_from_branch", "cleanup_parse_issue_from_branch")
    if parsed is not None and parsed.get("ok") is True and parsed.get("reason") == "no_branch":
        return noop("no_branch")
    removed = evidence("remove_worktree", "cleanup_remove_worktree")
    closed = evidence("check_issue_closed", "cleanup_check_issue_closed")
    no_open_pr = evidence("check_no_open_pr", "cleanup_check_no_open_pr")
    deleted = evidence("delete_local_fix_branch", "cleanup_delete_local_fix_branch")
    if dry:
        for name in ("parse_issue_from_branch", "cleanup_parse_issue_from_branch", "remove_worktree", "cleanup_remove_worktree", "check_issue_closed", "cleanup_check_issue_closed", "check_no_open_pr", "cleanup_check_no_open_pr", "delete_local_fix_branch", "cleanup_delete_local_fix_branch"):
            blob = evidence(name)
            if blob is not None and blob.get("status") in {"noop", "planned"}:
                details = {k: v for k, v in blob.items() if k not in {"status", "ok", "mutated", "reason", "dry_run"}}
                return noop(str(blob.get("reason") or "cleanup_planned"), **details)
    if removed is None:
        return fail("remove_worktree_evidence_missing", failure_class="terminal", retry_safe=False)
    if removed.get("ok") is not True or removed.get("status") not in {"removed", "already_absent"}:
        return fail("remove_worktree_not_successful", failure_class="terminal", retry_safe=False, evidence=removed)
    if closed is None:
        return fail("check_issue_closed_evidence_missing", failure_class="terminal", retry_safe=False)
    if closed.get("ok") is not True or closed.get("closed") is not True:
        return fail("issue_not_closed", failure_class="terminal", retry_safe=False, evidence=closed)
    if no_open_pr is None:
        return fail("check_no_open_pr_evidence_missing", failure_class="terminal", retry_safe=False)
    if no_open_pr.get("ok") is not True or no_open_pr.get("safe_to_cleanup") is not True:
        return fail("open_pr_cleanup_unsafe", failure_class="terminal", retry_safe=False, evidence=no_open_pr)
    if deleted is None:
        return fail("delete_local_fix_branch_evidence_missing", failure_class="terminal", retry_safe=False)
    if deleted.get("ok") is not True or deleted.get("status") not in {"deleted", "already_absent"}:
        return fail("delete_local_fix_branch_not_successful", failure_class="terminal", retry_safe=False, evidence=deleted)

    claim_path = str(data.get("claim_path") or cfg.get("active_issue_path") or "").strip()
    repo = str(data.get("repo") or closed.get("repo") or "").strip()
    raw_issue = data.get("issue") or (parsed or {}).get("issue") or closed.get("issue")
    if isinstance(raw_issue, bool):
        issue = 0
    else:
        try:
            issue = int(raw_issue)
        except (TypeError, ValueError):
            issue = 0
    if not claim_path:
        return fail("missing_claim_path", failure_class="terminal", retry_safe=False)
    if not repo or issue <= 0:
        return fail("missing_claim_identity", failure_class="terminal", retry_safe=False, repo=repo, issue=issue)
    path = Path(claim_path).expanduser()
    if not path.exists():
        return ok(status="already_absent", claim_path=str(path), repo=repo, issue=issue, mutated=False)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return fail("claim_corrupt", failure_class="terminal", retry_safe=False, claim_path=str(path), error=str(exc))
    if not isinstance(payload, dict):
        return fail("claim_corrupt", failure_class="terminal", retry_safe=False, claim_path=str(path), payload=payload)
    claim_repo = str(payload.get("repo") or "").strip()
    claim_board = str(payload.get("board") or "").strip()
    claim_at = str(payload.get("claimedAt") or "").strip()
    claim_issue = payload.get("issue")




    if payload.get("version") != 1 or not claim_repo or not claim_board or not claim_at or isinstance(claim_issue, bool) or not isinstance(claim_issue, int) or claim_issue <= 0:
        return fail("claim_malformed", failure_class="terminal", retry_safe=False, claim_path=str(path), payload=payload)
    if claim_repo != repo:
        return fail("claim_repo_mismatch", failure_class="terminal", retry_safe=False, claim_path=str(path), payload=payload, repo=repo)
    if claim_issue != issue:
        return fail("claim_issue_mismatch", failure_class="terminal", retry_safe=False, claim_path=str(path), payload=payload, issue=issue)
    if dry:
        return planned(claim_path=str(path), payload=payload, repo=repo, issue=issue)
    try:
        path.unlink()
    except OSError as exc:
        return fail("unlink_failed", failure_class="reconcile_then_retry", retry_safe=False, claim_path=str(path), error=str(exc), mutated=True)
    return ok(status="released", claim_path=str(path), repo=repo, issue=issue, mutated=True)


@contextmanager
def _receipt_directory_lock(directory: Path):
    """Serialize publication, reconciliation, and rollback in one directory."""
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _publish_cleanup_receipt(p: Path, payload: dict[str, Any], path: str) -> Result:
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
    tmp_path: Path | None = None
    published_identity: tuple[int, int] | None = None
    try:
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
        published = p.stat()
        published_identity = (published.st_dev, published.st_ino)
        os.unlink(tmp_path)
        tmp_path = None
        dir_fd = os.open(str(p.parent), os.O_RDONLY)
        try:
            try:
                os.fsync(dir_fd)
            except OSError:
                try:
                    current = p.stat()
                    if published_identity == (current.st_dev, current.st_ino):
                        os.unlink(p)
                except OSError:
                    pass
                try:
                    os.fsync(dir_fd)
                except OSError:
                    pass
                raise
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


def write_cleanup_receipt(request: Request) -> Result:
    data = input_of(request)
    cfg = cfg_of(request)
    path = str(data.get("receipt_path") or cfg.get("receipt_path") or "").strip()
    if not path:
        return fail("missing_receipt_path", failure_class="terminal", retry_safe=False)
    conduction = data.get("conduction")
    required = {
        "parse_issue_from_branch": ("parse_issue_from_branch", "cleanup_parse_issue_from_branch"),
        "check_issue_closed": ("check_issue_closed", "cleanup_check_issue_closed"),
        "check_no_open_pr": ("check_no_open_pr", "cleanup_check_no_open_pr"),
        "remove_worktree": ("remove_worktree", "cleanup_remove_worktree"),
        "delete_local_fix_branch": ("delete_local_fix_branch", "cleanup_delete_local_fix_branch"),
        "release_active_issue_claim": ("release_active_issue_claim", "cleanup_release_active_issue_claim"),
    }
    if not isinstance(conduction, dict):
        return fail("cleanup_evidence_missing", failure_class="terminal", retry_safe=False, receipt_path=path)
    evidence = {
        name: next((conduction[key] for key in aliases if isinstance(conduction.get(key), dict)), None)
        for name, aliases in required.items()
    }
    if any(blob is None for blob in evidence.values()):
        return fail("cleanup_evidence_missing", failure_class="terminal", retry_safe=False, receipt_path=path)
    steps = {name: {"status": blob.get("status"), "mutated": bool(blob.get("mutated", False)), "reason": blob.get("reason"), "failure": blob.get("failure_class") or blob.get("error")} for name, blob in evidence.items()}
    cancelled = any(blob.get("status") in {"cancelled", "timed_out"} for blob in evidence.values())
    failed = any(blob.get("ok") is False for blob in evidence.values())
    if cancelled:
        outcome = "cancelled"
    elif failed:
        outcome = "partial" if any(item["mutated"] for item in steps.values()) else "failure"
    elif all(blob.get("ok") is True for blob in evidence.values()):
        outcome = "noop" if all(blob.get("status") in {"noop", "already_absent"} for blob in evidence.values()) else "success"
    else:
        return fail("cleanup_evidence_inconclusive", failure_class="terminal", retry_safe=False, receipt_path=path, steps=steps)
    payload = dict(data.get("payload") or {})
    payload.update({"phase": "CLEANUP_TERMINAL", "outcome": outcome, "run_id": data.get("run_id") or cfg.get("run_id") or request.get("run_id") or "", "path_id": data.get("path_id") or cfg.get("path_id") or request.get("path_id") or "cleanup", "process_id": data.get("process_id") or cfg.get("process_id") or request.get("process_id") or "", "entity": {"task": data.get("task_id") or cfg.get("task_id") or "", "repo": data.get("repo") or cfg.get("repo") or "", "issue": data.get("issue") or cfg.get("issue") or "", "receipt": data.get("receipt_id") or path, "branch": data.get("branch") or cfg.get("branch") or "", "clone_path": data.get("clone_path") or cfg.get("clone_path") or "", "worktree_path": data.get("worktree_path") or cfg.get("worktree_path") or ""}, "steps": steps})
    if dry_run_flag(request):
        return planned(receipt_path=path, payload=payload)
    p = Path(path)
    try:
        with _receipt_directory_lock(p.parent):
            return _publish_cleanup_receipt(p, payload, path)
    except OSError as exc:
        return fail("receipt_write_failed", failure_class="terminal", retry_safe=False, error=str(exc), receipt_path=path, mutated=False)

def create_maintenance_task(request: Request) -> Result:
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
