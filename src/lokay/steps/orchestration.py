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
    status = str(receipt.get("status") or "")
    if receipt.get("ok") is False or status in _TERMINAL:
        return {
            "lane": lane,
            "reason": str(receipt.get("reason") or status or "lane_failed"),
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
        if isinstance(payload, Mapping) and isinstance(payload.get("provenance"), Mapping):
            repair.append({**candidate, **payload, **payload["provenance"]})
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
    remote_oid = str(source.get("remote_oid") or source.get("after_oid") or "").strip()
    target_branch = str(source.get("target_branch") or branch).strip()
    if not (repo and issue > 0 and pr > 0 and branch and receipt and remote_oid and target_branch == branch):
        return None
    return {"repo": repo, "issue": issue, "pr_number": pr, "branch": branch,
            "local_branch": local_branch, "worktree_path": path, "receipt": receipt,
            "remote_oid": remote_oid, "target_branch": target_branch,
            "task": str(source.get("task_id") or source.get("task") or "").strip()}


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
    if evidence is None:
        for candidate in lifecycle_evidence:
            if str(candidate.get("status") or "") == "decided" and str(candidate.get("outcome") or "") in {"finalize_merged", "finalize_closed"}:
                identity = candidate.get("identity")
                if isinstance(identity, Mapping):
                    evidence = candidate
                    provenance = identity
                    break
    if evidence is None or not isinstance(provenance, Mapping):
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

    required = ("repo", "issue", "pr_number", "branch", "head_oid")
    if any(key not in identity or identity[key] in (None, "") for key in required):
        return None
    # Verified provenance and lifecycle identity must agree with the exposed
    # identity; never authorize cleanup from a mismatched receipt.
    if provenance.get("repo") not in (None, identity["repo"]):
        return None
    if provenance.get("number") not in (None, identity["pr_number"]):
        return None
    if provenance.get("head_oid") not in (None, identity["head_oid"]):
        return None
    if provenance.get("head_ref") not in (None, identity["branch"]):
        return None
    repair_identity = _repair_cleanup_identity(request, tuple(repair_candidates))
    if repair_identity is not None:
        for key in ("board", "clone_path", "priority"):
            value = _identity_value(tuple(lanes.values()), key)
            if value not in (None, ""):
                repair_identity[key] = value
        # A repair receipt identifies owned local state, but only terminal merge/lifecycle
        # evidence authorizes deleting it.
        if identity is not None and all(str(identity.get(key) or "") == str(repair_identity.get(key) or "") for key in ("repo", "issue", "pr_number", "branch")) and str(identity.get("head_oid") or "") == str(repair_identity.get("remote_oid") or ""):
            repair_identity["head_oid"] = identity["head_oid"]
            return repair_identity
    return identity


def aggregate_lane_results(request: Request) -> Result:
    """Join terminal intake, dispatch, triage, and lifecycle lane receipts."""
    lanes = {lane: _lane_result(request, lane) for lane in _LANE_TAILS}
    failures = [failure for lane, receipt in lanes.items() if (failure := _terminal_failure(lane, receipt)) is not None]
    result: Result = {
        "status": "failed" if failures else "aggregated",
        "ok": not failures,
        "mutated": False,
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
