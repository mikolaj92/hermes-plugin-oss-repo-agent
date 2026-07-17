from __future__ import annotations

from typing import Any

from fala.adapters import EffectorRunRequest, EffectorRunResult

from repo_agent.adapters_cli import CommandError, gh_json
from repo_agent.envelope import cfg_of, dry_run_flag, fail, input_of, ok


def _issue_eligible(issue: dict[str, Any], *, ready_label: str, assignee: str) -> tuple[bool, str]:
    labels = {
        str(item.get("name") or "")
        for item in (issue.get("labels") or [])
        if isinstance(item, dict)
    }
    if "ai:blocked" in labels:
        return False, "ai:blocked"
    if "ai:in-progress" in labels:
        return False, "ai:in-progress"
    if "ai:pr-opened" in labels:
        return False, "ai:pr-opened"
    if ready_label not in labels:
        return False, f"missing:{ready_label}"

    assignees = [
        str(item.get("login") or "")
        for item in (issue.get("assignees") or [])
        if isinstance(item, dict)
    ]
    assignees = [a for a in assignees if a]
    if assignees and assignee not in assignees:
        return False, f"foreign_assignee:{','.join(assignees)}"
    return True, "ok"


def poll_eligible_issues(request: EffectorRunRequest) -> EffectorRunResult:
    """Atomic: read eligible GitHub issues (gh only)."""
    cfg = cfg_of(request)
    data = input_of(request)
    repos = data.get("repos") or []
    limit = int(data.get("limit") or cfg.get("limit") or 10)
    ready_label = str(cfg.get("ready_label") or "ai:ready")
    assignee = str(cfg.get("assignee") or "mikolaj92")
    gh = str(cfg.get("gh_cli") or "gh")
    dry_run = dry_run_flag(request)

    eligible: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for entry in repos:
        repo = str(entry.get("repo") or "")
        board = str(entry.get("board") or "")
        if not repo:
            continue
        try:
            issues = gh_json(
                [
                    "issue",
                    "list",
                    "--repo",
                    repo,
                    "--state",
                    "open",
                    "--limit",
                    str(limit),
                    "--json",
                    "number,title,url,labels,assignees",
                ],
                gh=gh,
            )
        except CommandError as exc:
            errors.append({"repo": repo, "error": str(exc), "stderr": exc.stderr[-500:]})
            continue
        if not isinstance(issues, list):
            errors.append({"repo": repo, "error": "invalid_gh_json"})
            continue
        for issue in issues:
            ok_flag, reason = _issue_eligible(
                issue, ready_label=ready_label, assignee=assignee
            )
            row = {
                "repo": repo,
                "board": board,
                "number": int(issue.get("number") or 0),
                "title": str(issue.get("title") or ""),
                "url": str(issue.get("url") or ""),
                "labels": sorted(
                    str(x.get("name") or "")
                    for x in (issue.get("labels") or [])
                    if isinstance(x, dict)
                ),
                "assignees": [
                    str(x.get("login") or "")
                    for x in (issue.get("assignees") or [])
                    if isinstance(x, dict) and x.get("login")
                ],
            }
            if ok_flag:
                eligible.append(row)
            else:
                skipped.append({**row, "reason": reason})

    selected = eligible[0] if eligible else None
    return ok(
        status="polled",
        dry_run=dry_run,
        eligible_count=len(eligible),
        skipped_count=len(skipped),
        error_count=len(errors),
        eligible=eligible,
        skipped=skipped[:50],
        errors=errors,
        selected=selected,
        # keep nested config snapshot for downstream dry-run defaults
        config={"ready_label": ready_label, "assignee": assignee, "limit": limit},
    )
