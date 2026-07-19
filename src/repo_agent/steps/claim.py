from __future__ import annotations

import json
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
    decide = dict(cond.get("decide_issue_action") or cond.get("decide") or {})
    decide_action = str(decide.get("action") or "")
    dry_run = dry_run_flag(request, default=bool(poll.get("dry_run", True)))
    # Direction reject/skip must never claim: durable comment is the only write.
    if decide_action in {"reject_comment", "skip"}:
        return noop(
            str(decide.get("reason") or f"issue_{decide_action}"),
            dry_run=dry_run,
            selected=None,
            decide_action=decide_action,
            decide_reason=decide.get("reason"),
        )
    if decide.get("status") == "noop":
        return noop(str(decide.get("reason") or "no_selected_issue"), dry_run=dry_run, selected=None)
    selected = decide.get("selected") if decide.get("selected") is not None else poll.get("selected")
    cfg = cfg_of(request)
    if not cfg and isinstance(poll.get("config"), dict):
        cfg = dict(poll["config"])
    assignee = str(cfg.get("assignee") or "mikolaj92")
    ready_label = str(cfg.get("ready_label") or "ai:ready")
    in_progress = str(cfg.get("in_progress_label") or "ai:in-progress")
    gh = str(cfg.get("gh_cli") or "gh")

    if selected is None:
        return noop("no_selected_issue", dry_run=dry_run, selected=None)
    if not isinstance(selected, dict):
        return fail("invalid_selected_issue", failure_class="terminal", retry_safe=False, selected=selected, mutated=False)
    if not selected:
        return noop("no_selected_issue", dry_run=dry_run, selected=selected)

    repo = str(selected.get("repo") or "")
    try:
        number = int(selected.get("number") or 0)
    except (TypeError, ValueError):
        number = 0
    if not repo or not number:
        return fail("invalid_selected_issue", failure_class="terminal", retry_safe=False, selected=selected, mutated=False)
    planned_actions = {
        "assign": assignee,
        "ensure_labels": [ready_label, in_progress],
        "repo": repo,
        "issue": number,
    }

    if dry_run:
        return planned(selected=selected, planned=planned_actions)

    def read_claim() -> tuple[set[str], set[str]]:
        view = run_cmd(
            [gh, "issue", "view", str(number), "--repo", repo, "--json", "assignees,labels"],
            timeout=60,
        )
        raw = (view.stdout or "").strip()
        if not raw:
            raise ValueError("blank claim read-back")
        current = json.loads(raw)
        if not isinstance(current, dict) or not isinstance(current.get("assignees"), list) or not isinstance(current.get("labels"), list):
            raise ValueError("invalid claim read-back shape")
        if any(not isinstance(item, (dict, str)) for item in current["assignees"] + current["labels"]):
            raise ValueError("invalid claim read-back item")
        assignees = {str(item.get("login") or "").strip() if isinstance(item, dict) else str(item).strip() for item in current["assignees"]}
        labels = {str(item.get("name") or "").strip() if isinstance(item, dict) else str(item).strip() for item in current["labels"]}
        if "" in assignees or "" in labels:
            raise ValueError("blank claim read-back item")
        return assignees, labels

    try:
        current_assignees, current_labels = read_claim()
    except CommandError as exc:
        return fail("claim_readback_failed", failure_class="retryable_read", retry_safe=True, selected=selected, error=str(exc), mutated=False, idempotency_key=f"issue:{repo}:{number}:claim")
    except (json.JSONDecodeError, TypeError, AttributeError, ValueError) as exc:
        return fail("claim_readback_failed", failure_class="terminal", retry_safe=False, selected=selected, error=str(exc), mutated=False, idempotency_key=f"issue:{repo}:{number}:claim")
    foreign = current_assignees - {assignee}
    if foreign:
        return fail("claim_foreign_assignee", failure_class="terminal", retry_safe=False, selected=selected, assignees=sorted(current_assignees), mutated=False)
    selected = {**selected, "assignees": sorted(current_assignees), "labels": sorted(current_labels)}
    actions: list[dict[str, Any]] = []
    try:
        if assignee not in current_assignees:
            run_cmd([gh, "issue", "edit", str(number), "--repo", repo, "--add-assignee", assignee], timeout=60)
            actions.append({"action": "add_assignee", "assignee": assignee, "ok": True})
        else:
            actions.append({"action": "add_assignee", "assignee": assignee, "ok": True, "skipped": True})
        for label in (ready_label, in_progress):
            if label and label not in current_labels:
                try:
                    run_cmd([gh, "issue", "edit", str(number), "--repo", repo, "--add-label", label], timeout=60)
                    actions.append({"action": "add_label", "label": label, "ok": True})
                except CommandError as exc:
                    actions.append({"action": "add_label", "label": label, "ok": False, "error": exc.stderr[-300:]})
                    if label == in_progress:
                        raise
    except CommandError as exc:
        mutation_observed = any(a.get("ok") and not a.get("skipped") for a in actions)
        try:
            checked_assignees, checked_labels = read_claim()
            foreign = checked_assignees - {assignee}
            required_labels = {ready_label, in_progress} - {""}
            reconciled_mutation = mutation_observed or (
                (assignee not in current_assignees and assignee in checked_assignees)
                or bool((required_labels - current_labels) & checked_labels)
            )
            reconciled_selected = {**selected, "assignees": sorted(checked_assignees), "labels": sorted(checked_labels)}
            if foreign:
                return fail("claim_foreign_assignee", failure_class="terminal", retry_safe=False, selected=reconciled_selected, actions=actions, assignees=sorted(checked_assignees), mutated=reconciled_mutation)
            if assignee in checked_assignees and required_labels.issubset(checked_labels):
                return ok(status="claimed", selected=reconciled_selected, planned=planned_actions, actions=actions, claimed={"repo": repo, "issue": number, "assignee": assignee}, idempotency_key=f"issue:{repo}:{number}:claim:{assignee}:{ready_label}:{in_progress}", reconciled=True, mutated=reconciled_mutation)
            return fail("claim_partial", failure_class="terminal", retry_safe=False, selected=reconciled_selected, planned=planned_actions, actions=actions, assignees=sorted(checked_assignees), labels=sorted(checked_labels), required_labels=sorted(required_labels), error=str(exc), mutated=reconciled_mutation)
        except (CommandError, json.JSONDecodeError, TypeError, AttributeError, ValueError) as reconcile_exc:
            return fail("claim_failed", failure_class="terminal", retry_safe=False, selected=selected, planned=planned_actions, actions=actions, error=f"{exc}; claim reconciliation failed: {reconcile_exc}", mutated=mutation_observed)

    mutation_observed = any(a.get("ok") and not a.get("skipped") for a in actions)
    try:
        checked_assignees, checked_labels = read_claim()
        foreign = checked_assignees - {assignee}
        required_labels = {ready_label, in_progress} - {""}
        if foreign:
            return fail("claim_foreign_assignee", failure_class="terminal", retry_safe=False, selected=selected, assignees=sorted(checked_assignees), labels=sorted(checked_labels), actions=actions, mutated=mutation_observed)
        if assignee not in checked_assignees or not required_labels.issubset(checked_labels):
            return fail("claim_readback_mismatch", failure_class="reconcile_then_retry", retry_safe=False, selected=selected, assignees=sorted(checked_assignees), labels=sorted(checked_labels), required_labels=sorted(required_labels), actions=actions, mutated=mutation_observed)
        selected = {**selected, "assignees": sorted(checked_assignees), "labels": sorted(checked_labels)}
    except (CommandError, json.JSONDecodeError, TypeError, AttributeError, ValueError) as exc:
        return fail("claim_readback_failed", failure_class="reconcile_then_retry", retry_safe=False, selected=selected, actions=actions, error=str(exc), mutated=mutation_observed)
    mutation_observed = any(a.get("ok") and not a.get("skipped") for a in actions)
    return ok(status="claimed", selected=selected, planned=planned_actions, actions=actions, claimed={"repo": repo, "issue": number, "assignee": assignee}, idempotency_key=f"issue:{repo}:{number}:claim:{assignee}:{ready_label}:{in_progress}", mutated=mutation_observed)
