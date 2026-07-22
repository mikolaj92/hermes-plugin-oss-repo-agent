from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from repo_agent.envelope import Request, Result

from repo_agent.adapters_cli import CommandError, run_cmd
from repo_agent.envelope import cfg_of, conduction_of, dry_run_flag, fail, input_of, noop, ok, planned


def _claim_file(configured: str) -> Path | None:
    value = str(configured or "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    if path.exists() and path.is_dir():
        return path / "claim.json"
    return path if path.suffix.lower() == ".json" else path / "claim.json"


def _claim_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _issue_number(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _claim_identity(payload: Any) -> tuple[str, int, str, str] | None:
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return None
    repo = _claim_text(payload, "repo")
    board = _claim_text(payload, "board")
    assignee = _claim_text(payload, "assignee")
    claimed_at = _claim_text(payload, "claimedAt")
    issue = _issue_number(payload.get("issue"))
    if not repo or not board or not assignee or not claimed_at or issue is None:
        return None
    return (repo, issue, board, assignee)


def _read_claim(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"claim_malformed:{exc}"
    if _claim_identity(payload) is None:
        return None, "claim_malformed:invalid_identity"
    return dict(payload), None


def _claims_in_directory(path: Path, max_active: int) -> tuple[list[tuple[Path, dict[str, Any]]], str | None]:
    if not path.exists() or not path.is_dir():
        return [], None
    try:
        entries = sorted(path.glob("*.json"))
    except OSError as exc:
        return [], f"claim_malformed:{exc}"
    claims: list[tuple[Path, dict[str, Any]]] = []
    for entry in entries:
        payload, error = _read_claim(entry)
        if error:
            return [], error
        if payload is not None:
            claims.append((entry, payload))
    return claims, None


def _reserve_claim(path: Path, *, repo: str, issue: int, board: str, assignee: str) -> tuple[dict[str, Any] | None, str | None, bool]:
    payload = {
        "version": 1,
        "repo": repo,
        "issue": issue,
        "board": board,
        "assignee": assignee,
        "claimedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        # A durable reservation requires both the file and its directory entry.
        parent_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except FileExistsError:
        existing, error = _read_claim(path)
        if error:
            return None, error, False
        assert existing is not None
        identity = (repo, issue, board, assignee)
        return (existing, None, True) if _claim_identity(existing) == identity else (existing, "claim_busy", False)
    except OSError as exc:
        # The file may exist even when the final fsync failed; never retry as if
        # no reservation was made.
        if path.exists():
            return payload, f"claim_uncertain:{exc}", False
        return None, f"claim_create_failed:{exc}", False
    existing, error = _read_claim(path)
    if error or existing is None:
        return payload, error or "claim_malformed:empty_readback", False
    if _claim_identity(existing) != (repo, issue, board, assignee):
        return existing, "claim_readback_mismatch", False
    return existing, None, False


def claim_github_issue(request: Request) -> Result:
    data = input_of(request)
    cond = conduction_of(request)
    poll = dict(cond.get("poll") or cond.get("poll_eligible_issues") or cond.get("intake_poll") or {})
    decide = dict(cond.get("decide_issue_action") or cond.get("decide") or cond.get("intake_decide_issue_action") or {})
    for upstream in (poll, decide):
        if upstream.get("ok") is False or str(upstream.get("status") or "") in {"failed", "cancelled", "timed_out"}:
            return fail("upstream_failed", failure_class="terminal", retry_safe=False, upstream=upstream)
    input_decide = data.get("decide_issue_action") or data.get("decide") or data.get("intake_decide_issue_action")
    if isinstance(input_decide, dict):
        decide = dict(input_decide)
    dry_run = dry_run_flag(request, default=bool(poll.get("dry_run", True)))
    if str(decide.get("action") or "") in {"reject_comment", "skip"} or decide.get("status") == "noop":
        return noop(str(decide.get("reason") or "no_selected_issue"), dry_run=dry_run, selected=None)
    cfg = dict(poll.get("config") or {})
    cfg.update(cfg_of(request))
    cfg.update({key: data[key] for key in ("assignee", "ready_label", "in_progress_label", "gh_cli", "active_issue_path", "max_active_issues") if key in data})
    assignee_value = cfg.get("assignee", "mikolaj92")
    if not isinstance(assignee_value, str) or not assignee_value.strip():
        return fail("invalid_assignee", failure_class="terminal", retry_safe=False)
    assignee = assignee_value.strip()
    ready_label = str(cfg.get("ready_label") or "ai:ready")
    in_progress = str(cfg.get("in_progress_label") or "ai:in-progress")
    gh = str(cfg.get("gh_cli") or "gh")
    configured_path = cfg.get("active_issue_path") or (cfg.get("paths") or {}).get("active_issue")
    try:
        max_active_value = cfg.get("max_active_issues", 1)
        if isinstance(max_active_value, bool):
            raise ValueError
        max_active = int(max_active_value)
    except (TypeError, ValueError):
        max_active = 0
    if "selected" in data:
        selected = data.get("selected")
    elif "selected" in decide:
        selected = decide.get("selected")
    else:
        selected = poll.get("selected")

    if selected is None:
        return noop("no_selected_issue", dry_run=dry_run, selected=None)
    if not isinstance(selected, dict):
        return fail("invalid_selected_issue", failure_class="terminal", retry_safe=False, selected=selected)
    repo_value = selected.get("repo")
    board_value = selected.get("board")
    if not isinstance(repo_value, str) or not repo_value.strip() or (not dry_run and (not isinstance(board_value, str) or not board_value.strip())):
        return fail("invalid_selected_issue", failure_class="terminal", retry_safe=False, selected=selected)
    repo = repo_value.strip()
    board = board_value.strip() if isinstance(board_value, str) else ""
    number_value = selected.get("number") if "number" in selected else selected.get("issue")
    if isinstance(number_value, bool) or not isinstance(number_value, int) or number_value <= 0:
        return fail("invalid_selected_issue", failure_class="terminal", retry_safe=False, selected=selected)
    number = number_value
    planned_actions = {"assign": assignee, "ensure_labels": [ready_label, in_progress], "repo": repo, "issue": number}
    if dry_run:
        return planned(selected=selected, planned=planned_actions)
    if max_active < 1:
        return fail("invalid_max_active_issues", failure_class="terminal", retry_safe=False, selected=selected, max_active_issues=max_active)
    configured_value = str(configured_path or "")
    configured_obj = Path(configured_value).expanduser() if configured_value else None
    is_claim_directory = bool(configured_obj and ((configured_obj.exists() and configured_obj.is_dir()) or configured_obj.suffix.lower() != ".json"))
    claim_path = _claim_file(configured_value)
    if claim_path is None:
        return fail("missing_claim_path", failure_class="terminal", retry_safe=False, selected=selected)
    claims, error = _claims_in_directory(claim_path.parent, max_active) if is_claim_directory else ([], None)
    if error:
        return fail(error.split(":", 1)[0], failure_class="terminal", retry_safe=False, selected=selected, claim_path=str(claim_path), error=error)
    identity = (repo, number, board, assignee)
    match = next(((p, c) for p, c in claims if _claim_identity(c) == identity), None)
    local_mutated = False
    if match:
        claim_path, claim = match
        reused = True
    else:
        if len(claims) >= max_active:
            return fail("claim_capacity_exhausted", failure_class="terminal", retry_safe=False, selected=selected, claim_path=str(claim_path), active_claims=[c for _, c in claims], max_active_issues=max_active)
        if max_active > 1 and claim_path.name == "claim.json" and claim_path.exists():
            claim_path = claim_path.parent / f"claim-{repo.replace('/', '_')}-{number}.json"
        claim, error, reused = _reserve_claim(claim_path, repo=repo, issue=number, board=board, assignee=assignee)
        local_mutated = not reused
        if error:
            reason = "claim_busy" if error == "claim_busy" else error.split(":", 1)[0]
            failure_class = "reconcile_then_retry" if local_mutated or reason == "claim_uncertain" else "terminal"
            return fail(reason, failure_class=failure_class, retry_safe=False, selected=selected, claim_path=str(claim_path), claim=claim, error=error, mutated=local_mutated)
    actions: list[dict[str, Any]] = []
    def read_claim() -> tuple[set[str], set[str]]:
        proc = run_cmd([gh, "issue", "view", str(number), "--repo", repo, "--json", "assignees,labels"], timeout=60)
        current = json.loads((proc.stdout or "").strip())
        if not isinstance(current, dict) or not isinstance(current.get("assignees"), list) or not isinstance(current.get("labels"), list):
            raise ValueError("invalid claim read-back shape")
        assignees = {str(x.get("login") or "").strip() if isinstance(x, dict) else str(x).strip() for x in current["assignees"]}
        labels = {str(x.get("name") or "").strip() if isinstance(x, dict) else str(x).strip() for x in current["labels"]}
        if "" in assignees or "" in labels:
            raise ValueError("blank claim read-back item")
        return assignees, labels
    try:
        current_assignees, current_labels = read_claim()
        if current_assignees - {assignee}:
            return fail("claim_foreign_assignee", failure_class="terminal", retry_safe=False, selected=selected, claim=claim, claim_path=str(claim_path), assignees=sorted(current_assignees), mutated=False)
        selected = {**selected, "assignees": sorted(current_assignees), "labels": sorted(current_labels)}
        if assignee not in current_assignees:
            run_cmd([gh, "issue", "edit", str(number), "--repo", repo, "--add-assignee", assignee], timeout=60)
            actions.append({"action": "add_assignee", "assignee": assignee, "ok": True})
        for label in (ready_label, in_progress):
            if label and label not in current_labels:
                run_cmd([gh, "issue", "edit", str(number), "--repo", repo, "--add-label", label], timeout=60)
                actions.append({"action": "add_label", "label": label, "ok": True})
        checked_assignees, checked_labels = read_claim()
        if checked_assignees - {assignee}:
            return fail("claim_foreign_assignee", failure_class="terminal", retry_safe=False, selected=selected, claim=claim, claim_path=str(claim_path), assignees=sorted(checked_assignees), mutated=bool(actions) or local_mutated)
        required = {x for x in (ready_label, in_progress) if x}
        if assignee not in checked_assignees or not required.issubset(checked_labels):
            return fail("claim_readback_mismatch", failure_class="reconcile_then_retry" if (actions or local_mutated) else "terminal", retry_safe=False, selected=selected, claim=claim, claim_path=str(claim_path), actions=actions, mutated=bool(actions) or local_mutated)
    except (CommandError, subprocess.TimeoutExpired, json.JSONDecodeError, TypeError, ValueError, OSError) as exc:
        reason = "claim_uncertain" if (actions or local_mutated) else "claim_readback_failed"
        return fail(reason, failure_class="reconcile_then_retry" if (actions or local_mutated) else "terminal", retry_safe=False, selected=selected, claim=claim, claim_path=str(claim_path), actions=actions, error=str(exc), mutated=bool(actions) or local_mutated)
    return ok(status="claimed", selected=selected, claim=claim, claim_path=str(claim_path), reused=reused, planned=planned_actions, actions=actions, claimed={"repo": repo, "issue": number, "assignee": assignee}, mutated=bool(actions) or local_mutated)
