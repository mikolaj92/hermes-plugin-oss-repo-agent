"""Pure joins for independent Lokay orchestration lanes."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
from pathlib import Path
from typing import Any

from lokay.envelope import Request, Result, conduction_of, input_of, ok

_TERMINAL = frozenset({"failed", "cancelled", "timed_out"})

# The first name is the canonical standalone tail; composed paths append a lane
# prefix. Earlier tails are accepted for callers joining before the lane's
# optional Kanban reconciliation atoms.
_LANE_TAILS: dict[str, tuple[str, ...]] = {
    "intake": ("reconcile_intake_task", "build_issue_claim_result", "select_issue_candidate"),
    "dispatch": ("verify_task_completed", "verify_dispatch_receipt", "select_dispatch_task"),
    "triage": ("verify_task_blocked", "verify_merge_receipt", "decide_triage_action", "select_fix_pr"),
    "lifecycle": ("decide_lifecycle_transition", "read_lifecycle_local_evidence", "read_lifecycle_github_state"),
}
_ISSUE_TRIAGE_ATOM_MARKERS = (
    "normalize_triage",
    "filter_triage",
    "select_triage",
    "triage_receipt_index",
    "triage_run_budget",
    "read_triage",
    "build_triage",
    "classify_triage",
    "publish_triage",
    "decide_triage_mutation",
    "ensure_triage_label",
    "mutate_triage",
    "post_triage_feedback",
    "verify_triage_feedback",
    "observe_triage_feedback",
    "close_triage_issue",
    "verify_triage_issue_closed",
    "triage_terminal",
)
_TRIAGE_WORK_STATUSES = frozenset({
    "labels_verified",
    "feedback_verified",
    "triage_issue_closed_verified",
})
_EXPECTED_NOOP_REASONS = frozenset({
    "not_selected",
    "no_candidate",
    "no-candidate",
    "no_triage_selection",
    "no_selected_issue",
    "no_selected_pr",
    "claim_busy",
})


def _is_issue_triage_atom(name: str) -> bool:
    """Recognize standalone and intake-prefixed issue-triage atoms."""
    atom = name.removeprefix("intake_")
    return any(marker in atom for marker in _ISSUE_TRIAGE_ATOM_MARKERS)


def _triage_receipts(request: Request) -> tuple[tuple[str, dict[str, Any]], ...]:
    conduction = conduction_of(request)
    receipts: list[tuple[str, dict[str, Any]]] = []
    for name, value in conduction.items():
        blob = _blob(value)
        if blob is not None and _is_issue_triage_atom(str(name)):
            receipts.append((str(name), blob))
    supplied = input_of(request).get("lanes")
    if isinstance(supplied, Mapping):
        triage = _blob(supplied.get("triage"))
        if triage is not None:
            receipts.append(("triage", triage))
    return tuple(receipts)


def _expected_noop(receipt: Mapping[str, Any]) -> bool:
    if receipt.get("code"):
        return False
    reason = str(receipt.get("reason") or receipt.get("status") or "").strip().casefold()
    return reason in _EXPECTED_NOOP_REASONS


def _triage_worked(receipts: tuple[tuple[str, dict[str, Any]], ...]) -> bool:
    for name, receipt in receipts:
        status = str(receipt.get("status") or "")
        operation = str(receipt.get("operation") or "")
        if status in _TRIAGE_WORK_STATUSES and (receipt.get("verified") is True or receipt.get("mutated") is True):
            return True
        if receipt.get("verified") is True and any(
            marker in f"{name}:{operation}"
            for marker in ("mutate_triage_issue_labels", "post_triage_feedback", "verify_triage_feedback", "close_triage_issue", "verify_triage_issue_closed")
        ):
            return True
    return False


def _blob(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) and value else None


def _conduction_match(conduction: Mapping[str, Any], tail: str) -> tuple[str, dict[str, Any]] | None:
    exact = _blob(conduction.get(tail))
    if exact is not None:
        return tail, exact
    suffix = f"_{tail}"
    matches = [(str(name), _blob(value)) for name, value in conduction.items() if str(name).endswith(suffix)]
    matches = [(name, value) for name, value in matches if value is not None]
    if not matches:
        return None
    # Prefer the most specific composed process id, matching envelope suffix
    # resolution, while retaining the actual process id for attribution.
    return max(matches, key=lambda item: len(item[0]))


def _lane_result(request: Request, lane: str) -> dict[str, Any]:
    data = input_of(request)
    supplied = data.get("lanes")
    if not isinstance(supplied, Mapping):
        supplied = data.get("lane_results")
    if isinstance(supplied, Mapping):
        value = _blob(supplied.get(lane))
        if value is not None:
            return value
    conduction = conduction_of(request)
    for tail in _LANE_TAILS[lane]:
        match = _conduction_match(conduction, tail)
        if match is not None:
            return match[1]
    return {"status": "noop", "ok": True, "mutated": False, "reason": "lane_result_missing"}


def _terminal_failure(lane: str, receipt: Mapping[str, Any]) -> dict[str, Any] | None:
    if _expected_noop(receipt):
        return None
    status = str(receipt.get("status") or "")
    code = str(receipt.get("code") or "")
    if receipt.get("ok") is False or status in _TERMINAL or code:
        return {
            "lane": lane,
            "reason": str(receipt.get("reason") or code or status or "lane_failed"),
            "failure_class": str(receipt.get("failure_class") or "terminal"),
        }
    return None


def _identity_value(sources: tuple[Mapping[str, Any], ...], *keys: str) -> Any:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return value
    return None

def _repair_cleanup_identity(request: Request, sources: tuple[Mapping[str, Any], ...]) -> dict[str, Any] | None:
    """Extract verified repair receipt remote and deterministic local identities."""
    data = input_of(request)
    cfg = dict(request.get("config") or {}) if isinstance(request, Mapping) else {}
    repair: list[Mapping[str, Any]] = []
    for candidate in sources:
        provenance = candidate.get("provenance")
        if isinstance(provenance, Mapping):
            repair.append({**candidate, **provenance})
        payload = candidate.get("payload")
        if isinstance(payload, Mapping):
            flattened = {**candidate, **payload}
            config = payload.get("config")
            if isinstance(config, Mapping):
                flattened.update(config)
            nested = payload.get("provenance")
            if isinstance(nested, Mapping):
                flattened.update(nested)
            repair.append(flattened)
    source = next((item for item in repair if item.get("local_branch") or item.get("worktree_path")), None)
    if source is None:
        return None
    merged = (source, *sources, data, cfg)
    repo = str(_identity_value(merged, "repo") or "").strip()
    branch = str(_identity_value(merged, "target_branch", "branch", "head_ref", "headRefName") or "").strip()
    try:
        pr = int(_identity_value(merged, "pr_number", "pr", "number") or 0)
        issue = int(_identity_value(merged, "issue", "issue_number") or 0)
    except (TypeError, ValueError):
        return None
    digest = hashlib.sha256(f"{repo}\0{pr}\0{branch}".encode()).hexdigest() if repo and pr > 0 and branch else ""
    expected_local = f"lokay/repair/{digest}" if digest else ""
    local_branch = str(source.get("local_branch") or "").strip() or expected_local
    if not expected_local or local_branch != expected_local:
        return None
    root = str(_identity_value(merged, "worktree_root") or "").strip()
    expected_path = str(Path(root) / expected_local) if root else ""
    path = str(source.get("worktree_path") or "").strip() or expected_path
    if not path or (expected_path and Path(path).resolve() != Path(expected_path).resolve()):
        return None
    receipt = str(_identity_value(merged, "receipt", "receipt_path") or "").strip()
    remote_oid = str(_identity_value(merged, "remote_oid", "after_oid") or "").strip()
    target_branch = str(source.get("target_branch") or branch).strip()
    clone_path = str(_identity_value(merged, "clone_path") or "").strip()
    if not (repo and issue > 0 and pr > 0 and branch and receipt and remote_oid and clone_path and target_branch == branch):
        return None
    return {"repo": repo, "issue": issue, "pr_number": pr, "branch": branch,
            "local_branch": local_branch, "worktree_path": path, "receipt": receipt,
            "remote_oid": remote_oid, "target_branch": target_branch, "clone_path": clone_path,
            "task": str(_identity_value(merged, "task_id", "task") or "").strip()}


def _verified_cleanup_identity(request: Request, lanes: Mapping[str, dict[str, Any]]) -> dict[str, Any] | None:
    data = input_of(request)
    conduction = conduction_of(request)
    triage_evidence: list[Mapping[str, Any]] = []
    lifecycle_evidence: list[Mapping[str, Any]] = []
    for tail in ("verify_merge_receipt", "verify_merge_provenance", "verify_linked_merge_provenance"):
        match = _conduction_match(conduction, tail)
        if match is not None:
            triage_evidence.append(match[1])
    for tail in ("decide_lifecycle_transition",):
        match = _conduction_match(conduction, tail)
        if match is not None:
            lifecycle_evidence.append(match[1])

    repair_candidates: list[Mapping[str, Any]] = []
    for tail in ("verify_repair_receipt", "triage_verify_repair_receipt", "build_repair_receipt", "triage_build_repair_receipt"):
        match = _conduction_match(conduction, tail)
        if match is not None:
            repair_candidates.append(match[1])
    evidence: Mapping[str, Any] | None = None
    provenance: Mapping[str, Any] | None = None
    for candidate in triage_evidence:
        status = str(candidate.get("status") or "")
        if status not in {"merge_receipt_verified", "verified"}:
            continue
        payload = candidate.get("payload")
        nested = payload.get("verified_provenance") if isinstance(payload, Mapping) else None
        candidate_prov = candidate.get("verified_provenance")
        if isinstance(candidate_prov, Mapping):
            provenance = candidate_prov
        elif isinstance(nested, Mapping):
            provenance = nested
        if isinstance(provenance, Mapping) and provenance.get("source") == "github_pr_readback":
            evidence = candidate
            break
    lifecycle = next((candidate for candidate in lifecycle_evidence
                      if str(candidate.get("status") or "") == "decided"
                      and str(candidate.get("outcome") or "") == "finalize_merged"
                      and isinstance(candidate.get("identity"), Mapping)), None)
    if lifecycle is None:
        return None
    lifecycle_identity = lifecycle["identity"]
    if evidence is None:
        evidence = lifecycle
        provenance = lifecycle_identity
    if not isinstance(provenance, Mapping):
        return None
    for key, aliases in {
        "repo": ("repo",), "issue": ("issue", "issue_number"),
        "pr_number": ("pr_number", "number", "pr"),
        "branch": ("branch", "head_ref", "headRefName"),
        "head_oid": ("head_oid", "expected_head_oid", "headRefOid"),
    }.items():
        expected = _identity_value((provenance,), *aliases)
        actual = _identity_value((lifecycle_identity,), *aliases)
        if expected not in (None, "") and str(expected) != str(actual or ""):
            return None

    contextual: list[Mapping[str, Any]] = []
    for tail in ("decide_triage_action", "load_pr_fields", "select_fix_pr", "decide_lifecycle_transition"):
        match = _conduction_match(conduction, tail)
        if match is not None:
            contextual.append(match[1])
    lane_sources = tuple(value for value in lanes.values() if isinstance(value, Mapping))
    sources = (provenance, evidence, *contextual, *lane_sources, data)
    identity: dict[str, Any] = {}
    for key, aliases in {
        "repo": ("repo",),
        "board": ("board",),
        "clone_path": ("clone_path",),
        "priority": ("priority",),
        "issue": ("issue", "issue_number"),
        "pr_number": ("pr_number", "number", "pr"),
        "branch": ("branch", "head_ref", "headRefName"),
        "head_oid": ("head_oid", "expected_head_oid", "headRefOid"),
    }.items():
        value = _identity_value(sources, *aliases)
        if value not in (None, ""):
            identity[key] = value

    repositories = data.get("repos")
    if isinstance(repositories, list):
        matches = [item for item in repositories if isinstance(item, Mapping) and str(item.get("repo") or "") == str(identity.get("repo") or "")]
        if len(matches) > 1:
            return None
        if matches:
            repository = matches[0]
            for key in ("board", "clone_path", "priority"):
                value = repository.get(key)
                if value not in (None, ""):
                    existing = identity.get(key)
                    if existing not in (None, "") and str(existing) != str(value):
                        return None
                    identity[key] = value

    required = ("repo", "issue", "pr_number", "branch", "head_oid")
    if any(key not in identity or identity[key] in (None, "") for key in required):
        return None
    if provenance.get("repo") not in (None, identity["repo"]):
        return None
    if provenance.get("number") not in (None, identity["pr_number"]):
        return None
    if provenance.get("head_oid") not in (None, identity["head_oid"]):
        return None
    if provenance.get("head_ref") not in (None, identity["branch"]):
        return None
    # Verified provenance and lifecycle identity must agree with the exposed
    repair_identity = _repair_cleanup_identity(request, tuple(repair_candidates))
    if repair_identity is not None:
        for key in ("board", "clone_path", "priority"):
            value = _identity_value(tuple(lanes.values()), key)
            if value not in (None, ""):
                if repair_identity.get(key) not in (None, "") and str(repair_identity[key]) != str(value):
                    return None
                repair_identity[key] = value
        # A repair receipt identifies owned local state, but only terminal merge/lifecycle
        # evidence authorizes deleting it.
        if identity is not None and all(str(identity.get(key) or "") == str(repair_identity.get(key) or "") for key in ("repo", "issue", "pr_number", "branch")) and str(identity.get("head_oid") or "") == str(repair_identity.get("remote_oid") or ""):
            repair_identity["head_oid"] = identity["head_oid"]
            return repair_identity

    return identity

def _legacy_lane_worked(lanes: Mapping[str, Mapping[str, Any]]) -> bool:
    """Count ordinary intake/dispatch selection or any verified lane mutation."""
    return any(receipt.get("mutated") is True for receipt in lanes.values()) or any(
        lanes[lane].get("selected") not in (None, False, "", {}, [])
        for lane in ("intake", "dispatch")
    )


def _pending_lane_work(lanes: Mapping[str, Mapping[str, Any]]) -> bool:
    """Recognize genuine lifecycle waiting without misreporting completed work."""
    lifecycle = lanes.get("lifecycle", {})
    return (
        str(lifecycle.get("outcome") or "").startswith("wait_")
        or str(lifecycle.get("status") or "") in {"pending", "waiting"}
    )


def aggregate_lane_results(request: Request) -> Result:
    """Join terminal intake, dispatch, triage, and lifecycle lane receipts."""
    lanes = {lane: _lane_result(request, lane) for lane in _LANE_TAILS}
    triage_receipts = _triage_receipts(request)
    triage_failures = [
        {
            "lane": "triage",
            "reason": str(receipt.get("reason") or receipt.get("code") or receipt.get("status") or "lane_failed"),
            "failure_class": str(receipt.get("failure_class") or "terminal"),
            "atom": name,
        }
        for name, receipt in triage_receipts
        if not _expected_noop(receipt) and _terminal_failure("triage", receipt) is not None
    ]
    failures = [failure for lane, receipt in lanes.items() if (failure := _terminal_failure(lane, receipt)) is not None]
    failures.extend(triage_failures)
    triage_worked = _triage_worked(triage_receipts)
    worked = triage_worked or _legacy_lane_worked(lanes)
    pending = _pending_lane_work(lanes)
    result: Result = {
        "status": "failed" if failures else "aggregated",
        "ok": not failures,
        "mutated": False,
        "worked": worked,
        "idle": not failures and not worked and not pending,
        "lanes": lanes,
        "terminal_failures": failures,
        "cleanup_authorized": False,
    }
    if failures:
        result.update({"reason": "lane_failed", "failure_class": "terminal", "retry_safe": False})
        return result
    identity = _verified_cleanup_identity(request, lanes)
    if identity is not None:
        result["cleanup_authorized"] = True
        result["cleanup_identity"] = identity
    return result
