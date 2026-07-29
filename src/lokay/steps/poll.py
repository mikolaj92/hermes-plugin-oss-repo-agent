from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from lokay.envelope import Request, Result

from lokay.adapters_cli import CommandError, gh_json
from lokay.envelope import cfg_of, cond_blob, dry_run_flag, fail, input_of, ok, terminal_upstream


_RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")


def _parse_updated_at(value: Any, *, required: bool = False) -> str | None:
    if value in (None, "") and not required:
        return None
    if not isinstance(value, str) or not _RFC3339_RE.fullmatch(value):
        raise ValueError("invalid_updatedAt")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid_updatedAt") from exc
    return value


def _updated_at_key(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _configured_label(cfg: dict[str, Any], data: dict[str, Any], name: str, default: str) -> str:
    labels = cfg.get("labels")
    labels = labels if isinstance(labels, Mapping) else {}
    return str(data.get(name) or cfg.get(name) or labels.get(name) or default)




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
        "triage_goal": str(entry.get("triage_goal") or data.get("triage_goal") or cfg.get("repo_goal") or ""),
        "triage_context_paths": entry.get("triage_context_paths", data.get("triage_context_paths", cfg.get("triage_context_paths", ()))),
        "auto_close_duplicates": entry.get("auto_close_duplicates", data.get("auto_close_duplicates", cfg.get("auto_close_duplicates", False))),
        "auto_close_out_of_scope": entry.get("auto_close_out_of_scope", data.get("auto_close_out_of_scope", cfg.get("auto_close_out_of_scope", False))),
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
            issues = gh_json(["issue", "list", "--repo", repo, "--state", "open", "--limit", str(context["limit"]), "--json", "number,title,body,url,updatedAt,labels,assignees"], gh=gh)
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
            updated_at = _parse_updated_at(issue.get("updatedAt"))
            rows.append({
                "repo": str(issue.get("repo") or source.get("repo") or data.get("repo") or ""),
                "board": str(issue.get("board") or source.get("board") or data.get("board") or ""),
                "clone_path": str(issue.get("clone_path") or source.get("clone_path") or data.get("clone_path") or ""),
                "priority": issue.get("priority", source.get("priority", data.get("priority", 0))),
                "triage_goal": str(issue.get("triage_goal") or source.get("triage_goal") or data.get("triage_goal") or ""),
                "triage_context_paths": issue.get("triage_context_paths", source.get("triage_context_paths", data.get("triage_context_paths", ()))),
                "auto_close_duplicates": issue.get("auto_close_duplicates", source.get("auto_close_duplicates", data.get("auto_close_duplicates", False))),
                "auto_close_out_of_scope": issue.get("auto_close_out_of_scope", source.get("auto_close_out_of_scope", data.get("auto_close_out_of_scope", False))),
                "number": int(issue.get("number") or 0),
                "title": str(issue.get("title") or ""),
                "body": str(issue.get("body") or ""),
                "url": str(issue.get("url") or ""),
                "updatedAt": updated_at,
                "labels": sorted(str(x.get("name") or "") for x in (issue.get("labels") or []) if isinstance(x, dict)),
                "assignees": [str(x.get("login") or "") for x in (issue.get("assignees") or []) if isinstance(x, dict) and x.get("login")],
            })
        except (TypeError, ValueError, AttributeError) as exc:
            return fail("malformed_issue_rows", failure_class="terminal", retry_safe=False, detail=str(exc), mutated=False)
    return ok(status="normalized", rows=rows, repositories=source.get("repositories") or [], dry_run=dry_run_flag(request))


def filter_issue_eligibility(request: Request) -> Result:
    """Purely apply configured ready/assignee eligibility policy."""
    terminal = terminal_upstream(request, "filter_issue_eligibility", "build_triage_terminal", "normalize_issue_rows")
    if terminal:
        return terminal
    cfg = cfg_of(request)
    data = input_of(request)
    source = cond_blob(request, "build_triage_terminal", "normalize_issue_rows")
    rows = data.get("rows") if isinstance(data.get("rows"), list) else source.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return fail("malformed_issue_rows", failure_class="terminal", retry_safe=False, mutated=False)
    configured = {
        "ready": _configured_label(cfg, data, "ready_label", "ai:ready"),
        "blocked": _configured_label(cfg, data, "blocked_label", "ai:blocked"),
        "in_progress": _configured_label(cfg, data, "in_progress_label", "ai:in-progress"),
        "pr_opened": _configured_label(cfg, data, "pr_opened_label", "ai:pr-opened"),
        "frozen": _configured_label(cfg, data, "frozen_label", "frozen"),
        "needs_feedback": _configured_label(cfg, data, "needs_feedback_label", "ai:needs-feedback"),
    }
    folded = {name: value.casefold() for name, value in configured.items()}
    assignee = str(data.get("assignee") or cfg.get("assignee") or source.get("config", {}).get("assignee") or "mikolaj92")
    eligible = []
    skipped = []
    for row in rows:
        labels = {str(value).casefold() for value in (row.get("labels") or [])}
        reason = "ok"
        allowed = True
        for name in ("blocked", "in_progress", "pr_opened", "frozen", "needs_feedback"):
            if folded[name] in labels:
                allowed = False
                reason = configured[name]
                break
        if allowed and folded["ready"] not in labels:
            allowed = False
            reason = f"missing:{configured['ready']}"
        people = [str(value) for value in (row.get("assignees") or []) if str(value)]
        if allowed and people and assignee not in people:
            allowed = False
            reason = f"foreign_assignee:{','.join(people)}"
        (eligible if allowed else skipped).append(row if allowed else {**row, "reason": reason})
    return ok(status="filtered", eligible=eligible, skipped=skipped, eligible_count=len(eligible), skipped_count=len(skipped), repositories=source.get("repositories") or [], ready_label=configured["ready"], assignee=assignee, dry_run=dry_run_flag(request))


def select_triage_candidate(request: Request) -> Result:
    """Select the highest-precedence pre-intake triage candidate without mutation."""
    terminal = terminal_upstream(request, "select_triage_candidate", "normalize_issue_rows", "read_triage_receipt_index")
    if terminal:
        return terminal
    cfg = cfg_of(request)
    data = input_of(request)
    source = cond_blob(request, "normalize_issue_rows")
    enabled = data.get("triage_enabled", cfg.get("triage_enabled", True))
    if type(enabled) is not bool:
        return fail("invalid_triage_enabled", failure_class="terminal", retry_safe=False, mutated=False)
    if not enabled:
        return ok(status="triage_disabled", selected=None, candidate_class=None, candidates=[], candidate_count=0, rows=source.get("rows", []), repositories=source.get("repositories") or [], dry_run=dry_run_flag(request))
    data = input_of(request)
    source = cond_blob(request, "normalize_issue_rows")
    rows = data.get("rows") if isinstance(data.get("rows"), list) else source.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        return fail("malformed_issue_rows", failure_class="terminal", retry_safe=False, mutated=False)
    receipt_index = data.get("receipt_index")
    if receipt_index is None:
        for effector in ("read_triage_receipt_index", "triage_receipt_index"):
            blob = cond_blob(request, effector)
            if isinstance(blob.get("index"), Mapping):
                receipt_index = blob["index"]
                break
            if isinstance(blob.get("receipt_index"), Mapping):
                receipt_index = blob["receipt_index"]
                break
    if receipt_index is None:
        receipt_index = {}
    if not isinstance(receipt_index, Mapping):
        return fail("malformed_receipt_index", failure_class="terminal", retry_safe=False, mutated=False)
    if isinstance(receipt_index.get("index"), Mapping):
        receipt_index = receipt_index["index"]
    ready = _configured_label(cfg, data, "ready_label", "ai:ready")
    frozen = _configured_label(cfg, data, "frozen_label", "frozen")
    needs_feedback = _configured_label(cfg, data, "needs_feedback_label", "ai:needs-feedback")
    duplicate = _configured_label(cfg, data, "duplicate_label", "duplicate")
    out_of_scope = _configured_label(cfg, data, "out_of_scope_label", "ai:out-of-scope")
    blocked = _configured_label(cfg, data, "blocked_label", "ai:blocked")
    in_progress = _configured_label(cfg, data, "in_progress_label", "ai:in-progress")
    pr_opened = _configured_label(cfg, data, "pr_opened_label", "ai:pr-opened")
    candidates: list[tuple[int, tuple[int, str, int], dict[str, Any], str]] = []
    try:
        for raw in rows:
            row = dict(raw)
            labels = {str(value).casefold() for value in (row.get("labels") or [])}
            repo = str(row.get("repo") or "")
            number = int(row.get("number") or 0)
            priority = int(row.get("priority", 0))
            identity = f"{repo}#{number}"
            summary = receipt_index.get(identity, {})
            if summary is None:
                summary = {}
            if not isinstance(summary, Mapping):
                raise ValueError("malformed_receipt_index")
            pending = bool(summary.get("pending", False)) and not bool(summary.get("verified", False))
            decision_recorded = summary.get("decision_recorded", False)
            triage_verified = summary.get("triage_verified", False)
            if type(decision_recorded) is not bool or type(triage_verified) is not bool:
                raise ValueError("malformed_receipt_index")
            terminal_labels = {value.casefold() for value in (ready, duplicate, out_of_scope, frozen, blocked, in_progress, pr_opened)}
            if not pending and not ({frozen.casefold(), ready.casefold()} <= labels) and needs_feedback.casefold() not in labels and labels.intersection(terminal_labels):
                continue
            if pending:
                candidate_class, rank = "reconcile_pending", 0
            elif frozen.casefold() in labels and ready.casefold() in labels:
                candidate_class, rank = "frozen_ready_conflict", 1
            elif not labels.intersection({value.casefold() for value in (ready, needs_feedback, duplicate, out_of_scope, frozen, blocked, in_progress, pr_opened)}):
                updated_at = _parse_updated_at(row.get("updatedAt"), required=decision_recorded)
                watermark = summary.get("feedback_watermark") or summary.get("decision_watermark")
                if decision_recorded:
                    if not isinstance(watermark, str):
                        raise ValueError("missing_triage_watermark")
                    _parse_updated_at(watermark, required=True)
                    if triage_verified and _updated_at_key(updated_at) <= _updated_at_key(watermark):
                        continue
                    candidate_class, rank = ("feedback_updated", 3) if triage_verified else ("reconcile_decision", 0)
                else:
                    candidate_class, rank = "untriaged", 2
            elif needs_feedback.casefold() in labels:
                updated_at = _parse_updated_at(row.get("updatedAt"), required=True)
                watermark = summary.get("feedback_watermark")
                if not isinstance(watermark, str):
                    continue
                _parse_updated_at(watermark, required=True)
                if _updated_at_key(updated_at) > _updated_at_key(watermark):
                    candidate_class, rank = "feedback_updated", 3
                else:
                    continue
            else:
                continue
            row["candidate_class"] = candidate_class
            candidates.append((rank, (priority, repo, number), row, candidate_class))
    except (TypeError, ValueError, AttributeError) as exc:
        return fail("malformed_triage_candidate", failure_class="terminal", retry_safe=False, detail=str(exc), mutated=False)
    candidates.sort(key=lambda item: (item[0], item[1]))
    selected = candidates[0][2] if candidates else None
    candidate_class = candidates[0][3] if candidates else None
    return ok(status="selected", selected=selected, candidate_class=candidate_class, candidates=[item[2] for item in candidates], candidate_count=len(candidates), dry_run=dry_run_flag(request))


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
