from __future__ import annotations

from typing import Any

from fala.adapters import EffectorRunRequest, EffectorRunResult

from repo_agent.adapters_cli import CommandError, run_cmd
from repo_agent.envelope import (
    cfg_of,
    conduction_of,
    dry_run_flag,
    fail,
    noop,
    ok,
    planned,
)


def claim_github_issue(request: EffectorRunRequest) -> EffectorRunResult:
    """Atomic: assign/label one selected GitHub issue."""
    cond = conduction_of(request)
    poll = dict(cond.get("poll") or cond.get("poll_eligible_issues") or {})
    selected = poll.get("selected")
    dry_run = dry_run_flag(request, default=bool(poll.get("dry_run", True)))
    cfg = cfg_of(request)
    if not cfg and isinstance(poll.get("config"), dict):
        cfg = dict(poll["config"])
    assignee = str(cfg.get("assignee") or "mikolaj92")
    ready_label = str(cfg.get("ready_label") or "ai:ready")
    in_progress = str(cfg.get("in_progress_label") or "ai:in-progress")
    gh = str(cfg.get("gh_cli") or "gh")

    if not selected:
        return noop("no_selected_issue", dry_run=dry_run, selected=None)

    repo = str(selected["repo"])
    number = int(selected["number"])
    planned_actions = {
        "assign": assignee,
        "ensure_labels": [ready_label, in_progress],
        "repo": repo,
        "issue": number,
    }

    if dry_run:
        return planned(selected=selected, planned=planned_actions)

    actions: list[dict[str, Any]] = []
    try:
        assignees = selected.get("assignees") or []
        if assignee not in assignees:
            run_cmd(
                [
                    gh,
                    "issue",
                    "edit",
                    str(number),
                    "--repo",
                    repo,
                    "--add-assignee",
                    assignee,
                ],
                timeout=60,
            )
            actions.append({"action": "add_assignee", "assignee": assignee, "ok": True})
        else:
            actions.append(
                {"action": "add_assignee", "assignee": assignee, "ok": True, "skipped": True}
            )

        for label in (ready_label, in_progress):
            if label and label not in (selected.get("labels") or []):
                try:
                    run_cmd(
                        [
                            gh,
                            "issue",
                            "edit",
                            str(number),
                            "--repo",
                            repo,
                            "--add-label",
                            label,
                        ],
                        timeout=60,
                    )
                    actions.append({"action": "add_label", "label": label, "ok": True})
                except CommandError as exc:
                    actions.append(
                        {
                            "action": "add_label",
                            "label": label,
                            "ok": False,
                            "error": exc.stderr[-300:],
                        }
                    )
                    if label == in_progress:
                        raise
    except CommandError as exc:
        return fail(
            "claim_failed",
            selected=selected,
            planned=planned_actions,
            actions=actions,
            error=str(exc),
            mutated=any(a.get("ok") and not a.get("skipped") for a in actions),
        )

    return ok(
        status="claimed",
        selected=selected,
        planned=planned_actions,
        actions=actions,
        claimed={"repo": repo, "issue": number, "assignee": assignee},
        mutated=True,
    )
