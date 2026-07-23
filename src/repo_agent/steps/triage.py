"""Mega-atomic effectors: PR triage domain."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from repo_agent.envelope import Request, Result

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

_TERMINAL_FAILURES = {"failed", "cancelled", "timed_out"}

def _decision_gate(request: Request, *, allowed: str | set[str]) -> Result | None:
    """No-op unless decide_triage_action selected the allowed action."""
    decide = cond_blob(request, "decide_triage_action", "decide", "triage_decide_triage_action")
    if not decide and "action" not in input_of(request):
        return None
    if decide.get("ok") is False or str(decide.get("status") or "") in {
        "failed",
        "cancelled",
        "timed_out",
    }:
        return fail(
            "upstream_failed",
            failure_class="terminal",
            retry_safe=False,
            upstream=decide,
            worked=False,
        )
    if decide.get("status") == "noop":
        return noop(
            str(decide.get("reason") or "no_selected_pr"),
            action=decide.get("action"),
            worked=False,
        )
    allowed_actions = {allowed} if isinstance(allowed, str) else set(allowed)
    action = str(input_of(request).get("action") or decide.get("action") or "")
    if action in allowed_actions:
        return None
    if not action or action == "skip":
        return noop(
            str(decide.get("reason") or "not_selected"),
            action=action or "skip",
            worked=False,
        )
    return noop(
        "not_selected",
        action=action,
        expected=sorted(allowed_actions),
        decide_reason=decide.get("reason"),
        worked=False,
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
def _read_merge_view(gh: str, repo: str, number: int) -> dict[str, Any]:
    proc = _pr_view(gh, repo, number, "state,mergedAt,mergeCommit,headRefOid,headRefName")
    raw = (getattr(proc, "stdout", "") or "").strip()
    if not raw:
        raise ValueError("blank merge read-back")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not payload:
        raise ValueError("invalid merge read-back shape")
    return payload


def _provenance_from_view(payload: dict[str, Any], *, repo: str, number: int, expected_head: str) -> dict[str, str | int]:
    if str(payload.get("state") or "").upper() != "MERGED":
        raise ValueError("PR is not merged")
    merged_at = str(payload.get("mergedAt") or "").strip()
    observed_head = str(payload.get("headRefOid") or "").strip()
    head_ref = str(payload.get("headRefName") or "").strip()
    commit = payload.get("mergeCommit")
    merge_oid = str(commit.get("oid") or "").strip() if isinstance(commit, dict) else ""
    if not merged_at or not observed_head or observed_head != expected_head or not head_ref or not merge_oid:
        raise ValueError("incomplete or mismatched merge provenance")
    return {
        "source": "github_pr_readback",
        "state": "MERGED",
        "repo": repo,
        "number": number,
        "head_oid": observed_head,
        "head_ref": head_ref,
        "merge_oid": merge_oid,
        "merged_at": merged_at,
    }


def _provided_provenance(request: Request, *effector_ids: str) -> dict[str, Any]:
    data = input_of(request)
    value = data.get("verified_provenance")
    if isinstance(value, dict):
        return dict(value)
    for effector_id in effector_ids:
        value = cond_blob(request, effector_id).get("verified_provenance")
        if isinstance(value, dict):
            return dict(value)
    return {}

def _terminal_upstream(request: Request, *effector_ids: str) -> Result | None:
    for effector_id in effector_ids:
        blob = cond_blob(request, effector_id)
        if blob and (blob.get("ok") is False or str(blob.get("status") or "") in _TERMINAL_FAILURES):
            return fail("upstream_failed", failure_class="terminal", retry_safe=False, upstream=blob, mutated=False)
    return None


def _verify_provenance(
    request: Request,
    *,
    repo: str,
    number: int,
    expected_head: str | None = None,
) -> dict[str, str | int]:
    provided = _provided_provenance(request, "merge", "merge_pull_request", "triage_merge")
    if not provided:
        raise ValueError("verified merge provenance is required")
    if provided.get("source") != "github_pr_readback" or provided.get("repo") != repo or int(provided.get("number") or 0) != number:
        raise ValueError("verified merge provenance identity mismatch")
    head = str(provided.get("head_oid") or expected_head or "").strip()
    if not head:
        raise ValueError("verified merge provenance has no head oid")
    authoritative = _provenance_from_view(_read_merge_view(str(cfg_of(request).get("gh_cli") or "gh"), repo, number), repo=repo, number=number, expected_head=head)
    if authoritative != provided:
        raise ValueError("verified merge provenance does not match authoritative read-back")
    return authoritative


def _comment_bodies(value: Any) -> list[str]:
    comments = value.get("comments") if isinstance(value, dict) else value
    if not isinstance(comments, list):
        return []
    return [str(item.get("body") or "") for item in comments if isinstance(item, dict)]


def list_ai_fix_prs(request: Request) -> Result:
    """List open PRs with head branch matching ai/fix/* (or branch_prefix)."""
    data = input_of(request)
    cfg = cfg_of(request)
    context = cond_blob(request, "dispatch_parse_issue_ref", "dispatch_load_kanban_task", "intake_kanban", "intake_poll")
    explicit_repo = str(data.get("repo") or context.get("repo") or cfg.get("repo") or "")
    repos = [explicit_repo] if explicit_repo else [str(entry.get("repo") or "") for entry in (data.get("repos") or cfg.get("repos") or []) if isinstance(entry, dict)]
    repos = list(dict.fromkeys(repo for repo in repos if repo))
    prefix = str(data.get("branch_prefix") or cfg.get("branch_prefix") or "ai/fix")
    limit = int(data.get("limit") or 50)
    gh = str(cfg.get("gh_cli") or "gh")
    if not repos:
        return fail("missing_repo", failure_class="terminal", retry_safe=False)
    selected: list[dict[str, Any]] = []
    all_open_count = 0
    for repo in repos:
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
            return fail("pr_list_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), repo=repo)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return fail("invalid_pr_list", failure_class="terminal", retry_safe=False, error=str(exc), repo=repo)
        all_open_count += len(prs)
        selected.extend({**pr, "repo": repo} for pr in prs if str(pr.get("headRefName") or "").startswith(prefix))
    if not selected:
        return noop("no_open_prs", repo=explicit_repo, repos=repos, count=0, prs=[], all_open_count=all_open_count)
    return ok(
        status="listed",
        repo=str(selected[0]["repo"]),
        repos=repos,
        count=len(selected),
        prs=selected,
        all_open_count=all_open_count,
    )


def load_pr_fields(request: Request) -> Result:
    """Load one PR JSON bundle for triage decisions."""
    data = input_of(request)
    cfg = cfg_of(request)
    listed = cond_blob(request, "list_ai_fix_prs", "list", "triage_list_ai_fix_prs")
    upstream = upstream_noop(request, "list_ai_fix_prs", "triage_list_ai_fix_prs")
    if upstream:
        return noop(str(upstream.get("reason") or "no_open_prs"))
    number = int(data.get("number") or data.get("pr_number") or 0)
    selected_pr: dict[str, Any] = {}
    if not number:
        prs = listed.get("prs") or []
        if isinstance(prs, list) and prs:
            selected_pr = prs[0] if isinstance(prs[0], dict) else {}
            number = int(selected_pr.get("number") or 0)
    repo = str(data.get("repo") or selected_pr.get("repo") or listed.get("repo") or cond_get(request, "repo", "dispatch_parse_issue_ref", "dispatch_load_kanban_task") or cfg.get("repo") or "")
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


def evaluate_checks(request: Request) -> Result:
    """Pure decision: do status checks pass? (from pr.statusCheckRollup)."""
    data = input_of(request)
    pr = data["pr"] if "pr" in data else (cond_get(request, "pr", "load_pr_fields", "triage_load_pr_fields") or {})
    if not isinstance(pr, dict):
        return fail(
            "invalid_checks_read",
            failure_class="terminal",
            retry_safe=False,
            mutated=False,
            error="PR payload must be an object",
        )
    upstream = upstream_noop(request, "load_pr_fields", "triage_load_pr_fields")
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


def evaluate_test_evidence(request: Request) -> Result:
    """Pure: does PR body contain test evidence markers?"""
    data = input_of(request)
    pr = data.get("pr") or cond_get(request, "pr", "load_pr_fields", "triage_load_pr_fields") or {}
    upstream = upstream_noop(request, "load_pr_fields", "triage_load_pr_fields")
    if upstream:
        return noop(str(upstream.get("reason") or "no_open_prs"))
    require = bool(
        data.get("require_test_evidence", cfg_of(request).get("require_test_evidence", True))
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


def decide_triage_action(request: Request) -> Result:
    """Pure router decision: merge | comment_block | repair | skip."""
    data = input_of(request)
    cfg = cfg_of(request)
    pr = data.get("pr") or cond_get(request, "pr", "load_pr_fields", "triage_load_pr_fields") or {}
    checks = cond_blob(request, "evaluate_checks", "checks", "triage_evaluate_checks")
    evidence = cond_blob(request, "evaluate_test_evidence", "evidence", "triage_evaluate_test_evidence")
    checks_pass = bool(
        data.get(
            "checks_pass",
            data.get("pass_", checks.get("pass_", checks.get("pass"))),
        )
    )
    upstream = upstream_noop(request, "list_ai_fix_prs", "load_pr_fields", "evaluate_checks", "evaluate_test_evidence", "triage_list_ai_fix_prs", "triage_load_pr_fields", "triage_evaluate_checks", "triage_evaluate_test_evidence")
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
            evidence.get("pass_", evidence.get("pass", False)),
        )
    )
    automerge = bool(data.get("automerge", cfg.get("automerge", False)))
    require_approval = bool(
        data.get("require_human_approval", cfg.get("require_human_approval", True))
    )
    branch_prefix = str(data.get("branch_prefix") or cfg.get("branch_prefix") or "ai/fix")
    base_branch = str(data.get("base_branch") or cfg.get("base_branch") or "main")
    require_owner = bool(data.get("require_owner", cfg.get("require_owner", True)))
    repo = str(data.get("repo") or cond_get(request, "repo", "load_pr_fields", "list_ai_fix_prs") or cfg.get("repo") or "")
    repo_owner = repo.split("/", 1)[0] if "/" in repo else str(cfg.get("assignee") or "")
    mergeable_value = pr.get("mergeable") or pr.get("mergeStateStatus") or ""
    mergeable = str(mergeable_value).upper() if isinstance(mergeable_value, str) else ""
    if mergeable == "CLEAN":
        mergeable = "MERGEABLE"
    review_value = pr.get("reviewDecision")
    review_decision = review_value.upper() if isinstance(review_value, str) else ""
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
    if require_owner and not author_login:
        return ok(status="decided", action="skip", reason="missing_author", owner=repo_owner)
    if require_owner and repo_owner and author_login != repo_owner:
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
        return ok(status="decided", action="repair", reason="checks_not_green")
    if not evidence_pass:
        return ok(status="decided", action="comment_block", reason="missing_test_evidence")
    if mergeable in {"CONFLICTING", "DIRTY"}:
        return ok(status="decided", action="repair", reason="merge_conflict")
    if mergeable != "MERGEABLE":
        return ok(status="decided", action="skip", reason="not_mergeable", mergeable=mergeable)
    if require_approval and review_decision != "APPROVED":
        return ok(
            status="decided",
            action="comment_block",
            reason="approval_required",
            review_decision=review_decision,
        )
    if automerge:
        return ok(status="decided", action="merge", reason="ready")
    return ok(status="decided", action="comment_block", reason="automerge_disabled")


def claim_pr_assignee(request: Request) -> Result:
    gated = _decision_gate(request, allowed="merge")
    if gated is not None:
        return gated
    """Assign PR to configured maintainer once, with authoritative reconciliation."""
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    loaded = cond_blob(request, "load_pr_fields", "list_ai_fix_prs", "triage_load_pr_fields", "triage_list_ai_fix_prs")
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

    def read_assignees() -> set[str]:
        view = _pr_view(gh, repo, number, "assignees")
        raw = (getattr(view, "stdout", "") or "").strip()
        if not raw:
            raise ValueError("blank assignee read-back")
        current = json.loads(raw)
        if not isinstance(current, dict) or not isinstance(current.get("assignees"), list):
            raise ValueError("invalid assignee read-back shape")
        names: set[str] = set()
        for item in current["assignees"]:
            if isinstance(item, dict):
                login = item.get("login")
                if not isinstance(login, str) or not login.strip():
                    raise ValueError("invalid assignee read-back item")
                names.add(login.strip())
            elif isinstance(item, str) and item.strip():
                names.add(item.strip())
            else:
                raise ValueError("invalid assignee read-back item")
        return names

    try:
        names = read_assignees()
    except CommandError as exc:
        return fail("assignee_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), mutated=False, **context)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("assignee_read_failed", failure_class="terminal", retry_safe=False, error=str(exc), mutated=False, **context)
    if assignee in names:
        return ok(status="already_claimed", mutated=False, **context)
    if names:
        return fail("assignee_conflict", failure_class="terminal", retry_safe=False, assignees=sorted(names), mutated=False, **context)
    try:
        run_cmd([gh, "pr", "edit", str(number), "--repo", repo, "--add-assignee", assignee], timeout=60)
    except CommandError as exc:
        return fail("claim_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), mutated=True, **context)
    try:
        after = read_assignees()
    except CommandError as exc:
        return fail("assignee_readback_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), mutated=True, **context)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("assignee_readback_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), mutated=True, **context)
    if assignee not in after:
        return fail("assignee_readback_mismatch", failure_class="reconcile_then_retry", retry_safe=False, assignees=sorted(after), mutated=True, **context)
    return ok(status="claimed", assignees=sorted(after), mutated=True, **context)
def comment_pr_once(request: Request) -> Result:
    gated = _decision_gate(request, allowed="comment_block")
    if gated is not None:
        return gated
    """Post one PR comment, reconciling an existing stable marker first."""
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    loaded = cond_blob(request, "load_pr_fields", "decide_triage_action", "triage_load_pr_fields", "triage_decide_triage_action")
    decide = cond_blob(request, "decide_triage_action", "decide", "triage_decide_triage_action")
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
        return fail("comment_marker_conflict", failure_class="terminal", retry_safe=False, **context)
    posted_body = body if hidden_marker in body else f"{body.rstrip()}\n\n{hidden_marker}"
    if dry:
        return planned(**context, body=body[:200])

    def read_comments() -> list[dict[str, Any]]:
        view = _pr_view(gh, repo, number, "comments")
        raw = (getattr(view, "stdout", "") or "").strip()
        if not raw:
            raise ValueError("blank comment read-back")
        existing = json.loads(raw)
        comments = existing.get("comments") if isinstance(existing, dict) else existing
        if not isinstance(comments, list) or any(not isinstance(item, dict) for item in comments):
            raise ValueError("invalid comment read-back shape")
        if any("body" not in item or not isinstance(item["body"], str) for item in comments):
            raise ValueError("invalid comment read-back body")
        return comments

    try:
        comments = read_comments()
    except CommandError as exc:
        return fail("comment_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), mutated=False, **context)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("comment_read_failed", failure_class="terminal", retry_safe=False, error=str(exc), mutated=False, **context)
    matches = sum(str(item.get("body") or "").count(hidden_marker) for item in comments)
    if matches > 1:
        return fail("comment_marker_conflict", failure_class="terminal", retry_safe=False, matches=matches, mutated=False, **context)
    if matches == 1:
        return ok(status="commented", reconciled=True, mutated=False, **context)
    try:
        run_cmd([gh, "pr", "comment", str(number), "--repo", repo, "--body", posted_body], timeout=60)
    except CommandError as exc:
        return fail("comment_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), mutated=True, **context)
    try:
        after = read_comments()
    except CommandError as exc:
        return fail("comment_readback_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), mutated=True, **context)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("comment_readback_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), mutated=True, **context)
    after_matches = sum(str(item.get("body") or "").count(hidden_marker) for item in after)
    if after_matches > 1:
        return fail("comment_marker_conflict", failure_class="terminal", retry_safe=False, matches=after_matches, mutated=True, **context)
    if after_matches != 1:
        return fail("comment_readback_mismatch", failure_class="reconcile_then_retry", retry_safe=False, matches=after_matches, mutated=True, **context)
    return ok(status="commented", mutated=True, **context)





def merge_pull_request(request: Request) -> Result:
    gated = _decision_gate(request, allowed="merge")
    terminal = _terminal_upstream(request, "claim_pr", "claim_pr_assignee", "triage_claim_pr")
    if terminal is not None:
        return terminal
    if gated is not None:
        return gated
    """Merge a PR only after authoritative pre/post state verification."""
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    loaded = cond_blob(request, "load_pr_fields", "claim_pr", "claim_pr_assignee", "triage_load_pr_fields", "triage_claim_pr")
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
    try:
        before = _read_merge_view(gh, repo, number)
        before_state = str(before.get("state") or "").upper()
        if before_state == "MERGED":
            provenance = _provenance_from_view(before, repo=repo, number=number, expected_head=head_oid)
            return ok(status="already_merged", mutated=False, reconciled=True, verified_provenance=provenance, **context)
        if before_state != "OPEN":
            return fail("merge_precondition_failed", failure_class="terminal", retry_safe=False, state=before_state, mutated=False, **context)
        observed_head = str(before.get("headRefOid") or "").strip()
        if observed_head != head_oid:
            return fail("merge_head_mismatch", failure_class="terminal", retry_safe=False, observed_head=observed_head, mutated=False, **context)
    except CommandError as exc:
        return fail("merge_precondition_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), mutated=False, **context)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("merge_precondition_read_failed", failure_class="terminal", retry_safe=False, error=str(exc), mutated=False, **context)

    merge_error: CommandError | None = None
    try:
        run_cmd([gh, "pr", "merge", str(number), "--repo", repo, f"--{method}", "--match-head-commit", head_oid], timeout=120)
    except CommandError as exc:
        merge_error = exc
    try:
        after = _read_merge_view(gh, repo, number)
        provenance = _provenance_from_view(after, repo=repo, number=number, expected_head=head_oid)
    except CommandError as exc:
        return fail(
            "merge_failed",
            failure_class="reconcile_then_retry",
            retry_safe=False,
            error=str(merge_error or exc),
            stderr=(merge_error.stderr if merge_error else exc.stderr)[-400:],
            mutated=True,
            **context,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail(
            "merge_failed",
            failure_class="reconcile_then_retry",
            retry_safe=False,
            error=str(merge_error or exc),
            stderr=(merge_error.stderr if merge_error else "")[-400:],
            mutated=True,
            **context,
        )
    if merge_error is not None:
        return ok(status="merge_verified", merge_oid=str(provenance["merge_oid"]), reconciled=True, mutated=True, verified_provenance=provenance, **context)
    return ok(status="merged", merge_oid=str(provenance["merge_oid"]), mutated=True, verified_provenance=provenance, **context)


def close_linked_issue(request: Request) -> Result:
    """Close an issue only after validating authoritative merged-PR provenance."""
    gated = _decision_gate(request, allowed="merge")
    terminal = _terminal_upstream(request, "claim_pr", "claim_pr_assignee", "triage_claim_pr")
    if terminal is not None:
        return terminal
    if gated is not None:
        return gated
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    loaded = cond_blob(request, "load_pr_fields", "merge", "merge_pull_request", "triage_load_pr_fields", "triage_merge")
    pr = data.get("pr") or loaded.get("pr") or {}
    repo = str(data.get("repo") or loaded.get("repo") or cfg.get("repo") or "")
    gh = str(cfg.get("gh_cli") or "gh")
    issue = int(data.get("issue") or data.get("number") or 0)
    if not issue and isinstance(pr, dict):
        match = re.search(r"(?:^|/)ai/fix/(\d+)", str(pr.get("headRefName") or ""))
        if match:
            issue = int(match.group(1))
    key = f"issue:{repo or 'unknown'}:{issue or 'unknown'}:close"
    context = {"repo": repo, "issue": issue, "idempotency_key": key}
    if not repo or not issue:
        return fail("missing_repo_or_issue", failure_class="terminal", retry_safe=False, **context)
    if dry:
        return planned(**context)
    provenance = _provided_provenance(request, "merge", "merge_pull_request", "triage_merge")
    try:
        verified = _verify_provenance(request, repo=repo, number=int(provenance.get("number") or 0))
        match = re.search(r"(?:^|/)ai/fix/(\d+)", str(verified.get("head_ref") or ""))
        if not match or int(match.group(1)) != issue:
            raise ValueError("merge provenance is not linked to issue")
    except (CommandError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("merge_provenance_unverified", failure_class="terminal", retry_safe=False, error=str(exc), mutated=False, **context)

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
        return fail("close_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), mutated=False, verified_provenance=verified, **context)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("close_read_failed", failure_class="terminal", retry_safe=False, error=str(exc), mutated=False, verified_provenance=verified, **context)
    if state == "CLOSED":
        return ok(status="already_closed", mutated=False, reconciled=True, verified_provenance=verified, **context)
    try:
        run_cmd([gh, "issue", "close", str(issue), "--repo", repo, "--reason", "completed"], timeout=60)
        final_state = read_state()
        if final_state != "CLOSED":
            return fail("close_readback_mismatch", failure_class="reconcile_then_retry", retry_safe=False, state=final_state, mutated=True, verified_provenance=verified, **context)
    except (CommandError, TypeError, ValueError, json.JSONDecodeError) as exc:
        try:
            if read_state() == "CLOSED":
                return ok(status="closed", mutated=True, reconciled=True, verified_provenance=verified, **context)
        except (CommandError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return fail("close_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), mutated=True, verified_provenance=verified, **context)
    return ok(status="closed", mutated=True, verified_provenance=verified, **context)
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


def write_merge_receipt(request: Request) -> Result:
    """Write a receipt only from a fresh authoritative merged-PR read-back."""
    merge_upstream = cond_blob(request, "merge", "merge_pull_request", "triage_merge")
    if merge_upstream and (merge_upstream.get("ok") is False or str(merge_upstream.get("status") or "") in {"failed", "cancelled", "timed_out"}):
        return fail("upstream_failed", failure_class="terminal", retry_safe=False, upstream=merge_upstream, mutated=False)
    claim_terminal = _terminal_upstream(request, "claim_pr", "claim_pr_assignee", "triage_claim_pr")
    if claim_terminal is not None:
        return claim_terminal
    gated = _decision_gate(request, allowed="merge")
    if gated is not None:
        return gated
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    path = str(data.get("receipt_path") or cfg.get("receipt_path") or "")
    payload = data.get("payload")
    if not isinstance(payload, dict) or not payload:
        merge = cond_blob(request, "merge", "merge_pull_request", "triage_merge")
        claim = cond_blob(request, "claim_pr", "claim_pr_assignee", "triage_claim_pr")
        payload = {"phase": "MERGED", "repo": merge.get("repo") or claim.get("repo"), "number": merge.get("number") or claim.get("number"), "dry_run": dry, "merge_status": merge.get("status")}
    else:
        payload = dict(payload)
    if not path:
        return fail("missing_receipt_path", failure_class="terminal", retry_safe=False)
    if dry:
        return planned(receipt_path=path, payload=payload)
    repo = str(payload.get("repo") or "").strip()
    number = int(payload.get("pr") or payload.get("number") or 0)
    if not repo or not number:
        return fail("merge_provenance_missing", failure_class="terminal", retry_safe=False, receipt_path=path)
    try:
        verified = _verify_provenance(request, repo=repo, number=number)
    except (CommandError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("merge_provenance_unverified", failure_class="terminal", retry_safe=False, error=str(exc), receipt_path=path)
    payload.update(
        {
            "repo": repo,
            "pr": number,
            "headSha": verified["head_oid"],
            "mergeSha": verified["merge_oid"],
            "mergedAt": verified["merged_at"],
            "verified_provenance": verified,
        }
    )
    entity = {
        "repo": repo,
        "pr_number": number,
        "head_sha": verified.get("head_oid"),
        "merge_sha": verified.get("merge_oid"),
        "merged_at": verified.get("merged_at"),
    }
    payload.update(_receipt_metadata(request, payload, entity=entity))
    p = Path(path)

    def existing_result() -> Result | None:
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
    return ok(status="written", receipt_path=path, payload=payload, verified_provenance=verified, mutated=True)
