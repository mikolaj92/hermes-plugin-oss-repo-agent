"""Mega-atomic effectors: PR triage domain."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from lokay.envelope import Request, Result

from lokay.adapters_cli import CommandError, run_cmd
from lokay.envelope import (
    cfg_of,
    cond_blob,
    cond_get,
    dry_run_flag,
    fail,
    input_of,
    noop,
    ok,
    planned,
    terminal_upstream,
    upstream_noop,
)

_TERMINAL_FAILURES = {"failed", "cancelled", "timed_out"}

def _decision_gate(request: Request, *, allowed: str | set[str]) -> Result | None:
    """No-op unless an authoritative successful decision selected the action."""
    decide = cond_blob(request, "decide_triage_action", "decide", "triage_decide_triage_action")
    if not decide:
        return noop("not_selected", action="skip", worked=False)
    if decide.get("ok") is False or str(decide.get("status") or "") in _TERMINAL_FAILURES:
        terminal = terminal_upstream(
            request,
            "triage_branch",
            "decide_triage_action",
            "decide",
            "triage_decide_triage_action",
        )
        if terminal is not None:
            return terminal
        return fail(
            "upstream_failed",
            failure_class="terminal",
            retry_safe=False,
            upstream=decide,
            upstream_effector="decide_triage_action",
            worked=False,
        )
    if decide.get("status") == "noop":
        return noop(
            str(decide.get("reason") or "no_selected_pr"),
            action=decide.get("action"),
            worked=False,
        )
    if decide.get("status") != "decided" or ("ok" in decide and decide.get("ok") is not True):
        return noop("not_selected", action="skip", worked=False)
    allowed_actions = {allowed} if isinstance(allowed, str) else set(allowed)
    action = decide.get("action")
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
    operation = effector_ids[0] if effector_ids else "triage_operation"
    return terminal_upstream(request, operation, *effector_ids)


def _verify_provenance(
    request: Request,
    *,
    repo: str,
    number: int,
    expected_head: str | None = None,
) -> dict[str, str | int]:
    provided = _provided_provenance(request, "verify_merge_provenance")
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

def _atomic_context(request: Request, *, kind: str = "pr") -> dict[str, Any]:
    data = input_of(request)
    cfg = cfg_of(request)
    upstream = cond_blob(request, "load_pr_fields", "read_open_prs", "select_fix_pr")
    pr = data.get("pr") if isinstance(data.get("pr"), dict) else upstream.get("pr", {})
    repo = str(data.get("repo") or upstream.get("repo") or cfg.get("repo") or "")
    number = int(data.get("number") or data.get("pr_number") or upstream.get("number") or (pr.get("number") if isinstance(pr, dict) else 0) or 0)
    if kind == "issue":
        number = int(data.get("issue") or data.get("issue_number") or number or 0)
    return {"repo": repo, "number": number}


def _atomic_terminal(request: Request, operation: str, *peers: str) -> Result | None:
    return terminal_upstream(request, operation, *peers)

def _upstream_noop(request: Request, operation: str, *peers: str) -> Result | None:
    idle = upstream_noop(request, *peers)
    if idle:
        return noop(str(idle.get("reason") or "no_selected_pr"), operation=operation, worked=False)
    return None


def read_open_prs(request: Request) -> Result:
    idle = _upstream_noop(request, "read_open_prs", "verify_task_completed")
    if idle is not None:
        return idle
    """Read open PR rows for exactly one configured repository."""
    data, cfg = input_of(request), cfg_of(request)
    repo = str(data.get("repo") or cfg.get("repo") or "")
    if not repo:
        return fail("missing_repo", failure_class="terminal", retry_safe=False)
    gh = str(cfg.get("gh_cli") or "gh")
    limit = int(data.get("limit") or cfg.get("limit") or 50)
    try:
        proc = run_cmd([gh, "pr", "list", "--repo", repo, "--state", "open", "--limit", str(limit), "--json", "number,title,url,headRefName,author,labels,mergeable,statusCheckRollup"], timeout=90)
        rows = json.loads(getattr(proc, "stdout", "") or "")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("invalid PR list read-back shape")
    except CommandError as exc:
        return fail("pr_list_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), repo=repo)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("pr_list_read_failed", failure_class="terminal", retry_safe=False, error=str(exc), repo=repo)
    return ok(status="read", repo=repo, prs=[{**row, "repo": repo} for row in rows], count=len(rows))


def filter_fix_prs(request: Request) -> Result:
    """Purely filter an open-PR read by the configured AI branch prefix."""
    terminal = _atomic_terminal(request, "filter_fix_prs", "read_open_prs")
    if terminal is not None:
        return terminal
    idle = _upstream_noop(request, "filter_fix_prs", "read_open_prs")
    if idle is not None:
        return idle
    data, cfg = input_of(request), cfg_of(request)
    source = cond_blob(request, "read_open_prs")
    rows = data.get("prs") if isinstance(data.get("prs"), list) else source.get("prs", [])
    prefix = str(data.get("branch_prefix") or cfg.get("branch_prefix") or "ai/fix")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return fail("invalid_pr_list", failure_class="terminal", retry_safe=False)
    filtered = [row for row in rows if str(row.get("headRefName") or "").startswith(prefix)]
    return ok(status="filtered", repo=str(data.get("repo") or source.get("repo") or cfg.get("repo") or ""), prs=filtered, count=len(filtered), branch_prefix=prefix)


def select_fix_pr(request: Request) -> Result:
    """Pure deterministic selection of the first fix PR."""
    terminal = _atomic_terminal(request, "select_fix_pr", "filter_fix_prs")
    if terminal is not None:
        return terminal
    idle = _upstream_noop(request, "select_fix_pr", "filter_fix_prs")
    if idle is not None:
        return idle
    data = input_of(request)
    source = cond_blob(request, "filter_fix_prs")
    rows = data.get("prs") if isinstance(data.get("prs"), list) else source.get("prs", [])
    if not rows:
        return noop("no_open_prs", prs=[])
    row = rows[0]
    if not isinstance(row, dict) or not row.get("number"):
        return fail("invalid_selected_pr", failure_class="terminal", retry_safe=False)
    return ok(status="selected", repo=row.get("repo") or source.get("repo"), number=int(row["number"]), pr=dict(row), prs=rows)


def read_pr_assignees(request: Request) -> Result:
    terminal = _atomic_terminal(request, "read_pr_assignees", "decide_triage_action")
    if terminal is not None:
        return terminal
    idle = _upstream_noop(request, "read_pr_assignees", "decide_triage_action")
    if idle is not None:
        return idle
    c = _atomic_context(request)
    if not c["repo"] or not c["number"]:
        return fail("missing_repo_or_number", failure_class="terminal", retry_safe=False, **c)
    try:
        proc = _pr_view(str(cfg_of(request).get("gh_cli") or "gh"), c["repo"], c["number"], "assignees")
        payload = json.loads(getattr(proc, "stdout", "") or "")
        if not isinstance(payload, dict) or not isinstance(payload.get("assignees"), list):
            raise ValueError("invalid assignee read-back shape")
        names = []
        for item in payload["assignees"]:
            name = item.get("login") if isinstance(item, dict) else item
            if not isinstance(name, str) or not name.strip():
                raise ValueError("invalid assignee read-back item")
            names.append(name.strip())
    except CommandError as exc:
        return fail("assignee_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), mutated=False, assignees=[], **c)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("assignee_read_failed", failure_class="terminal", retry_safe=False, error=str(exc), mutated=False, assignees=[], **c)
    return ok(status="assignees_read", assignees=sorted(set(names)), mutated=False, **c)


def decide_pr_assignee(request: Request) -> Result:
    terminal = _atomic_terminal(request, "decide_pr_assignee", "read_pr_assignees")
    if terminal is not None:
        return terminal
    idle = _upstream_noop(request, "decide_pr_assignee", "read_pr_assignees")
    if idle is not None:
        return idle
    data, cfg = input_of(request), cfg_of(request)
    source = cond_blob(request, "read_pr_assignees")
    assignee = str(data.get("assignee") or cfg.get("assignee") or "mikolaj92")
    names = source.get("assignees", data.get("assignees", []))
    if assignee in names:
        return noop("already_assigned", assignee=assignee, assign=False, **_atomic_context(request))
    if names:
        return fail("assignee_conflict", failure_class="terminal", retry_safe=False, assignees=names, assign=False, **_atomic_context(request))
    return ok(status="assign_selected", assignee=assignee, assign=True, **_atomic_context(request))


def assign_pr(request: Request) -> Result:
    terminal = _atomic_terminal(request, "assign_pr", "decide_pr_assignee")
    if terminal is not None:
        return terminal
    idle = _upstream_noop(request, "assign_pr", "decide_pr_assignee")
    if idle is not None:
        return idle
    gate = _decision_gate(request, allowed="merge")
    if gate is not None:
        return gate
    decision = cond_blob(request, "decide_pr_assignee")
    if decision.get("assign") is not True:
        return noop(str(decision.get("reason") or "not_selected"), **{k: decision[k] for k in ("repo", "number", "assignee") if k in decision})
    c = _atomic_context(request); data, cfg = input_of(request), cfg_of(request)
    assignee = str(data.get("assignee") or decision.get("assignee") or cfg.get("assignee") or "mikolaj92")
    context = {**c, "assignee": assignee}
    if dry_run_flag(request):
        return planned(**context)
    try:
        run_cmd([str(cfg.get("gh_cli") or "gh"), "pr", "edit", str(c["number"]), "--repo", c["repo"], "--add-assignee", assignee], timeout=60)
    except CommandError as exc:
        return fail("assign_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), mutated=True, **context)
    return ok(status="assigned", mutated=True, **context)


def verify_pr_assignee(request: Request) -> Result:
    terminal = _atomic_terminal(request, "verify_pr_assignee", "assign_pr")
    if terminal is not None:
        return terminal
    idle = _upstream_noop(request, "verify_pr_assignee", "assign_pr", "decide_pr_assignee")
    if idle is not None:
        return idle
    c = _atomic_context(request); data, cfg = input_of(request), cfg_of(request)
    assignee = str(data.get("assignee") or cond_get(request, "assignee", "decide_pr_assignee") or cfg.get("assignee") or "mikolaj92")
    if dry_run_flag(request):
        return planned(repo=c["repo"], number=c["number"], assignee=assignee)
    try:
        proc = _pr_view(str(cfg.get("gh_cli") or "gh"), c["repo"], c["number"], "assignees")
        payload = json.loads(getattr(proc, "stdout", "") or "")
        names = _names(payload.get("assignees") if isinstance(payload, dict) else None)
        if assignee not in names:
            return fail("assignee_readback_mismatch", failure_class="reconcile_then_retry", retry_safe=False, assignees=sorted(names), mutated=False, repo=c["repo"], number=c["number"], assignee=assignee)
    except CommandError as exc:
        return fail("assignee_verify_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), mutated=False, **c, assignee=assignee)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("assignee_verify_failed", failure_class="terminal", retry_safe=False, error=str(exc), mutated=False, **c, assignee=assignee)
    return ok(status="assignee_verified", assignees=[assignee], mutated=False, **c, assignee=assignee)


def read_pr_comments(request: Request) -> Result:
    terminal = _atomic_terminal(request, "read_pr_comments", "decide_triage_action")
    if terminal is not None:
        return terminal
    idle = _upstream_noop(request, "read_pr_comments", "decide_triage_action", "verify_pr_assignee")
    if idle is not None:
        return idle
    c = _atomic_context(request); data, cfg = input_of(request), cfg_of(request)
    if not c["repo"] or not c["number"]:
        return fail("missing_repo_or_number", failure_class="terminal", retry_safe=False, **c)
    try:
        proc = _pr_view(str(cfg.get("gh_cli") or "gh"), c["repo"], c["number"], "comments")
        payload = json.loads(getattr(proc, "stdout", "") or "")
        comments = payload.get("comments") if isinstance(payload, dict) else payload
        if not isinstance(comments, list) or any(not isinstance(item, dict) or not isinstance(item.get("body"), str) for item in comments):
            raise ValueError("invalid comment read-back shape")
    except CommandError as exc:
        return fail("comment_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), mutated=False, comments=[], **c)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("comment_read_failed", failure_class="terminal", retry_safe=False, error=str(exc), mutated=False, comments=[], **c)
    return ok(status="comments_read", comments=comments, mutated=False, **c)


def decide_pr_comment(request: Request) -> Result:
    terminal = _atomic_terminal(request, "decide_pr_comment", "read_pr_comments")
    if terminal is not None:
        return terminal
    idle = _upstream_noop(request, "decide_pr_comment", "read_pr_comments", "decide_triage_action")
    if idle is not None:
        return idle
    data, cfg = input_of(request), cfg_of(request); source = cond_blob(request, "read_pr_comments")
    c = _atomic_context(request); reason = str(data.get("reason") or cond_get(request, "reason", "decide_triage_action") or "needs human review")
    body = str(data.get("body") or f"lokay triage: action=comment_block reason={reason}. Please add test evidence or address blockers.")
    marker = f"lokay:{c['repo'] or 'unknown'}:{c['number'] or 'unknown'}:triage"; hidden = f"<!-- {marker} -->"
    comments = source.get("comments", data.get("comments", []))
    matches = sum(str(item.get("body") or "").count(hidden) for item in comments) if isinstance(comments, list) else 0
    if matches > 1 or body.count(hidden) > 1:
        return fail("comment_marker_conflict", failure_class="terminal", retry_safe=False, matches=matches, **c)
    if matches == 1:
        return noop("already_commented", comment_marker=marker, post=False, **c)
    return ok(status="comment_selected", post=True, body=body, posted_body=body if hidden in body else f"{body.rstrip()}\n\n{hidden}", comment_marker=marker, **c)


def post_pr_comment(request: Request) -> Result:
    terminal = _atomic_terminal(request, "post_pr_comment", "decide_pr_comment")
    if terminal is not None:
        return terminal
    idle = _upstream_noop(request, "post_pr_comment", "decide_pr_comment")
    if idle is not None:
        return idle
    gate = _decision_gate(request, allowed="comment_block")
    if gate is not None:
        return gate
    decision = cond_blob(request, "decide_pr_comment"); c = _atomic_context(request); data, cfg = input_of(request), cfg_of(request)
    if decision.get("post") is not True:
        return noop(str(decision.get("reason") or "not_selected"), **c)
    body = str(data.get("body") or decision.get("posted_body") or "")
    if not c["repo"] or not c["number"] or not body:
        return fail("missing_repo_number_or_body", failure_class="terminal", retry_safe=False, **c)
    if dry_run_flag(request):
        return planned(body=body[:200], comment_marker=decision.get("comment_marker"), **c)
    try:
        run_cmd([str(cfg.get("gh_cli") or "gh"), "pr", "comment", str(c["number"]), "--repo", c["repo"], "--body", body], timeout=60)
    except CommandError as exc:
        return fail("comment_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), mutated=True, **c)
    return ok(status="comment_posted", mutated=True, body=body, comment_marker=decision.get("comment_marker"), **c)


def verify_pr_comment(request: Request) -> Result:
    terminal = _atomic_terminal(request, "verify_pr_comment", "post_pr_comment")
    if terminal is not None:
        return terminal
    idle = _upstream_noop(request, "verify_pr_comment", "post_pr_comment", "decide_pr_comment")
    if idle is not None:
        return idle
    c = _atomic_context(request); data, cfg = input_of(request), cfg_of(request); decision = cond_blob(request, "decide_pr_comment")
    marker = str(data.get("comment_marker") or decision.get("comment_marker") or f"lokay:{c['repo']}:{c['number']}:triage"); hidden = f"<!-- {marker} -->"
    if dry_run_flag(request):
        return planned(comment_marker=marker, **c)
    try:
        proc = _pr_view(str(cfg.get("gh_cli") or "gh"), c["repo"], c["number"], "comments")
        payload = json.loads(getattr(proc, "stdout", "") or ""); comments = payload.get("comments") if isinstance(payload, dict) else payload
        matches = sum(str(item.get("body") or "").count(hidden) for item in comments) if isinstance(comments, list) else 0
        if matches != 1:
            return fail("comment_readback_mismatch", failure_class="reconcile_then_retry", retry_safe=False, matches=matches, mutated=False, comment_marker=marker, **c)
    except CommandError as exc:
        return fail("comment_verify_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), mutated=False, comment_marker=marker, **c)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("comment_verify_failed", failure_class="terminal", retry_safe=False, error=str(exc), mutated=False, comment_marker=marker, **c)
    return ok(status="comment_verified", mutated=False, comment_marker=marker, **c)


def read_merge_preconditions(request: Request) -> Result:
    terminal = _atomic_terminal(request, "read_merge_preconditions", "verify_pr_assignee")
    if terminal is not None:
        return terminal
    idle = _upstream_noop(request, "read_merge_preconditions", "verify_pr_assignee", "verify_pr_comment")
    if idle is not None:
        return idle
    c = _atomic_context(request); data, cfg = input_of(request), cfg_of(request); head = str(data.get("head_oid") or data.get("headRefOid") or "").strip(); c["head_oid"] = head
    if not c["repo"] or not c["number"] or not head:
        return fail("missing_repo_number_or_head_oid", failure_class="terminal", retry_safe=False, **c)
    if dry_run_flag(request):
        return planned(**c)
    try:
        view = _read_merge_view(str(cfg.get("gh_cli") or "gh"), c["repo"], c["number"]); state = str(view.get("state") or "").upper()
        if state != "OPEN":
            return fail("merge_precondition_failed", failure_class="terminal", retry_safe=False, state=state, **c)
        observed = str(view.get("headRefOid") or "").strip()
        if observed != head:
            return fail("merge_head_mismatch", failure_class="terminal", retry_safe=False, observed_head=observed, **c)
    except CommandError as exc:
        return fail("merge_precondition_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), **c)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("merge_precondition_read_failed", failure_class="terminal", retry_safe=False, error=str(exc), **c)
    return ok(status="merge_preconditions_read", view=view, **c)


def merge_pr(request: Request) -> Result:
    terminal = _atomic_terminal(request, "merge_pr", "read_merge_preconditions", "verify_pr_assignee")
    if terminal is not None:
        return terminal
    if not cond_blob(request, "decide_triage_action", "decide", "triage_decide_triage_action") and input_of(request).get("action") == "merge":
        c = _atomic_context(request)
        return fail("merge_preconditions_required", failure_class="terminal", retry_safe=False, **c)
    idle = _upstream_noop(request, "merge_pr", "read_merge_preconditions")
    if idle is not None:
        return idle
    gate = _decision_gate(request, allowed="merge")
    if gate is not None:
        return gate
    source = cond_blob(request, "read_merge_preconditions"); c = _atomic_context(request); data, cfg = input_of(request), cfg_of(request); head = str(data.get("head_oid") or source.get("head_oid") or "").strip(); c["head_oid"] = head
    if source.get("status") != "merge_preconditions_read":
        return fail("merge_preconditions_required", failure_class="terminal", retry_safe=False, **c)
    if dry_run_flag(request):
        return planned(method=str(data.get("merge_method") or cfg.get("merge_method") or "merge"), **c)
    method = str(data.get("merge_method") or cfg.get("merge_method") or "merge")
    try:
        run_cmd([str(cfg.get("gh_cli") or "gh"), "pr", "merge", str(c["number"]), "--repo", c["repo"], f"--{method}", "--match-head-commit", head], timeout=120)
    except CommandError as exc:
        return fail("merge_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), mutated=True, **c)
    return ok(status="merge_requested", mutated=True, **c)


def read_merge_postcondition(request: Request) -> Result:
    terminal = _atomic_terminal(request, "read_merge_postcondition", "merge_pr")
    if terminal is not None:
        return terminal
    c = _atomic_context(request); data, cfg = input_of(request), cfg_of(request); c["head_oid"] = str(data.get("head_oid") or cond_get(request, "head_oid", "read_merge_preconditions") or "").strip()
    idle = _upstream_noop(request, "read_merge_postcondition", "merge_pr", "read_merge_preconditions")
    if idle is not None:
        return idle
    if dry_run_flag(request):
        return planned(**c)
    if not c["repo"] or not c["number"] or not c["head_oid"]:
        return fail("missing_repo_number_or_head_oid", failure_class="terminal", retry_safe=False, **c)
    try:
        view = _read_merge_view(str(cfg.get("gh_cli") or "gh"), c["repo"], c["number"])
    except CommandError as exc:
        return fail("merge_postcondition_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), **c)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("merge_postcondition_read_failed", failure_class="terminal", retry_safe=False, error=str(exc), **c)
    try:
        provenance = _provenance_from_view(view, repo=c["repo"], number=c["number"], expected_head=c["head_oid"])
    except (TypeError, ValueError) as exc:
        return fail("merge_postcondition_failed", failure_class="terminal", retry_safe=False, error=str(exc), **c)
    return ok(status="merge_postcondition_read", view=view, verified_provenance=provenance, **c)


def verify_merge_provenance(request: Request) -> Result:
    terminal = _atomic_terminal(request, "verify_merge_provenance", "read_merge_postcondition")
    if terminal is not None:
        return terminal
    idle = _upstream_noop(request, "verify_merge_provenance", "read_merge_postcondition")
    if idle is not None:
        return idle
    source = cond_blob(request, "read_merge_postcondition"); prov = source.get("verified_provenance")
    if not isinstance(prov, dict) or prov.get("source") != "github_pr_readback":
        return fail("merge_provenance_unverified", failure_class="terminal", retry_safe=False)
    return ok(status="merge_provenance_verified", verified_provenance=dict(prov), repo=prov.get("repo"), number=prov.get("number"), head_oid=prov.get("head_oid"))


def load_pr_fields(request: Request) -> Result:
    """Load one PR JSON bundle for triage decisions."""
    data = input_of(request)
    cfg = cfg_of(request)
    listed = cond_blob(request, "select_fix_pr", "filter_fix_prs", "read_open_prs")
    upstream = upstream_noop(request, "select_fix_pr", "filter_fix_prs", "read_open_prs")
    if upstream:
        return noop(str(upstream.get("reason") or "no_open_prs"))
    number = int(data.get("number") or data.get("pr_number") or 0)
    selected_pr: dict[str, Any] = {}
    if not number:
        prs = listed.get("prs") or []
        if isinstance(prs, list) and prs:
            selected_pr = prs[0] if isinstance(prs[0], dict) else {}
            number = int(selected_pr.get("number") or 0)
    repo = str(data.get("repo") or selected_pr.get("repo") or listed.get("repo") or cfg.get("repo") or "")
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
    board = ""
    clone_path = ""
    for r in (data.get("repos") or cfg.get("repos") or []):
        if isinstance(r, dict) and r.get("repo") == repo:
            board = r.get("board") or ""
            clone_path = r.get("clone_path") or ""
            break
    return ok(status="loaded", repo=repo, number=number, board=board, clone_path=clone_path, pr=pr)


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

    terminal = _terminal_upstream(
        request,
        "evaluate_checks",
        "evaluate_test_evidence",
        "triage_evaluate_checks",
        "triage_evaluate_test_evidence",
    )
    if terminal is not None:
        return terminal
    upstream = upstream_noop(
        request,
        "read_open_prs",
        "filter_fix_prs",
        "select_fix_pr",
        "load_pr_fields",
        "evaluate_checks",
        "evaluate_test_evidence",
    )
    if upstream:
        return noop(str(upstream.get("reason") or "no_open_prs"))
    if not isinstance(pr, dict):
        return fail(
            "invalid_pr",
            failure_class="terminal",
            retry_safe=False,
            error="PR payload must be an object",
        )
    conducted_pr = isinstance(data.get("conduction"), dict) and bool(data["conduction"])
    if conducted_pr:
        for field in ("state", "headRefName", "baseRefName"):
            if not isinstance(pr.get(field), str) or not pr[field].strip():
                return fail(
                    "invalid_pr",
                    failure_class="terminal",
                    retry_safe=False,
                    error=f"PR payload requires non-empty string {field}",
                )

    def decision_bool(name: str, blob: dict[str, Any]) -> bool | Result:
        if name in data:
            value = data[name]
        elif name == "checks_pass":
            value = data.get("pass_", blob.get("pass_", blob.get("pass", False)))
        else:
            value = blob.get("pass_", blob.get("pass", False))
        if not isinstance(value, bool):
            return fail(
                "invalid_decision_input",
                failure_class="terminal",
                retry_safe=False,
                error=f"{name} must be boolean",
            )
        return value

    checks_pass = decision_bool("checks_pass", checks)
    if isinstance(checks_pass, dict):
        return checks_pass
    evidence_pass = decision_bool("evidence_pass", evidence)
    if isinstance(evidence_pass, dict):
        return evidence_pass
    automerge = bool(data.get("automerge", cfg.get("automerge", False)))
    require_approval = bool(
        data.get("require_human_approval", cfg.get("require_human_approval", True))
    )
    branch_prefix = str(data.get("branch_prefix") or cfg.get("branch_prefix") or "ai/fix")
    base_branch = str(data.get("base_branch") or cfg.get("base_branch") or "main")
    require_owner = bool(data.get("require_owner", cfg.get("require_owner", True)))
    repo = str(data.get("repo") or cond_get(request, "repo", "load_pr_fields", "select_fix_pr") or cfg.get("repo") or "")
    repo_owner = repo.split("/", 1)[0] if "/" in repo else str(cfg.get("assignee") or "")
    mergeable_value = pr.get("mergeable") or pr.get("mergeStateStatus") or ""
    mergeable = str(mergeable_value).upper() if isinstance(mergeable_value, str) else ""
    if mergeable == "CLEAN":
        mergeable = "MERGEABLE"
    review_value = pr.get("reviewDecision")
    review_decision = review_value.upper() if isinstance(review_value, str) else ""
    state = str(pr.get("state") or "").upper()
    head = str(pr.get("headRefName") or "")
    author = pr.get("author") if isinstance(pr.get("author"), dict) else {}
    base = str(pr.get("baseRefName") or "")
    author_login = str(author.get("login") or pr.get("author") or "").strip()
    is_draft = bool(pr.get("isDraft") or pr.get("is_draft"))
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









def read_linked_issue_state(request: Request) -> Result:
    terminal = _atomic_terminal(request, "read_linked_issue_state", "verify_linked_merge_provenance")
    if terminal is not None:
        return terminal
    source = cond_blob(request, "verify_linked_merge_provenance"); c = _atomic_context(request, kind="issue"); cfg = cfg_of(request)
    idle = _upstream_noop(request, "read_linked_issue_state", "verify_linked_merge_provenance")
    if idle is not None:
        return idle
    if dry_run_flag(request):
        return planned(repo=c["repo"], issue=c["number"])
    try:
        proc = run_cmd([str(cfg.get("gh_cli") or "gh"), "issue", "view", str(c["number"]), "--repo", c["repo"], "--json", "state"], timeout=60)
        payload = json.loads(getattr(proc, "stdout", "") or ""); state = str(payload.get("state") or "").upper()
        if state not in {"OPEN", "CLOSED"}:
            raise ValueError("invalid issue state read-back")
    except CommandError as exc:
        return fail("close_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), mutated=False, verified_provenance=source.get("verified_provenance"), repo=c["repo"], issue=c["number"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("close_read_failed", failure_class="terminal", retry_safe=False, error=str(exc), mutated=False, verified_provenance=source.get("verified_provenance"), repo=c["repo"], issue=c["number"])
    return ok(status="issue_state_read", state=state, verified_provenance=source.get("verified_provenance"), repo=c["repo"], issue=c["number"], mutated=False)


def close_linked_issue(request: Request) -> Result:
    terminal = _atomic_terminal(request, "close_linked_issue", "read_linked_issue_state", "verify_pr_assignee")
    if terminal is not None:
        return terminal
    idle = _upstream_noop(request, "close_linked_issue", "read_linked_issue_state")
    if idle is not None:
        return idle
    gate = _decision_gate(request, allowed="merge")
    if gate is not None:
        return gate
    source = cond_blob(request, "read_linked_issue_state"); c = _atomic_context(request, kind="issue"); cfg = cfg_of(request)
    if source.get("status") != "issue_state_read":
        return fail("linked_issue_state_required", failure_class="terminal", retry_safe=False, mutated=False, **c)
    if source.get("state") == "CLOSED":
        return ok(status="already_closed", reconciled=True, mutated=False, **c)
    if dry_run_flag(request):
        return planned(**c)
    try:
        run_cmd([str(cfg.get("gh_cli") or "gh"), "issue", "close", str(c["number"]), "--repo", c["repo"], "--reason", "completed"], timeout=60)
    except CommandError as exc:
        return fail("close_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), mutated=True, **c)
    return ok(status="issue_closed", mutated=True, **c)


def verify_linked_issue_closed(request: Request) -> Result:
    terminal = _atomic_terminal(request, "verify_linked_issue_closed", "close_linked_issue")
    if terminal is not None:
        return terminal
    idle = _upstream_noop(request, "verify_linked_issue_closed", "close_linked_issue")
    if idle is not None:
        return idle
    c = _atomic_context(request, kind="issue"); cfg = cfg_of(request)
    if dry_run_flag(request):
        return planned(**c)
    try:
        proc = run_cmd([str(cfg.get("gh_cli") or "gh"), "issue", "view", str(c["number"]), "--repo", c["repo"], "--json", "state"], timeout=60)
        payload = json.loads(getattr(proc, "stdout", "") or "")
        if str(payload.get("state") or "").upper() != "CLOSED":
            return fail("close_readback_mismatch", failure_class="reconcile_then_retry", retry_safe=False, mutated=False, **c)
    except CommandError as exc:
        return fail("close_verify_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), mutated=False, **c)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("close_verify_failed", failure_class="terminal", retry_safe=False, error=str(exc), mutated=False, **c)
    return ok(status="issue_close_verified", mutated=False, **c)


def build_merge_receipt(request: Request) -> Result:
    terminal = _atomic_terminal(request, "build_merge_receipt", "verify_merge_provenance", "verify_pr_assignee")
    if terminal is not None:
        return terminal
    idle = _upstream_noop(request, "build_merge_receipt", "verify_merge_provenance", "read_merge_postcondition", "verify_linked_issue_closed")
    if idle is not None:
        return idle
    gate = _decision_gate(request, allowed="merge")
    if gate is not None:
        return gate
    data, cfg = input_of(request), cfg_of(request); source = cond_blob(request, "verify_merge_provenance", "read_merge_postcondition"); prov = source.get("verified_provenance"); path = str(data.get("receipt_path") or cfg.get("receipt_path") or "")
    if not path:
        return fail("missing_receipt_path", failure_class="terminal", retry_safe=False)
    if not isinstance(prov, dict) or prov.get("source") != "github_pr_readback":
        return fail("merge_provenance_missing", failure_class="terminal", retry_safe=False, receipt_path=path)
    payload = dict(data.get("payload") or {}); payload.update({"phase": "MERGED", "repo": prov.get("repo"), "pr": prov.get("number"), "headSha": prov.get("head_oid"), "mergeSha": prov.get("merge_oid"), "mergedAt": prov.get("merged_at"), "verified_provenance": dict(prov)})
    return ok(status="merge_receipt_built", receipt_path=path, payload=payload, verified_provenance=dict(prov), mutated=False)


def read_receipt_merge_provenance(request: Request) -> Result:
    terminal = _atomic_terminal(request, "read_receipt_merge_provenance", "build_merge_receipt")
    if terminal is not None:
        return terminal
    idle = _upstream_noop(request, "read_receipt_merge_provenance", "build_merge_receipt")
    if idle is not None:
        return idle
    built = cond_blob(request, "build_merge_receipt"); prov = built.get("verified_provenance")
    if not isinstance(prov, dict) or prov.get("source") != "github_pr_readback":
        return fail("merge_provenance_unverified", failure_class="terminal", retry_safe=False)
    return ok(status="receipt_provenance_read", verified_provenance=dict(prov), payload=built.get("payload"), receipt_path=built.get("receipt_path"), mutated=False)


def publish_merge_receipt(request: Request) -> Result:
    terminal = _atomic_terminal(request, "publish_merge_receipt", "read_receipt_merge_provenance")
    if terminal is not None:
        return terminal
    idle = _upstream_noop(request, "read_receipt_merge_provenance", "build_merge_receipt")
    if idle is not None:
        return idle
    gate = _decision_gate(request, allowed="merge")
    if gate is not None:
        return gate
    source = cond_blob(request, "read_receipt_merge_provenance")
    path = str(input_of(request).get("receipt_path") or source.get("receipt_path") or "")
    payload = source.get("payload")
    if not path or not isinstance(payload, dict):
        return fail("receipt_payload_required", failure_class="terminal", retry_safe=False)
    if dry_run_flag(request):
        return planned(receipt_path=path, payload=payload)
    target = Path(path)
    try:
        if target.exists():
            existing = json.loads(target.read_text(encoding="utf-8"))
            if existing != payload:
                return fail("receipt_conflict", failure_class="terminal", retry_safe=False, receipt_path=path)
            return ok(status="exists", receipt_path=path, payload=payload, mutated=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.link(temporary_path, target)
        except FileExistsError:
            existing = json.loads(target.read_text(encoding="utf-8"))
            if existing != payload:
                return fail("receipt_conflict", failure_class="terminal", retry_safe=False, receipt_path=path)
            return ok(status="exists", receipt_path=path, payload=payload, mutated=False)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
        dir_fd = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        if json.loads(target.read_text(encoding="utf-8")) != payload:
            raise ValueError("receipt read-back mismatch")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("receipt_write_failed", failure_class="terminal", retry_safe=False, error=str(exc), receipt_path=path, mutated=True)
    return ok(status="written", receipt_path=path, payload=payload, mutated=True)


def verify_merge_receipt(request: Request) -> Result:
    terminal = _atomic_terminal(request, "verify_merge_receipt", "publish_merge_receipt")
    if terminal is not None:
        return terminal
    idle = _upstream_noop(request, "verify_merge_receipt", "publish_merge_receipt")
    if idle is not None:
        return idle
    source = cond_blob(request, "publish_merge_receipt"); path = str(input_of(request).get("receipt_path") or source.get("receipt_path") or "")
    if not path:
        return fail("missing_receipt_path", failure_class="terminal", retry_safe=False)
    if dry_run_flag(request):
        return planned(receipt_path=path)
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload != source.get("payload"):
            raise ValueError("receipt read-back mismatch")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("receipt_verify_failed", failure_class="terminal", retry_safe=False, error=str(exc), receipt_path=path)
    return ok(status="merge_receipt_verified", receipt_path=path, payload=payload, mutated=False)
def _receipt_metadata(request: Request, payload: dict[str, Any], *, entity: dict[str, Any]) -> dict[str, Any]:
    data = input_of(request)
    cfg = cfg_of(request)
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




def verify_linked_merge_provenance(request: Request) -> Result:
    terminal = _atomic_terminal(request, "verify_linked_merge_provenance", "verify_merge_provenance")
    if terminal is not None:
        return terminal
    idle = _upstream_noop(request, "verify_linked_merge_provenance", "verify_merge_provenance")
    if idle is not None:
        return idle
    source = cond_blob(request, "verify_merge_provenance", "read_merge_postcondition")
    prov = source.get("verified_provenance")
    issue = int(input_of(request).get("issue") or input_of(request).get("issue_number") or 0)
    match = re.search(r"(?:^|/)ai/fix/(\d+)", str((prov or {}).get("head_ref") or "")) if isinstance(prov, dict) else None
    if not isinstance(prov, dict) or not match or int(match.group(1)) != issue:
        return fail("merge_provenance_unverified", failure_class="terminal", retry_safe=False)
    return ok(status="linked_merge_provenance_verified", verified_provenance=dict(prov), issue=issue)
