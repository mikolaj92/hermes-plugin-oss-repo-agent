from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from lokay.envelope import Request, Result, cfg_of, cond_blob, cond_get, fail, input_of, noop, terminal_upstream, upstream_noop


def triage_gate(request: Request, operation: str, *upstream_ids: str) -> Result | None:
    """Resolve triage identity and make expected graph skips explicit."""
    data, cfg = input_of(request), cfg_of(request)
    enabled = data.get("triage_enabled", cfg.get("triage_enabled", True))
    if enabled is False:
        return noop("triage_disabled", selected=None)
    if enabled is not True:
        return fail("invalid_triage_enabled", failure_class="terminal", retry_safe=False, mutated=False)
    terminal = terminal_upstream(request, operation, *upstream_ids)
    if terminal:
        return terminal
    if triage_selected(request, *upstream_ids):
        return None
    if any(cond_blob(request, upstream_id) for upstream_id in upstream_ids):
        upstream = upstream_noop(request, *upstream_ids)
        return noop(str(upstream.get("reason") or "triage_candidate_missing"), selected=None)
    if str(data.get("repo") or cfg.get("repo") or "").strip():
        return None
    return noop("triage_candidate_missing", selected=None)


def triage_selected(request: Request, *upstream_ids: str) -> dict[str, Any]:
    """Resolve the selected triage identity from input or successful conduction."""
    data = input_of(request)
    selected = data.get("selected")
    if isinstance(selected, Mapping) and selected:
        return dict(selected)
    # Bare repo/number is only identity when the upstream blob itself proves a
    # successful selection/mutation decision. Idle or failed blobs must not
    # authorize receipt writes.
    allowed_status = {
        "selected",
        "mutation_decided",
        "classified",
        "triage_labels_read",
        "feedback_verified",
        "feedback_already_posted",
        "labels_verified",
        "context_packet",
        "snapshot_unchanged",
        "written",
        "exists",
        "planned",
        "feedback_observed",
        "label_resolved",
        "label_provisioned",
        "triage_close_requested",
        "triage_issue_closed_verified",
        "already_closed",
    }
    for upstream_id in upstream_ids:
        blob = cond_blob(request, upstream_id)
        if not blob:
            continue
        nested = blob.get("selected")
        if isinstance(nested, Mapping) and nested:
            return dict(nested)
        if blob.get("ok") is not True:
            continue
        status = str(blob.get("status") or "")
        reason = str(blob.get("reason") or "")
        if status not in allowed_status and not (status == "noop" and reason == "feedback_already_posted"):
            continue
        repo = str(blob.get("repo") or "").strip()
        number = blob.get("number", blob.get("issue"))
        if repo and isinstance(number, int) and not isinstance(number, bool) and number > 0:
            return {"repo": repo, "number": number}
    return {}


def triage_identity(request: Request, *upstream_ids: str) -> dict[str, Any]:
    data, cfg = input_of(request), cfg_of(request)
    selected = triage_selected(request, *upstream_ids)
    repo = str(data.get("repo") or selected.get("repo") or cfg.get("repo") or "").strip()
    raw_number = data.get("number", data.get("issue", selected.get("number", selected.get("issue"))))
    try:
        number = int(raw_number) if not isinstance(raw_number, bool) else 0
    except (TypeError, ValueError):
        number = 0
    clone_path = str(data.get("clone_path") or selected.get("clone_path") or cfg.get("clone_path") or "").strip()
    return {"selected": selected or None, "repo": repo, "number": number, "clone_path": clone_path}


def triage_candidate_class(request: Request, *upstream_ids: str) -> str:
    """Return the selected triage candidate_class from input or conduction."""
    data = input_of(request)
    selected = triage_selected(request, *upstream_ids)
    if isinstance(selected, Mapping) and selected.get("candidate_class") not in (None, ""):
        return str(selected["candidate_class"])
    for upstream_id in ("select_triage_candidate", "reserve_triage_run_budget", *upstream_ids):
        blob = cond_blob(request, upstream_id)
        if not blob:
            continue
        if blob.get("candidate_class") not in (None, ""):
            return str(blob["candidate_class"])
        nested = blob.get("selected")
        if isinstance(nested, Mapping) and nested.get("candidate_class") not in (None, ""):
            return str(nested["candidate_class"])
    return str(data.get("candidate_class") or "")


def is_triage_reconcile(request: Request, *upstream_ids: str) -> bool:
    """True when the candidate only needs label/decision reconciliation."""
    return triage_candidate_class(request, *upstream_ids) in {"reconcile_decision", "reconcile_pending"}



_CLASSIFICATIONS = frozenset({"ready", "needs_feedback", "duplicate", "out_of_scope", "ambiguous"})
_EVIDENCE_KINDS = frozenset({"issue", "comment", "repository_context"})
_KEYS = frozenset({"schema_version", "classification", "reason", "question", "canonical_issue", "evidence"})
_EVIDENCE_KEYS = frozenset({"kind", "identity", "quote"})
_TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
_LOKAY_MARKER = "<!-- lokay:"
_UNTRUSTED_BEGIN = "--- BEGIN UNTRUSTED_GITHUB_CONTENT ---"
_UNTRUSTED_END = "--- END UNTRUSTED_GITHUB_CONTENT ---"


def untrusted_github_block(payload: Any) -> str:
    """Serialize GitHub-controlled evidence as inert, explicitly bounded data."""
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"{_UNTRUSTED_BEGIN}\n{encoded}\n{_UNTRUSTED_END}"


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate_key:{key}")
        result[key] = value
    return result


def parse_classification_output(
    stdout: str,
    *,
    max_bytes: int | None = None,
    sources: Mapping[str, str] | None = None,
    issue_number: int | None = None,
) -> dict[str, Any]:
    if not isinstance(stdout, str):
        raise ValueError("stdout_must_be_string")
    encoded = stdout.encode("utf-8")
    if max_bytes is not None and (isinstance(max_bytes, bool) or max_bytes < 1 or len(encoded) > max_bytes):
        raise ValueError("omp_output_oversized")
    try:
        decoder = json.JSONDecoder(object_pairs_hook=_object_pairs)
        value, end = decoder.raw_decode(stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid_classifier_json") from exc
    if stdout[end:].strip():
        raise ValueError("classifier_trailing_data")
    return validate_classification(value, sources=sources, issue_number=issue_number)


def validate_classification(
    payload: Any,
    *,
    sources: Mapping[str, str] | None = None,
    issue_number: int | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _KEYS:
        raise ValueError("invalid_classifier_keys")
    if payload.get("schema_version") != 1 or isinstance(payload.get("schema_version"), bool):
        raise ValueError("invalid_schema_version")
    classification = payload.get("classification")
    if not isinstance(classification, str) or classification not in _CLASSIFICATIONS:
        raise ValueError("invalid_classification")
    reason = payload.get("reason")
    question = payload.get("question")
    canonical = payload.get("canonical_issue")
    evidence = payload.get("evidence")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("invalid_reason")
    if not isinstance(question, str):
        raise ValueError("invalid_question")
    if classification == "needs_feedback":
        if not question.strip():
            raise ValueError("missing_question")
    elif question:
        raise ValueError("unexpected_question")
    if isinstance(canonical, bool) or not isinstance(canonical, int):
        raise ValueError("invalid_canonical_issue")
    if classification == "duplicate":
        if canonical <= 0 or (issue_number is not None and canonical == issue_number):
            raise ValueError("invalid_canonical_issue")
    elif canonical != 0:
        raise ValueError("unexpected_canonical_issue")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("invalid_evidence")
    normalized_evidence: list[dict[str, str]] = []
    for item in evidence:
        if not isinstance(item, dict) or set(item) != _EVIDENCE_KEYS:
            raise ValueError("invalid_evidence_keys")
        kind = item.get("kind")
        identity = item.get("identity")
        quote = item.get("quote")
        if kind not in _EVIDENCE_KINDS or not isinstance(identity, str) or not identity.strip():
            raise ValueError("invalid_evidence_identity")
        if not isinstance(quote, str) or not quote.strip() or len(quote) > 2_000:
            raise ValueError("invalid_evidence_quote")
        if sources is not None:
            source = sources.get(identity)
            if not isinstance(source, str) or quote not in source:
                raise ValueError("unverifiable_evidence_quote")
        normalized_evidence.append({"kind": kind, "identity": identity, "quote": quote})
    return {
        "schema_version": 1,
        "classification": classification,
        "reason": reason.strip(),
        "question": question.strip(),
        "canonical_issue": canonical,
        "evidence": normalized_evidence,
    }


def decision_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _login(identity: Mapping[str, Any]) -> str:
    author = identity.get("author")
    if isinstance(author, Mapping):
        return str(author.get("login") or "").strip()
    return str(identity.get("author_login") or identity.get("login") or "").strip()


def is_non_lokay(identity: Mapping[str, Any], *, lokay_logins: Sequence[str] = ("lokay", "lokay-intake", "lokay-fixer")) -> bool:
    body = str(identity.get("body") or "")
    login = _login(identity).casefold()
    if _LOKAY_MARKER in body.casefold():
        return False
    configured = {value.casefold() for value in lokay_logins}
    return bool(login) and login not in configured and not login.endswith("[bot]")


def is_trusted_maintainer(identity: Mapping[str, Any], *, lokay_logins: Sequence[str] = ("lokay", "lokay-intake", "lokay-fixer")) -> bool:
    association = str(identity.get("authorAssociation") or identity.get("author_association") or "").upper()
    return association in _TRUSTED_ASSOCIATIONS and is_non_lokay(identity, lokay_logins=lokay_logins)


def _base_close_gate(classification: Mapping[str, Any], fresh_state: Mapping[str, Any], expected: str, auto_close: bool) -> dict[str, Any] | None:
    if classification.get("classification") != expected:
        return {"authorized": False, "reason": "classification_mismatch"}
    if not auto_close:
        return {"authorized": False, "reason": "auto_close_disabled"}
    if str(fresh_state.get("state") or "").upper() != "OPEN":
        return {"authorized": False, "reason": "issue_not_open"}
    labels = {str(value).casefold() for value in fresh_state.get("labels") or ()}
    if "frozen" in labels:
        return {"authorized": False, "reason": "issue_frozen"}
    if fresh_state.get("updatedAt") != fresh_state.get("classified_updatedAt"):
        return {"authorized": False, "reason": "issue_changed"}
    return None


def authorize_duplicate_close(
    classification: Mapping[str, Any],
    fresh_state: Mapping[str, Any],
    comments: Sequence[Mapping[str, Any]],
    *,
    auto_close: bool,
    lokay_logins: Sequence[str] = ("lokay", "lokay-intake", "lokay-fixer"),
) -> dict[str, Any]:
    rejected = _base_close_gate(classification, fresh_state, "duplicate", auto_close)
    if rejected:
        return rejected
    target = classification.get("canonical_issue")
    canonical = fresh_state.get("canonical")
    if not isinstance(target, int) or target <= 0 or target == fresh_state.get("number"):
        return {"authorized": False, "reason": "invalid_canonical_issue"}
    if not isinstance(canonical, Mapping) or canonical.get("number") != target:
        return {"authorized": False, "reason": "canonical_issue_unverified"}
    repo = str(fresh_state.get("repo") or "")
    patterns = (re.compile(rf"(?<![\w#])#{target}(?!\d)"), re.compile(rf"https://github\.com/{re.escape(repo)}/issues/{target}(?:\b|$)"))
    for comment in comments:
        body = str(comment.get("body") or "")
        if not is_trusted_maintainer(comment, lokay_logins=lokay_logins) or not any(pattern.search(body) for pattern in patterns):
            continue
        return {
            "authorized": True,
            "reason": "trusted_duplicate_evidence",
            "evidence": {
                "comment_id": comment.get("databaseId"),
                "author": _login(comment),
                "createdAt": comment.get("createdAt"),
                "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "canonical_issue": target,
            },
        }
    return {"authorized": False, "reason": "trusted_duplicate_evidence_missing"}


def authorize_out_of_scope_close(
    classification: Mapping[str, Any],
    fresh_state: Mapping[str, Any],
    comments: Sequence[Mapping[str, Any]],
    *,
    auto_close: bool,
    triage_goal: str,
    reject_labels: Sequence[str],
    lokay_logins: Sequence[str] = ("lokay", "lokay-intake", "lokay-fixer"),
) -> dict[str, Any]:
    rejected = _base_close_gate(classification, fresh_state, "out_of_scope", auto_close)
    if rejected:
        return rejected
    if not triage_goal.strip():
        return {"authorized": False, "reason": "triage_goal_missing"}
    labels = {str(value).casefold(): str(value) for value in fresh_state.get("labels") or ()}
    preexisting = {str(value).casefold() for value in fresh_state.get("preexisting_labels") or ()}
    for configured in reject_labels:
        key = str(configured).casefold()
        if key in labels and key in preexisting:
            return {"authorized": True, "reason": "preexisting_out_of_scope_label", "evidence": {"label": labels[key]}}
    scope_terms = ("out of scope", "unrelated", "not related to this repository")
    for comment in comments:
        body = str(comment.get("body") or "")
        if not is_trusted_maintainer(comment, lokay_logins=lokay_logins) or not any(term in body.casefold() for term in scope_terms):
            continue
        return {
            "authorized": True,
            "reason": "trusted_out_of_scope_evidence",
            "evidence": {
                "comment_id": comment.get("databaseId"),
                "author": _login(comment),
                "createdAt": comment.get("createdAt"),
                "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            },
        }
    return {"authorized": False, "reason": "trusted_out_of_scope_evidence_missing"}


def triage_precedence_action(row: Mapping[str, Any]) -> dict[str, Any] | None:
    receipt = row.get("triage_receipt")
    if row.get("triage_verified") is True and isinstance(receipt, Mapping) and receipt.get("verified") is True:
        return {"action": "accept", "reason": "triage_verified"}
    return None
