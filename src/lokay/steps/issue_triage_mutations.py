"""Verified GitHub mutations for pre-intake issue triage.

Each function is a request -> Result atom.  Reads are deliberately fresh at
mutation boundaries; mutation errors are uncertain and therefore never
reported as safely retryable.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

from lokay.adapters_cli import CommandError, run_cmd
from lokay.envelope import cfg_of, cond_blob, cond_get, dry_run_flag, fail, input_of, noop, ok, planned, terminal_upstream


def _conduction(request: Mapping[str, Any]) -> dict[str, Any]:
    value = input_of(request).get("conduction")
    return dict(value) if isinstance(value, Mapping) else {}


def _selected(request: Mapping[str, Any]) -> dict[str, Any]:
    data = input_of(request)
    explicit = data.get("selected")
    if isinstance(explicit, Mapping) and explicit:
        return dict(explicit)
    cond = _conduction(request)
    for name in (
        "select_triage_candidate",
        "reserve_triage_run_budget",
        "classify_triage_issue",
        "decide_triage_mutation",
        "read_triage_labels",
        "read_triage_issue_state",
    ):
        value = cond.get(name)
        if not isinstance(value, Mapping):
            value = next((blob for key, blob in cond.items() if key.endswith(f"_{name}") and isinstance(blob, Mapping)), None)
        if not isinstance(value, Mapping) or not value:
            continue
        nested = value.get("selected")
        if isinstance(nested, Mapping) and nested:
            return dict(nested)
        repo = str(value.get("repo") or "").strip()
        number = value.get("number")
        if repo and isinstance(number, int) and not isinstance(number, bool) and number > 0:
            selected = {"repo": repo, "number": number}
            for key in ("title", "body", "labels", "clone_path", "priority", "candidate_class", "triage_goal"):
                if key in value:
                    selected[key] = value[key]
            return selected
    repo = str(data.get("repo") or "").strip()
    number = data.get("number") if isinstance(data.get("number"), int) and not isinstance(data.get("number"), bool) else data.get("issue")
    if repo and isinstance(number, int) and not isinstance(number, bool) and number > 0:
        return {"repo": repo, "number": number}
    return {}


def _triage_enabled(request: Mapping[str, Any]) -> bool:
    data, cfg = input_of(request), cfg_of(request)
    value = data.get("triage_enabled", cfg.get("triage_enabled", True))
    return bool(value)


def _idle(request: Mapping[str, Any]) -> dict[str, Any] | None:
    if not _triage_enabled(request):
        return noop("triage_disabled", selected=None)
    selected = _selected(request)
    data = input_of(request)
    # An explicit repo/number is itself a selected identity for standalone atoms.
    if not selected and data.get("repo") and data.get("number") not in (None, "", 0):
        return None
    if not selected:
        return noop("no_triage_selection", selected=None)
    return None
def _decision(request: Mapping[str, Any]) -> dict[str, Any]:
    cond = _conduction(request)
    for name in ("decide_triage_mutation", "classify_triage_issue", "select_triage_candidate"):
        value = cond.get(name)
        if not isinstance(value, Mapping):
            value = next((blob for key, blob in cond.items() if key.endswith(f"_{name}") and isinstance(blob, Mapping)), None)
        if isinstance(value, Mapping) and value:
            return dict(value)
    return {}

def _classification(request: Mapping[str, Any], decision: Mapping[str, Any] | None = None) -> str:
    data = input_of(request)
    value = cond_get(request, "classification", "decide_triage_mutation", "classify_triage_issue", default=data.get("classification", ""))
    if isinstance(value, Mapping):
        value = value.get("classification")
    if not value and isinstance((decision or {}).get("classification"), Mapping):
        value = (decision or {}).get("classification", {}).get("classification")
    return str(value or "").strip().casefold()


def _action(request: Mapping[str, Any], decision: Mapping[str, Any] | None = None) -> str:
    data = input_of(request)
    value = cond_get(request, "action", "decide_triage_mutation", "classify_triage_issue", default=data.get("action", ""))
    return str(value or (decision or {}).get("action") or "").strip().casefold()


def _digest(request: Mapping[str, Any]) -> str:
    data = input_of(request)
    return str(cond_get(request, "decision_digest", "decide_triage_mutation", "classify_triage_issue", aliases=("digest",), default=data.get("decision_digest") or data.get("digest") or "") or "")
 
def _gate(request: Mapping[str, Any], *actions: str) -> dict[str, Any] | None:
    idle = _idle(request)
    if idle is not None:
        return idle
    decision = _decision(request)
    action = _action(request, decision)
    classification = _classification(request, decision)
    if actions and action not in actions:
        return noop("action_not_selected", action=action, classification=classification, **_identity(request))
    return None


def _close_authorized(request: Mapping[str, Any]) -> bool:
    for name in ("publish_triage_close_authorization", "verify_triage_receipt"):
        blob = cond_blob(request, name)
        payload = blob.get("payload") if isinstance(blob.get("payload"), Mapping) else blob
        if blob.get("ok") is True and blob.get("status") in {"written", "published", "verified"} and isinstance(payload, Mapping):
            if payload.get("authorized") is True and (payload.get("verified") is True or name == "verify_triage_receipt"):
                return True
    return False


def _rows(request: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    data = input_of(request)
    source = cond_blob(request, "normalize_issue_rows")
    rows = data.get("rows") if isinstance(data.get("rows"), list) else source.get("rows")
    if isinstance(rows, list) and all(isinstance(row, Mapping) for row in rows):
        return [dict(row) for row in rows]
    return None


def _values(request: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str, int, str]:
    data, cfg = input_of(request), cfg_of(request)
    selected = _selected(request)
    if not isinstance(selected, Mapping):
        selected = {}
    repo = str(data.get("repo") or selected.get("repo") or cfg.get("repo") or "").strip()
    raw = data.get("number") or data.get("issue") or selected.get("number") or selected.get("issue")
    number = raw if isinstance(raw, int) and not isinstance(raw, bool) else 0
    gh = str(data.get("gh_cli") or cfg.get("gh_cli") or "gh")
    return data, cfg, repo, number, gh


def _identity(request: Mapping[str, Any]) -> dict[str, Any]:
    _, _, repo, number, _ = _values(request)
    return {"repo": repo, "number": number}


def _invalid(repo: str, number: int) -> dict[str, Any] | None:
    if not repo or number <= 0:
        return fail("missing_repo_or_number", failure_class="terminal", retry_safe=False, repo=repo, number=number)
    return None


def _json(proc: Any) -> Any:
    return json.loads((getattr(proc, "stdout", "") or "").strip())


def _label_name(item: Any) -> str:
    return str(item.get("name") or "").strip() if isinstance(item, Mapping) else str(item).strip()


def _labels(payload: Any) -> list[str]:
    raw = payload.get("labels") if isinstance(payload, Mapping) else payload
    if not isinstance(raw, list):
        raise ValueError("invalid labels read-back shape")
    result = [_label_name(item) for item in raw]
    if any(not value for value in result):
        raise ValueError("blank label in read-back")
    if len({value.casefold() for value in result}) != len(result):
        raise ValueError("ambiguous case-folded labels")
    return result
def _frozen_state(request: Mapping[str, Any]) -> bool:
    data, cfg, _, _, _ = _values(request)
    state = cond_blob(request, "read_triage_labels")
    labels = list(state.get("labels") or data.get("current_labels") or [])
    configured = str(data.get("frozen_label") or cfg.get("frozen_label") or (cfg.get("labels") or {}).get("frozen") or "frozen")
    return configured.casefold() in {str(value).casefold() for value in labels}


def _read_issue(gh: str, repo: str, number: int) -> dict[str, Any]:
    proc = run_cmd([gh, "issue", "view", str(number), "--repo", repo, "--json", "labels,state,stateReason,updatedAt,comments"], timeout=60)
    payload = _json(proc)
    if not isinstance(payload, Mapping):
        raise ValueError("invalid issue read-back shape")
    labels = _labels(payload)
    state = str(payload.get("state") or "").upper()
    if state not in {"OPEN", "CLOSED"}:
        raise ValueError("invalid issue state")
    comments = payload.get("comments", [])
    if not isinstance(comments, list) or any(not isinstance(item, Mapping) for item in comments):
        raise ValueError("invalid comments read-back shape")
    reason = str(payload.get("stateReason") or payload.get("closedReason") or "").upper()
    return {"labels": labels, "state": state, "stateReason": reason, "updatedAt": payload.get("updatedAt"), "comments": [dict(x) for x in comments]}


def read_triage_labels(request: Mapping[str, Any]) -> dict[str, Any]:
    """Read authoritative issue labels/state/comments for the selected issue."""
    data, cfg, repo, number, gh = _values(request)
    idle = _idle(request)
    if idle is not None:
        return idle
    bad = _invalid(repo, number)
    if bad:
        return bad
    try:
        state = _read_issue(gh, repo, number)
    except CommandError as exc:
        return fail("triage_labels_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), mutated=False, **_identity(request))
    except (subprocess.TimeoutExpired, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("triage_labels_read_failed", failure_class="terminal", retry_safe=False, error=str(exc), mutated=False, **_identity(request))
    return ok(status="triage_labels_read", **state, **_identity(request), selected=_selected(request) or data.get("selected"), dry_run=dry_run_flag(request))

def _configured_label(data: Mapping[str, Any], cfg: Mapping[str, Any], action: str = "", classification: str = "") -> str:
    action = str(action or "").strip().casefold()
    classification = str(classification or "").strip().casefold()
    label = data.get("label") or data.get("triage_label")
    labels = data.get("labels") or cfg.get("labels")
    labels = labels if isinstance(labels, Mapping) else {}
    if not label and action in {"add_ready", "remove_ready", "ready"}:
        label = labels.get("ready") or labels.get(action) or data.get("ready_label") or cfg.get("ready_label")
    if not label and action in {"feedback", "close"}:
        # Class labels are terminal for poll; stamp those instead of needs_feedback.
        if classification in {"duplicate", "out_of_scope"}:
            label = (
                data.get(f"{classification}_label")
                or cfg.get(f"{classification}_label")
                or labels.get(classification)
                or ("duplicate" if classification == "duplicate" else "ai:out-of-scope")
            )
        elif action == "feedback":
            label = (
                data.get("needs_feedback_label")
                or cfg.get("needs_feedback_label")
                or labels.get("needs_feedback")
                or "ai:needs-feedback"
            )
    if not label and action:
        label = labels.get(action) or data.get(f"{action}_label") or cfg.get(f"{action}_label")
    return str(label or "").strip()


def _auto_close_out_of_scope(request: Mapping[str, Any]) -> bool:
    data, cfg = input_of(request), cfg_of(request)
    selected = _selected(request)
    value = data.get("auto_close_out_of_scope", selected.get("auto_close_out_of_scope", cfg.get("auto_close_out_of_scope", True)))
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)



def decide_triage_mutation(request: Mapping[str, Any]) -> dict[str, Any]:
    idle = _idle(request)
    if idle is not None:
        return idle
    """Purely choose the remote action, enforcing frozen precedence."""
    data, cfg, repo, number, _ = _values(request)
    state = cond_blob(request, "read_triage_labels")
    if state.get("status") == "failed" or state.get("ok") is False:
        return fail("upstream_failed", failure_class="terminal", retry_safe=False, upstream=state, **_identity(request))
    labels = list(state.get("labels") or data.get("current_labels") or [])
    folded = {str(x).casefold() for x in labels}
    frozen = str(data.get("frozen_label") or cfg.get("frozen_label") or (cfg.get("labels") or {}).get("frozen") or "frozen").casefold()
    ready = str(data.get("ready_label") or cfg.get("ready_label") or (cfg.get("labels") or {}).get("ready") or "ai:ready").casefold()
    conducted = cond_blob(request, "classify_triage_issue")
    if conducted.get("status") == "failed" or conducted.get("ok") is False:
        return fail("upstream_failed", failure_class="terminal", retry_safe=False, upstream=conducted, **_identity(request))
    decision_value = data.get("decision")
    if decision_value is None:
        decision_value = conducted.get("classification")
    classification = str(data.get("classification") or (decision_value.get("classification") if isinstance(decision_value, Mapping) else "")).strip()
    digest = _digest(request)
    if not digest and isinstance(decision_value, Mapping):
        digest = str(decision_value.get("decision_digest") or conducted.get("decision_digest") or "")
    if not digest:
        digest = str(conducted.get("decision_digest") or "")
    if frozen in folded:
        if ready in folded:
            return ok(
                status="mutation_decided",
                action="remove_ready",
                label=next(x for x in labels if x.casefold() == ready),
                reason="frozen_ready_reconciliation",
                decision_digest=digest or None,
                **_identity(request),
            )
        return noop("frozen", **_identity(request))
    if data.get("action"):
        action = str(data["action"])
    elif classification == "ready":
        action = "add_ready"
    elif classification == "out_of_scope":
        action = "close" if _auto_close_out_of_scope(request) else "feedback"
    elif classification in {"needs_feedback", "duplicate", "ambiguous"}:
        action = "feedback"
    elif classification in {"close", "closed"}:
        action = "close"
    else:
        return fail("unknown_triage_action", failure_class="terminal", retry_safe=False, **_identity(request))
    label = _configured_label(data, cfg, action, classification) if action in {"add_ready", "feedback", "label", "close"} else None
    result = ok(
        status="mutation_decided",
        action=action,
        classification=classification,
        decision_digest=digest or None,
        **_identity(request),
    )
    if label:
        result["label"] = label
    return result


def _repo_labels(gh: str, repo: str) -> list[dict[str, Any]]:
    proc = run_cmd([gh, "label", "list", "--repo", repo, "--limit", "1000", "--json", "name,color,description"], timeout=60)
    payload = _json(proc)
    if not isinstance(payload, list) or any(not isinstance(item, Mapping) for item in payload):
        raise ValueError("invalid repository labels read-back shape")
    return [dict(item) for item in payload]


def ensure_triage_label(request: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve/provision one label using case-folded identity and readback."""
    data, cfg, repo, number, gh = _values(request)
    bad = _invalid(repo, number)
    if _frozen_state(request):
        return noop("frozen", **_identity(request))
    gate = _gate(request, "add_ready", "remove_ready", "label", "feedback", "close")
    if gate is not None:
        return gate
    if bad:
        return bad
    action = _action(request) or str(data.get("label_kind") or data.get("action") or "")
    label = _configured_label(data, cfg, action, _classification(request))
    if not label and action in {"add_ready", "remove_ready", "ready"}:
        label = str(data.get("ready_label") or cfg.get("ready_label") or "ai:ready")
    if not label:
        return fail("missing_label", failure_class="terminal", retry_safe=False, **_identity(request))
    try:
        available = _repo_labels(gh, repo)
        matches = [item for item in available if str(item.get("name") or "").casefold() == label.casefold()]
        if len(matches) > 1:
            raise ValueError("ambiguous configured label")
        desired = data.get("label_metadata") if isinstance(data.get("label_metadata"), Mapping) else {}
        if matches:
            existing = matches[0]
            for key in ("color", "description"):
                if key in desired and str(existing.get(key) or "") != str(desired[key]):
                    raise ValueError("incompatible configured label metadata")
            return ok(status="label_resolved", label=str(existing.get("name")), configured_label=label, created=False, **_identity(request))
        if dry_run_flag(request):
            return planned(label=label, configured_label=label, created=False, **_identity(request))
        color = str(desired.get("color") or data.get("label_color") or "B60205")
        description = str(desired.get("description") or data.get("label_description") or "")
        command = [gh, "label", "create", label, "--repo", repo, "--color", color]
        if description:
            command.extend(["--description", description])
        run_cmd(command, timeout=60)
        after = _repo_labels(gh, repo)
        matches = [item for item in after if str(item.get("name") or "").casefold() == label.casefold()]
        if len(matches) != 1:
            return fail("label_provision_readback_mismatch", failure_class="reconcile_then_retry", retry_safe=False, mutated=True, **_identity(request))
        return ok(status="label_provisioned", label=str(matches[0].get("name")), configured_label=label, created=True, mutated=True, **_identity(request))
    except CommandError as exc:
        return fail("label_provision_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), mutated=True, **_identity(request))
    except (subprocess.TimeoutExpired, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("label_provision_failed", failure_class="terminal", retry_safe=False, error=str(exc), mutated=False, **_identity(request))


def mutate_triage_issue_labels(request: Mapping[str, Any]) -> dict[str, Any]:
    """Add/remove a classification label, then verify with a fresh issue read."""
    gate = _gate(request, "add_ready", "remove_ready", "label", "feedback", "close")
    if gate is not None:
        return gate
    data, cfg, repo, number, gh = _values(request)
    bad = _invalid(repo, number)
    if bad:
        return bad
    decision = cond_blob(request, "decide_triage_mutation")
    action = str(data.get("action") or decision.get("action") or "")
    digest = _digest(request)
    if not digest:
        digest = str(decision.get("decision_digest") or "")
    state = cond_blob(request, "read_triage_labels")
    labels = list(state.get("labels") or data.get("current_labels") or [])
    folded = {str(value).casefold() for value in labels}
    frozen = str(data.get("frozen_label") or cfg.get("frozen_label") or (cfg.get("labels") or {}).get("frozen") or "frozen")
    ready = str(data.get("ready_label") or cfg.get("ready_label") or (cfg.get("labels") or {}).get("ready") or "ai:ready")
    if frozen.casefold() in folded and str(decision.get("action") or "") == "remove_ready":
        action = "remove_ready"
    if frozen.casefold() in folded and action != "remove_ready":
        return noop("frozen", **_identity(request))
    if action == "remove_ready":
        label = next((value for value in labels if value.casefold() == ready.casefold()), ready)
        if label.casefold() not in folded:
            return noop("ready_absent", action=action, decision_digest=digest or None, **_identity(request))
        verb = "--remove-label"
    else:
        classification = str(data.get("classification") or decision.get("classification") or _classification(request) or "").strip().casefold()
        label = str(data.get("label") or decision.get("label") or _configured_label(data, cfg, action, classification)).strip()
        if not label:
            names = cfg.get("labels") if isinstance(cfg.get("labels"), Mapping) else {}
            # Terminal class labels win over needs_feedback for poll freeze.
            if action in {"feedback", "close"} and classification in {"duplicate", "out_of_scope"}:
                kind = classification
            elif action == "feedback" or classification in {"", "ambiguous"}:
                kind = "needs_feedback"
            else:
                kind = classification
            label = str(data.get(f"{kind}_label") or names.get(kind) or "").strip()
        if action == "add_ready" and not label:
            label = ready
        if not label:
            return fail("missing_label", failure_class="terminal", retry_safe=False, **_identity(request))
        if label.casefold() in folded:
            return ok(
                status="labels_verified",
                verified=True,
                mutated=False,
                reason="already_labeled",
                action=action,
                labels=labels,
                label=label,
                decision_digest=digest or None,
                updatedAt=state.get("updatedAt"),
                issue_updated_at=state.get("updatedAt"),
                **_identity(request),
            )
        verb = "--add-label"
    if dry_run_flag(request):
        return planned(action=action, label=label, decision_digest=digest or None, **_identity(request))
    try:
        run_cmd([gh, "issue", "edit", str(number), "--repo", repo, verb, label], timeout=60)
        after = _read_issue(gh, repo, number)
        actual = {value.casefold() for value in after["labels"]}
        expected = verb == "--add-label"
        if (label.casefold() in actual) != expected:
            return fail("label_mutation_readback_mismatch", failure_class="reconcile_then_retry", retry_safe=False, mutated=True, action=action, label=label, decision_digest=digest or None, **_identity(request))
    except (CommandError, subprocess.TimeoutExpired) as exc:
        return fail("label_mutation_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), mutated=True, action=action, label=label, decision_digest=digest or None, **_identity(request))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("label_mutation_readback_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), mutated=True, action=action, label=label, decision_digest=digest or None, **_identity(request))
    return ok(
        status="labels_verified",
        verified=True,
        mutated=True,
        action=action,
        labels=after["labels"],
        label=label,
        decision_digest=digest or None,
        updatedAt=after.get("updatedAt"),
        issue_updated_at=after.get("updatedAt"),
        **_identity(request),
    )


def _marker(repo: str, number: int, digest: str) -> str:
    return f"<!-- lokay:issue-triage:{repo}:{number}:{digest} -->"


def _marker_prefix(repo: str, number: int) -> str:
    return f"<!-- lokay:issue-triage:{repo}:{number}:"


def _comment_has_marker_prefix(comment: Mapping[str, Any], prefix: str) -> bool:
    return prefix in str(comment.get("body") or "")

def _comment_database_id(comment: Mapping[str, Any]) -> int | None:
    value = comment.get("databaseId")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    url = str(comment.get("url") or "")
    match = re.search(r"issuecomment-(\d+)", url)
    if match:
        return int(match.group(1))
    return None


def _comment_id(comment: Mapping[str, Any]) -> str | int | None:
    database_id = _comment_database_id(comment)
    if database_id is not None:
        return database_id
    value = comment.get("id")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def post_triage_feedback(request: Mapping[str, Any]) -> dict[str, Any]:
    """Post one marker-bearing question, verifying the marker and comment id."""
    gate = _gate(request, "feedback")
    if gate is not None:
        return gate
    data, cfg, repo, number, gh = _values(request)
    bad = _invalid(repo, number)
    if bad:
        return bad
    if _frozen_state(request):
        return noop("frozen", **_identity(request))
    digest = _digest(request)
    if not digest:
        return fail("missing_decision_digest", failure_class="terminal", retry_safe=False, **_identity(request))
    marker = _marker(repo, number, digest)
    question = str(
        data.get("question")
        or cond_get(request, "question", "classify_triage_issue", "decide_triage_mutation")
        or "Please provide maintainer confirmation for this issue."
    ).strip()
    state = cond_blob(request, "read_triage_labels")
    comments = state.get("comments") or data.get("comments") or []
    if not isinstance(comments, list):
        return fail("invalid_comments", failure_class="terminal", retry_safe=False, **_identity(request))
    prefix = _marker_prefix(repo, number)
    existing = [x for x in comments if isinstance(x, Mapping) and _comment_has_marker_prefix(x, prefix)]
    if existing:
        existing.sort(key=lambda item: str(item.get("createdAt") or ""))
        chosen = existing[-1]
        body = str(chosen.get("body") or "")
        posted_digest = digest
        if "<!-- lokay:issue-triage:" in body:
            tail = body.rsplit(":", 1)[-1]
            extracted = tail.removesuffix(" -->").strip()
            if re.fullmatch(r"[0-9a-f]{64}", extracted):
                posted_digest = extracted
        return noop(
            "feedback_already_posted",
            marker=body or marker,
            comment_id=_comment_id(chosen),
            decision_digest=posted_digest,
            matches=len(existing),
            **_identity(request),
        )
    body = f"{question}\n\n{marker}"
    if dry_run_flag(request):
        return planned(marker=marker, body=body, **_identity(request))
    try:
        run_cmd([gh, "issue", "comment", str(number), "--repo", repo, "--body", body], timeout=60)
        after = _read_issue(gh, repo, number)
        matches = [x for x in after["comments"] if marker in str(x.get("body") or "")]
        if len(matches) != 1 or not _comment_id(matches[0]):
            return fail("feedback_readback_mismatch", failure_class="reconcile_then_retry", retry_safe=False, mutated=True, marker=marker, **_identity(request))
    except CommandError as exc:
        return fail("feedback_post_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), mutated=True, marker=marker, **_identity(request))
    except (subprocess.TimeoutExpired, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("feedback_verify_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), mutated=True, marker=marker, **_identity(request))
    return ok(
        status="feedback_verified",
        verified=True,
        mutated=True,
        marker=marker,
        comment=matches[0],
        comment_id=_comment_id(matches[0]),
        decision_digest=digest,
        updatedAt=after.get("updatedAt"),
        issue_updated_at=after.get("updatedAt"),
        verified_readback_state="verified",
        **_identity(request),
    )

def verify_triage_feedback(request: Mapping[str, Any]) -> dict[str, Any]:
    post = cond_blob(request, "post_triage_feedback")
    gate = _gate(request, "feedback")
    post_ready = post.get("ok") is True and (
        post.get("status") in {"feedback_verified", "feedback_already_posted", "planned"}
        or (post.get("status") == "noop" and post.get("reason") == "feedback_already_posted")
    )
    if gate is not None and not post_ready:
        return gate
    data, cfg, repo, number, gh = _values(request)
    bad = _invalid(repo, number)
    if bad:
        return bad
    marker = str(data.get("marker") or post.get("marker") or "")
    if not marker:
        digest = _digest(request)
        if not digest:
            return fail("missing_decision_digest", failure_class="terminal", retry_safe=False, **_identity(request))
        marker = _marker(repo, number, digest)
    if _frozen_state(request):
        return noop("frozen", **_identity(request))
    if dry_run_flag(request):
        return planned(marker=marker, **_identity(request))
    try:
        state = _read_issue(gh, repo, number)
        prefix = _marker_prefix(repo, number)
        matches = [x for x in state["comments"] if marker in str(x.get("body") or "") or _comment_has_marker_prefix(x, prefix)]
        # Prefer exact marker; otherwise accept the newest issue-scoped marker.
        exact = [x for x in matches if marker in str(x.get("body") or "")]
        if exact:
            matches = exact
        elif matches:
            matches = sorted(matches, key=lambda item: str(item.get("createdAt") or ""))[-1:]
        if len(matches) != 1 or not _comment_id(matches[0]):
            return fail("feedback_readback_mismatch", failure_class="reconcile_then_retry", retry_safe=False, mutated=True, marker=marker, **_identity(request))
        comment = matches[0]
        comment_id = _comment_id(comment)
        body = str(comment.get("body") or "")
        digest = ""
        if "<!-- lokay:issue-triage:" in body:
            tail = body.rsplit(":", 1)[-1]
            extracted = tail.removesuffix(" -->").strip()
            if re.fullmatch(r"[0-9a-f]{64}", extracted):
                digest = extracted
        if not digest:
            digest = _digest(request)
        if not digest:
            digest = str(post.get("decision_digest") or "")
        if not digest and "<!-- lokay:issue-triage:" in marker:
            tail = marker.rsplit(":", 1)[-1]
            digest = tail.removesuffix(" -->").strip()
    except CommandError as exc:
        return fail("feedback_verify_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), mutated=True, **_identity(request))
    except (subprocess.TimeoutExpired, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("feedback_verify_failed", failure_class="terminal", retry_safe=False, error=str(exc), mutated=True, **_identity(request))
    return ok(
        status="feedback_verified",
        verified=True,
        marker=marker,
        comment=comment,
        comment_id=comment_id,
        decision_digest=digest or None,
        issue_updated_at=state.get("updatedAt"),
        updated_at=state.get("updatedAt"),
        verified_readback_state="verified",
        **_identity(request),
    )


def observe_triage_feedback(request: Mapping[str, Any]) -> dict[str, Any]:
    """Select one later human response, excluding Lokay and marker comments."""
    verified = cond_blob(request, "verify_triage_feedback")
    gate = _gate(request, "feedback")
    verified_ready = verified.get("ok") is True and verified.get("status") in {"feedback_verified", "planned"}
    if gate is not None and not verified_ready:
        return gate
    data, cfg, repo, number, gh = _values(request)
    bad = _invalid(repo, number)
    if bad:
        return bad
    watermark = str(data.get("watermark") or data.get("feedback_watermark") or "")
    if _frozen_state(request):
        return noop("frozen", **_identity(request))
    try:
        state = _read_issue(gh, repo, number)
        candidates = []
        for comment in state["comments"]:
            created = str(comment.get("createdAt") or "")
            author = comment.get("author") if isinstance(comment.get("author"), Mapping) else {}
            login = str(author.get("login") or comment.get("author_login") or "").casefold()
            body = str(comment.get("body") or "")
            if created <= watermark or not login or login.endswith("[bot]") or "<!-- lokay:" in body.casefold() or login in {str(x).casefold() for x in data.get("lokay_logins", ("lokay", "lokay-intake", "lokay-fixer"))}:
                continue
            candidates.append(comment)
        candidates.sort(key=lambda x: str(x.get("createdAt") or ""))
    except CommandError as exc:
        return fail("feedback_observation_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), **_identity(request))
    except (subprocess.TimeoutExpired, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("feedback_observation_failed", failure_class="terminal", retry_safe=False, error=str(exc), **_identity(request))
    if not candidates:
        return noop("no_human_response", watermark=state.get("updatedAt"), **_identity(request))
    return ok(status="feedback_observed", response=candidates[0], watermark=candidates[0].get("createdAt"), **_identity(request))


def close_triage_issue(request: Mapping[str, Any]) -> dict[str, Any]:
    data, cfg, repo, number, gh = _values(request)
    idle = _idle(request)
    if idle is not None:
        return idle
    bad = _invalid(repo, number)
    if bad:
        return bad
    decision = _decision(request)
    action = _action(request, decision)
    authorized = _close_authorized(request)
    if action and action != "close" and not authorized:
        return noop("action_not_selected", action=action, classification=_classification(request, decision), **_identity(request))
    if _frozen_state(request):
        return noop("frozen", **_identity(request))
    state = cond_blob(request, "read_triage_labels")
    if not authorized:
        return noop("close_not_authorized", **_identity(request))
    if state.get("state") == "CLOSED":
        return ok(status="already_closed", reconciled=True, **_identity(request))
    if dry_run_flag(request):
        return planned(reason="not planned", **_identity(request))
    try:
        run_cmd([gh, "issue", "close", str(number), "--repo", repo, "--reason", "not planned"], timeout=60)
    except (CommandError, subprocess.TimeoutExpired) as exc:
        return fail("triage_close_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), mutated=True, **_identity(request))
    return ok(status="triage_close_requested", mutated=True, **_identity(request))


def verify_triage_issue_closed(request: Mapping[str, Any]) -> dict[str, Any]:
    idle = _idle(request)
    if idle is not None:
        return idle
    data, cfg, repo, number, gh = _values(request)
    decision = _decision(request)
    action = _action(request, decision)
    if action and action != "close" and not _close_authorized(request):
        return noop("action_not_selected", action=action, classification=_classification(request, decision), **_identity(request))
    bad = _invalid(repo, number)
    if bad:
        return bad
    if dry_run_flag(request):
        return planned(**_identity(request))
    try:
        state = _read_issue(gh, repo, number)
    except CommandError as exc:
        return fail("triage_close_verify_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), mutated=True, **_identity(request))
    except (subprocess.TimeoutExpired, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("triage_close_verify_failed", failure_class="terminal", retry_safe=False, error=str(exc), mutated=True, **_identity(request))
    if state["state"] != "CLOSED" or state.get("stateReason") not in {"NOT_PLANNED", "NOT PLANNED"}:
        return fail("triage_close_readback_mismatch", failure_class="reconcile_then_retry", retry_safe=False, mutated=True, state=state["state"], stateReason=state.get("stateReason"), **_identity(request))
    return ok(status="triage_issue_closed_verified", verified=True, mutated=True, state=state, **_identity(request))


def build_triage_terminal(request: Mapping[str, Any]) -> dict[str, Any]:
    """Emit a terminal row set; verified evidence replaces only its selected row."""
    data, cfg, repo, number, gh = _values(request)
    rows = _rows(request)
    selected = _selected(request)
    idle = _idle(request)
    if rows is None:
        return fail("malformed_issue_rows", failure_class="terminal", retry_safe=False)
    terminal = terminal_upstream(
        request,
        "build_triage_terminal",
        "select_triage_candidate",
        "reserve_triage_run_budget",
        "decide_triage_mutation",
        "mutate_triage_issue_labels",
        "verify_triage_feedback",
        "verify_triage_issue_closed",
    )
    if terminal is not None:
        return terminal
    if idle is not None:
        return noop(str(idle.get("reason") or "no_triage_selection"), rows=rows, selected=None)
    receipt = data.get("triage_receipt") or cond_blob(request, "verify_triage_receipt").get("payload")
    upstream = {name: cond_blob(request, name) for name in ("verify_triage_feedback", "verify_triage_issue_closed", "mutate_triage_issue_labels")}
    closed = upstream["verify_triage_issue_closed"]
    verified_blob = closed if closed.get("verified") is True else next(
        (
            upstream[name]
            for name in ("mutate_triage_issue_labels", "verify_triage_feedback")
            if upstream[name].get("verified") is True
            or (
                name == "mutate_triage_issue_labels"
                and upstream[name].get("ok") is True
                and upstream[name].get("status") == "labels_verified"
            )
        ),
        None,
    )
    receipt_verified = isinstance(receipt, Mapping) and (
        receipt.get("verified") is True
        or str(receipt.get("verified_readback_state") or "").casefold() == "verified"
        or str(receipt.get("stage") or "") in {"mutation-verified", "feedback-verified", "close-verified"}
    )
    if verified_blob is None or not receipt_verified:
        return noop("triage_not_verified", rows=rows, selected=selected or None)
    out = list(rows)
    row = dict(selected or {})
    state = verified_blob.get("state") if isinstance(verified_blob.get("state"), Mapping) else {}
    row.update(_identity(request))
    if state:
        row.update(state=state.get("state"), labels=state.get("labels", row.get("labels")), updatedAt=state.get("updatedAt", row.get("updatedAt")), comments=state.get("comments", row.get("comments", [])))
    else:
        row.update(labels=verified_blob.get("labels", row.get("labels")), updatedAt=verified_blob.get("updatedAt", row.get("updatedAt")))
    row["triage_verified"] = True
    row["triage_receipt"] = dict(receipt)
    replaced = False
    for index, candidate in enumerate(out):
        if str(candidate.get("repo") or "") == repo and candidate.get("number") == number:
            out[index] = row
            replaced = True
            break
    if not replaced:
        return fail("selected_row_missing", failure_class="terminal", retry_safe=False, selected=selected or None, **_identity(request))
    return ok(status="triage_terminal", triage_verified=True, triage_receipt=dict(receipt), rows=out, selected=row, decision=data.get("decision") or cond_get(request, "decision", "classify_triage_issue"), action=_action(request))
