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


def _repo_entries(data: dict[str, Any], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Return configured repository contexts without collapsing the list."""
    values = data.get("repos")
    if not isinstance(values, list):
        values = cfg.get("repos")
    entries = [dict(value) for value in values if isinstance(value, dict)] if isinstance(values, list) else []
    if entries:
        requested = str(data.get("repo") or "").strip()
        if requested:
            entries = [entry for entry in entries if str(entry.get("repo") or "").strip() == requested]
        return entries
    entry = data.get("repo") if isinstance(data.get("repo"), dict) else data
    return [dict(entry)] if isinstance(entry, dict) else []


def _repo_context(entry: dict[str, Any], data: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "repo": str(entry.get("repo") or data.get("repo") or cfg.get("repo") or "").strip(),
        "board": str(entry.get("board") or data.get("board") or cfg.get("board") or ""),
        "clone_path": str(entry.get("clone_path") or data.get("clone_path") or ""),
        "priority": entry.get("priority", data.get("priority", 0)),
    }


def read_open_issues(request: Request) -> Result:
    """Read and aggregate open issues for every configured repository."""
    cfg = cfg_of(request)
    data = input_of(request)
    entries = _repo_entries(data, cfg)
    if not entries:
        return fail("missing_repo", failure_class="terminal", retry_safe=False, repository_results=[])
    limit_value = data.get("limit") or cfg.get("limit") or 10
    try:
        limit = int(limit_value)
    except (TypeError, ValueError):
        return fail("invalid_limit", failure_class="terminal", retry_safe=False, repository_results=[])
    results: list[dict[str, Any]] = []
    aggregate: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for entry in entries:
        context = _repo_context(entry, data, cfg)
        repo = context["repo"]
        try:
            context["limit"] = int(entry.get("limit") or limit)
        except (TypeError, ValueError) as exc:
            failure = {**context, "reason": "invalid_limit", "failure_class": "terminal", "retry_safe": False, "detail": str(exc)}
            failures.append(failure)
            results.append(failure)
            continue
        if not repo:
            failure = {**context, "reason": "missing_repo", "failure_class": "terminal", "retry_safe": False}
            failures.append(failure)
            results.append(failure)
            continue
        gh = str(entry.get("gh_cli") or data.get("gh_cli") or cfg.get("gh_cli") or "gh")
        try:
            issues = gh_json(["issue", "list", "--repo", repo, "--state", "open", "--limit", str(context["limit"]), "--json", "number,title,body,url,labels,assignees"], gh=gh)
            if not isinstance(issues, list) or any(not isinstance(issue, dict) for issue in issues):
                raise ValueError("malformed_gh_json")
        except CommandError as exc:
            failure = {**context, "reason": "open_issue_read_failed", "failure_class": "retryable_read", "retry_safe": True, "error": str(exc), "stderr": exc.stderr[-500:]}
            failures.append(failure)
            results.append(failure)
            continue
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            failure = {**context, "reason": "open_issue_read_failed", "failure_class": "terminal", "retry_safe": False, "error": "malformed_gh_json", "detail": str(exc)}
            failures.append(failure)
            results.append(failure)
            continue
        rows = [{**issue, **context} for issue in issues]
        aggregate.extend(rows)
        results.append({**context, "status": "read", "issues": rows, "count": len(rows)})
    if failures:
        return fail("open_issue_read_failed", failure_class="terminal", retry_safe=False, failures=failures, repository_results=results, issues=[])
    return ok(status="read", dry_run=dry_run_flag(request), issues=aggregate, count=len(aggregate), repositories=[_repo_context(entry, data, cfg) for entry in entries], repository_results=results)


def normalize_issue_rows(request: Request) -> Result:
    """Purely normalize an aggregated open-issues response into routed rows."""
    terminal = terminal_upstream(request, "normalize_issue_rows", "read_open_issues")
    if terminal:
        return terminal
    data = input_of(request)
    source = _poll_selected(request)
    issues = data.get("issues") if isinstance(data.get("issues"), list) else source.get("issues")
    if not isinstance(issues, list) or any(not isinstance(issue, dict) for issue in issues):
        return fail("malformed_issue_rows", failure_class="terminal", retry_safe=False, mutated=False)
    rows = []
    for issue in issues:
        try:
            rows.append({
                "repo": str(issue.get("repo") or source.get("repo") or data.get("repo") or ""),
                "board": str(issue.get("board") or source.get("board") or data.get("board") or ""),
                "clone_path": str(issue.get("clone_path") or source.get("clone_path") or data.get("clone_path") or ""),
                "priority": issue.get("priority", source.get("priority", data.get("priority", 0))),
                "number": int(issue.get("number") or 0),
                "title": str(issue.get("title") or ""),
                "body": str(issue.get("body") or ""),
                "url": str(issue.get("url") or ""),
                "labels": sorted(str(x.get("name") or "") for x in (issue.get("labels") or []) if isinstance(x, dict)),
                "assignees": [str(x.get("login") or "") for x in (issue.get("assignees") or []) if isinstance(x, dict) and x.get("login")],
            })
        except (TypeError, ValueError, AttributeError) as exc:
            return fail("malformed_issue_rows", failure_class="terminal", retry_safe=False, detail=str(exc), mutated=False)
    return ok(status="normalized", rows=rows, repositories=source.get("repositories") or [], dry_run=dry_run_flag(request))


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
    return ok(status="filtered", eligible=eligible, skipped=skipped, eligible_count=len(eligible), skipped_count=len(skipped), repositories=source.get("repositories") or [], ready_label=ready, assignee=assignee, dry_run=dry_run_flag(request))


def select_issue_candidate(request: Request) -> Result:
    """Pure deterministic candidate selection across repositories."""
    terminal = terminal_upstream(request, "select_issue_candidate", "filter_issue_eligibility")
    if terminal:
        return terminal
    data = input_of(request); blob = cond_blob(request, "filter_issue_eligibility")
    eligible = data.get("eligible") if isinstance(data.get("eligible"), list) else blob.get("eligible", [])
    if not isinstance(eligible, list) or any(not isinstance(row, dict) for row in eligible):
        return fail("malformed_candidates", failure_class="terminal", retry_safe=False, mutated=False)
    try:
        ordered = sorted(eligible, key=lambda row: (int(row.get("priority", 0)), str(row.get("repo") or ""), int(row.get("number") or 0)))
    except (TypeError, ValueError, AttributeError) as exc:
        return fail("malformed_candidates", failure_class="terminal", retry_safe=False, detail=str(exc), mutated=False)
    selected = ordered[0] if ordered else None
    return ok(status="selected", selected=selected, eligible=ordered, eligible_count=len(ordered), skipped=blob.get("skipped", []), skipped_count=len(blob.get("skipped", [])), repositories=blob.get("repositories") or [], dry_run=dry_run_flag(request))
