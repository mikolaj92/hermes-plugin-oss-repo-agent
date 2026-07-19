"""Mega-atomic effectors: PR triage domain."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from fala.adapters import EffectorRunRequest, EffectorRunResult

from repo_agent.adapters_cli import CommandError, run_cmd
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


def _json_output(stdout: str, default: Any) -> Any:
    """Decode optional gh JSON; test doubles and old gh versions may be blank."""
    text = (stdout or "").strip()
    if not text:
        return default
    return json.loads(text)


def _names(values: Any, key: str = "login") -> set[str]:
    return {str(item.get(key) or item) for item in (values or []) if item}
def _json_state(proc: Any, default: Any = None) -> Any:
    """Decode a read-back response; blank test doubles remain compatible."""
    try:
        return _json_output(getattr(proc, "stdout", ""), default)
    except json.JSONDecodeError:
        return default


def _pr_view(gh: str, repo: str, number: int, fields: str) -> Any:
    return run_cmd(
        [gh, "pr", "view", str(number), "--repo", repo, "--json", fields],
        timeout=60,
    )


def _comment_bodies(value: Any) -> list[str]:
    comments = value.get("comments") if isinstance(value, dict) else value
    if not isinstance(comments, list):
        return []
    return [str(item.get("body") or "") for item in comments if isinstance(item, dict)]


def list_ai_fix_prs(request: EffectorRunRequest) -> EffectorRunResult:
    """List open PRs with head branch matching ai/fix/* (or branch_prefix)."""
    data = input_of(request)
    cfg = cfg_of(request)
    repo = str(data.get("repo") or cfg.get("repo") or "")
    prefix = str(data.get("branch_prefix") or cfg.get("branch_prefix") or "ai/fix")
    limit = int(data.get("limit") or 50)
    gh = str(cfg.get("gh_cli") or "gh")
    if not repo:
        return fail("missing_repo", failure_class="terminal", retry_safe=False)
    try:
        proc = run_cmd(
            [
                gh,
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--limit",
                str(limit),
                "--json",
                "number,title,url,headRefName,author,labels,mergeable,statusCheckRollup",
            ],
            timeout=90,
        )
        prs = json.loads(proc.stdout or "")
        if not isinstance(prs, list) or any(not isinstance(pr, dict) for pr in prs):
            raise ValueError("invalid PR list read-back shape")
    except CommandError as exc:
        return fail("pr_list_failed", failure_class="retryable_read", retry_safe=True, error=str(exc))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("invalid_pr_list", failure_class="terminal", retry_safe=False, error=str(exc))
    selected = [
        p
        for p in prs
        if str(p.get("headRefName") or "").startswith(prefix)
    ]
    if not selected:
        return noop("no_open_prs", repo=repo, count=0, prs=[], all_open_count=len(prs))
    return ok(
        status="listed",
        repo=repo,
        count=len(selected),
        prs=selected,
        all_open_count=len(prs),
    )


def load_pr_fields(request: EffectorRunRequest) -> EffectorRunResult:
    """Load one PR JSON bundle for triage decisions."""
    data = input_of(request)
    cfg = cfg_of(request)
    listed = cond_blob(request, "list_ai_fix_prs", "list")
    upstream = upstream_noop(request, "list_ai_fix_prs")
    if upstream:
        return noop(str(upstream.get("reason") or "no_open_prs"))
    repo = str(data.get("repo") or listed.get("repo") or cfg.get("repo") or "")
    number = int(data.get("number") or data.get("pr_number") or 0)
    if not number:
        prs = listed.get("prs") or []
        if isinstance(prs, list) and prs:
            first = prs[0] if isinstance(prs[0], dict) else {}
            number = int(first.get("number") or 0)
    gh = str(cfg.get("gh_cli") or "gh")
    if not repo or not number:
        return fail("missing_repo_or_number", failure_class="terminal", retry_safe=False)
    try:
        proc = run_cmd(
            [
                gh,
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "number,title,url,body,state,isDraft,headRefName,headRefOid,baseRefName,"
                "author,labels,mergeable,reviewDecision,statusCheckRollup,commits",
            ],
            timeout=60,
        )
        pr = json.loads(proc.stdout or "")
        if not isinstance(pr, dict) or not pr:
            raise ValueError("invalid PR read-back shape")
    except CommandError as exc:
        return fail("pr_view_failed", failure_class="retryable_read", retry_safe=True, error=str(exc))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("invalid_pr_readback", failure_class="terminal", retry_safe=False, error=str(exc))
    return ok(status="loaded", repo=repo, number=number, pr=pr)


def evaluate_checks(request: EffectorRunRequest) -> EffectorRunResult:
    """Pure decision: do status checks pass? (from pr.statusCheckRollup)."""
    data = input_of(request)
    pr = data["pr"] if "pr" in data else (cond_get(request, "pr", "load_pr_fields") or {})
    if not isinstance(pr, dict):
        return fail(
            "invalid_checks_read",
            failure_class="terminal",
            retry_safe=False,
            mutated=False,
            error="PR payload must be an object",
        )
    upstream = upstream_noop(request, "load_pr_fields")
    if upstream:
        return noop(str(upstream.get("reason") or "no_open_prs"))
    require_checks = bool(
        data.get(
            "require_checks",
            cfg_of(request).get("require_checks", True),
        )
    )
    allow_no_checks = not require_checks
    if "statusCheckRollup" not in pr:
        rollup = []
    else:
        raw_rollup = pr["statusCheckRollup"]
        if not isinstance(raw_rollup, list) or any(not isinstance(item, dict) for item in raw_rollup):
            return fail(
                "invalid_checks_read",
                failure_class="terminal",
                retry_safe=False,
                mutated=False,
                error="statusCheckRollup must be a list of objects",
            )
        rollup = raw_rollup
    if not rollup:
        if allow_no_checks:
            return ok(status="no_checks", pass_=True, allow_no_checks=True)
        return ok(status="no_checks", pass_=False, allow_no_checks=False)
    failures = []
    pending = []
    successful = {"SUCCESS", "NEUTRAL", "SKIPPED"}
    failed = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}
    waiting = {"PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", "WAITING"}
    known = successful | failed | waiting
    for item in rollup:
        conclusion_value = item.get("conclusion")
        state_value = item.get("state")
        values = []
        for field, value in (("conclusion", conclusion_value), ("state", state_value)):
            if value is None or value == "":
                continue
            if not isinstance(value, str) or value.upper() not in known:
                return fail(
                    "invalid_checks_read",
                    failure_class="terminal",
                    retry_safe=False,
                    mutated=False,
                    error=f"unknown {field} in statusCheckRollup",
                )
            values.append(value.upper())
        if not values:
            return fail(
                "invalid_checks_read",
                failure_class="terminal",
                retry_safe=False,
                mutated=False,
                error="check rollup item has no conclusion or state",
            )
        conclusion = values[0]
        name = str(item.get("name") or item.get("context") or "?")
        if any(value in failed for value in values):
            failures.append(name)
        elif any(value in waiting for value in values):
            pending.append(name)
    if failures:
        return ok(status="checks_failed", pass_=False, failures=failures, pending=pending)
    if pending:
        return ok(status="checks_pending", pass_=False, pending=pending)
    return ok(status="checks_passed", pass_=True)


def evaluate_test_evidence(request: EffectorRunRequest) -> EffectorRunResult:
    """Pure: does PR body contain test evidence markers?"""
    data = input_of(request)
    pr = data.get("pr") or cond_get(request, "pr", "load_pr_fields") or {}
    upstream = upstream_noop(request, "load_pr_fields")
    if upstream:
        return noop(str(upstream.get("reason") or "no_open_prs"))
    require = bool(
        data.get("require_test_evidence", cfg_of(request).get("require_test_evidence", False))
    )
    body = str(pr.get("body") or "")
    markers = data.get("markers") or [
        "Test plan",
        "test evidence",
        "How to test",
        "pytest",
        "unittest",
        "Verified",
    ]
    hits = [m for m in markers if m.lower() in body.lower()]
    present = bool(hits)
    if not require:
        return ok(status="evidence_optional", pass_=True, present=present, hits=hits)
    if present:
        return ok(status="evidence_present", pass_=True, hits=hits)
    return ok(status="evidence_missing", pass_=False, hits=[])


def decide_triage_action(request: EffectorRunRequest) -> EffectorRunResult:
    """Pure router decision: merge | comment_block | repair | skip."""
    data = input_of(request)
    cfg = cfg_of(request)
    pr = data.get("pr") or cond_get(request, "pr", "load_pr_fields") or {}
    checks = cond_blob(request, "evaluate_checks", "checks")
    evidence = cond_blob(request, "evaluate_test_evidence", "evidence")
    checks_pass = bool(
        data.get(
            "checks_pass",
            data.get("pass_", checks.get("pass_", checks.get("pass"))),
        )
    )
    upstream = upstream_noop(request, "list_ai_fix_prs", "load_pr_fields", "evaluate_checks", "evaluate_test_evidence")
    if upstream:
        return noop(str(upstream.get("reason") or "no_open_prs"))
    if not isinstance(pr, dict):
        return fail(
            "invalid_pr",
            failure_class="terminal",
            retry_safe=False,
            error="PR payload must be an object",
        )
    evidence_pass = bool(
        data.get(
            "evidence_pass",
            evidence.get("pass_", evidence.get("pass", True)),
        )
    )
    automerge = bool(data.get("automerge", cfg.get("automerge", False)))
    branch_prefix = str(data.get("branch_prefix") or cfg.get("branch_prefix") or "ai/fix")
    base_branch = str(data.get("base_branch") or cfg.get("base_branch") or "main")
    require_owner = bool(data.get("require_owner", cfg.get("require_owner", True)))
    repo = str(data.get("repo") or cond_get(request, "repo", "load_pr_fields", "list_ai_fix_prs") or cfg.get("repo") or "")
    repo_owner = repo.split("/", 1)[0] if "/" in repo else str(cfg.get("assignee") or "")
    mergeable = str(pr.get("mergeable") or pr.get("mergeStateStatus") or "").upper()
    if mergeable == "CLEAN":
        mergeable = "MERGEABLE"
    state = str(pr.get("state") or "").upper()
    head = str(pr.get("headRefName") or "")
    base = str(pr.get("baseRefName") or "")
    is_draft = bool(pr.get("isDraft") or pr.get("is_draft"))
    author = pr.get("author") if isinstance(pr.get("author"), dict) else {}
    author_login = str(author.get("login") or pr.get("author") or "").strip()
    labels = {
        str(x.get("name") or "")
        for x in (pr.get("labels") or [])
        if isinstance(x, dict)
    }
    labels |= {str(x) for x in (pr.get("labels") or []) if isinstance(x, str)}
    if state and state != "OPEN":
        return ok(status="decided", action="skip", reason=f"state_{state.lower()}")
    if is_draft:
        return ok(status="decided", action="skip", reason="draft_pr")
    if head and not head.startswith(branch_prefix):
        return ok(status="decided", action="skip", reason="non_ai_fix_branch", head=head)
    if base and base != base_branch:
        return ok(status="decided", action="skip", reason="wrong_base", base=base, base_branch=base_branch)
    if require_owner and author_login and repo_owner and author_login != repo_owner:
        return ok(
            status="decided",
            action="skip",
            reason="external_author",
            author=author_login,
            owner=repo_owner,
        )
    if "ai:blocked" in labels:
        return ok(status="decided", action="skip", reason="ai_blocked_label")
    if not checks_pass:
        # failing checks → repair if agent-owned, else comment
        return ok(status="decided", action="repair", reason="checks_not_green")
    if not evidence_pass:
        return ok(status="decided", action="comment_block", reason="missing_test_evidence")
    if mergeable in {"CONFLICTING", "DIRTY"}:
        return ok(status="decided", action="repair", reason="merge_conflict")
    if automerge and mergeable in ("MERGEABLE", "UNKNOWN", ""):
        return ok(status="decided", action="merge", reason="ready")
    if automerge is False:
        return ok(status="decided", action="comment_block", reason="automerge_disabled")
    return ok(status="decided", action="skip", reason="not_mergeable", mergeable=mergeable)


def claim_pr_assignee(request: EffectorRunRequest) -> EffectorRunResult:
    """Assign PR to configured maintainer once."""
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    loaded = cond_blob(request, "load_pr_fields", "list_ai_fix_prs")
    pr = data.get("pr") or loaded.get("pr") or {}
    repo = str(data.get("repo") or loaded.get("repo") or cfg.get("repo") or "")
    number = int(data.get("number") or data.get("pr_number") or loaded.get("number") or (pr.get("number") if isinstance(pr, dict) else 0) or 0)
    assignee = str(data.get("assignee") or cfg.get("assignee") or "mikolaj92")
    gh = str(cfg.get("gh_cli") or "gh")
    key = f"pr:{repo or 'unknown'}:{number or 'unknown'}:assignee:{assignee}"
    context = {"repo": repo, "number": number, "assignee": assignee, "idempotency_key": key}
    if not repo or not number:
        return fail("missing_repo_or_number", failure_class="terminal", retry_safe=False, **context)
    if dry:
        return planned(**context)
    try:
        view = _pr_view(gh, repo, number, "assignees")
    except CommandError as exc:
        return fail(
            "assignee_read_failed",
            failure_class="retryable_read",
            retry_safe=True,
            error=str(exc),
            mutated=False,
            **context,
        )
    raw = (getattr(view, "stdout", "") or "").strip()
    if not raw:
        return fail("assignee_read_failed", failure_class="terminal", retry_safe=False, error="blank assignee read-back", mutated=False, **context)
    try:
        current = _json_output(raw, None)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("assignee_read_failed", failure_class="terminal", retry_safe=False, error=f"invalid assignee JSON: {exc}", mutated=False, **context)
    if not isinstance(current, dict) or not isinstance(current.get("assignees"), list):
        return fail("assignee_read_failed", failure_class="terminal", retry_safe=False, error="invalid assignee read-back shape", mutated=False, **context)
    names = _names(current["assignees"])
    if assignee in names:
        return ok(status="already_claimed", mutated=False, **context)
    if names:
        return fail("assignee_conflict", failure_class="terminal", retry_safe=False, assignees=sorted(names), mutated=False, **context)
    try:
        run_cmd([gh, "pr", "edit", str(number), "--repo", repo, "--add-assignee", assignee], timeout=60)
    except CommandError as exc:
        return fail("claim_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), mutated=True, **context)
    return ok(status="claimed", mutated=True, **context)
def comment_pr_once(request: EffectorRunRequest) -> EffectorRunResult:
    """Post one PR comment, reconciling an existing stable marker first."""
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    loaded = cond_blob(request, "load_pr_fields", "decide_triage_action")
    decide = cond_blob(request, "decide_triage_action", "decide")
    pr = data.get("pr") or loaded.get("pr") or {}
    repo = str(data.get("repo") or loaded.get("repo") or cfg.get("repo") or "")
    number = int(
        data.get("number")
        or loaded.get("number")
        or (pr.get("number") if isinstance(pr, dict) else 0)
        or 0
    )
    body = str(data.get("body") or "")
    if not body:
        reason = decide.get("reason") or "needs human review"
        body = (
            f"repo-agent triage: action=comment_block reason={reason}. "
            f"Please add test evidence or address blockers."
        )
    gh = str(cfg.get("gh_cli") or "gh")
    marker = f"repo-agent:{repo or 'unknown'}:{number or 'unknown'}:triage"
    hidden_marker = f"<!-- {marker} -->"
    key = f"pr:{repo or 'unknown'}:{number or 'unknown'}:comment:{marker}"
    context = {"repo": repo, "number": number, "comment_marker": marker, "idempotency_key": key}
    if not repo or not number or not body:
        return fail("missing_repo_number_or_body", failure_class="terminal", retry_safe=False, **context)
    if body.count(hidden_marker) > 1:
        return fail(
            "comment_marker_conflict",
            failure_class="terminal",
            retry_safe=False,
            **context,
        )
    posted_body = body if hidden_marker in body else f"{body.rstrip()}\n\n{hidden_marker}"
    if dry:
        return planned(**context, body=body[:200])
    try:
        view = _pr_view(gh, repo, number, "comments")
    except CommandError as exc:
        return fail(
            "comment_read_failed",
            failure_class="retryable_read",
            retry_safe=True,
            error=str(exc),
            mutated=False,
            **context,
        )
    raw = (getattr(view, "stdout", "") or "").strip()
    if not raw:
        return fail(
            "comment_read_failed",
            failure_class="terminal",
            retry_safe=False,
            error="blank comment read-back",
            mutated=False,
            **context,
        )
    try:
        existing = _json_output(raw, None)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail(
            "comment_read_failed",
            failure_class="terminal",
            retry_safe=False,
            error=f"invalid comment JSON: {exc}",
            mutated=False,
            **context,
        )
    comments = existing.get("comments") if isinstance(existing, dict) else existing
    if not isinstance(comments, list) or any(not isinstance(item, dict) for item in comments):
        return fail(
            "comment_read_failed",
            failure_class="terminal",
            retry_safe=False,
            error="invalid comment read-back shape",
            mutated=False,
            **context,
        )
    matches = sum(str(item.get("body") or "").count(hidden_marker) for item in comments)
    if matches > 1:
        return fail(
            "comment_marker_conflict",
            failure_class="terminal",
            retry_safe=False,
            matches=matches,
            mutated=False,
            **context,
        )
    if matches == 1:
        return ok(status="commented", reconciled=True, mutated=False, **context)
    try:
        run_cmd(
            [gh, "pr", "comment", str(number), "--repo", repo, "--body", posted_body],
            timeout=60,
        )
    except CommandError as exc:
        return fail(
            "comment_failed",
            failure_class="reconcile_then_retry",
            retry_safe=False,
            error=str(exc),
            mutated=True,
            **context,
        )
    return ok(status="commented", mutated=True, **context)





def merge_pull_request(request: EffectorRunRequest) -> EffectorRunResult:
    """Merge PR with optional --match-head-commit."""
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    loaded = cond_blob(request, "load_pr_fields", "claim_pr", "claim_pr_assignee")
    pr = data.get("pr") or loaded.get("pr") or {}
    repo = str(data.get("repo") or loaded.get("repo") or cfg.get("repo") or "")
    number = int(
        data.get("number")
        or data.get("pr_number")
        or loaded.get("number")
        or (pr.get("number") if isinstance(pr, dict) else 0)
        or 0
    )
    head_oid = str(
        data.get("head_oid")
        or data.get("headRefOid")
        or (pr.get("headRefOid") if isinstance(pr, dict) else "")
        or ""
    ).strip()
    method = str(data.get("merge_method") or cfg.get("merge_method") or "merge")
    gh = str(cfg.get("gh_cli") or "gh")
    key = f"pr:{repo or 'unknown'}:{number or 'unknown'}:merge:{head_oid or 'unknown'}"
    context = {"repo": repo, "number": number, "head_oid": head_oid, "idempotency_key": key}
    if not repo or not number:
        return fail("missing_repo_or_number", failure_class="terminal", retry_safe=False, **context)
    if dry:
        return planned(method=method, **context)
    if not head_oid:
        return fail("missing_head_oid", failure_class="terminal", retry_safe=False, **context)
    args = [gh, "pr", "merge", str(number), "--repo", repo, f"--{method}", "--match-head-commit", head_oid]
    try:
        run_cmd(args, timeout=120)
    except CommandError as exc:
        try:
            view = _pr_view(gh, repo, number, "state,mergedAt,mergeCommit,headRefOid")
            raw = (getattr(view, "stdout", "") or "").strip()
            payload = json.loads(raw)
            commit = payload.get("mergeCommit") if isinstance(payload, dict) else None
            observed_head = str(payload.get("headRefOid") or "").strip() if isinstance(payload, dict) else ""
            if (
                isinstance(payload, dict)
                and payload.get("state") == "MERGED"
                and payload.get("mergedAt")
                and isinstance(commit, dict)
                and str(commit.get("oid") or "").strip()
                and observed_head == head_oid
            ):
                return ok(status="merge_verified", merge_oid=str(commit["oid"]), reconciled=True, mutated=True, **context)
        except (CommandError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return fail(
            "merge_failed",
            failure_class="reconcile_then_retry",
            retry_safe=False,
            error=str(exc),
            stderr=exc.stderr[-400:],
            mutated=True,
            **context,
        )
    return ok(status="merged", mutated=True, **context)


def close_linked_issue(request: EffectorRunRequest) -> EffectorRunResult:
    """Close GitHub issue after merge, reusing an already-closed state."""
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    loaded = cond_blob(request, "load_pr_fields", "merge", "merge_pull_request")
    pr = data.get("pr") or loaded.get("pr") or {}
    repo = str(data.get("repo") or loaded.get("repo") or cfg.get("repo") or "")
    gh = str(cfg.get("gh_cli") or "gh")
    issue = int(data.get("issue") or data.get("number") or 0)
    if not issue and isinstance(pr, dict):
        import re
        match = re.search(r"(?:^|/)ai/fix/(\d+)", str(pr.get("headRefName") or ""))
        if match:
            issue = int(match.group(1))
    key = f"issue:{repo or 'unknown'}:{issue or 'unknown'}:close"
    context = {"repo": repo, "issue": issue, "idempotency_key": key}
    if not repo or not issue:
        return fail("missing_repo_or_issue", failure_class="terminal", retry_safe=False, **context)
    if dry:
        return planned(**context)
    def read_state() -> str:
        view = run_cmd([gh, "issue", "view", str(issue), "--repo", repo, "--json", "state"], timeout=60)
        raw = (view.stdout or "").strip()
        if not raw:
            raise ValueError("blank issue state read-back")
        payload = json.loads(raw)
        if not isinstance(payload, dict) or str(payload.get("state") or "").upper() not in {"OPEN", "CLOSED"}:
            raise ValueError("invalid issue state read-back")
        return str(payload["state"]).upper()

    try:
        state = read_state()
    except CommandError as exc:
        return fail(
            "close_read_failed",
            failure_class="retryable_read",
            retry_safe=True,
            error=str(exc),
            mutated=False,
            **context,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail(
            "close_read_failed",
            failure_class="terminal",
            retry_safe=False,
            error=str(exc),
            mutated=False,
            **context,
        )
    if state == "CLOSED":
        return ok(status="already_closed", mutated=False, **context)
    try:
        run_cmd([gh, "issue", "close", str(issue), "--repo", repo, "--reason", "completed"], timeout=60)
        final_state = read_state()
        if final_state != "CLOSED":
            return fail(
                "close_readback_mismatch",
                failure_class="reconcile_then_retry",
                retry_safe=False,
                state=final_state,
                mutated=True,
                **context,
            )
    except (CommandError, TypeError, ValueError, json.JSONDecodeError) as exc:
        try:
            if read_state() == "CLOSED":
                return ok(
                    status="closed",
                    mutated=True,
                    reconciled=True,
                    **context,
                )
        except (CommandError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return fail(
            "close_failed",
            failure_class="reconcile_then_retry",
            retry_safe=False,
            error=str(exc),
            mutated=True,
            **context,
        )
    return ok(status="closed", mutated=True, **context)
def write_merge_receipt(request: EffectorRunRequest) -> EffectorRunResult:
    """Write merge receipt atomically, reusing only an exact match."""
    data = input_of(request)
    dry = dry_run_flag(request)
    path = str(data.get("receipt_path") or cfg_of(request).get("receipt_path") or "")
    payload = data.get("payload")
    if not isinstance(payload, dict) or not payload:
        merge = cond_blob(request, "merge", "merge_pull_request")
        claim = cond_blob(request, "claim_pr", "claim_pr_assignee")
        payload = {"phase": "MERGED", "repo": merge.get("repo") or claim.get("repo"), "number": merge.get("number") or claim.get("number"), "dry_run": dry, "merge_status": merge.get("status")}
    if not path:
        return fail("missing_receipt_path", failure_class="terminal", retry_safe=False)
    if dry:
        return planned(receipt_path=path, payload=payload)
    try:
        p = Path(path)
        if p.exists():
            try:
                existing = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                return fail("receipt_conflict", failure_class="terminal", retry_safe=False, error=str(exc), receipt_path=path)
            if existing == payload:
                return ok(status="exists", receipt_path=path, payload=payload, mutated=False)
            return fail("receipt_conflict", failure_class="terminal", retry_safe=False, receipt_path=path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        with tmp.open("r+b") as fh:
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(p)
    except OSError as exc:
        return fail("receipt_write_failed", failure_class="terminal", retry_safe=False, error=str(exc), receipt_path=path, mutated=True)
    return ok(status="written", receipt_path=path, mutated=True)
