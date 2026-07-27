from __future__ import annotations

import json
from typing import Any

from lokay.envelope import Request, Result

from lokay.adapters_cli import CommandError, gh_json
from lokay.envelope import cfg_of, cond_blob, dry_run_flag, fail, input_of, ok, terminal_upstream




def _poll_selected(request: Request) -> dict[str, Any]:
    data = input_of(request)
    for key in ("read_open_issues", "issues", "read"):
        value = data.get(key)
        if isinstance(value, dict):
            return dict(value)
    return cond_blob(request, "read_open_issues")


def read_open_issues(request: Request) -> Result:
    """Read open issues for exactly one configured repository."""
    cfg = cfg_of(request)
    data = input_of(request)
    entry = data.get("repo") if isinstance(data.get("repo"), dict) else None
    if entry is None and isinstance(data.get("repos"), list) and data["repos"]:
        entry = data["repos"][0] if isinstance(data["repos"][0], dict) else None
    entry = entry or data
    repo = str(entry.get("repo") or cfg.get("repo") or "").strip()
    board = str(entry.get("board") or cfg.get("board") or "")
    limit = int(entry.get("limit") or data.get("limit") or cfg.get("limit") or 10)
    gh = str(entry.get("gh_cli") or cfg.get("gh_cli") or "gh")
    context = {"repo": repo, "board": board, "clone_path": str(entry.get("clone_path") or ""), "priority": entry.get("priority", 0), "limit": limit}
    if not repo:
        return fail("missing_repo", failure_class="terminal", retry_safe=False, **context)
    try:
        issues = gh_json(["issue", "list", "--repo", repo, "--state", "open", "--limit", str(limit), "--json", "number,title,body,url,labels,assignees"], gh=gh)
    except CommandError as exc:
        return fail("open_issue_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), stderr=exc.stderr[-500:], **context)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return fail("open_issue_read_failed", failure_class="terminal", retry_safe=False, error="malformed_gh_json", detail=str(exc), **context)
    if not isinstance(issues, list) or any(not isinstance(issue, dict) for issue in issues):
        return fail("open_issue_read_failed", failure_class="terminal", retry_safe=False, error="malformed_gh_json", **context)
    return ok(status="read", dry_run=dry_run_flag(request), issues=issues, **context)


def normalize_issue_rows(request: Request) -> Result:
    """Purely normalize one direct open-issues response into routed rows."""
    terminal = terminal_upstream(request, "normalize_issue_rows", "read_open_issues")
    if terminal:
        return terminal
    data = input_of(request)
    source = _poll_selected(request)
    issues = source.get("issues") if source else data.get("issues")
    if not isinstance(issues, list) or any(not isinstance(issue, dict) for issue in issues):
        return fail("malformed_issue_rows", failure_class="terminal", retry_safe=False, mutated=False)
    rows = []
    for issue in issues:
        try:
            rows.append({"repo": str(source.get("repo") or data.get("repo") or ""), "board": str(source.get("board") or data.get("board") or ""), "clone_path": str(source.get("clone_path") or ""), "priority": source.get("priority", 0), "number": int(issue.get("number") or 0), "title": str(issue.get("title") or ""), "body": str(issue.get("body") or ""), "url": str(issue.get("url") or ""), "labels": sorted(str(x.get("name") or "") for x in (issue.get("labels") or []) if isinstance(x, dict)), "assignees": [str(x.get("login") or "") for x in (issue.get("assignees") or []) if isinstance(x, dict) and x.get("login")]})
        except (TypeError, ValueError, AttributeError) as exc:
            return fail("malformed_issue_rows", failure_class="terminal", retry_safe=False, detail=str(exc), mutated=False)
    return ok(status="normalized", rows=rows, repo=source.get("repo"), board=source.get("board"), dry_run=dry_run_flag(request))


def filter_issue_eligibility(request: Request) -> Result:
    """Purely apply ready/assignee eligibility policy to normalized rows."""
    terminal = terminal_upstream(request, "filter_issue_eligibility", "normalize_issue_rows")
    if terminal:
        return terminal
    cfg = cfg_of(request); data = input_of(request); source = cond_blob(request, "normalize_issue_rows")
    rows = data.get("rows") if isinstance(data.get("rows"), list) else source.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return fail("malformed_issue_rows", failure_class="terminal", retry_safe=False, mutated=False)
    ready = str(data.get("ready_label") or cfg.get("ready_label") or source.get("config", {}).get("ready_label") or "ai:ready")
    assignee = str(data.get("assignee") or cfg.get("assignee") or source.get("config", {}).get("assignee") or "mikolaj92")
    eligible=[]; skipped=[]
    for row in rows:
        labels = set(str(x) for x in (row.get("labels") or [])); reason="ok"; allowed=True
        if "ai:blocked" in labels: allowed=False; reason="ai:blocked"
        elif "ai:in-progress" in labels: allowed=False; reason="ai:in-progress"
        elif "ai:pr-opened" in labels: allowed=False; reason="ai:pr-opened"
        elif ready not in labels: allowed=False; reason=f"missing:{ready}"
        people=[str(x) for x in (row.get("assignees") or []) if str(x)]
        if allowed and people and assignee not in people: allowed=False; reason=f"foreign_assignee:{','.join(people)}"
        (eligible if allowed else skipped).append(row if allowed else {**row, "reason": reason})
    return ok(status="filtered", eligible=eligible, skipped=skipped, eligible_count=len(eligible), skipped_count=len(skipped), ready_label=ready, assignee=assignee, dry_run=dry_run_flag(request))


def select_issue_candidate(request: Request) -> Result:
    """Pure deterministic candidate selection and aggregation."""
    from lokay.envelope import cond_blob, terminal_upstream
    terminal = terminal_upstream(request, "select_issue_candidate", "filter_issue_eligibility")
    if terminal:
        return terminal
    data = input_of(request); blob = cond_blob(request, "filter_issue_eligibility")
    eligible = data.get("eligible") if isinstance(data.get("eligible"), list) else blob.get("eligible", [])
    if not isinstance(eligible, list) or any(not isinstance(row, dict) for row in eligible):
        return fail("malformed_candidates", failure_class="terminal", retry_safe=False, mutated=False)
    selected = eligible[0] if eligible else None
    return ok(status="selected", selected=selected, eligible=eligible, eligible_count=len(eligible), skipped=blob.get("skipped", []), skipped_count=len(blob.get("skipped", [])), dry_run=dry_run_flag(request))
