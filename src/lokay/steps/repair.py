"""Mega-atomic effectors: PR repair domain."""

from __future__ import annotations
import hashlib
import re
import json
import os
import sqlite3
import subprocess
from lokay.steps.claim import claim_directory_lock
from lokay.adapters_cli import CommandError, hermes_kanban_json, run_cmd
from lokay.config import MAX_EXECUTOR_TIMEOUT_SECONDS
from lokay.envelope import (
    Request,
    Result,
    cfg_of,
    cond_blob,
    dry_run_flag,
    fail,
    input_of,
    noop,
    ok,
    planned,
    upstream_noop,
)


def _repair_decision_gate(request: Request) -> Result | None:
    """No-op unless decide_triage_action selected repair."""
    decide = cond_blob(request, "decide_triage_action", "decide", "triage_decide_triage_action")
    if not decide and "action" not in input_of(request):
        return None
    if decide.get("ok") is False or str(decide.get("status") or "") in {
        "failed",
        "cancelled",
        "timed_out",
    }:
        return fail(
            "upstream_failed",
            failure_class="terminal",
            retry_safe=False,
            upstream=decide,
            worked=False,
        )
    if decide.get("status") == "noop":
        return noop(
            str(decide.get("reason") or "no_selected_pr"),
            action=decide.get("action"),
            worked=False,
        )
    action = str(input_of(request).get("action") or decide.get("action") or "")
    if action == "repair":
        return None
    if not action or action == "skip":
        return noop(
            str(decide.get("reason") or "not_selected"),
            action=action or "skip",
            worked=False,
        )
    return noop(
        "not_selected",
        action=action,
        expected=["repair"],
        decide_reason=decide.get("reason"),
        worked=False,
    )


def _repair_value(request: Request, key: str, *aliases: str) -> object:
    """Resolve explicit request input, then effector config, then conduction."""
    data = input_of(request)
    cfg = cfg_of(request)
    for source in (data, cfg):
        for name in (key, *aliases):
            if name in source:
                return source[name]
    for blob in (cond_blob(request, "decide_repair_attempt", "repair_attempt", "verify_repair_head", "evaluate_checks", "checks"),):
        for name in (key, *aliases):
            if name in blob:
                return blob[name]
    return None


def _repair_identity(request: Request) -> tuple[dict[str, object] | None, str | None]:
    data = input_of(request)
    loaded = cond_blob(request, "load_pr_fields", "triage_load_pr_fields")
    pr = data.get("pr") or loaded.get("pr") or {}
    if not isinstance(pr, dict):
        return None, "invalid_pr"
    repo = str(_repair_value(request, "repo") or pr.get("repo") or loaded.get("repo") or "").strip()
    number_value = _repair_value(request, "pr_number", "number")
    number_value = number_value or pr.get("number") or loaded.get("number")
    remote = cond_blob(request, "read_repair_remote_head", "triage_read_repair_remote_head")
    head = str(_repair_value(request, "verified_head", "head_sha", "head_oid", "headRefOid") or remote.get("remote_oid") or pr.get("verified_head") or pr.get("headSha") or pr.get("headRefOid") or "").strip()
    candidate = str(_repair_value(request, "candidate", "candidate_id", "candidate_sha") or "").strip()
    run_id = str(_repair_value(request, "run_id") or "").strip()
    try:
        number = int(number_value)
    except (TypeError, ValueError):
        number = 0
    if not repo or number <= 0 or not head or not candidate or not run_id:
        return None, "missing_repair_provenance"
    return {"repo": repo, "pr_number": number, "verified_head": head, "candidate": candidate, "run_id": run_id}, None


def _repair_checks(request: Request) -> tuple[list[dict[str, str]] | None, str | None, bool]:
    data = input_of(request)
    checks = data.get("checks")
    if checks is None:
        checks = cond_blob(request, "evaluate_checks", "triage_evaluate_checks", "checks").get("checks")
    if checks is None:
        pr = data.get("pr") or cond_blob(request, "load_pr_fields", "triage_load_pr_fields").get("pr") or {}
        checks = pr.get("statusCheckRollup") if isinstance(pr, dict) else None
    if not isinstance(checks, list) or not checks:
        return None, "missing_check_evidence", False
    normalized: list[dict[str, str]] = []
    pending = False
    for item in checks:
        if not isinstance(item, dict):
            return None, "malformed_check_evidence", False
        identity = str(item.get("id") or item.get("name") or item.get("context") or "").strip()
        conclusion = str(item.get("conclusion") or item.get("state") or "").strip().upper()
        if not identity or not conclusion:
            return None, "malformed_check_evidence", False
        if conclusion in {"PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", "WAITING"}:
            pending = True
        elif conclusion not in {"SUCCESS", "NEUTRAL", "SKIPPED", "FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}:
            return None, "malformed_check_evidence", False
        normalized.append({"identity": identity, "conclusion": conclusion})
    return normalized, None, pending
def _repair_state(request: Request) -> dict[str, object] | None:
    data = input_of(request)
    sources: list[object] = [data.get("attempt_state"), data.get("repair_attempt")]
    for blob in (
        cond_blob(request, "read_repair_attempt_state", "triage_read_repair_attempt_state", "read_repair_attempt", "repair_attempt_state"),
        cond_blob(request, "decide_repair_attempt"),
    ):
        sources.extend((blob.get("attempt_state"), blob.get("repair_attempt")))
    for source in sources:
        if isinstance(source, dict):
            return source
    return None

def _repair_state_root(request: Request) -> Path | None:
    data, cfg = input_of(request), cfg_of(request)
    root = data.get("repair_state_root") or cfg.get("repair_state_root") or data.get("repair_receipt_root") or cfg.get("repair_receipt_root")
    return Path(str(root)).expanduser() if root else None



def _repair_reservation_path(request: Request, identity: dict[str, object]) -> Path:
    root = _repair_state_root(request)
    if root is None:
        raise ValueError("missing repair state root")
    key = f"{identity['repo']}:{identity['pr_number']}:{identity['verified_head']}".encode()
    digest = hashlib.sha256(key).hexdigest()[:32]
    return root / "repair-attempts" / str(identity["repo"]).replace("/", "__") / str(identity["pr_number"]) / f"{digest}.json"


def _reservation_identity(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    required = ("repo", "pr_number", "verified_head", "candidate", "run_id")
    if any(not payload.get(key) for key in required):
        return None
    try:
        number = int(payload["pr_number"])
    except (TypeError, ValueError):
        return None
    identity = {key: payload[key] for key in required}
    identity["pr_number"] = number
    if "check_run_id" in payload:
        identity["check_run_id"] = payload["check_run_id"]
    return identity

def _repair_recovery_claim_path(reservation_path: Path, evidence_process_id: str) -> Path:
    digest = hashlib.sha256(evidence_process_id.encode()).hexdigest()[:32]
    return reservation_path.with_name(f"{reservation_path.stem}.recovery.{digest}.json")

def _repair_invoke_evidence_path(reservation_path: Path, process_id: str) -> Path:
    digest = hashlib.sha256(process_id.encode()).hexdigest()[:32]
    return reservation_path.with_name(f"{reservation_path.stem}.invoke.{digest}.json")
def _repair_invoke_terminal_evidence_path(reservation_path: Path, process_id: str) -> Path:
    started = _repair_invoke_evidence_path(reservation_path, process_id)
    return started.with_name(f"{started.stem}.terminal.json")



def _read_repair_invoke_evidence(reservation_path: Path, process_id: str) -> dict[str, object] | None | Result:
    started_path = _repair_invoke_evidence_path(reservation_path, process_id)
    terminal_path = _repair_invoke_terminal_evidence_path(reservation_path, process_id)
    try:
        started = json.loads(started_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if terminal_path.exists():
            return fail("repair_invoke_evidence_malformed", failure_class="terminal", retry_safe=False, operation="read_repair_invoke_evidence", invoke_evidence_path=str(terminal_path))
        return None
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("repair_invoke_evidence_read_failed", failure_class="terminal", retry_safe=False, operation="read_repair_invoke_evidence", error=str(exc), invoke_evidence_path=str(started_path))
    if (
        not isinstance(started, dict)
        or started.get("kind") != "repair_invoke_evidence"
        or started.get("process_id") != process_id
        or started.get("status") != "started"
        or not isinstance(started.get("pre_head"), str)
        or not isinstance(started.get("pre_status"), str)
        or started.get("mutated") is not None
    ):
        return fail("repair_invoke_evidence_malformed", failure_class="terminal", retry_safe=False, operation="read_repair_invoke_evidence", invoke_evidence_path=str(started_path))
    try:
        terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {**started, "status": "unknown"}
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("repair_invoke_evidence_read_failed", failure_class="terminal", retry_safe=False, operation="read_repair_invoke_evidence", error=str(exc), invoke_evidence_path=str(terminal_path))
    if (
        not isinstance(terminal, dict)
        or terminal.get("kind") != "repair_invoke_evidence"
        or terminal.get("process_id") != process_id
        or terminal.get("status") not in {"failed", "succeeded", "timed_out"}
        or type(terminal.get("mutated")) is not bool
        or not isinstance(terminal.get("post_head"), str)
        or not isinstance(terminal.get("post_status"), str)
        or any(terminal.get(key) != started.get(key) for key in ("kind", "process_id", "pre_head", "pre_status"))
        or (terminal.get("mutated") is False and any(terminal.get(post) != terminal.get(pre) for pre, post in (("pre_head", "post_head"), ("pre_status", "post_status"))))
        or (terminal.get("status") == "failed" and not isinstance(terminal.get("error"), str))
    ):
        return fail("repair_invoke_evidence_malformed", failure_class="terminal", retry_safe=False, operation="read_repair_invoke_evidence", invoke_evidence_path=str(terminal_path))
    return terminal

def _write_invoke_evidence(path: Path, payload: dict[str, object], exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path
    if not exclusive:
        try:
            started = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OSError(f"cannot read started invoke evidence: {exc}") from exc
        if (
            not isinstance(started, dict)
            or started.get("status") != "started"
            or started.get("mutated") is not None
            or any(started.get(key) != payload.get(key) for key in ("kind", "process_id", "pre_head", "pre_status"))
        ):
            raise OSError("invoke evidence transition mismatch")
        target = path.with_name(f"{path.stem}.terminal.json")
    import tempfile
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile("w", dir=str(target.parent), delete=False, encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temp_name = handle.name
        os.link(temp_name, target)
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
    parent_fd = os.open(str(target.parent), os.O_RDONLY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)

def _repair_recovery_continuation_path(reservation_path: Path, predecessor_sha256: str) -> Path:
    digest = str(predecessor_sha256 or "").strip()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError("invalid predecessor_sha256")
    return reservation_path.with_name(f"{reservation_path.stem}.continuation.{digest}.json")


def _repair_recovery_transition_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _load_repair_recovery_continuation_chain(
    reservation_path: Path,
    claim: dict[str, object],
    recovery_claim_path: str,
) -> tuple[str, str, str, str, str] | Result:
    """Return (predecessor_hash, predecessor_kind, latest_run_id, latest_candidate, latest_path) or fail Result."""
    claim_hash = _repair_recovery_transition_hash(claim)
    try:
        candidates = sorted(reservation_path.parent.glob(f"{reservation_path.stem}.continuation.*.json"))
        if len(candidates) > 64:
            return fail("repair_recovery_continuation_chain_too_long", failure_class="terminal", retry_safe=False, operation="read_repair_recovery_continuation_evidence", count=len(candidates))
        links = [(path, json.loads(path.read_text(encoding="utf-8"))) for path in candidates]
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("repair_recovery_continuation_chain_read_failed", failure_class="terminal", retry_safe=False, operation="read_repair_recovery_continuation_evidence", error=str(exc))
    by_predecessor: dict[str, list[tuple[Path, dict[str, object]]]] = {}
    for path, link in links:
        if not isinstance(link, dict):
            return fail("repair_recovery_continuation_chain_malformed", failure_class="terminal", retry_safe=False, operation="read_repair_recovery_continuation_evidence", continuation_path=str(path))
        try:
            expected_path = _repair_recovery_continuation_path(reservation_path, str(link.get("predecessor_sha256") or ""))
        except ValueError:
            return fail("repair_recovery_continuation_chain_malformed", failure_class="terminal", retry_safe=False, operation="read_repair_recovery_continuation_evidence", continuation_path=str(path))
        valid_link = bool(
            link.get("kind") == "repair_attempt_recovery_continuation"
            and str(link.get("repo") or "") == str(claim.get("repo") or "")
            and str(link.get("pr_number") or "") == str(claim.get("pr_number") or "")
            and str(link.get("verified_head") or "") == str(claim.get("verified_head") or "")
            and str(link.get("recovery_claim_path") or "") == recovery_claim_path
            and str(link.get("predecessor_sha256") or "")
            and path == expected_path
            and str(link.get("prior_recovery_run_id") or "")
            and str(link.get("prior_recovery_candidate") or "")
            and str(link.get("continuation_run_id") or "")
            and str(link.get("continuation_candidate") or "")
        )
        if not valid_link:
            return fail("repair_recovery_continuation_chain_malformed", failure_class="terminal", retry_safe=False, operation="read_repair_recovery_continuation_evidence", continuation_path=str(path))
        by_predecessor.setdefault(str(link["predecessor_sha256"]), []).append((path, link))
    predecessor_hash = claim_hash
    predecessor_kind = "repair_attempt_recovery_claim"
    latest_run_id = str(claim.get("recovery_run_id") or "")
    latest_candidate = str(claim.get("recovery_candidate") or "")
    visited: set[Path] = set()
    latest_path = recovery_claim_path
    while predecessor_hash in by_predecessor:
        successors = by_predecessor[predecessor_hash]
        if len(successors) != 1:
            return fail("repair_recovery_continuation_fork", failure_class="terminal", retry_safe=False, operation="read_repair_recovery_continuation_evidence", count=len(successors))
        path, link = successors[0]
        if path in visited or str(link.get("predecessor_kind") or "") != predecessor_kind or str(link.get("prior_recovery_run_id") or "") != latest_run_id or str(link.get("prior_recovery_candidate") or "") != latest_candidate:
            return fail("repair_recovery_continuation_chain_mismatch", failure_class="terminal", retry_safe=False, operation="read_repair_recovery_continuation_evidence", continuation_path=str(path))
        visited.add(path)
        predecessor_hash = _repair_recovery_transition_hash(link)
        predecessor_kind = "repair_attempt_recovery_continuation"
        latest_run_id = str(link["continuation_run_id"])
        latest_candidate = str(link["continuation_candidate"])
        latest_path = str(path)
    if len(visited) != len(links):
        return fail("repair_recovery_continuation_orphan", failure_class="terminal", retry_safe=False, operation="read_repair_recovery_continuation_evidence", count=len(links) - len(visited))
    return predecessor_hash, predecessor_kind, latest_run_id, latest_candidate, latest_path



def _write_exclusive_json(path: Path, payload: dict[str, object]) -> bool:
    """Publish one durable JSON claim; return false when it already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    parent_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return True

def _repair_completed_receipt_path(request: Request, identity: dict[str, object]) -> Path | None:
    """Return the deterministic completed-receipt path for repo/PR/before-head."""
    root = _repair_state_root(request)
    if root is None:
        return None
    repo = str(identity.get("repo") or "")
    pr_number = identity.get("pr_number")
    verified_head = str(identity.get("verified_head") or "")
    if not repo or pr_number in (None, "") or not verified_head:
        return None
    key = f"{repo}:{pr_number}:{verified_head}".encode()
    digest = hashlib.sha256(key).hexdigest()[:32]
    return root / "repair-receipts" / repo.replace("/", "__") / str(pr_number) / f"{digest}.json"


def _repair_completed_receipt(
    request: Request,
    identity: dict[str, object],
) -> tuple[str, dict[str, object]] | None | Result:
    """Return found receipt, None when absent, or a fail Result for malformed evidence."""
    path = _repair_completed_receipt_path(request, identity)
    if path is None:
        return fail(
            "missing_repair_completed_receipt_path",
            failure_class="terminal",
            retry_safe=False,
            operation="read_repair_completed_receipt",
            **{key: identity.get(key) for key in ("repo", "pr_number", "verified_head") if key in identity},
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except OSError as exc:
        return fail(
            "repair_completed_receipt_read_failed",
            failure_class="retryable_read",
            retry_safe=True,
            operation="read_repair_completed_receipt",
            error=str(exc),
            receipt_path=str(path),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail(
            "repair_completed_receipt_malformed",
            failure_class="terminal",
            retry_safe=False,
            operation="read_repair_completed_receipt",
            error=str(exc),
            receipt_path=str(path),
        )
    if not isinstance(payload, dict):
        return fail(
            "repair_completed_receipt_malformed",
            failure_class="terminal",
            retry_safe=False,
            operation="read_repair_completed_receipt",
            receipt_path=str(path),
        )
    provenance = payload.get("provenance")
    run = payload.get("run")
    valid = bool(
        isinstance(provenance, dict)
        and isinstance(run, dict)
        and str(provenance.get("repo") or "") == str(identity["repo"])
        and str(provenance.get("pr_number") or "") == str(identity["pr_number"])
        and str(payload.get("before_oid") or "") == str(identity["verified_head"])
        and str(run.get("status") or "") == "completed"
        and str(run.get("run_id") or "")
        and str(run.get("omp_process_id") or "")
        and str(run.get("receipt_process_id") or "")
        and str(payload.get("candidate") or "")
        and str(payload.get("after_oid") or "")
        and str(payload.get("after_oid")) != str(identity["verified_head"])
    )
    if not valid:
        return fail(
            "repair_completed_receipt_mismatch",
            failure_class="terminal",
            retry_safe=False,
            operation="read_repair_completed_receipt",
            receipt_path=str(path),
        )
    return str(path), payload

def _repair_restart_recovery(request: Request, state: dict[str, object], reservation_path: Path, current: dict[str, object] | None = None, reconciliation: dict[str, object] | None = None) -> Result:
    """Read and validate one exact failed read-only pre-OMP process row."""
    data, cfg = input_of(request), cfg_of(request)
    recovery = data.get("attempt_recovery") or cfg.get("attempt_recovery")
    if not recovery:
        return ok(status="inactive", operation="read_repair_attempt_recovery_evidence", recovery_active=False, mutated=False)
    if not isinstance(recovery, dict):
        return fail("invalid_repair_attempt_recovery", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_recovery_evidence")
    run_id = str(recovery.get("run_id") or "").strip()
    process_id = str(recovery.get("process_id") or "").strip()
    candidate = str(recovery.get("candidate") or "").strip()
    path_id = str(recovery.get("path_id") or "").strip()
    effector_id = str(recovery.get("effector_id") or "").strip()
    expected_process_id = f"{run_id}:{path_id}:{effector_id}"
    allowed = {
        ("auto_worker", "triage_verify_repair_attempt_reservation"),
        ("pr_triage", "verify_repair_attempt_reservation"),
    }
    if (
        (path_id, effector_id) not in allowed
        or not run_id
        or process_id != expected_process_id
        or run_id != str(state.get("run_id") or "")
        or candidate != str(state.get("candidate") or "")
        or str(recovery.get("repo") or "") != str(state.get("repo") or "")
        or str(recovery.get("pr_number") or "") != str(state.get("pr_number") or "")
        or str(recovery.get("verified_head") or "") != str(state.get("verified_head") or "")
    ):
        return fail("repair_attempt_recovery_mismatch", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_recovery_evidence")
    db_path = str(data.get("db_path") or cfg.get("db_path") or "").strip()
    if not db_path:
        return fail("invalid_repair_attempt_recovery", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_recovery_evidence")
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT run_id,id,status,input_json,output_json,error_json,metadata FROM processes WHERE run_id=? AND id=?",
                (run_id, process_id),
            ).fetchall()
        if len(rows) != 1:
            return fail("repair_attempt_recovery_not_unique", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_recovery_evidence", count=len(rows))
        row_run, row_id, status, raw_input, raw_output, raw_error, raw_metadata = rows[0]
        process_input = json.loads(raw_input)
        process_output = json.loads(raw_output)
        process_error = json.loads(raw_error)
        metadata = json.loads(raw_metadata)
    except (OSError, sqlite3.Error, TypeError, json.JSONDecodeError) as exc:
        return fail("repair_attempt_recovery_read_failed", failure_class="retryable_read", retry_safe=True, operation="read_repair_attempt_recovery_evidence", error=str(exc))
    conduction = process_input.get("conduction") if isinstance(process_input, dict) else None
    reserve_key = "triage_reserve_repair_attempt" if path_id == "auto_worker" else "reserve_repair_attempt"
    reservation = conduction.get(reserve_key) if isinstance(conduction, dict) else None
    reservation_values = reservation.get("reservation") if isinstance(reservation, dict) else None
    invoke_process_id = f"{run_id}:{path_id}:{'triage_invoke_repair_omp' if path_id == 'auto_worker' else 'invoke_repair_omp'}"
    invoke_evidence = _read_repair_invoke_evidence(reservation_path, invoke_process_id)
    invoke_no_mutation = bool(
        isinstance(invoke_evidence, dict)
        and invoke_evidence.get("ok", True)
        and invoke_evidence.get("status") == "failed"
        and invoke_evidence.get("mutated") is False
        and invoke_evidence.get("pre_head") == state.get("pre_head")
        and invoke_evidence.get("pre_status") == state.get("pre_status")
    )
    binding = metadata.get("__adapter_binding") if isinstance(metadata, dict) else None
    expected_cwd = (Path(db_path).resolve().parent.parent / "deployment" / "versions" / candidate / "source" / "project").resolve()
    valid = bool(
        row_run == run_id
        and row_id == process_id
        and status == "failed"
        and isinstance(process_output, dict)
        and not process_output
        and isinstance(process_error, dict)
        and process_error.get("code") == "adapter_failed"
        and invoke_no_mutation
        and isinstance(reservation, dict)
        and reservation.get("ok") is True
        and reservation.get("mutated") is True
        and str(reservation.get("reservation_path") or "") == str(reservation_path)
        and isinstance(reservation_values, dict)
        and _reservation_identity(reservation_values) == _reservation_identity(state)
        and str(process_input.get("candidate") or "") == candidate
        and str(process_input.get("candidate_id") or "") == candidate
        and isinstance(binding, dict)
        and Path(str(binding.get("cwd") or "")).resolve() == expected_cwd
    )
    if not valid:
        return fail("repair_attempt_recovery_mismatch", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_recovery_evidence")
    current = current or {}
    recovery_run_id = str(current.get("run_id") or input_of(request).get("run_id") or cfg_of(request).get("run_id") or "").strip()
    recovery_candidate = str(current.get("candidate") or input_of(request).get("candidate") or input_of(request).get("candidate_id") or cfg_of(request).get("candidate") or "").strip()
    if (
        not recovery_run_id
        or not recovery_candidate
        or str(current.get("repo") or state.get("repo") or "") != str(state.get("repo") or "")
        or str(current.get("pr_number") or state.get("pr_number") or "") != str(state.get("pr_number") or "")
        or str(current.get("verified_head") or state.get("verified_head") or "") != str(state.get("verified_head") or "")
    ):
        return fail("terminal_conflict", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_recovery_evidence", conflict="missing_repair_provenance")
    if reconciliation is None:
        reconciliation_blob = cond_blob(request, "read_repair_attempt_reconciliation")
        if reconciliation_blob.get("ok") is not True or reconciliation_blob.get("status") != "unchanged" or reconciliation_blob.get("authorize_reinvoke") is not True or not isinstance(reconciliation_blob.get("snapshot"), dict):
            return fail("repair_attempt_reconciliation_required", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_recovery_evidence")
        reconciliation = reconciliation_blob["snapshot"]
    claim = {
        "kind": "repair_attempt_recovery_claim",
        "repo": state["repo"],
        "pr_number": state["pr_number"],
        "verified_head": state["verified_head"],
        "reservation_run_id": state["run_id"],
        "reservation_candidate": state["candidate"],
        "evidence_process_id": process_id,
        "recovery_run_id": recovery_run_id,
        "recovery_candidate": recovery_candidate,
        "reconciliation": reconciliation,
    }
    return ok(status="validated", operation="read_repair_attempt_recovery_evidence", recovery_claim=claim, recovery_claim_path=str(_repair_recovery_claim_path(reservation_path, process_id)), reservation_path=str(reservation_path), mutated=False)


def read_repair_attempt_recovery_evidence(request: Request) -> Result:
    """Validate configured recovery against the immutable reservation and process journal."""
    gated = _repair_decision_gate(request)
    if gated is not None:
        return gated
    upstream = _repair_upstream(request, "read_repair_attempt_recovery_evidence", "read_repair_attempt_state", "read_repair_attempt_reconciliation")
    if upstream:
        return upstream
    source = cond_blob(request, "read_repair_attempt_state")
    state = source.get("attempt_state")
    path = str(source.get("reservation_path") or "")
    if source.get("status") == "absent":
        return ok(status="inactive", operation="read_repair_attempt_recovery_evidence", recovery_active=False, mutated=False)
    if source.get("ok") is not True or not isinstance(state, dict) or not path:
        return fail("repair_attempt_state_required", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_recovery_evidence")
    current = {
        "run_id": str(source.get("run_id") or input_of(request).get("run_id") or cfg_of(request).get("run_id") or "").strip(),
        "candidate": str(source.get("candidate") or input_of(request).get("candidate") or input_of(request).get("candidate_id") or cfg_of(request).get("candidate") or "").strip(),
        "repo": str(source.get("repo") or state.get("repo") or "").strip(),
        "pr_number": source.get("pr_number") if source.get("pr_number") not in (None, "") else state.get("pr_number"),
        "verified_head": str(source.get("verified_head") or state.get("verified_head") or "").strip(),
    }
    return _repair_restart_recovery(request, state, Path(path), current)

def claim_repair_attempt_recovery(request: Request) -> Result:
    """Publish the one immutable recovery transition."""
    gated = _repair_decision_gate(request)
    if gated is not None:
        return gated
    upstream = _repair_upstream(request, "claim_repair_attempt_recovery", "read_repair_attempt_recovery_evidence")
    if upstream:
        return upstream
    evidence = cond_blob(request, "read_repair_attempt_recovery_evidence")
    if evidence.get("status") == "inactive":
        return ok(status="inactive", operation="claim_repair_attempt_recovery", recovery_active=False, mutated=False)
    if evidence.get("ok") is not True or evidence.get("status") != "validated":
        return fail("repair_attempt_recovery_evidence_required", failure_class="terminal", retry_safe=False, operation="claim_repair_attempt_recovery")
    claim = evidence.get("recovery_claim")
    path = str(evidence.get("recovery_claim_path") or "")
    if not isinstance(claim, dict) or not path:
        return fail("invalid_repair_attempt_recovery_claim", failure_class="terminal", retry_safe=False, operation="claim_repair_attempt_recovery")
    if dry_run_flag(request):
        return planned(operation="claim_repair_attempt_recovery", recovery_claim=claim, recovery_claim_path=path)
    try:
        if not _write_exclusive_json(Path(path), claim):
            return ok(status="exists", operation="claim_repair_attempt_recovery", recovery_claim=claim, recovery_claim_path=path, mutated=False)
    except (OSError, TypeError, ValueError) as exc:
        return fail("repair_attempt_recovery_claim_failed", failure_class="terminal", retry_safe=False, operation="claim_repair_attempt_recovery", error=str(exc), recovery_claim_path=path, mutated=True)
    return ok(status="claimed", operation="claim_repair_attempt_recovery", recovery_claim=claim, recovery_claim_path=path, mutated=True)


def verify_repair_attempt_recovery(request: Request) -> Result:
    """Read back the immutable recovery transition before authorization."""
    gated = _repair_decision_gate(request)
    if gated is not None:
        return gated
    upstream = _repair_upstream(request, "verify_repair_attempt_recovery", "claim_repair_attempt_recovery")
    if upstream:
        return upstream
    claimed = cond_blob(request, "claim_repair_attempt_recovery")
    if claimed.get("status") == "inactive":
        return ok(status="inactive", operation="verify_repair_attempt_recovery", recovery_active=False, mutated=False)
    expected = claimed.get("recovery_claim")
    path = str(claimed.get("recovery_claim_path") or "")
    if claimed.get("ok") is not True or claimed.get("status") not in {"claimed", "exists", "planned"} or not isinstance(expected, dict) or not path:
        return fail("repair_attempt_recovery_claim_required", failure_class="terminal", retry_safe=False, operation="verify_repair_attempt_recovery")
    if dry_run_flag(request):
        return planned(operation="verify_repair_attempt_recovery", recovery_claim=expected, recovery_claim_path=path)
    try:
        actual = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("repair_attempt_recovery_claim_readback_failed", failure_class="terminal", retry_safe=False, operation="verify_repair_attempt_recovery", error=str(exc), recovery_claim_path=path)
    if actual != expected:
        return fail("repair_attempt_recovery_claim_mismatch", failure_class="terminal", retry_safe=False, operation="verify_repair_attempt_recovery", recovery_claim_path=path)
    return ok(status="verified", operation="verify_repair_attempt_recovery", recovery_verified=True, recovery_claim=actual, recovery_claim_path=path, mutated=False)


def read_repair_recovery_continuation_evidence(request: Request) -> Result:
    """Validate the unique chain from explicit historical no-mutation evidence."""
    gated = _repair_decision_gate(request)
    if gated is not None:
        return gated
    upstream = _repair_upstream(request, "read_repair_recovery_continuation_evidence", "verify_repair_attempt_recovery", "read_repair_attempt_state")
    if upstream:
        return upstream
    verified = cond_blob(request, "verify_repair_attempt_recovery")
    if verified.get("status") == "inactive":
        return ok(status="inactive", operation="read_repair_recovery_continuation_evidence", recovery_active=False, mutated=False)
    if verified.get("ok") is not True or verified.get("status") != "verified":
        return fail("repair_attempt_recovery_verification_required", failure_class="terminal", retry_safe=False, operation="read_repair_recovery_continuation_evidence")
    claim = verified.get("recovery_claim")
    data, cfg = input_of(request), cfg_of(request)
    current_run_id = str(data.get("run_id") or cfg.get("run_id") or "").strip()
    current_candidate = str(data.get("candidate") or data.get("candidate_id") or cfg.get("candidate") or "").strip()
    db_path = str(data.get("db_path") or cfg.get("db_path") or "").strip()
    reservation_path_value = str(cond_blob(request, "read_repair_attempt_state").get("reservation_path") or verified.get("reservation_path") or "")
    recovery_claim_path = str(verified.get("recovery_claim_path") or "")
    if not isinstance(claim, dict) or not current_run_id or not current_candidate or not db_path or not reservation_path_value or not recovery_claim_path:
        return fail("invalid_repair_recovery_continuation", failure_class="terminal", retry_safe=False, operation="read_repair_recovery_continuation_evidence")
    reservation_path = Path(reservation_path_value)
    loaded = _load_repair_recovery_continuation_chain(reservation_path, claim, recovery_claim_path)
    if isinstance(loaded, dict):
        return loaded
    predecessor_hash, predecessor_kind, latest_run_id, latest_candidate, latest_path = loaded
    if current_run_id == latest_run_id and current_candidate == latest_candidate:
        return ok(status="original", operation="read_repair_recovery_continuation_evidence", continuation_required=False, continuation_verified=True, recovery_claim=claim, latest_transition_path=latest_path, mutated=False)
    path_id = str(data.get("path_id") or cfg.get("path_id") or "")
    invoke_id = "triage_invoke_repair_omp" if path_id == "auto_worker" else "invoke_repair_omp"
    push_id = "triage_push_repair_branch" if path_id == "auto_worker" else "push_repair_branch"
    if path_id not in {"auto_worker", "pr_triage"}:
        return fail("invalid_repair_recovery_continuation", failure_class="terminal", retry_safe=False, operation="read_repair_recovery_continuation_evidence")
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT id,status,output_json,error_json FROM processes WHERE run_id=? AND id IN (?,?) ORDER BY id",
                (latest_run_id, f"{latest_run_id}:{path_id}:{invoke_id}", f"{latest_run_id}:{path_id}:{push_id}"),
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        return fail("repair_recovery_continuation_read_failed", failure_class="retryable_read", retry_safe=True, operation="read_repair_recovery_continuation_evidence", error=str(exc))
    if len(rows) != 2:
        return fail("repair_recovery_continuation_not_unique", failure_class="terminal", retry_safe=False, operation="read_repair_recovery_continuation_evidence", count=len(rows))
    observed: dict[str, tuple[str, dict[str, object], dict[str, object]]] = {}
    try:
        for process_id, status, raw_output, raw_error in rows:
            observed[str(process_id).rsplit(":", 1)[-1]] = (str(status), json.loads(raw_output), json.loads(raw_error))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("repair_recovery_continuation_malformed", failure_class="terminal", retry_safe=False, operation="read_repair_recovery_continuation_evidence", error=str(exc))
    invoke_status, invoke_output, invoke_error = observed.get(invoke_id, ("", {}, {}))
    push_status, push_output, _ = observed.get(push_id, ("", {}, {}))
    push_values = push_output.get("values") if isinstance(push_output, dict) else None
    invoke_process_id = f"{latest_run_id}:{path_id}:{invoke_id}"
    invoke_evidence = _read_repair_invoke_evidence(reservation_path, invoke_process_id)
    reconciliation = claim.get("reconciliation") if isinstance(claim.get("reconciliation"), dict) else {}
    invoke_no_mutation = bool(
        isinstance(invoke_evidence, dict)
        and invoke_evidence.get("ok", True)
        and invoke_evidence.get("status") == "failed"
        and invoke_evidence.get("mutated") is False
        and invoke_evidence.get("pre_head") == reconciliation.get("pre_head")
        and invoke_evidence.get("pre_status") == reconciliation.get("pre_status")
        and invoke_evidence.get("post_head") == reconciliation.get("actual_head")
        and invoke_evidence.get("post_status") == reconciliation.get("actual_status")
        and reconciliation.get("actual_head") == reconciliation.get("pre_head")
        and reconciliation.get("actual_status") == reconciliation.get("pre_status")
    )
    if not (
        invoke_status == "failed"
        and not invoke_output
        and isinstance(invoke_error, dict)
        and invoke_error.get("code") == "adapter_failed"
        and invoke_no_mutation
        and push_status == "succeeded"
        and isinstance(push_values, dict)
        and push_values.get("ok") is True
        and push_values.get("status") == "noop"
        and push_values.get("mutated") is False
    ):
        return fail("repair_recovery_continuation_mutation_unknown", failure_class="terminal", retry_safe=False, operation="read_repair_recovery_continuation_evidence", conflict="journal_boundary_not_noop")
    continuation = {
        "kind": "repair_attempt_recovery_continuation",
        "repo": claim.get("repo"),
        "pr_number": claim.get("pr_number"),
        "verified_head": claim.get("verified_head"),
        "recovery_claim_path": recovery_claim_path,
        "predecessor_kind": predecessor_kind,
        "predecessor_sha256": predecessor_hash,
        "prior_recovery_run_id": latest_run_id,
        "prior_recovery_candidate": latest_candidate,
        "continuation_run_id": current_run_id,
        "continuation_candidate": current_candidate,
        "invoke_process_id": f"{latest_run_id}:{path_id}:{invoke_id}",
        "push_process_id": f"{latest_run_id}:{path_id}:{push_id}",
    }
    try:
        path = _repair_recovery_continuation_path(reservation_path, predecessor_hash)
    except ValueError as exc:
        return fail("invalid_repair_recovery_continuation", failure_class="terminal", retry_safe=False, operation="read_repair_recovery_continuation_evidence", error=str(exc))
    return ok(status="validated", operation="read_repair_recovery_continuation_evidence", continuation_required=True, continuation=continuation, continuation_path=str(path), recovery_claim=claim, reservation_path=str(reservation_path), mutated=False)

def claim_repair_recovery_continuation(request: Request) -> Result:
    gated = _repair_decision_gate(request)
    if gated is not None:
        return gated
    upstream = _repair_upstream(request, "claim_repair_recovery_continuation", "read_repair_recovery_continuation_evidence")
    if upstream:
        return upstream
    evidence = cond_blob(request, "read_repair_recovery_continuation_evidence")
    if evidence.get("status") in {"inactive", "original"}:
        return ok(status=str(evidence.get("status")), operation="claim_repair_recovery_continuation", continuation_required=False, recovery_claim=evidence.get("recovery_claim"), mutated=False)
    continuation = evidence.get("continuation")
    path = str(evidence.get("continuation_path") or "")
    reservation_path_value = str(evidence.get("reservation_path") or cond_blob(request, "read_repair_attempt_state").get("reservation_path") or "")
    if evidence.get("ok") is not True or evidence.get("status") != "validated" or not isinstance(continuation, dict) or not path or not reservation_path_value:
        return fail("repair_recovery_continuation_evidence_required", failure_class="terminal", retry_safe=False, operation="claim_repair_recovery_continuation")
    predecessor_hash = str(continuation.get("predecessor_sha256") or "")
    try:
        expected_path = _repair_recovery_continuation_path(Path(reservation_path_value), predecessor_hash)
    except ValueError as exc:
        return fail("invalid_repair_recovery_continuation", failure_class="terminal", retry_safe=False, operation="claim_repair_recovery_continuation", error=str(exc))
    if Path(path) != expected_path:
        return fail("repair_recovery_continuation_path_mismatch", failure_class="terminal", retry_safe=False, operation="claim_repair_recovery_continuation", continuation_path=path)
    if dry_run_flag(request):
        return planned(operation="claim_repair_recovery_continuation", continuation=continuation, continuation_path=path)
    try:
        created = _write_exclusive_json(Path(path), continuation)
        if not created:
            existing = json.loads(Path(path).read_text(encoding="utf-8"))
            if existing != continuation:
                return fail("repair_recovery_continuation_conflict", failure_class="terminal", retry_safe=False, operation="claim_repair_recovery_continuation", continuation_path=path)
            return ok(status="exists", operation="claim_repair_recovery_continuation", continuation=continuation, continuation_path=path, reservation_path=reservation_path_value, mutated=False)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("repair_recovery_continuation_claim_failed", failure_class="terminal", retry_safe=False, operation="claim_repair_recovery_continuation", error=str(exc), continuation_path=path)
    return ok(status="claimed", operation="claim_repair_recovery_continuation", continuation=continuation, continuation_path=path, reservation_path=reservation_path_value, mutated=True)


def verify_repair_recovery_continuation(request: Request) -> Result:
    gated = _repair_decision_gate(request)
    if gated is not None:
        return gated
    upstream = _repair_upstream(request, "verify_repair_recovery_continuation", "claim_repair_recovery_continuation", "verify_repair_attempt_recovery", "read_repair_attempt_state")
    if upstream:
        return upstream
    claimed = cond_blob(request, "claim_repair_recovery_continuation")
    if claimed.get("status") in {"inactive", "original"}:
        return ok(status=str(claimed.get("status")), operation="verify_repair_recovery_continuation", continuation_verified=claimed.get("status") == "original", recovery_claim=claimed.get("recovery_claim"), mutated=False)
    expected = claimed.get("continuation")
    path = str(claimed.get("continuation_path") or "")
    if claimed.get("ok") is not True or claimed.get("status") not in {"claimed", "exists", "planned"} or not isinstance(expected, dict) or not path:
        return fail("repair_recovery_continuation_claim_required", failure_class="terminal", retry_safe=False, operation="verify_repair_recovery_continuation")
    if dry_run_flag(request):
        return planned(operation="verify_repair_recovery_continuation", continuation=expected, continuation_path=path)
    recovery = cond_blob(request, "verify_repair_attempt_recovery")
    claim = recovery.get("recovery_claim")
    recovery_claim_path = str(recovery.get("recovery_claim_path") or "")
    reservation_path_value = str(claimed.get("reservation_path") or cond_blob(request, "read_repair_attempt_state").get("reservation_path") or "")
    if not isinstance(claim, dict) or not recovery_claim_path or not reservation_path_value:
        return fail("repair_recovery_continuation_claim_required", failure_class="terminal", retry_safe=False, operation="verify_repair_recovery_continuation")
    try:
        actual = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("repair_recovery_continuation_readback_failed", failure_class="terminal", retry_safe=False, operation="verify_repair_recovery_continuation", error=str(exc), continuation_path=path)
    if actual != expected:
        return fail("repair_recovery_continuation_claim_mismatch", failure_class="terminal", retry_safe=False, operation="verify_repair_recovery_continuation", continuation_path=path)
    loaded = _load_repair_recovery_continuation_chain(Path(reservation_path_value), claim, recovery_claim_path)
    if isinstance(loaded, dict):
        return loaded
    predecessor_hash, _predecessor_kind, latest_run_id, latest_candidate, latest_path = loaded
    if (
        latest_path != path
        or latest_run_id != str(expected.get("continuation_run_id") or "")
        or latest_candidate != str(expected.get("continuation_candidate") or "")
        or predecessor_hash != _repair_recovery_transition_hash(expected)
    ):
        return fail("repair_recovery_continuation_not_head", failure_class="terminal", retry_safe=False, operation="verify_repair_recovery_continuation", continuation_path=path)
    return ok(status="verified", operation="verify_repair_recovery_continuation", continuation_verified=True, continuation=actual, continuation_path=path, recovery_claim=claim, mutated=False)



def read_repair_attempt_state(request: Request) -> Result:
    """Read one stable head-bound reservation; absence is safe and malformed is terminal."""
    gated = _repair_decision_gate(request)
    if gated is not None:
        return gated
    upstream = _repair_upstream(request, "read_repair_attempt_state", "evaluate_checks", "load_pr_fields", "read_repair_remote_head", "lifecycle_decide_lifecycle_transition")
    if upstream:
        return upstream
    identity, error = _repair_identity(request)
    if identity is None:
        return fail("terminal_conflict", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_state", conflict=error)
    root = _repair_state_root(request)
    if root is None:
        return fail("missing_repair_state_root", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_state", **identity)
    path = _repair_reservation_path(request, identity)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ok(status="absent", operation="read_repair_attempt_state", attempt_state=None, reservation_path=str(path), **identity)
    except OSError as exc:
        return fail("repair_attempt_state_read_failed", failure_class="retryable_read", retry_safe=True, operation="read_repair_attempt_state", error=str(exc), reservation_path=str(path), **identity)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("repair_attempt_state_malformed", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_state", error=str(exc), reservation_path=str(path), **identity)
    stored = _reservation_identity(payload)
    if stored is None or any(str(stored.get(key)) != str(identity.get(key)) for key in ("repo", "pr_number", "verified_head")):
        return fail("repair_attempt_state_mismatch", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_state", reservation_path=str(path), **identity)
    if str(payload.get("status") or "") not in {"reserved", "invoked", "completed", "failed"} or payload.get("attempted") is not True:
        return fail("repair_attempt_state_malformed", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_state", reservation_path=str(path), **identity)
    return ok(status="found", operation="read_repair_attempt_state", attempt_state=payload, reservation_path=str(path), **identity)


def read_repair_completed_receipt(request: Request) -> Result:
    """Read one durable completed repair receipt for the current head identity."""
    gated = _repair_decision_gate(request)
    if gated is not None:
        return gated
    upstream = _repair_upstream(request, "read_repair_completed_receipt", "load_pr_fields", "read_repair_remote_head", "lifecycle_decide_lifecycle_transition")
    if upstream:
        return upstream
    identity, error = _repair_identity(request)
    if identity is None:
        return fail("terminal_conflict", failure_class="terminal", retry_safe=False, operation="read_repair_completed_receipt", conflict=error)
    completed = _repair_completed_receipt(request, identity)
    if isinstance(completed, dict):
        return completed
    if completed is None:
        return ok(status="absent", operation="read_repair_completed_receipt", receipt=None, receipt_path=None, **identity)
    receipt_path, receipt = completed
    return ok(status="found", operation="read_repair_completed_receipt", receipt=receipt, receipt_path=receipt_path, **identity)

def read_repair_attempt_baseline(request: Request) -> Result:
    """Read the exact clean worktree baseline before immutable reservation."""
    upstream = _repair_upstream(request, "read_repair_attempt_baseline", "verify_repair_worktree")
    if upstream:
        return upstream
    verified = cond_blob(request, "verify_repair_worktree")
    if dry_run_flag(request) and verified.get("status") == "planned":
        return noop("dry_run", operation="read_repair_attempt_baseline")
    context = _repair_context(request)
    expected = str(verified.get("head") or "")
    if verified.get("ok") is not True or verified.get("status") != "verified" or not expected:
        return fail("repair_attempt_baseline_worktree_required", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_baseline")
    try:
        actual_head = rev_parse(context["worktree_path"])
        status = git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=context["worktree_path"])
    except (CommandError, OSError) as exc:
        return fail("repair_attempt_baseline_read_failed", failure_class="retryable_read", retry_safe=True, operation="read_repair_attempt_baseline", error=str(exc), mutated=False)
    if actual_head != expected or status:
        return fail("repair_attempt_baseline_mismatch", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_baseline", expected_head=expected, actual_head=actual_head, dirty=bool(status), mutated=False)
    return ok(status="read", operation="read_repair_attempt_baseline", baseline_verified=True, pre_head=actual_head, pre_status=status, **context, mutated=False)


def read_repair_attempt_reconciliation(request: Request) -> Result:
    """Classify an interrupted reservation from immutable baseline and live Git state."""
    gated = _repair_decision_gate(request)
    if gated is not None:
        return gated
    terminal = _atomic_terminal(request, "read_repair_attempt_reconciliation", "read_repair_attempt_state", "read_repair_completed_receipt", "read_repair_remote_head", "read_repair_worktree_inventory", "read_repair_branch_provenance")
    if terminal is not None:
        return terminal
    idle = upstream_noop(request, "read_repair_attempt_state", "read_repair_completed_receipt", "read_repair_remote_head", "read_repair_worktree_inventory", "read_repair_branch_provenance")
    if idle:
        return noop(str(idle.get("reason") or "repair_reconciliation_prerequisite_inactive"), operation="read_repair_attempt_reconciliation")
    state_read = cond_blob(request, "read_repair_attempt_state")
    completed = cond_blob(request, "read_repair_completed_receipt")
    if state_read.get("status") == "absent" or completed.get("status") == "found":
        return ok(status="inactive", operation="read_repair_attempt_reconciliation", reconciliation_required=False, mutated=False)
    state = state_read.get("attempt_state")
    if state_read.get("ok") is not True or state_read.get("status") != "found" or not isinstance(state, dict):
        return fail("repair_attempt_reconciliation_state_required", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_reconciliation")
    idle = upstream_noop(request, "read_repair_remote_head", "read_repair_worktree_inventory", "read_repair_branch_provenance")
    if idle:
        return noop(str(idle.get("reason") or "repair_reconciliation_prerequisite_inactive"), operation="read_repair_attempt_reconciliation")
    required_baseline = ("pre_head", "pre_status", "repo_branch", "local_branch", "worktree_path")
    if any(key not in state for key in required_baseline) or any(not isinstance(state.get(key), str) for key in required_baseline):
        return fail("repair_attempt_reconciliation_legacy_missing_baseline", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_reconciliation", mutated=False)
    context = _repair_context(request)
    data, cfg = input_of(request), cfg_of(request)
    recovery = data.get("attempt_recovery") or cfg.get("attempt_recovery")
    if isinstance(recovery, dict):
        reservation_path = str(state_read.get("reservation_path") or "").strip()
        run_id = str(recovery.get("run_id") or "").strip()
        path_id = str(recovery.get("path_id") or "").strip()
        if not reservation_path:
            return fail("repair_attempt_reconciliation_state_required", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_reconciliation")
        if path_id not in {"auto_worker", "pr_triage"} or not run_id:
            return fail("repair_attempt_reconciliation_mutation_unknown", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_reconciliation", conflict="invoke identity unknown")
        invoke_effector = "triage_invoke_repair_omp" if path_id == "auto_worker" else "invoke_repair_omp"
        invoke_process_id = f"{run_id}:{path_id}:{invoke_effector}"
        invoke_evidence = _read_repair_invoke_evidence(Path(reservation_path), invoke_process_id)
        if isinstance(invoke_evidence, dict) and not invoke_evidence.get("ok", True):
            return invoke_evidence
        if invoke_evidence is None:
            return fail("repair_attempt_reconciliation_mutation_unknown", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_reconciliation", conflict="invoke evidence absent")
        if invoke_evidence.get("mutated") is True:
            return fail("repair_attempt_reconciliation_mutated_blocked", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_reconciliation", conflict="explicit invoke mutated blocks recovery")
        if invoke_evidence.get("status") != "failed" or invoke_evidence.get("mutated") is not False:
            return fail("repair_attempt_reconciliation_mutation_unknown", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_reconciliation", conflict="invoke mutation unknown")
    remote = cond_blob(request, "read_repair_remote_head")
    inventory = cond_blob(request, "read_repair_worktree_inventory")
    provenance_read = cond_blob(request, "read_repair_branch_provenance")
    expected = {key: str(state.get(key) or "") for key in ("verified_head", "pre_head", "pre_status", "repo_branch", "local_branch", "worktree_path")}
    rows = inventory.get("worktrees") if isinstance(inventory.get("worktrees"), list) else []
    matching = [row for row in rows if isinstance(row, dict) and Path(str(row.get("path") or "")).resolve() == Path(context["worktree_path"]).resolve()]
    provenance = provenance_read.get("provenance") if isinstance(provenance_read.get("provenance"), dict) else {}
    valid = bool(
        completed.get("status") in {None, "", "absent"}
        and all(expected[key] or key == "pre_status" for key in expected)
        and expected["verified_head"] == expected["pre_head"]
        and expected["repo_branch"] == context["branch"]
        and expected["local_branch"] == context["local_branch"]
        and Path(expected["worktree_path"]).resolve() == Path(context["worktree_path"]).resolve()
        and remote.get("ok") is True
        and str(remote.get("remote_oid") or "") == expected["verified_head"]
        and provenance_read.get("ok") is True
        and provenance_read.get("exists") is True
        and str(provenance_read.get("branch_head") or "") == expected["verified_head"]
        and str(provenance.get("repo") or "") == str(state.get("repo") or "")
        and str(provenance.get("pr") or "") == str(state.get("pr_number") or "")
        and str(provenance.get("remote_oid") or "") == expected["verified_head"]
        and str(provenance.get("target_branch") or "") == expected["repo_branch"]
        and len(matching) == 1
        and str(matching[0].get("branch") or "") == expected["local_branch"]
    )
    if not valid:
        return fail("repair_attempt_reconciliation_mismatch", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_reconciliation", mutated=False)
    try:
        actual_head = rev_parse(context["worktree_path"])
        actual_status = git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=context["worktree_path"])
    except (CommandError, OSError) as exc:
        return fail("repair_attempt_reconciliation_read_failed", failure_class="retryable_read", retry_safe=True, operation="read_repair_attempt_reconciliation", error=str(exc), mutated=False)
    snapshot = {"pre_head": expected["pre_head"], "pre_status": expected["pre_status"], "actual_head": actual_head, "actual_status": actual_status, "remote_oid": str(remote.get("remote_oid") or ""), "worktree_path": expected["worktree_path"], "local_branch": expected["local_branch"], "repo_branch": expected["repo_branch"]}
    if actual_head == expected["pre_head"] and actual_status == expected["pre_status"]:
        return ok(status="unchanged", operation="read_repair_attempt_reconciliation", reconciliation_required=True, authorize_reinvoke=True, snapshot=snapshot, mutated=False)
    if actual_head != expected["pre_head"] and actual_status == "":
        return ok(status="committed", operation="read_repair_attempt_reconciliation", reconciliation_required=True, authorize_reinvoke=False, resume_postconditions=True, snapshot=snapshot, mutated=False)
    return fail("repair_attempt_reconciliation_dirty", failure_class="terminal", retry_safe=False, operation="read_repair_attempt_reconciliation", snapshot=snapshot, mutated=False)


def reserve_repair_attempt(request: Request) -> Result:
    """Atomically reserve the immutable head before invoking OMP."""
    peers = ("read_repair_attempt_baseline", "verify_repair_attempt_recovery", "verify_repair_recovery_continuation")
    gated = _repair_execution_gate(request, "reserve_repair_attempt", *peers)
    if gated:
        return gated
    upstream = _repair_upstream(request, "reserve_repair_attempt", *peers)
    if upstream:
        return upstream
    decision = cond_blob(request, "decide_repair_attempt")
    recovery = cond_blob(request, "verify_repair_attempt_recovery")
    if decision.get("reason") == "verified_failed_attempt_recovery":
        if recovery.get("ok") is not True or recovery.get("status") != "verified" or recovery.get("recovery_verified") is not True:
            return fail("repair_attempt_recovery_verification_required", failure_class="terminal", retry_safe=False, operation="reserve_repair_attempt")
        return ok(status="recovered", operation="reserve_repair_attempt", reservation_path=str(decision.get("reservation_path") or ""), recovery_claim=recovery.get("recovery_claim"), recovery_claim_path=recovery.get("recovery_claim_path"), mutated=False)
    if decision.get("authorize") is not True:
        return noop(str(decision.get("reason") or "repair_attempt_not_authorized"), operation="reserve_repair_attempt")
    identity, error = _repair_identity(request)
    if identity is None:
        return fail("terminal_conflict", failure_class="terminal", retry_safe=False, operation="reserve_repair_attempt", conflict=error)
    if _repair_state_root(request) is None:
        return fail("missing_repair_state_root", failure_class="terminal", retry_safe=False, operation="reserve_repair_attempt", **identity)
    path = _repair_reservation_path(request, identity)
    baseline = cond_blob(request, "read_repair_attempt_baseline")
    if baseline.get("ok") is not True or baseline.get("status") != "read" or baseline.get("baseline_verified") is not True:
        return fail("repair_attempt_baseline_required", failure_class="terminal", retry_safe=False, operation="reserve_repair_attempt")
    pre_head = str(baseline.get("pre_head") or "")
    pre_status = str(baseline.get("pre_status") or "")
    context = _repair_context(request)
    if pre_head != str(identity["verified_head"]) or any(str(baseline.get(key) or "") != context[key] for key in ("branch", "local_branch", "worktree_path")):
        return fail("repair_attempt_baseline_mismatch", failure_class="terminal", retry_safe=False, operation="reserve_repair_attempt", expected_head=identity["verified_head"], actual_head=pre_head)
    payload = {**identity, "checks": decision.get("checks") or [], "status": "reserved", "attempted": True, "kind": "repair_attempt_reservation", "pre_head": pre_head, "pre_status": pre_status, "repo_branch": str(baseline["branch"]), "local_branch": str(baseline["local_branch"]), "worktree_path": str(baseline["worktree_path"])}
    data = input_of(request)
    if data.get("check_run_id") or decision.get("check_run_id"):
        payload["check_run_id"] = data.get("check_run_id") or decision.get("check_run_id")
    if dry_run_flag(request):
        return planned(operation="reserve_repair_attempt", reservation_path=str(path), reservation=payload)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        parent_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        actual = json.loads(path.read_text(encoding="utf-8"))
        if actual != payload:
            raise ValueError("repair attempt reservation read-back mismatch")
    except FileExistsError:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return fail("repair_attempt_reservation_conflict", failure_class="terminal", retry_safe=False, operation="reserve_repair_attempt", error=str(exc), reservation_path=str(path))
        if existing == payload:
            return ok(status="exists", operation="reserve_repair_attempt", reservation=existing, reservation_path=str(path), mutated=False)
        return fail("repair_attempt_reservation_conflict", failure_class="terminal", retry_safe=False, operation="reserve_repair_attempt", reservation_path=str(path))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("repair_attempt_reservation_write_failed", failure_class="terminal", retry_safe=False, operation="reserve_repair_attempt", error=str(exc), reservation_path=str(path), mutated=True)
    return ok(status="reserved", operation="reserve_repair_attempt", reservation=payload, reservation_path=str(path), mutated=True)


def verify_repair_attempt_reservation(request: Request) -> Result:
    """Read the immutable reservation and bind it to a normal or recovered attempt."""
    gated = _repair_decision_gate(request)
    if gated is not None:
        return gated
    peers = ("reserve_repair_attempt", "verify_repair_attempt_recovery", "verify_repair_recovery_continuation")
    upstream = _repair_upstream(request, "verify_repair_attempt_reservation", *peers)
    if upstream:
        return upstream
    source = cond_blob(request, "reserve_repair_attempt")
    path = str(input_of(request).get("reservation_path") or source.get("reservation_path") or "")
    if not path:
        return fail("missing_repair_attempt_reservation", failure_class="terminal", retry_safe=False, operation="verify_repair_attempt_reservation")
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("repair_attempt_reservation_readback_failed", failure_class="terminal", retry_safe=False, operation="verify_repair_attempt_reservation", error=str(exc), reservation_path=path)
    stored = _reservation_identity(payload)
    if stored is None:
        return fail("repair_attempt_reservation_mismatch", failure_class="terminal", retry_safe=False, operation="verify_repair_attempt_reservation", reservation_path=path, conflict="invalid_reservation_identity")
    required_baseline = ("pre_head", "pre_status", "repo_branch", "local_branch", "worktree_path")
    if payload.get("status") != "reserved" or payload.get("attempted") is not True or any(key not in payload for key in required_baseline):
        return fail("repair_attempt_reservation_invalid", failure_class="terminal", retry_safe=False, operation="verify_repair_attempt_reservation", reservation_path=path)
    if source.get("status") == "recovered":
        recovery = cond_blob(request, "verify_repair_attempt_recovery")
        continuation = cond_blob(request, "verify_repair_recovery_continuation")
        claim = source.get("recovery_claim")
        current_run_id = str(input_of(request).get("run_id") or cfg_of(request).get("run_id") or "")
        current_candidate = str(input_of(request).get("candidate") or input_of(request).get("candidate_id") or cfg_of(request).get("candidate") or "")
        original = bool(isinstance(claim, dict) and str(claim.get("recovery_candidate") or "") == current_candidate and str(claim.get("recovery_run_id") or "") == current_run_id)
        continued = bool(continuation.get("ok") is True and continuation.get("status") in {"original", "verified"} and continuation.get("continuation_verified") is True)
        valid = bool(
            recovery.get("ok") is True
            and recovery.get("status") == "verified"
            and recovery.get("recovery_verified") is True
            and isinstance(claim, dict)
            and claim == recovery.get("recovery_claim")
            and str(claim.get("repo") or "") == str(stored["repo"])
            and str(claim.get("pr_number") or "") == str(stored["pr_number"])
            and str(claim.get("verified_head") or "") == str(stored["verified_head"])
            and str(claim.get("reservation_candidate") or "") == str(stored["candidate"])
            and str(claim.get("reservation_run_id") or "") == str(stored["run_id"])
            and current_candidate
            and current_run_id
            and (original or continued)
        )
        if not valid:
            return fail("repair_attempt_recovery_claim_mismatch", failure_class="terminal", retry_safe=False, operation="verify_repair_attempt_reservation", reservation_path=path)
        return ok(status="verified", operation="verify_repair_attempt_reservation", verified=True, recovered=True, reservation=payload, recovery_claim=claim, continuation=continuation.get("continuation"), reservation_path=path, mutated=False)
    identity, error = _repair_identity(request)
    if identity is None:
        return fail("repair_attempt_reservation_mismatch", failure_class="terminal", retry_safe=False, operation="verify_repair_attempt_reservation", reservation_path=path, conflict=error)
    if any(str(stored.get(key)) != str(identity.get(key)) for key in ("repo", "pr_number", "verified_head", "candidate", "run_id")):
        return fail("repair_attempt_reservation_mismatch", failure_class="terminal", retry_safe=False, operation="verify_repair_attempt_reservation", reservation_path=path, conflict=error)
    return ok(status="verified", operation="verify_repair_attempt_reservation", verified=True, reservation=payload, reservation_path=path, mutated=False)


def _repair_attempt_state(
    identity: dict[str, object], checks: list[dict[str, str]], *, status: str = "pending"
) -> dict[str, object]:
    """Build the immutable identity/check snapshot carried across retries."""
    return {**identity, "checks": [dict(item) for item in checks], "status": status, "attempted": True}


def decide_repair_attempt(request: Request) -> Result:
    """Pure authorization gate for exactly one OMP repair attempt."""
    gated = _repair_decision_gate(request)
    if gated is not None:
        return gated
    predecessors = ("read_repair_attempt_state", "read_repair_completed_receipt", "verify_repair_attempt_recovery", "verify_repair_recovery_continuation", "evaluate_checks", "load_pr_fields", "read_repair_remote_head", "lifecycle_decide_lifecycle_transition")
    lifecycle = _repair_lifecycle_gate(request, "decide_repair_attempt", *predecessors)
    if lifecycle is not None:
        return lifecycle
    checked_predecessors = tuple(peer for peer in predecessors if peer != "lifecycle_decide_lifecycle_transition")
    terminal = _atomic_terminal(request, "decide_repair_attempt", *checked_predecessors)
    if terminal is not None:
        return terminal
    idle = upstream_noop(request, *checked_predecessors)
    if idle:
        return noop(str(idle.get("reason") or "repair_attempt_prerequisite_inactive"), decision="wait", authorize=False)
    data = input_of(request)
    cfg = cfg_of(request)
    enabled = data.get("executor_enabled", data.get("enabled", cfg.get("executor_enabled", cfg.get("enabled", False))))
    live = data.get("live", cfg.get("live", False))
    dry = data.get("dry_run", cfg.get("dry_run", True))
    for key, value in (("executor_enabled", enabled), ("live", live), ("dry_run", dry)):
        if type(value) is not bool:
            return fail(
                "terminal_conflict",
                failure_class="terminal",
                retry_safe=False,
                decision="terminal_conflict",
                authorize=False,
                conflict=f"{key}_must_be_boolean",
            )
    if not enabled:
        return noop("executor_disabled", decision="wait", authorize=False)
    if not live:
        return noop("not_live", decision="wait", authorize=False)
    if dry:
        return noop("dry_run", decision="wait", authorize=False)
    identity, identity_error = _repair_identity(request)
    if identity is None:
        return fail("terminal_conflict", failure_class="terminal", retry_safe=False, decision="terminal_conflict", authorize=False, conflict=identity_error)
    checks, check_error, pending = _repair_checks(request)
    if checks is None:
        return fail("terminal_conflict", failure_class="terminal", retry_safe=False, decision="terminal_conflict", authorize=False, conflict=check_error, **identity)
    completed = cond_blob(request, "read_repair_completed_receipt")
    if completed and completed.get("ok") is not True:
        return fail(
            str(completed.get("reason") or "repair_completed_receipt_failed"),
            failure_class=str(completed.get("failure_class") or "terminal"),
            retry_safe=bool(completed.get("retry_safe")),
            decision="terminal_conflict",
            authorize=False,
            conflict=str(completed.get("reason") or "repair_completed_receipt_failed"),
            receipt_path=completed.get("receipt_path"),
            **identity,
        )
    if completed.get("status") not in {None, "", "absent", "found"}:
        return fail(
            "repair_completed_receipt_invalid",
            failure_class="terminal",
            retry_safe=False,
            decision="terminal_conflict",
            authorize=False,
            conflict="repair_completed_receipt_invalid",
            **identity,
        )
    if completed.get("ok") is True and completed.get("status") == "found" and isinstance(completed.get("receipt"), dict):
        receipt = completed["receipt"]
        if any(str(completed.get(key) or "") != str(identity[key]) for key in ("repo", "pr_number", "verified_head")):
            return fail("terminal_conflict", failure_class="terminal", retry_safe=False, decision="terminal_conflict", authorize=False, conflict="completed_receipt_identity_mismatch", **identity)
        state = {
            "repo": identity["repo"],
            "pr_number": identity["pr_number"],
            "verified_head": identity["verified_head"],
            "candidate": receipt.get("candidate") or identity["candidate"],
            "run_id": ((receipt.get("run") or {}) if isinstance(receipt.get("run"), dict) else {}).get("run_id") or identity["run_id"],
            "status": "completed",
            "attempted": True,
            "checks": receipt.get("checks") or checks,
        }
        return ok(status="already_repaired", decision="already_repaired", authorize=False, reason="attempt_already_recorded", attempt_state=state, receipt_path=completed.get("receipt_path"), receipt=receipt, **identity)
    state = _repair_state(request)
    if state:
        for key in ("repo", "pr_number", "verified_head"):
            if key in state and str(state.get(key)) != str(identity[key]):
                return fail("terminal_conflict", failure_class="terminal", retry_safe=False, decision="terminal_conflict", authorize=False, conflict=f"{key}_mismatch", **identity)
        prior_status = str(state.get("status") or state.get("decision") or "").lower()
        recovery = cond_blob(request, "verify_repair_attempt_recovery")
        continuation = cond_blob(request, "verify_repair_recovery_continuation")
        if recovery.get("ok") is True and recovery.get("status") == "verified" and recovery.get("recovery_verified") is True:
            claim = recovery.get("recovery_claim")
            if not isinstance(claim, dict) or any(str(claim.get(key) or "") != str(identity[value]) for key, value in (("repo", "repo"), ("pr_number", "pr_number"), ("verified_head", "verified_head"))):
                return fail("terminal_conflict", failure_class="terminal", retry_safe=False, decision="terminal_conflict", authorize=False, conflict="repair_recovery_identity_mismatch", **identity)
            original = str(claim.get("recovery_candidate") or "") == str(identity["candidate"]) and str(claim.get("recovery_run_id") or "") == str(identity["run_id"])
            continued = bool(continuation.get("ok") is True and continuation.get("status") in {"original", "verified"} and continuation.get("continuation_verified") is True)
            if not original and not continued:
                return fail("terminal_conflict", failure_class="terminal", retry_safe=False, decision="terminal_conflict", authorize=False, conflict="repair_recovery_continuation_required", **identity)
            return ok(status="invoke", decision="invoke", authorize=True, reason="verified_failed_attempt_recovery", checks=checks, attempt_state=_repair_attempt_state(identity, checks), reservation_path=str(cond_blob(request, "read_repair_attempt_state").get("reservation_path") or ""), recovery_claim=claim, recovery_claim_path=recovery.get("recovery_claim_path"), continuation=continuation.get("continuation"), continuation_path=continuation.get("continuation_path"), **identity)
        if prior_status in {"pending", "waiting", "awaiting_checks", "running", "authorized"}:
            return ok(status="pending", decision="wait", authorize=False, reason="awaiting_checks", **identity)
        if prior_status in {"reserved", "repaired", "succeeded", "completed", "invoked", "failed", "already_repaired"} or state.get("attempted") is True:
            return ok(status="already_repaired", decision="already_repaired", authorize=False, reason="attempt_already_recorded", **identity)
    if pending:
        return ok(status="pending", decision="wait", authorize=False, reason="checks_pending", checks=checks, **identity)
    failures = [item for item in checks if item["conclusion"] in {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}]
    triage = cond_blob(request, "decide_triage_action", "triage_decide_triage_action")
    missing_test_evidence = (
        triage.get("ok") is True
        and triage.get("status") == "decided"
        and triage.get("action") == "repair"
        and triage.get("reason") == "missing_test_evidence"
    )
    if not failures and not missing_test_evidence:
        return ok(status="already_repaired", decision="already_repaired", authorize=False, reason="checks_passed", checks=checks, **identity)
    return ok(
        status="invoke",
        decision="invoke",
        authorize=True,
        reason="missing_test_evidence" if missing_test_evidence and not failures else "checks_failed",
        checks=checks,
        failures=failures,
        attempt_state=_repair_attempt_state(identity, checks),
        **identity,
    )

_COMPLETED_TASK_STATUSES = {"done", "completed", "archived"}


def _task_body(task: object) -> str:
    if not isinstance(task, dict):
        return ""
    return str(task.get("body") or task.get("description") or "")


def _task_id(task: object) -> object:
    if not isinstance(task, dict):
        return None
    return task.get("id") or task.get("task_id")


def _task_status(task: object) -> str:
    if not isinstance(task, dict):
        return ""
    return str(task.get("status") or task.get("state") or "").strip().lower()


def _tasks_with_marker(tasks: object, marker: str) -> list[dict[str, object]] | None:
    """Return exact Idempotency-Key body matches, or ``None`` if malformed."""
    import re

    if not isinstance(tasks, list) or any(not isinstance(task, dict) for task in tasks):
        return None
    marker_line = re.compile(r"(?m)^Idempotency-Key:\s*" + re.escape(marker) + r"\s*$")
    return [task for task in tasks if marker_line.search(_task_body(task))]

def _reconcile_marker_read(
    *, board: str, marker: str, title: str
) -> tuple[str, dict[str, object] | None, str | None]:
    """Re-list a board and classify a stable-marker read-back.

    A single marker match is safe to reuse, including a completed task.  No
    match is the only state in which an ambiguous create may be retried;
    malformed reads and duplicate/conflicting matches fail closed.
    """
    try:
        tasks = hermes_kanban_json(
            ["--board", board, "list", "--json", "--sort", "created-desc"]
        )
    except CommandError as exc:
        return "read_failed", None, str(exc)
    matches = _tasks_with_marker(tasks, marker)
    if matches is None:
        return "malformed", None, "kanban list returned non-list JSON"
    if len(matches) > 1:
        return "conflict", None, f"found {len(matches)} tasks with marker {marker}"
    if not matches:
        return "absent", None, None
    match = matches[0]
    match_title = str(match.get("title") or "")
    if match_title and match_title != title:
        return "conflict", None, "stable marker matched a task with a conflicting title"
    return "found", match, None


def _existing_task_result(
    *, board: str, marker: str, task: dict[str, object], title: str
) -> Result:
    task_id = _task_id(task)
    if task_id is None or not str(task_id).strip():
        return fail(
            "invalid_kanban_task_id",
            failure_class="terminal",
            retry_safe=False,
            board=board,
            idempotency_key=marker,
        )
    status = "already_completed" if _task_status(task) in _COMPLETED_TASK_STATUSES else "exists"
    return ok(
        status=status,
        board=board,
        task_id=task_id,
        title=str(task.get("title") or title),
        idempotency_key=marker,
        mutated=False,
    )

def _linked_repair_issue(data: dict[str, object], loaded: dict[str, object], pr: dict[str, object]) -> tuple[int | None, str | None]:
    """Resolve the repair issue from GitHub's exact closing issue references."""
    refs = pr.get("closingIssuesReferences")
    if not isinstance(refs, list):
        return None, "missing_closing_issue_references"
    linked: list[int] = []
    for ref in refs:
        if not isinstance(ref, dict) or isinstance(ref.get("number"), bool):
            return None, "malformed_closing_issue_references"
        try:
            issue_number = int(ref.get("number"))
        except (TypeError, ValueError):
            return None, "malformed_closing_issue_references"
        if issue_number <= 0:
            return None, "malformed_closing_issue_references"
        linked.append(issue_number)
    if len(linked) != 1:
        return None, "expected_exactly_one_closing_issue"
    explicit = data.get("issue")
    if explicit is None:
        explicit = data.get("issue_number")
    if explicit is not None:
        try:
            explicit_number = int(explicit)
        except (TypeError, ValueError):
            return None, "explicit_issue_mismatch"
        if explicit_number != linked[0]:
            return None, "explicit_issue_mismatch"
    return linked[0], None


def build_repair_prompt(request: Request) -> Result:
    """Pure: build OMP prompt from PR checks/review context."""
    gated = _repair_decision_gate(request)
    if gated is not None:
        return gated
    upstream = _repair_upstream(
        request,
        "build_repair_prompt",
        "evaluate_checks",
        "load_pr_fields",
        "create_review_task",
        "reconcile_review_task",
    )
    if upstream:
        return upstream
    data = input_of(request)
    decide = cond_blob(request, "decide_triage_action", "decide", "triage_decide_triage_action")
    checks = cond_blob(request, "evaluate_checks", "checks", "triage_evaluate_checks")
    loaded = cond_blob(request, "load_pr_fields", "triage_load_pr_fields")
    created = cond_blob(request, "create_review_task", "create_fix_task")
    pr = loaded.get("pr") if isinstance(loaded.get("pr"), dict) else data.get("pr") or {}
    if not isinstance(pr, dict):
        return fail("invalid_pr", failure_class="terminal", retry_safe=False)
    issue, issue_error = _linked_repair_issue(data, loaded, pr)
    if issue_error:
        return fail(issue_error, failure_class="terminal", retry_safe=False, pr_number=pr.get("number"))
    failures = data.get("failures") or checks.get("failures") or []
    reason = str(data.get("reason") or decide.get("reason") or "repair")
    number = pr.get("number") or loaded.get("number") or data.get("pr_number") or data.get("number")
    title = pr.get("title") or ""
    repo = loaded.get("repo") or data.get("repo")
    board = loaded.get("board") or data.get("board")
    clone_path = loaded.get("clone_path") or data.get("clone_path")
    priority = loaded.get("priority", data.get("priority"))
    branch = pr.get("headRefName") or loaded.get("branch") or data.get("branch")
    body = (
        f"Repair PR #{number}: {title}\n"
        f"Repository: {repo or 'n/a'}\n"
        f"Issue: #{issue}\n"
        f"Branch: {branch or 'n/a'}\n"
        f"Clone: {clone_path or 'n/a'}\n"
        f"Board: {board or 'n/a'} (priority {priority if priority is not None else 'n/a'})\n"
        f"Reason: {reason}\n"
        f"Failing checks: {', '.join(str(item) for item in failures) if failures else 'n/a'}\n"
        "Update the branch to fix CI/merge issues. Keep scope minimal.\n"
        "Do not force-push. Do not merge.\n"
    )
    task_id = created.get("task_id") or data.get("task_id")
    return ok(
        status="built", prompt=body, reason=reason, pr_number=number, branch=branch,
        **({"task_id": task_id} if task_id else {}),
        **({"repo": repo} if repo else {}), issue=issue,
        **({"board": board} if board else {}),
        **({"clone_path": clone_path} if clone_path else {}),
        **({"priority": priority} if priority is not None else {}),
    )






# Atomic repair-chain handlers share the Kanban/read primitives with dispatch.
from lokay.steps.issue_to_pr import (
    _atomic_terminal,
    _reconcile_kanban_marker,
)

def read_review_tasks(request: Request) -> Result:
    gated = _repair_decision_gate(request)
    if gated is not None: return gated
    terminal = _atomic_terminal(request, "read_review_tasks", "decide_triage_action")
    if terminal: return terminal
    idle = upstream_noop(request, "decide_triage_action")
    if idle: return noop(str(idle.get("reason") or "no_selected_pr"), operation="read_review_tasks")
    data, cfg = input_of(request), cfg_of(request); selected = cond_blob(request, "select_fix_pr", "triage_select_fix_pr"); board = str(data.get("board") or cfg.get("board") or selected.get("board") or "")
    if not board: return fail("missing_board", failure_class="terminal", retry_safe=False, operation="read_review_tasks")
    try: tasks = hermes_kanban_json(["--board", board, "list", "--json", "--sort", "created-desc"])
    except CommandError as exc: return fail("kanban_list_failed", failure_class="retryable_read", retry_safe=True, operation="read_review_tasks", error=str(exc), board=board)
    if not isinstance(tasks, list) or any(not isinstance(t, dict) for t in tasks): return fail("invalid_kanban_json", failure_class="terminal", retry_safe=False, operation="read_review_tasks")
    return ok(status="read", operation="read_review_tasks", board=board, tasks=tasks)

def find_review_marker(request: Request) -> Result:
    gated = _repair_decision_gate(request)
    if gated is not None: return gated
    terminal = _atomic_terminal(request, "find_review_marker", "read_review_tasks")
    if terminal: return terminal
    idle = upstream_noop(request, "read_review_tasks")
    if idle: return noop(str(idle.get("reason") or "no_selected_pr"), operation="find_review_marker")
    data = input_of(request); loaded = cond_blob(request, "load_pr_fields"); pr = loaded.get("pr") if isinstance(loaded.get("pr"), dict) else {}; rows = data.get("tasks") or cond_blob(request, "read_review_tasks").get("tasks") or []; repo, number = str(data.get("repo") or loaded.get("repo") or ""), str(data.get("pr_number") or data.get("number") or loaded.get("number") or pr.get("number") or ""); marker = str(data.get("idempotency_key") or f"fix-pr-review:{repo}:{number}")
    if not isinstance(rows, list): return fail("missing_review_rows", failure_class="terminal", retry_safe=False, operation="find_review_marker")
    matches = [t for t in rows if marker in str(t.get("body") or t.get("description") or "")]
    if len(matches) > 1: return fail("ambiguous_review_task", failure_class="terminal", retry_safe=False, operation="find_review_marker", matches=matches)
    return ok(status="found" if matches else "absent", operation="find_review_marker", marker=marker, task=matches[0] if matches else None)

def create_review_task(request: Request) -> Result:
    gated = _repair_decision_gate(request)
    if gated is not None: return gated
    terminal = _atomic_terminal(request, "create_review_task", "find_review_marker")
    if terminal: return terminal
    idle = upstream_noop(request, "find_review_marker")
    if idle: return noop(str(idle.get("reason") or "no_selected_pr"), operation="create_review_task")
    data, cfg = input_of(request), cfg_of(request); loaded = cond_blob(request, "load_pr_fields"); pr = loaded.get("pr") if isinstance(loaded.get("pr"), dict) else {}; board, repo, number = str(data.get("board") or loaded.get("board") or cfg.get("board") or ""), str(data.get("repo") or loaded.get("repo") or ""), str(data.get("pr_number") or data.get("number") or loaded.get("number") or pr.get("number") or ""); reason = str(data.get("reason") or "checks_failed"); marker = str(data.get("idempotency_key") or cond_blob(request, "find_review_marker").get("marker") or f"fix-pr-review:{repo}:{number}"); title = str(data.get("title") or f"[fix-pr-review] {repo}#{number}: {reason}")
    if not board or not repo or not number: return fail("missing_board_repo_or_number", failure_class="terminal", retry_safe=False, operation="create_review_task", idempotency_key=marker)
    if dry_run_flag(request): return planned(operation="create_review_task", board=board, title=title, idempotency_key=marker)
    body = str(data.get("body") or f"Repository: {repo}\nPR: #{number}\nReason: {reason}\nIdempotency-Key: {marker}\n")
    try: proc = run_cmd(["hermes", "kanban", "--board", board, "create", "--body", body, "--assignee", str(cfg.get("fixer_assignee") or "lokay-fixer"), "--idempotency-key", marker, title], timeout=90)
    except CommandError as exc: return fail("review_task_create_failed", failure_class="reconcile_then_retry", retry_safe=False, operation="create_review_task", error=str(exc), mutated=True)
    return ok(status="created", operation="create_review_task", board=board, title=title, marker=marker, stdout=(proc.stdout or "")[-400:], mutated=True)

def reconcile_review_task(request: Request) -> Result:
    gated = _repair_decision_gate(request)
    if gated is not None: return gated
    terminal = _atomic_terminal(request, "reconcile_review_task", "create_review_task")
    if terminal: return terminal
    idle = upstream_noop(request, "create_review_task")
    if idle: return noop(str(idle.get("reason") or "no_selected_pr"), operation="reconcile_review_task")
    created = cond_blob(request, "create_review_task")
    if dry_run_flag(request) and created.get("status") == "planned":
        return planned(
            operation="reconcile_review_task",
            board=created.get("board"),
            idempotency_key=created.get("idempotency_key"),
        )
    return _reconcile_kanban_marker(request, "reconcile_review_task", "create_review_task", "fix-pr-review")

def read_task_for_block(request: Request) -> Result:
    gated = _repair_decision_gate(request)
    if gated is not None: return gated
    terminal = _atomic_terminal(request, "read_task_for_block", "build_repair_prompt")
    if terminal: return terminal
    idle = upstream_noop(request, "build_repair_prompt")
    if idle: return noop(str(idle.get("reason") or "no_selected_pr"), operation="read_task_for_block")
    data, cfg = input_of(request), cfg_of(request); board, task_id = str(data.get("board") or cfg.get("board") or ""), str(data.get("task_id") or "")
    try: tasks = hermes_kanban_json(["--board", board, "list", "--json", "--sort", "created-desc"])
    except CommandError as exc: return fail("kanban_list_failed", failure_class="retryable_read", retry_safe=True, operation="read_task_for_block", error=str(exc))
    if not isinstance(tasks, list) or any(not isinstance(t, dict) for t in tasks): return fail("invalid_kanban_json", failure_class="terminal", retry_safe=False, operation="read_task_for_block")
    matches = [t for t in tasks if str(t.get("id") or t.get("task_id") or "") == task_id]
    if len(matches) != 1: return fail("task_not_found" if not matches else "ambiguous_task", failure_class="terminal", retry_safe=False, operation="read_task_for_block", task_id=task_id)
    return ok(status="read", operation="read_task_for_block", board=board, task=matches[0], task_id=task_id)

def decide_task_block(request: Request) -> Result:
    gated = _repair_decision_gate(request)
    if gated is not None: return gated
    terminal = _atomic_terminal(request, "decide_task_block", "read_task_for_block")
    if terminal: return terminal
    idle = upstream_noop(request, "read_task_for_block")
    if idle: return noop(str(idle.get("reason") or "no_selected_pr"), operation="decide_task_block")
    task = input_of(request).get("task") or cond_blob(request, "read_task_for_block").get("task") or {}; state = _task_status(task)
    if state == "blocked": return ok(status="already_blocked", operation="decide_task_block", should_block=False)
    if state in _COMPLETED_TASK_STATUSES: return ok(status="already_completed", operation="decide_task_block", should_block=False)
    return ok(status="should_block", operation="decide_task_block", should_block=True)

def block_task(request: Request) -> Result:
    gated = _repair_decision_gate(request)
    if gated is not None: return gated
    terminal = _atomic_terminal(request, "block_task", "decide_task_block")
    if terminal: return terminal
    idle = upstream_noop(request, "decide_task_block")
    if idle: return noop(str(idle.get("reason") or "no_selected_pr"), operation="block_task")
    data, cfg = input_of(request), cfg_of(request); board, task_id, reason = str(data.get("board") or cfg.get("board") or ""), str(data.get("task_id") or ""), str(data.get("reason") or "blocked"); decision = cond_blob(request, "decide_task_block")
    if decision.get("should_block") is False: return ok(status=decision.get("status") or "already_blocked", operation="block_task", mutated=False)
    if not board or not task_id: return fail("missing_board_or_task_id", failure_class="terminal", retry_safe=False, operation="block_task")
    if dry_run_flag(request): return planned(operation="block_task", board=board, task_id=task_id, reason=reason)
    try: run_cmd(["hermes", "kanban", "--board", board, "block", task_id, "--reason", reason], timeout=60)
    except CommandError as exc: return fail("block_failed", failure_class="reconcile_then_retry", retry_safe=False, operation="block_task", error=str(exc), mutated=True)
    return ok(status="blocked", operation="block_task", board=board, task_id=task_id, mutated=True)

def verify_task_blocked(request: Request) -> Result:
    gated = _repair_decision_gate(request)
    if gated is not None: return gated
    terminal = _atomic_terminal(request, "verify_task_blocked", "block_task")
    if terminal: return terminal
    idle = upstream_noop(request, "block_task")
    if idle: return noop(str(idle.get("reason") or "no_selected_pr"), operation="verify_task_blocked")
    read = read_task_for_block(request)
    if read.get("ok") is False: return read
    state = _task_status(read["task"])
    if state not in {"blocked", *_COMPLETED_TASK_STATUSES}: return fail("block_not_confirmed", failure_class="reconcile_then_retry", retry_safe=False, operation="verify_task_blocked", task_id=read["task_id"], state=state, mutated=True)

 # Repair worktree atoms intentionally do not reuse the new-issue branch creator.
 # A repair branch is anchored to the authoritative PR head and may never be reset.
from pathlib import Path
from typing import Any
from lokay.adapters_git import (
    branch_config_get,
    branch_config_set,
    branch_config_unset,
    branch_exists,
    git,
    parse_worktree_porcelain,
    remote_ref,
    rev_parse,
    worktree_add,
    worktree_list,
)

_REPAIR_CONTEXT_ALIASES = (
    "build_repair_prompt", "triage_build_repair_prompt", "read_repair_context",
    "read_repair_remote_head", "fetch_repair_remote_head", "verify_fetched_repair_remote_head", "read_repair_worktree_inventory",
    "read_repair_branch_provenance", "read_repair_creation_evidence",
    "read_repair_worktree_cleanliness", "read_repair_remote_ancestry",
    "decide_repair_worktree_fast_forward", "read_repair_worktree_branch_before_fast_forward", "read_repair_worktree_head_before_fast_forward", "read_repair_worktree_cleanliness_before_fast_forward", "decide_repair_worktree_fast_forward_execution", "fast_forward_repair_worktree",
    "decide_repair_worktree_ownership", "create_repair_branch", "write_repair_branch_provenance", "add_repair_worktree",
    "prepare_repair_worktree", "verify_repair_worktree",
)
def _repair_blob(request: Request, name: str) -> dict[str, Any]:
     """Read a canonical conduction blob, including its triage-prefixed alias."""
     return cond_blob(request, name)

def _repair_acquired_ref(context: dict[str, str], remote_oid: str) -> str:
     """Return a deterministic ref unique to this exact advertised head."""
     digest = hashlib.sha256(
         f"{context['repo']}\0{context['pr_number']}\0{context['branch']}\0{remote_oid}".encode()
     ).hexdigest()
     return f"refs/lokay/repair-acquire/{digest}"

def _repair_blobs(request: Request) -> list[dict[str, Any]]:
     conduction = input_of(request).get("conduction")
     if not isinstance(conduction, dict):
         return []
     found: list[dict[str, Any]] = []
     for name, value in conduction.items():
         if isinstance(value, dict) and (name in _REPAIR_CONTEXT_ALIASES or any(name.endswith(f"_{alias}") for alias in _REPAIR_CONTEXT_ALIASES)) and value not in found:
             found.append(dict(value))
     return found




def _repair_values(request: Request, key: str) -> list[str]:
     values: list[str] = []
     sources = [input_of(request), cfg_of(request), *_repair_blobs(request)]
     for source in sources:
         value = source.get(key)
         if key == "task_id" and isinstance(value, dict):
             value = value.get("id") or value.get("task_id")
         if value is None or not str(value).strip():
             continue
         text = str(value).strip()
         if text not in values:
             values.append(text)
     return values

def _repair_field(request: Request, *keys: str, default: str = "") -> str:
     for key in keys:
         values = _repair_values(request, key)
         if values:
             return values[0]
     return default

def _repair_local_branch(repo: str, pr_number: str, branch: str) -> str:
     """Return the collision-safe local ref owned by one remote repair target."""
     digest = hashlib.sha256(f"{repo}\0{pr_number}\0{branch}".encode()).hexdigest()
     return f"lokay/repair/{digest}"

def _repair_ownership_receipt(request: Request, repo: str, pr_number: str, branch: str) -> str:
     root = _repair_state_root(request)
     if root is None or not repo or not pr_number or not branch:
         return ""
     digest = hashlib.sha256(f"{repo}\0{pr_number}\0{branch}".encode()).hexdigest()
     return str(root / "repair-ownership" / repo.replace("/", "__") / pr_number / f"{digest}.json")


def _repair_context(request: Request) -> dict[str, str]:
     data = input_of(request)
     cfg = cfg_of(request)
     root = _repair_field(request, "worktree_root")
     branch = _repair_field(request, "branch", "head_ref_name")
     repo = _repair_field(request, "repo")
     pr_number = _repair_field(request, "pr_number", "number")
     local_branch = _repair_local_branch(repo, pr_number, branch) if repo and pr_number and branch else ""
     path = str(Path(root) / local_branch) if root and local_branch else ""
     return {
         "repo": repo,
         "issue": _repair_field(request, "issue"),
         "pr_number": pr_number,
         "branch": branch,
         "local_branch": local_branch,
         "clone_path": _repair_field(request, "clone_path"),
         "worktree_root": root,
         "worktree_path": path,
         "remote": _repair_field(request, "remote", default="origin") or "origin",
         "task_id": _repair_field(request, "task_id"),
         "receipt": _repair_ownership_receipt(request, repo, pr_number, branch),
     }

def _repair_context_error(context: dict[str, str]) -> str | None:
     for key in ("repo", "issue", "pr_number", "branch", "local_branch", "clone_path", "worktree_root", "worktree_path"):
         if not context.get(key):
             return f"missing_repair_{key}"
     branch = Path(context["branch"])
     local_branch = Path(context["local_branch"])
     if branch.is_absolute() or ".." in branch.parts or not branch.parts:
         return "invalid_repair_branch"
     if local_branch.is_absolute() or ".." in local_branch.parts or not local_branch.parts:
         return "invalid_repair_local_branch"
     try:
         Path(context["worktree_path"]).resolve().relative_to(Path(context["worktree_root"]).resolve())
     except ValueError:
         return "repair_worktree_path_escape"
     return None


def _repair_lifecycle_gate(request: Request, operation: str, *peers: str) -> Result | None:
     if "lifecycle_decide_lifecycle_transition" not in peers:
         return None
     lifecycle = cond_blob(request, "lifecycle_decide_lifecycle_transition")
     if not lifecycle:
         return None
     if lifecycle.get("ok") is not True or lifecycle.get("status") in {"failed", "cancelled", "timed_out"}:
         return fail("upstream_failed", failure_class="terminal", retry_safe=False, operation=operation, upstream=lifecycle)
     outcome = str(lifecycle.get("outcome") or lifecycle.get("action") or "")
     if outcome == "resume_repair":
         return None
     if outcome in {"wait_pending_checks", "finalize_merged", "finalize_closed", "ready_for_merge"}:
         return noop(outcome, operation=operation)
     return fail("invalid_repair_lifecycle", failure_class="terminal", retry_safe=False, operation=operation, upstream=lifecycle)



def _repair_upstream(request: Request, operation: str, *peers: str) -> Result | None:
     gated = _repair_decision_gate(request)
     if gated is not None:
         return gated
     lifecycle = _repair_lifecycle_gate(request, operation, *peers)
     if lifecycle is not None:
         return lifecycle
     checked_peers = tuple(peer for peer in peers if peer != "lifecycle_decide_lifecycle_transition")
     terminal = _atomic_terminal(request, operation, *checked_peers)
     if terminal:
         return terminal
     for peer in checked_peers:
         refreshed = cond_blob(request, peer)
         if refreshed.get("status") == "refreshed" and refreshed.get("refresh_kind") == "legacy_base_synchronization":
             return noop("legacy_base_refreshed", operation=operation, refresh_kind="legacy_base_synchronization", worked=False)
     idle = upstream_noop(request, *checked_peers)
     if idle:
         return noop(str(idle.get("reason") or "no_selected_pr"), operation=operation)
     return None
def read_repair_context(request: Request) -> Result:
     gated = _repair_decision_gate(request)
     if gated is not None:
         return gated
     terminal = _atomic_terminal(request, "read_repair_context", "build_repair_prompt")
     if terminal is not None:
         return terminal
     idle = upstream_noop(request, "build_repair_prompt")
     if idle:
         return noop(str(idle.get("reason") or "not_selected"), operation="read_repair_context", worked=False)
     context = _repair_context(request)
     error = _repair_context_error(context)
     if error:
         return fail(error, failure_class="terminal", retry_safe=False, operation="read_repair_context", context=context)
     # Every identity must be singular across request/config/conduction.
     conflicts = {key: _repair_values(request, key) for key in ("repo", "issue", "pr_number", "branch", "clone_path", "worktree_root") if len(_repair_values(request, key)) > 1}
     if conflicts:
         return fail("conflicting_repair_context", failure_class="terminal", retry_safe=False, operation="read_repair_context", conflicts=conflicts)
     return ok(status="read", operation="read_repair_context", **context)

def read_repair_remote_head(request: Request) -> Result:
     upstream = _repair_upstream(request, "read_repair_remote_head", "read_repair_context")
     if upstream:
         return upstream
     context = _repair_context(request)
     error = _repair_context_error(context)
     if error:
         return fail(error, failure_class="terminal", retry_safe=False, operation="read_repair_remote_head")
     try:
         # ls-remote is authoritative; a fetched origin/* ref can be stale.
         text = git(["ls-remote", context["remote"], f"refs/heads/{context['branch']}"], cwd=context["clone_path"])
     except CommandError as exc:
         return fail("repair_remote_head_read_failed", failure_class="retryable_read", retry_safe=True, operation="read_repair_remote_head", error=str(exc), **context)
     rows = [line.split() for line in text.splitlines() if line.split()]
     if len(rows) != 1 or len(rows[0]) < 2 or rows[0][1] != f"refs/heads/{context['branch']}":
         return fail("repair_remote_head_missing", failure_class="terminal", retry_safe=False, operation="read_repair_remote_head", output=text, **context)
     return ok(status="read", operation="read_repair_remote_head", remote_oid=rows[0][0], **context)

def read_repair_worktree_inventory(request: Request) -> Result:
     upstream = _repair_upstream(request, "read_repair_worktree_inventory", "read_repair_context", "read_repair_remote_head")
     if upstream:
         return upstream
     context = _repair_context(request)
     try:
         rows = parse_worktree_porcelain(worktree_list(context["clone_path"]))
     except (CommandError, OSError, ValueError) as exc:
         return fail("repair_worktree_inventory_failed", failure_class="retryable_read", retry_safe=True, operation="read_repair_worktree_inventory", error=str(exc), **context)
     return ok(status="read", operation="read_repair_worktree_inventory", worktrees=rows, **context)

def read_repair_branch_provenance(request: Request) -> Result:
    upstream = _repair_upstream(request, "read_repair_branch_provenance", "read_repair_context", "read_repair_remote_head", "read_repair_worktree_inventory")
    if upstream:
        return upstream
    context = _repair_context(request)
    try:
        exists = branch_exists(context["clone_path"], context["local_branch"])
        provenance: dict[str, str] = {}
        branch_head = ""
        if exists:
            branch_head = rev_parse(context["clone_path"], context["local_branch"])
            for key in ("task", "issue", "repo", "pr", "receipt", "repair_receipt", "remote_oid", "target_branch"):
                try:
                    provenance[key] = branch_config_get(context["clone_path"], context["local_branch"], f"lokay-{key}").strip()
                except CommandError:
                    provenance[key] = ""
    except (CommandError, OSError) as exc:
        return fail("repair_branch_provenance_read_failed", failure_class="retryable_read", retry_safe=True, operation="read_repair_branch_provenance", error=str(exc), **context)
    return ok(status="read", operation="read_repair_branch_provenance", exists=exists, provenance=provenance, branch_head=branch_head, **context)
def fetch_repair_remote_head(request: Request) -> Result:
    """Acquire the authoritative PR head into an isolated, exact-OID ref."""
    upstream = _repair_upstream(request, "fetch_repair_remote_head", "read_repair_remote_head")
    if upstream:
        return upstream
    context = _repair_context(request)
    error = _repair_context_error(context)
    if error:
        return fail(error, failure_class="terminal", retry_safe=False, operation="fetch_repair_remote_head")
    head = cond_blob(request, "read_repair_remote_head")
    remote_oid = str(head.get("remote_oid") or "").strip()
    if not remote_oid:
        return fail("missing_repair_remote_head", failure_class="terminal", retry_safe=False, operation="fetch_repair_remote_head", **context)
    acquired_ref = _repair_acquired_ref(context, remote_oid)
    if dry_run_flag(request):
        return planned(operation="fetch_repair_remote_head", remote=context["remote"], branch=context["branch"], remote_oid=remote_oid, acquired_ref=acquired_ref)
    try:
        git(["fetch", "--no-tags", context["remote"], f"refs/heads/{context['branch']}:{acquired_ref}"], cwd=context["clone_path"])
    except (CommandError, OSError) as exc:
        # Fetch is a mutation atom: an error cannot prove that no ref update occurred.
        return fail("repair_remote_head_fetch_failed", failure_class="reconcile_then_retry", retry_safe=False, mutated=True, mutation_unknown=True, operation="fetch_repair_remote_head", error=str(exc), remote_oid=remote_oid, acquired_ref=acquired_ref, **context)
    return ok(status="fetched", operation="fetch_repair_remote_head", remote_oid=remote_oid, acquired_ref=acquired_ref, mutated=True, **context)


def verify_fetched_repair_remote_head(request: Request) -> Result:
    """Read the acquired ref once and prove it equals the advertised remote head."""
    upstream = _repair_upstream(request, "verify_fetched_repair_remote_head", "fetch_repair_remote_head")
    if upstream:
        return upstream
    context = _repair_context(request)
    fetched = cond_blob(request, "fetch_repair_remote_head")
    remote_oid = str(fetched.get("remote_oid") or "").strip()
    acquired_ref = str(fetched.get("acquired_ref") or "").strip()
    if dry_run_flag(request) and fetched.get("status") == "planned" and remote_oid and acquired_ref:
        return planned(
            operation="verify_fetched_repair_remote_head",
            remote_oid=remote_oid,
            acquired_ref=acquired_ref,
        )
    if fetched.get("ok") is not True or fetched.get("status") != "fetched" or not remote_oid or not acquired_ref:
        return fail("repair_remote_head_not_fetched", failure_class="terminal", retry_safe=False, operation="verify_fetched_repair_remote_head", remote_oid=remote_oid, acquired_ref=acquired_ref, **context)
    try:
        acquired_oid = rev_parse(context["clone_path"], acquired_ref)
    except (CommandError, OSError) as exc:
        return fail("repair_remote_head_verification_missing", failure_class="terminal", retry_safe=False, operation="verify_fetched_repair_remote_head", error=str(exc), remote_oid=remote_oid, acquired_ref=acquired_ref, **context)
    if acquired_oid != remote_oid:
        return fail("repair_remote_head_verification_mismatch", failure_class="terminal", retry_safe=False, operation="verify_fetched_repair_remote_head", remote_oid=remote_oid, acquired_oid=acquired_oid, acquired_ref=acquired_ref, **context)
    return ok(status="verified", operation="verify_fetched_repair_remote_head", verified=True, remote_oid=remote_oid, acquired_oid=acquired_oid, acquired_ref=acquired_ref, **context)

def _repair_inventory_row(request: Request) -> dict[str, Any] | None:
    inventory = cond_blob(request, "read_repair_worktree_inventory")
    rows = inventory.get("worktrees")
    if not isinstance(rows, list):
        row = inventory.get("worktree")
        rows = [row] if isinstance(row, dict) else []
    context = _repair_context(request)
    target = Path(context.get("worktree_path") or "").resolve()
    for row in rows:
        if isinstance(row, dict) and Path(str(row.get("path") or "")).resolve() == target:
            return dict(row)
    return None


def read_repair_worktree_cleanliness(request: Request) -> Result:
    """Read the owned target worktree status exactly once."""
    upstream = _repair_upstream(
        request, "read_repair_worktree_cleanliness", "read_repair_context",
        "read_repair_remote_head", "read_repair_worktree_inventory",
        "read_repair_branch_provenance",
    )
    if upstream:
        return upstream
    context = _repair_context(request)
    row = _repair_inventory_row(request)
    if row is None:
        return ok(status="inactive", operation="read_repair_worktree_cleanliness", worktree_present=False, clean=False, **context)
    try:
        status = git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=context["worktree_path"])
    except (CommandError, OSError) as exc:
        return fail("repair_worktree_cleanliness_read_failed", failure_class="retryable_read", retry_safe=True, operation="read_repair_worktree_cleanliness", error=str(exc), **context)
    return ok(status="read", operation="read_repair_worktree_cleanliness", worktree_present=True, clean=not bool(status), dirty=bool(status), porcelain=status, **context)


def read_repair_remote_ancestry(request: Request) -> Result:
    """Read whether the existing local head is an ancestor of the verified PR head."""
    upstream = _repair_upstream(
        request, "read_repair_remote_ancestry", "read_repair_context",
        "read_repair_remote_head", "fetch_repair_remote_head", "verify_fetched_repair_remote_head",
        "read_repair_worktree_inventory", "read_repair_branch_provenance",
    )
    if upstream:
        return upstream
    context = _repair_context(request)
    verified = cond_blob(request, "verify_fetched_repair_remote_head")
    remote_oid = str(verified.get("remote_oid") or "")
    acquired_oid = str(verified.get("acquired_oid") or "")
    acquired_ref = str(verified.get("acquired_ref") or "")
    if dry_run_flag(request) and verified.get("status") == "planned" and remote_oid and acquired_ref:
        return planned(
            operation="read_repair_remote_ancestry",
            remote_oid=remote_oid,
            acquired_ref=acquired_ref,
            **context,
        )
    if verified.get("ok") is not True or verified.get("status") != "verified" or verified.get("verified") is not True or not acquired_ref or not acquired_oid or acquired_oid != remote_oid:
        return fail("repair_remote_head_not_verified", failure_class="terminal", retry_safe=False, operation="read_repair_remote_ancestry", remote_oid=remote_oid, acquired_oid=acquired_oid, acquired_ref=acquired_ref, **context)
    row = _repair_inventory_row(request)
    if row is None:
        return ok(status="inactive", operation="read_repair_remote_ancestry", worktree_present=False, descendant=False, remote_oid=remote_oid, acquired_oid=acquired_oid, acquired_ref=acquired_ref, **context)
    local_oid = str(_repair_field(request, "local_head", "branch_head") or row.get("head") or "")
    if not local_oid:
        return fail("missing_repair_ancestry_head", failure_class="terminal", retry_safe=False, operation="read_repair_remote_ancestry", remote_oid=remote_oid, local_oid=local_oid, **context)
    try:
        proc = run_cmd(["git", "merge-base", "--is-ancestor", local_oid, acquired_ref], cwd=context["clone_path"], check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return fail("repair_remote_ancestry_read_failed", failure_class="retryable_read", retry_safe=True, operation="read_repair_remote_ancestry", error=str(exc), local_oid=local_oid, remote_oid=remote_oid, **context)
    if proc.returncode == 0:
        return ok(status="read", operation="read_repair_remote_ancestry", descendant=True, local_oid=local_oid, remote_oid=remote_oid, acquired_oid=acquired_oid, acquired_ref=acquired_ref, **context)
    if proc.returncode == 1:
        return fail("repair_worktree_diverged", failure_class="terminal", retry_safe=False, operation="read_repair_remote_ancestry", descendant=False, local_oid=local_oid, remote_oid=remote_oid, acquired_oid=acquired_oid, acquired_ref=acquired_ref, **context)
    return fail("repair_remote_ancestry_read_failed", failure_class="retryable_read", retry_safe=True, operation="read_repair_remote_ancestry", error=(proc.stderr or proc.stdout or "").strip(), local_oid=local_oid, remote_oid=remote_oid, acquired_oid=acquired_oid, acquired_ref=acquired_ref, **context)


def decide_repair_worktree_fast_forward(request: Request) -> Result:
    """Purely authorize one safe fast-forward of an owned repair worktree."""
    upstream = _repair_upstream(
        request, "decide_repair_worktree_fast_forward", "read_repair_context",
        "read_repair_remote_head", "read_repair_worktree_inventory",
        "read_repair_branch_provenance", "read_repair_worktree_cleanliness",
        "read_repair_remote_ancestry",
    )
    if upstream:
        return upstream
    context = _repair_context(request)
    inventory = cond_blob(request, "read_repair_worktree_inventory")
    branch_read = cond_blob(request, "read_repair_branch_provenance")
    cleanliness = cond_blob(request, "read_repair_worktree_cleanliness")
    ancestry = cond_blob(request, "read_repair_remote_ancestry")
    row = _repair_inventory_row(request)
    if row is None:
        return ok(status="inactive", operation="decide_repair_worktree_fast_forward", should_fast_forward=False, worktree_present=False, **context)
    if str(row.get("branch") or "") != context["local_branch"]:
        return fail("foreign_repair_worktree", failure_class="terminal", retry_safe=False, operation="decide_repair_worktree_fast_forward", actual_branch=row.get("branch"), **context)
    provenance = dict(branch_read.get("provenance") or {})
    expected = {"task": context["task_id"], "issue": context["issue"], "repo": context["repo"], "pr": context["pr_number"], "receipt": context["receipt"], "target_branch": context["branch"]}
    if not branch_read.get("exists") or any(provenance.get(key) != value for key, value in expected.items() if value or key == "task"):
        return fail("foreign_repair_branch_ownership", failure_class="terminal", retry_safe=False, operation="decide_repair_worktree_fast_forward", expected=expected, actual=provenance, **context)
    if cleanliness.get("clean") is not True:
        return fail("repair_worktree_dirty", failure_class="terminal", retry_safe=False, operation="decide_repair_worktree_fast_forward", dirty=True, **context)
    remote_oid = str(cond_blob(request, "read_repair_remote_head").get("remote_oid") or "")
    local_oid = str(row.get("head") or branch_read.get("branch_head") or "")
    if not remote_oid or not local_oid:
        return fail("missing_repair_ancestry_head", failure_class="terminal", retry_safe=False, operation="decide_repair_worktree_fast_forward", **context)
    if local_oid == remote_oid:
        return ok(status="inactive", operation="decide_repair_worktree_fast_forward", should_fast_forward=False, already_current=True, local_oid=local_oid, remote_oid=remote_oid, **context)
    if ancestry.get("descendant") is not True:
        return fail("repair_worktree_diverged", failure_class="terminal", retry_safe=False, operation="decide_repair_worktree_fast_forward", descendant=ancestry.get("descendant"), local_oid=local_oid, remote_oid=remote_oid, **context)
    return ok(status="authorized", operation="decide_repair_worktree_fast_forward", should_fast_forward=True, local_oid=local_oid, remote_oid=remote_oid, **context)

def read_repair_worktree_branch_before_fast_forward(request: Request) -> Result:
    """Read the checked-out branch immediately before fast-forward execution."""
    upstream = _repair_upstream(request, "read_repair_worktree_branch_before_fast_forward", "decide_repair_worktree_fast_forward")
    if upstream:
        return upstream
    decision = cond_blob(request, "decide_repair_worktree_fast_forward")
    context = _repair_context(request)
    if decision.get("status") == "inactive":
        return ok(status="inactive", operation="read_repair_worktree_branch_before_fast_forward", current_branch="", **context)
    try:
        current_branch = git(["branch", "--show-current"], cwd=context["worktree_path"])
    except (CommandError, OSError) as exc:
        return fail("repair_fast_forward_branch_read_failed", failure_class="retryable_read", retry_safe=True, operation="read_repair_worktree_branch_before_fast_forward", error=str(exc), **context)
    return ok(status="read", operation="read_repair_worktree_branch_before_fast_forward", current_branch=current_branch, **context)


def read_repair_worktree_head_before_fast_forward(request: Request) -> Result:
    """Read HEAD immediately before fast-forward execution."""
    upstream = _repair_upstream(request, "read_repair_worktree_head_before_fast_forward", "decide_repair_worktree_fast_forward")
    if upstream:
        return upstream
    decision = cond_blob(request, "decide_repair_worktree_fast_forward")
    context = _repair_context(request)
    if decision.get("status") == "inactive":
        return ok(status="inactive", operation="read_repair_worktree_head_before_fast_forward", local_oid="", **context)
    try:
        local_oid = rev_parse(context["worktree_path"])
    except (CommandError, OSError) as exc:
        return fail("repair_fast_forward_head_read_failed", failure_class="retryable_read", retry_safe=True, operation="read_repair_worktree_head_before_fast_forward", error=str(exc), **context)
    return ok(status="read", operation="read_repair_worktree_head_before_fast_forward", local_oid=local_oid, **context)


def read_repair_worktree_cleanliness_before_fast_forward(request: Request) -> Result:
    """Read exact porcelain cleanliness immediately before fast-forward execution."""
    upstream = _repair_upstream(request, "read_repair_worktree_cleanliness_before_fast_forward", "decide_repair_worktree_fast_forward")
    if upstream:
        return upstream
    decision = cond_blob(request, "decide_repair_worktree_fast_forward")
    context = _repair_context(request)
    if decision.get("status") == "inactive":
        return ok(status="inactive", operation="read_repair_worktree_cleanliness_before_fast_forward", clean=False, dirty=False, porcelain="", **context)
    try:
        porcelain = git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=context["worktree_path"])
    except (CommandError, OSError) as exc:
        return fail("repair_fast_forward_cleanliness_read_failed", failure_class="retryable_read", retry_safe=True, operation="read_repair_worktree_cleanliness_before_fast_forward", error=str(exc), **context)
    return ok(status="read", operation="read_repair_worktree_cleanliness_before_fast_forward", clean=not bool(porcelain), dirty=bool(porcelain), porcelain=porcelain, **context)


def decide_repair_worktree_fast_forward_execution(request: Request) -> Result:
    """Bind fresh worktree state to the original fast-forward authorization."""
    upstream = _repair_upstream(request, "decide_repair_worktree_fast_forward_execution", "decide_repair_worktree_fast_forward", "read_repair_worktree_branch_before_fast_forward", "read_repair_worktree_head_before_fast_forward", "read_repair_worktree_cleanliness_before_fast_forward")
    if upstream:
        return upstream
    decision = cond_blob(request, "decide_repair_worktree_fast_forward")
    context = _repair_context(request)
    if decision.get("status") == "inactive":
        return ok(status="inactive", operation="decide_repair_worktree_fast_forward_execution", should_fast_forward=False, **context)
    if decision.get("ok") is not True or decision.get("should_fast_forward") is not True:
        return fail("repair_fast_forward_not_authorized", failure_class="terminal", retry_safe=False, operation="decide_repair_worktree_fast_forward_execution", **context)
    branch = cond_blob(request, "read_repair_worktree_branch_before_fast_forward")
    head = cond_blob(request, "read_repair_worktree_head_before_fast_forward")
    cleanliness = cond_blob(request, "read_repair_worktree_cleanliness_before_fast_forward")
    expected_branch = str(decision.get("local_branch") or context["local_branch"])
    expected_head = str(decision.get("local_oid") or "")
    actual_branch = str(branch.get("current_branch") or "")
    actual_head = str(head.get("local_oid") or "")
    if actual_branch != expected_branch:
        return fail("repair_fast_forward_branch_changed", failure_class="terminal", retry_safe=False, operation="decide_repair_worktree_fast_forward_execution", expected_branch=expected_branch, actual_branch=actual_branch, **context)
    if actual_head != expected_head:
        return fail("repair_fast_forward_head_changed", failure_class="terminal", retry_safe=False, operation="decide_repair_worktree_fast_forward_execution", expected_head=expected_head, actual_head=actual_head, **context)
    if cleanliness.get("clean") is not True:
        return fail("repair_fast_forward_worktree_dirty", failure_class="terminal", retry_safe=False, operation="decide_repair_worktree_fast_forward_execution", dirty=True, **context)
    return ok(status="authorized", operation="decide_repair_worktree_fast_forward_execution", should_fast_forward=True, authorized_branch=expected_branch, authorized_local_oid=expected_head, remote_oid=str(decision.get("remote_oid") or ""), **context)

def fast_forward_repair_worktree(request: Request) -> Result:
    """Guarded ff mutation: revalidate branch, HEAD, and status under the worktree lock."""
    upstream = _repair_upstream(request, "fast_forward_repair_worktree", "verify_legacy_repair_pr_head", "decide_repair_worktree_fast_forward_execution")
    if upstream:
        return upstream
    decision = cond_blob(request, "decide_repair_worktree_fast_forward_execution")
    context = _repair_context(request)
    if decision.get("status") == "inactive":
        return ok(status="inactive", operation="fast_forward_repair_worktree", should_fast_forward=False, **context)
    if decision.get("ok") is not True or decision.get("should_fast_forward") is not True:
        return fail("repair_fast_forward_not_authorized", failure_class="terminal", retry_safe=False, operation="fast_forward_repair_worktree", **context)
    remote_oid = str(decision.get("remote_oid") or "")
    expected_branch = str(decision.get("authorized_branch") or "")
    expected_head = str(decision.get("authorized_local_oid") or "")
    if not remote_oid or not expected_branch or not expected_head:
        return fail("repair_fast_forward_guard_missing", failure_class="terminal", retry_safe=False, operation="fast_forward_repair_worktree", **context)
    if dry_run_flag(request):
        return planned(operation="fast_forward_repair_worktree", remote_oid=remote_oid, worktree_path=context["worktree_path"])
    try:
        # Serialize cooperating worktree mutations while checking and merging.
        with claim_directory_lock(Path(context["worktree_path"])):
            current_branch = git(["branch", "--show-current"], cwd=context["worktree_path"])
            current_head = rev_parse(context["worktree_path"])
            porcelain = git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=context["worktree_path"])
            if current_branch != expected_branch:
                return fail("repair_fast_forward_branch_changed", failure_class="terminal", retry_safe=False, operation="fast_forward_repair_worktree", expected_branch=expected_branch, actual_branch=current_branch, mutated=False, **context)
            if current_head != expected_head:
                return fail("repair_fast_forward_head_changed", failure_class="terminal", retry_safe=False, operation="fast_forward_repair_worktree", expected_head=expected_head, actual_head=current_head, mutated=False, **context)
            if porcelain:
                return fail("repair_fast_forward_worktree_dirty", failure_class="terminal", retry_safe=False, operation="fast_forward_repair_worktree", dirty=True, porcelain=porcelain, mutated=False, **context)
            git(["merge", "--ff-only", remote_oid], cwd=context["worktree_path"])
            observed_head = rev_parse(context["worktree_path"])
            if observed_head != remote_oid:
                return fail("repair_fast_forward_readback_mismatch", failure_class="reconcile_then_retry", retry_safe=False, operation="fast_forward_repair_worktree", expected_head=remote_oid, actual_head=observed_head, mutated=True, **context)
    except CommandError as exc:
        return fail("repair_fast_forward_failed", failure_class="reconcile_then_retry", retry_safe=False, operation="fast_forward_repair_worktree", error=str(exc), mutated=True, **context)
    except OSError as exc:
        return fail("repair_fast_forward_lock_failed", failure_class="retryable_read", retry_safe=True, operation="fast_forward_repair_worktree", error=str(exc), mutated=False, **context)
    return ok(status="advanced", operation="fast_forward_repair_worktree", local_oid=remote_oid, remote_oid=remote_oid, mutated=True, **context)


def read_repair_creation_evidence(request: Request) -> Result:
    """Read one explicitly configured, exact prior branch-creation process."""
    upstream = _repair_upstream(request, "read_repair_creation_evidence", "read_repair_context", "read_repair_remote_head", "read_repair_branch_provenance")
    if upstream:
        return upstream
    context = _repair_context(request)
    branch_read = cond_blob(request, "read_repair_branch_provenance")
    if not branch_read.get("exists") or any((branch_read.get("provenance") or {}).values()):
        return ok(status="not_needed", operation="read_repair_creation_evidence", verified=False, **context)
    data, cfg = input_of(request), cfg_of(request)
    recovery = (
        data.get("repair_creation_recovery")
        or data.get("creation_recovery")
        or cfg.get("repair_creation_recovery")
        or cfg.get("creation_recovery")
    )
    if not isinstance(recovery, dict):
        return ok(status="absent", operation="read_repair_creation_evidence", verified=False, **context)
    run_id = str(recovery.get("run_id") or "").strip()
    process_id = str(recovery.get("process_id") or "").strip()
    candidate = str(recovery.get("candidate") or "").strip()
    db_path = str(data.get("db_path") or cfg.get("db_path") or "").strip()
    path_id = str(recovery.get("path_id") or "").strip()
    effector_id = str(recovery.get("effector_id") or "").strip()
    allowed = {
        ("auto_worker", "triage_create_repair_branch"),
        ("pr_triage", "create_repair_branch"),
    }
    if (path_id, effector_id) not in allowed:
        return fail("invalid_repair_recovery_identity", failure_class="terminal", retry_safe=False, operation="read_repair_creation_evidence")
    expected_process_id = f"{run_id}:{path_id}:{effector_id}"
    if not run_id or process_id != expected_process_id or len(candidate) != 64 or not db_path:
        return fail("invalid_repair_recovery_identity", failure_class="terminal", retry_safe=False, operation="read_repair_creation_evidence")
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT run_id,id,status,input_json,output_json,metadata FROM processes WHERE run_id=? AND id=?",
                (run_id, process_id),
            ).fetchall()
        if len(rows) != 1:
            return fail("repair_creation_evidence_not_unique", failure_class="terminal", retry_safe=False, operation="read_repair_creation_evidence", count=len(rows))
        row_run, row_id, status, raw_input, raw_output, raw_metadata = rows[0]
        process_input = json.loads(raw_input)
        process_output = json.loads(raw_output)
        metadata = json.loads(raw_metadata)
    except (OSError, sqlite3.Error, TypeError, json.JSONDecodeError) as exc:
        return fail("repair_creation_evidence_read_failed", failure_class="retryable_read", retry_safe=True, operation="read_repair_creation_evidence", error=str(exc))
    values = process_output.get("values") if isinstance(process_output, dict) else None
    binding = metadata.get("__adapter_binding") if isinstance(metadata, dict) else None
    conduction = process_input.get("conduction") if isinstance(process_input, dict) else None
    decision = None
    if isinstance(conduction, dict):
        decision = conduction.get("triage_decide_repair_worktree_ownership") or conduction.get("decide_repair_worktree_ownership")
    remote_oid = str(cond_blob(request, "read_repair_remote_head").get("remote_oid") or "")
    exact = {
        "repo": context["repo"], "issue": context["issue"], "pr_number": context["pr_number"],
        "branch": context["branch"], "local_branch": context["local_branch"], "remote_oid": remote_oid,
        "clone_path": context["clone_path"], "worktree_path": context["worktree_path"], "receipt": context["receipt"],
    }
    expected_cwd = (Path(db_path).resolve().parent.parent / "deployment" / "versions" / candidate / "source" / "project").resolve()
    valid = bool(
        row_run == run_id and row_id == process_id and status == "succeeded"
        and isinstance(values, dict) and values.get("ok") is True and values.get("status") == "created" and values.get("mutated") is True
        and isinstance(decision, dict) and decision.get("status") == "create" and decision.get("reuse") is False
        and all(str(values.get(key) or "") == str(value) for key, value in exact.items())
        and all(str(decision.get(key) or "") == str(value) for key, value in exact.items())
        and str(process_input.get("candidate") or "") == candidate
        and str(process_input.get("candidate_id") or "") == candidate
        and isinstance(binding, dict) and Path(str(binding.get("cwd") or "")).resolve() == expected_cwd
    )
    if not valid:
        return fail("repair_creation_evidence_mismatch", failure_class="terminal", retry_safe=False, operation="read_repair_creation_evidence")
    return ok(status="verified", operation="read_repair_creation_evidence", verified=True, run_id=run_id, process_id=process_id, candidate=candidate, remote_oid=remote_oid, **context)

def decide_repair_worktree_ownership(request: Request) -> Result:
    upstream = _repair_upstream(request, "decide_repair_worktree_ownership", "verify_legacy_repair_pr_head", "read_repair_context", "read_repair_remote_head", "read_repair_worktree_inventory", "read_repair_branch_provenance", "read_repair_creation_evidence", "read_repair_worktree_cleanliness", "read_repair_remote_ancestry", "decide_repair_worktree_fast_forward", "fast_forward_repair_worktree")
    if upstream:
        return upstream
    context = _repair_context(request)
    context_error = _repair_context_error(context)
    if context_error:
        return fail(context_error, failure_class="terminal", retry_safe=False, operation="decide_repair_worktree_ownership")
    remote = cond_blob(request, "read_repair_remote_head")
    inventory = cond_blob(request, "read_repair_worktree_inventory")
    branch_read = cond_blob(request, "read_repair_branch_provenance")
    evidence = cond_blob(request, "read_repair_creation_evidence")
    remote_oid = str(remote.get("remote_oid") or _repair_field(request, "remote_oid"))
    ff = cond_blob(request, "fast_forward_repair_worktree")
    if not remote_oid:
        return fail("missing_repair_remote_oid", failure_class="terminal", retry_safe=False, operation="decide_repair_worktree_ownership")
    expected = {"task": context["task_id"], "issue": context["issue"], "repo": context["repo"], "pr": context["pr_number"], "receipt": context["receipt"], "remote_oid": remote_oid, "target_branch": context["branch"]}
    actual = dict(branch_read.get("provenance") or {})
    recover_branch = bool(branch_read.get("exists") and not any(actual.values()) and evidence.get("verified") is True and str(evidence.get("remote_oid") or "") == remote_oid and str(branch_read.get("branch_head") or "") == remote_oid)
    sync_verified = ff.get("status") in {"advanced", "inactive", "planned"} or not branch_read.get("exists")
    mismatches = [key for key, value in expected.items() if key != "remote_oid" and (value or key == "task") and actual.get(key) != value]
    if branch_read.get("exists") and not recover_branch and (mismatches or (actual.get("remote_oid") != remote_oid and not sync_verified)):
        return fail("foreign_repair_branch_ownership", failure_class="terminal", retry_safe=False, operation="decide_repair_worktree_ownership", expected=expected, actual=actual)
    rows = inventory.get("worktrees") if isinstance(inventory.get("worktrees"), list) else []
    target = Path(context["worktree_path"]).resolve()
    matching = [row for row in rows if isinstance(row, dict) and Path(str(row.get("path") or "")).resolve() == target]
    if matching and any(str(row.get("branch") or "") != context["local_branch"] for row in matching):
        return fail("repair_worktree_path_collision", failure_class="terminal", retry_safe=False, operation="decide_repair_worktree_ownership", worktree_path=str(target))
    if matching and str(matching[0].get("head") or "") != remote_oid and ff.get("status") not in {"advanced", "planned"}:
        return fail("stale_repair_remote_head", failure_class="terminal", retry_safe=False, operation="decide_repair_worktree_ownership", expected_head=remote_oid, actual_head=matching[0].get("head"))
    if (target.exists() or target.is_symlink()) and not matching:
        return fail("repair_worktree_path_collision", failure_class="terminal", retry_safe=False, operation="decide_repair_worktree_ownership", worktree_path=str(target))
    reuse = bool(branch_read.get("exists") and matching and str(matching[0].get("branch") or "") == context["local_branch"])
    return ok(status="reuse" if reuse else "recover" if recover_branch else "create", operation="decide_repair_worktree_ownership", should_prepare=True, reuse=reuse, recover_branch=recover_branch, remote_oid=remote_oid, expected=expected, **context)

def create_repair_branch(request: Request) -> Result:
     upstream = _repair_upstream(request, "create_repair_branch", "decide_repair_worktree_ownership")
     if upstream:
         return upstream
     decision = cond_blob(request, "decide_repair_worktree_ownership")
     context = _repair_context(request)
     if decision.get("recover_branch") is True:
         return ok(status="recovered", operation="create_repair_branch", mutated=False, remote_oid=str(decision.get("remote_oid") or ""), **context)
     if decision.get("reuse") is True:
         return ok(status="reused", operation="create_repair_branch", mutated=False, remote_oid=str(decision.get("remote_oid") or ""), **context)
     remote_oid = str(decision.get("remote_oid") or cond_blob(request, "read_repair_remote_head").get("remote_oid") or "")
     if not remote_oid:
         return fail("missing_repair_remote_oid", failure_class="terminal", retry_safe=False, operation="create_repair_branch")
     if dry_run_flag(request):
         return planned(operation="create_repair_branch", branch=context["branch"], local_branch=context["local_branch"], remote_oid=remote_oid)
     try:
         git(["branch", context["local_branch"], remote_oid], cwd=context["clone_path"])
     except CommandError as exc:
         return fail("repair_branch_create_failed", failure_class="terminal", retry_safe=False, operation="create_repair_branch", error=str(exc), mutated=False, **context)
     return ok(status="created", operation="create_repair_branch", mutated=True, remote_oid=remote_oid, **context)

def write_repair_branch_provenance(request: Request) -> Result:
     upstream = _repair_upstream(request, "write_repair_branch_provenance", "create_repair_branch")
     if upstream:
         return upstream
     context = _repair_context(request)
     if cond_blob(request, "create_repair_branch").get("status") == "reused":
         return ok(status="verified", operation="write_repair_branch_provenance", mutated=False, **context)
     decision = cond_blob(request, "decide_repair_worktree_ownership")
     values = {"task": context["task_id"], "issue": context["issue"], "repo": context["repo"], "pr": context["pr_number"], "receipt": context["receipt"], "remote_oid": str(decision.get("remote_oid") or ""), "target_branch": context["branch"]}
     if not all(values[key] for key in ("issue", "repo", "pr", "remote_oid", "target_branch")):
         return fail("missing_repair_provenance", failure_class="terminal", retry_safe=False, operation="write_repair_branch_provenance")
     if dry_run_flag(request):
         return planned(operation="write_repair_branch_provenance", branch=context["branch"], local_branch=context["local_branch"], provenance=values)
     try:
         for key, value in values.items():
             if value:
                 branch_config_set(context["clone_path"], context["local_branch"], f"lokay-{key}", value)
     except CommandError as exc:
         return fail("repair_provenance_write_failed", failure_class="retryable", retry_safe=True, operation="write_repair_branch_provenance", error=str(exc), mutated=True)
     return ok(status="written", operation="write_repair_branch_provenance", provenance=values, mutated=True, **context)

def add_repair_worktree(request: Request) -> Result:
     upstream = _repair_upstream(request, "add_repair_worktree", "create_repair_branch", "write_repair_branch_provenance")
     if upstream:
         return upstream
     context = _repair_context(request)
     decision = cond_blob(request, "decide_repair_worktree_ownership")
     if decision.get("reuse") is True:
         return ok(status="reused", operation="add_repair_worktree", worktree_path=context["worktree_path"], branch=context["branch"], local_branch=context["local_branch"], mutated=False)
     path, root = Path(context["worktree_path"]).resolve(), Path(context["worktree_root"]).resolve()
     try:
         path.relative_to(root)
     except ValueError:
         return fail("repair_worktree_path_escape", failure_class="terminal", retry_safe=False, operation="add_repair_worktree", worktree_path=str(path))
     if path.exists() or path.is_symlink():
         return fail("repair_worktree_path_collision", failure_class="terminal", retry_safe=False, operation="add_repair_worktree", worktree_path=str(path))
     if dry_run_flag(request):
         return planned(operation="add_repair_worktree", worktree_path=str(path), branch=context["branch"], local_branch=context["local_branch"])
     try:
         worktree_add(context["clone_path"], str(path), context["local_branch"], create_branch=False)
     except CommandError as exc:
         return fail("repair_worktree_add_failed", failure_class="terminal", retry_safe=False, operation="add_repair_worktree", error=str(exc), mutated=False)
     return ok(status="added", operation="add_repair_worktree", worktree_path=str(path), branch=context["branch"], local_branch=context["local_branch"], mutated=True)

def prepare_repair_worktree(request: Request) -> Result:
     """Mutation atom for the already-decided exact-head worktree add."""
     return add_repair_worktree(request)

def verify_repair_worktree(request: Request) -> Result:
     upstream = _repair_upstream(request, "verify_repair_worktree", "add_repair_worktree", "prepare_repair_worktree")
     if upstream:
         return upstream
     added = cond_blob(request, "add_repair_worktree", "prepare_repair_worktree")
     if dry_run_flag(request) and added.get("status") == "planned":
         return planned(operation="verify_repair_worktree", worktree_path=_repair_context(request)["worktree_path"])
     context = _repair_context(request)
     expected = str(cond_blob(request, "decide_repair_worktree_ownership").get("remote_oid") or cond_blob(request, "read_repair_remote_head").get("remote_oid") or _repair_field(request, "remote_oid"))
     path = context["worktree_path"]
     try:
         top = git(["rev-parse", "--show-toplevel"], cwd=path)
         branch = git(["branch", "--show-current"], cwd=path)
         head = rev_parse(path)
     except (CommandError, OSError) as exc:
         return fail("repair_worktree_readback_failed", failure_class="retryable_read", retry_safe=True, operation="verify_repair_worktree", error=str(exc), **context)
     if Path(top).resolve() != Path(path).resolve() or branch != context["local_branch"]:
         return fail("repair_worktree_confinement_failed", failure_class="terminal", retry_safe=False, operation="verify_repair_worktree", top_level=top, actual_branch=branch, **context)
     if not expected or head != expected:
         return fail("repair_worktree_head_mismatch", failure_class="terminal", retry_safe=False, operation="verify_repair_worktree", expected_head=expected, actual_head=head, **context)
     return ok(status="verified", operation="verify_repair_worktree", head=head, remote_oid=expected, actual_local_branch=branch, **context)

def verify_repair_worktree_head(request: Request):
     return verify_repair_worktree(request)
_REPAIR_CONTEXT_ALIASES = _REPAIR_CONTEXT_ALIASES + (
    "read_repair_omp_preconditions", "invoke_repair_omp", "verify_repair_omp_postconditions",
    "read_repair_worktree_head", "decide_repair_push", "push_repair_branch",
    "read_repair_pushed_ref", "verify_repair_push_oid", "update_repair_branch_provenance",
    "verify_updated_repair_branch_provenance", "read_existing_repair_pr",
    "verify_existing_repair_pr", "build_repair_receipt", "publish_repair_receipt", "verify_repair_receipt",
)

import json
from lokay.adapters_omp import run_omp
from lokay.adapters_git import push_branch as git_push_branch
from lokay.steps.cleanup import _publish_cleanup_receipt, _receipt_directory_lock


def _repair_execution_gate(request: Request, operation: str, *peers: str) -> Result | None:
    """Require the explicit live authorization atom before any execution."""
    gated = _repair_decision_gate(request)
    if gated is not None:
        return gated
    terminal = _atomic_terminal(request, operation, "decide_repair_attempt", *peers)
    if terminal:
        return terminal
    decision = cond_blob(request, "decide_repair_attempt")
    if not decision:
        return noop("repair_attempt_not_authorized", operation=operation)
    if decision.get("authorize") is not True:
        return noop(str(decision.get("reason") or decision.get("decision") or "repair_attempt_not_authorized"), operation=operation)
    return None


def read_repair_omp_preconditions(request: Request) -> Result:
    gated = _repair_execution_gate(request, "read_repair_omp_preconditions", "verify_repair_worktree", "verify_repair_worktree_head")
    if gated:
        return gated
    upstream = _repair_upstream(request, "read_repair_omp_preconditions", "verify_repair_worktree", "verify_repair_worktree_head")
    if upstream:
        return upstream
    context = _repair_context(request)
    path = context["worktree_path"]
    try:
        top = git(["rev-parse", "--show-toplevel"], cwd=path)
        branch = git(["branch", "--show-current"], cwd=path)
        head = rev_parse(path)
    except (CommandError, OSError) as exc:
        return fail("repair_omp_precondition_failed", failure_class="terminal", retry_safe=False, operation="read_repair_omp_preconditions", error=str(exc), **context)
    if Path(top).resolve() != Path(path).resolve():
        return fail("repair_omp_worktree_confinement", failure_class="terminal", retry_safe=False, operation="read_repair_omp_preconditions", top_level=top, **context)
    if branch != context["local_branch"]:
        return fail("repair_omp_branch_mismatch", failure_class="terminal", retry_safe=False, operation="read_repair_omp_preconditions", expected_branch=context["local_branch"], actual_branch=branch, **context)
    expected = str(cond_blob(request, "verify_repair_worktree", "verify_repair_worktree_head").get("head") or cond_blob(request, "decide_repair_worktree_ownership").get("remote_oid") or cond_blob(request, "read_repair_remote_head").get("remote_oid") or "")
    if expected and head != expected:
        return fail("repair_omp_head_mismatch", failure_class="terminal", retry_safe=False, operation="read_repair_omp_preconditions", expected_head=expected, actual_head=head, **context)
    return ok(**{**context, "status": "ready", "operation": "read_repair_omp_preconditions", "pre_head": head, "worktree_path": path, "remote_oid": expected})


def invoke_repair_omp(request: Request) -> Result:
    peers = ("read_repair_omp_preconditions", "verify_repair_attempt_reservation", "build_repair_prompt")
    gated = _repair_execution_gate(request, "invoke_repair_omp", *peers)
    if gated:
        return gated
    upstream = _repair_upstream(request, "invoke_repair_omp", *peers)
    if upstream:
        return upstream
    reservation = cond_blob(request, "verify_repair_attempt_reservation")
    if reservation.get("verified") is not True:
        return fail("repair_attempt_reservation_required", failure_class="terminal", retry_safe=False, operation="invoke_repair_omp")
    data, cfg = input_of(request), cfg_of(request)
    pre = cond_blob(request, "read_repair_omp_preconditions")
    path = str(data.get("worktree_path") or pre.get("worktree_path") or _repair_context(request)["worktree_path"])
    prompt = str(data.get("prompt") or cond_blob(request, "build_repair_prompt").get("prompt") or "")
    if not path or not prompt:
        return fail("missing_repair_worktree_or_prompt", failure_class="terminal", retry_safe=False, operation="invoke_repair_omp")
    pre_head = str(pre.get("pre_head") or "")
    try:
        actual_pre_head = rev_parse(path)
        pre_status = git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=path)
    except (CommandError, OSError) as exc:
        return fail("repair_omp_precondition_failed", failure_class="terminal", retry_safe=False, operation="invoke_repair_omp", error=str(exc), mutated=False)
    if not pre_head or actual_pre_head != pre_head or pre_status:
        return fail("repair_omp_precondition_failed", failure_class="terminal", retry_safe=False, operation="invoke_repair_omp", expected_head=pre_head, actual_head=actual_pre_head, dirty=bool(pre_status), mutated=False)
    try:
        timeout = float(data.get("timeout_seconds") or cfg.get("timeout_seconds") or 1800)
    except (TypeError, ValueError) as exc:
        return fail("invalid_repair_omp_timeout", failure_class="terminal", retry_safe=False, operation="invoke_repair_omp", error=str(exc), mutated=False)
    if not 0 < timeout <= MAX_EXECUTOR_TIMEOUT_SECONDS:
        return fail("invalid_repair_omp_timeout", failure_class="terminal", retry_safe=False, operation="invoke_repair_omp", timeout_seconds=timeout, mutated=False)
    res_path_str = str(reservation.get("reservation_path") or "").strip()
    process_id = str(request.get("process_id") or "").strip()
    if not res_path_str or not process_id:
        return fail("repair_invoke_evidence_identity_required", failure_class="terminal", retry_safe=False, operation="invoke_repair_omp", mutated=False)
    res_path = Path(res_path_str)
    invoke_path = _repair_invoke_evidence_path(res_path, process_id)
    pre_info = {
        "kind": "repair_invoke_evidence",
        "process_id": process_id,
        "status": "started",
        "pre_head": pre_head,
        "pre_status": pre_status,
        "mutated": None,
    }
    try:
        _write_invoke_evidence(invoke_path, pre_info, exclusive=True)
    except OSError as exc:
        return fail("repair_invoke_evidence_write_failed", failure_class="terminal", retry_safe=False, operation="invoke_repair_omp", error=str(exc), mutated=False)
    try:
        out = run_omp(prompt=prompt, cwd=path, command=str(data.get("command") or cfg.get("executor_command") or "omp"), model=str(data.get("model") or cfg.get("model") or "omniroute/omp/default"), thinking=str(data.get("thinking") or cfg.get("thinking") or "medium"), timeout=timeout, dry_run=False)
    except (CommandError, subprocess.TimeoutExpired) as exc:
        evidence_status = "timed_out" if isinstance(exc, subprocess.TimeoutExpired) else "failed"
        try:
            post_head = rev_parse(path)
            post_status = git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=path)
            mutated = post_head != pre_head or post_status != pre_status
        except (CommandError, OSError):
            post_head = ""
            post_status = ""
            mutated = True
        try:
            post_info = {
                "kind": "repair_invoke_evidence",
                "process_id": process_id,
                "status": evidence_status,
                "pre_head": pre_head,
                "pre_status": pre_status,
                "post_head": post_head,
                "post_status": post_status,
                "mutated": mutated,
                "error": str(exc),
            }
            _write_invoke_evidence(invoke_path, post_info)
        except OSError as write_exc:
            return fail("repair_invoke_evidence_write_failed", failure_class="terminal", retry_safe=False, operation="invoke_repair_omp", error=str(write_exc), mutated=True)
        return fail("repair_omp_failed", failure_class="terminal", retry_safe=False, operation="invoke_repair_omp", error=str(exc), pre_head=pre_head, mutated=mutated)
    try:
        post_head = rev_parse(path)
        post_status = git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=path)
        mutated_success = post_head != pre_head or post_status != pre_status
    except (CommandError, OSError):
        post_head = ""
        post_status = ""
        mutated_success = True
    try:
        post_info = {
            "kind": "repair_invoke_evidence",
            "process_id": process_id,
            "status": "succeeded",
            "pre_head": pre_head,
            "pre_status": pre_status,
            "post_head": post_head,
            "post_status": post_status,
            "mutated": mutated_success,
        }
        _write_invoke_evidence(invoke_path, post_info)
    except OSError as exc:
        return fail("repair_invoke_evidence_write_failed", failure_class="terminal", retry_safe=False, operation="invoke_repair_omp", error=str(exc), mutated=True)
    decision = cond_blob(request, "decide_repair_attempt")
    pre_head = str(pre.get("pre_head") or decision.get("verified_head") or "")
    attempt_state = {
        "repo": decision.get("repo") or _repair_context(request)["repo"],
        "pr_number": decision.get("pr_number") or _repair_context(request)["pr_number"],
        "verified_head": decision.get("verified_head") or pre_head,
        "pre_head": pre_head,
        "candidate": decision.get("candidate") or data.get("candidate") or "",
        "run_id": decision.get("run_id") or data.get("run_id") or "",
        "checks": decision.get("checks") or data.get("checks") or [],
        "status": "invoked",
        "attempted": True,
    }
    return ok(status="invoked", operation="invoke_repair_omp", omp=out, omp_process_id=process_id, worktree_path=path, pre_head=pre_head, attempt_state=attempt_state, mutated=mutated_success)


def verify_repair_omp_postconditions(request: Request) -> Result:
    gated = _repair_execution_gate(request, "verify_repair_omp_postconditions", "invoke_repair_omp")
    if gated:
        return gated
    upstream = _repair_upstream(request, "verify_repair_omp_postconditions", "invoke_repair_omp", "read_repair_omp_preconditions")
    if upstream:
        return upstream
    data = input_of(request)
    source = cond_blob(request, "read_repair_omp_preconditions")
    path = str(data.get("worktree_path") or source.get("worktree_path") or _repair_context(request)["worktree_path"])
    before = str(data.get("before_oid") or source.get("pre_head") or "")
    try:
        top = git(["rev-parse", "--show-toplevel"], cwd=path)
        head = rev_parse(path)
        changed = git(["diff", "--name-only", before, "HEAD"], cwd=path) if before else ""
    except (CommandError, OSError) as exc:
        return fail("repair_omp_postcondition_failed", failure_class="terminal", retry_safe=False, operation="verify_repair_omp_postconditions", error=str(exc))
    if Path(top).resolve() != Path(path).resolve():
        return fail("repair_omp_worktree_confinement", failure_class="terminal", retry_safe=False, operation="verify_repair_omp_postconditions", top_level=top)
    if before and head == before:
        return fail("repair_omp_head_unchanged", failure_class="terminal", retry_safe=False, operation="verify_repair_omp_postconditions", before_oid=before, after_oid=head)
    if before and not changed:
        return fail("repair_omp_no_changes", failure_class="terminal", retry_safe=False, operation="verify_repair_omp_postconditions", before_oid=before, after_oid=head)
    return ok(status="verified", operation="verify_repair_omp_postconditions", before_oid=before, after_oid=head, changed_paths=changed.splitlines(), omp=cond_blob(request, "invoke_repair_omp").get("omp"), omp_process_id=cond_blob(request, "invoke_repair_omp").get("omp_process_id"), mutated=False)


def read_repair_worktree_head(request: Request) -> Result:
    gated = _repair_execution_gate(request, "read_repair_worktree_head", "verify_repair_omp_postconditions")
    if gated:
        return gated
    upstream = _repair_upstream(request, "read_repair_worktree_head", "verify_repair_omp_postconditions")
    if upstream:
        return upstream
    path = str(input_of(request).get("worktree_path") or cond_blob(request, "verify_repair_omp_postconditions").get("worktree_path") or _repair_context(request)["worktree_path"])
    try:
        head = rev_parse(path)
    except CommandError as exc:
        return fail("repair_worktree_head_read_failed", failure_class="retryable_read", retry_safe=True, operation="read_repair_worktree_head", error=str(exc))
    return ok(status="read", operation="read_repair_worktree_head", local_oid=head, after_oid=head, worktree_path=path)


def decide_repair_push(request: Request) -> Result:
    gated = _repair_execution_gate(request, "decide_repair_push", "read_repair_worktree_head")
    if gated:
        return gated
    upstream = _repair_upstream(request, "decide_repair_push", "read_repair_worktree_head", "verify_repair_omp_postconditions")
    if upstream:
        return upstream
    local = str(cond_blob(request, "read_repair_worktree_head").get("local_oid") or input_of(request).get("local_oid") or "")
    before = str(input_of(request).get("before_oid") or cond_blob(request, "read_repair_omp_postconditions").get("before_oid") or cond_blob(request, "read_repair_remote_head").get("remote_oid") or "")
    if not local or not before:
        return fail("missing_repair_push_oids", failure_class="terminal", retry_safe=False, operation="decide_repair_push")
    if local == before:
        return fail("repair_head_unchanged", failure_class="terminal", retry_safe=False, operation="decide_repair_push", before_oid=before, local_oid=local)
    return ok(status="push", operation="decide_repair_push", should_push=True, before_oid=before, local_oid=local, branch=_repair_context(request)["branch"])


def push_repair_branch(request: Request) -> Result:
    gated = _repair_execution_gate(request, "push_repair_branch", "decide_repair_push")
    if gated:
        return gated
    upstream = _repair_upstream(request, "push_repair_branch", "decide_repair_push")
    if upstream:
        return upstream
    decision = cond_blob(request, "decide_repair_push")
    if decision.get("should_push") is not True:
        return noop("repair_push_not_authorized", operation="push_repair_branch")
    context = _repair_context(request)
    try:
        out = git_push_branch(context["worktree_path"], context["branch"], remote=context["remote"], set_upstream=False)
    except CommandError as exc:
        return fail("repair_push_failed", failure_class="reconcile_then_retry", retry_safe=False, operation="push_repair_branch", error=str(exc), mutated=True, **context)
    return ok(status="pushed", operation="push_repair_branch", stdout_tail=out[-400:], mutated=True, **context)


def read_repair_pushed_ref(request: Request) -> Result:
    gated = _repair_execution_gate(request, "read_repair_pushed_ref", "push_repair_branch")
    if gated:
        return gated
    upstream = _repair_upstream(request, "read_repair_pushed_ref", "push_repair_branch")
    if upstream:
        return upstream
    context = _repair_context(request)
    try:
        text = git(["ls-remote", context["remote"], f"refs/heads/{context['branch']}"], cwd=context["worktree_path"])
    except CommandError as exc:
        return fail("repair_push_readback_failed", failure_class="retryable_read", retry_safe=True, operation="read_repair_pushed_ref", error=str(exc), **context)
    rows = [line.split() for line in text.splitlines() if line.split()]
    if len(rows) != 1 or len(rows[0]) < 2 or rows[0][1] != f"refs/heads/{context['branch']}":
        return fail("repair_pushed_ref_missing", failure_class="terminal", retry_safe=False, operation="read_repair_pushed_ref", output=text, **context)
    return ok(status="read", operation="read_repair_pushed_ref", remote_oid=rows[0][0], **context)


def verify_repair_push_oid(request: Request) -> Result:
    gated = _repair_execution_gate(request, "verify_repair_push_oid", "read_repair_pushed_ref")
    if gated:
        return gated
    upstream = _repair_upstream(request, "verify_repair_push_oid", "read_repair_pushed_ref", "decide_repair_push")
    if upstream:
        return upstream
    local = str(cond_blob(request, "decide_repair_push").get("local_oid") or cond_blob(request, "read_repair_worktree_head").get("local_oid") or "")
    remote = str(cond_blob(request, "read_repair_pushed_ref").get("remote_oid") or "")
    if not local or not remote:
        return fail("missing_repair_push_oids", failure_class="terminal", retry_safe=False, operation="verify_repair_push_oid")
    if local != remote:
        return fail("repair_push_readback_mismatch", failure_class="terminal", retry_safe=False, operation="verify_repair_push_oid", local_oid=local, remote_oid=remote, mutated=True)
    return ok(status="verified", operation="verify_repair_push_oid", local_oid=local, remote_oid=remote, mutated=False)

def update_repair_branch_provenance(request: Request) -> Result:
    peers = ("verify_repair_receipt", "verify_repair_push_oid")
    gated = _repair_execution_gate(request, "update_repair_branch_provenance", *peers)
    if gated:
        return gated
    upstream = _repair_upstream(request, "update_repair_branch_provenance", *peers)
    if upstream:
        return upstream
    context = _repair_context(request)
    remote_oid = str(cond_blob(request, "verify_repair_push_oid").get("remote_oid") or "")
    values = {"task": context["task_id"], "issue": context["issue"], "repo": context["repo"], "pr": context["pr_number"], "receipt": context["receipt"], "repair_receipt": str(cond_blob(request, "verify_repair_receipt").get("receipt_path") or ""), "remote_oid": remote_oid, "target_branch": context["branch"]}
    if not all(values[key] for key in ("issue", "repo", "pr", "receipt", "repair_receipt", "remote_oid", "target_branch")):
        return fail("missing_repair_provenance", failure_class="terminal", retry_safe=False, operation="update_repair_branch_provenance")
    if dry_run_flag(request):
        return planned(operation="update_repair_branch_provenance", branch=context["branch"], local_branch=context["local_branch"], provenance=values)
    try:
        for key, value in values.items():
            if value:
                branch_config_set(context["clone_path"], context["local_branch"], f"lokay-{key}", value)
            else:
                branch_config_unset(context["clone_path"], context["local_branch"], f"lokay-{key}")
    except CommandError as exc:
        return fail("repair_provenance_update_failed", failure_class="reconcile_then_retry", retry_safe=False, operation="update_repair_branch_provenance", error=str(exc), mutated=True)
    return ok(status="updated", operation="update_repair_branch_provenance", provenance=values, mutated=True, **context)


def verify_updated_repair_branch_provenance(request: Request) -> Result:
    upstream = _repair_upstream(request, "verify_updated_repair_branch_provenance", "update_repair_branch_provenance")
    if upstream:
        return upstream
    updated = cond_blob(request, "update_repair_branch_provenance")
    expected = updated.get("provenance")
    context = _repair_context(request)
    if not isinstance(expected, dict):
        return fail("missing_repair_provenance", failure_class="terminal", retry_safe=False, operation="verify_updated_repair_branch_provenance")
    if dry_run_flag(request):
        return planned(operation="verify_updated_repair_branch_provenance", branch=context["branch"], local_branch=context["local_branch"])
    try:
        actual = {}
        for key in expected:
            try:
                actual[key] = branch_config_get(context["clone_path"], context["local_branch"], f"lokay-{key}").strip()
            except CommandError:
                if key != "task":
                    raise
                actual[key] = ""
    except CommandError as exc:
        return fail("repair_provenance_readback_failed", failure_class="retryable_read", retry_safe=True, operation="verify_updated_repair_branch_provenance", error=str(exc))
    if actual != expected:
        return fail("repair_provenance_readback_mismatch", failure_class="terminal", retry_safe=False, operation="verify_updated_repair_branch_provenance", expected=expected, actual=actual)
    return ok(status="verified", operation="verify_updated_repair_branch_provenance", provenance=actual, mutated=False, **context)



def read_existing_repair_pr(request: Request) -> Result:
    gated = _repair_execution_gate(request, "read_existing_repair_pr", "verify_repair_push_oid")
    if gated:
        return gated
    upstream = _repair_upstream(request, "read_existing_repair_pr", "verify_repair_push_oid")
    if upstream:
        return upstream
    context = _repair_context(request)
    cfg = cfg_of(request)
    gh = str(cfg.get("gh_cli") or "gh")
    try:
        proc = run_cmd([gh, "pr", "list", "--repo", context["repo"], "--head", context["branch"], "--state", "open", "--json", "number,headRefName,headRefOid,baseRefName,repository,url"], timeout=120)
        rows = json.loads(proc.stdout or "[]")
    except (CommandError, json.JSONDecodeError, ValueError) as exc:
        return fail("repair_pr_read_failed", failure_class="retryable_read", retry_safe=True, operation="read_existing_repair_pr", error=str(exc), **context)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return fail("invalid_repair_pr_readback", failure_class="terminal", retry_safe=False, operation="read_existing_repair_pr", **context)
    if len(rows) != 1:
        return fail("existing_repair_pr_missing" if not rows else "ambiguous_existing_repair_pr", failure_class="terminal", retry_safe=False, operation="read_existing_repair_pr", prs=rows, **context)
    return ok(status="read", operation="read_existing_repair_pr", pr=rows[0], **context)


def verify_existing_repair_pr(request: Request) -> Result:
    gated = _repair_execution_gate(request, "verify_existing_repair_pr", "read_existing_repair_pr")
    if gated:
        return gated
    upstream = _repair_upstream(request, "verify_existing_repair_pr", "read_existing_repair_pr", "verify_repair_push_oid")
    if upstream:
        return upstream
    context = _repair_context(request)
    pr = cond_blob(request, "read_existing_repair_pr").get("pr") or {}
    expected_number = int(context["pr_number"] or 0)
    expected_oid = str(cond_blob(request, "verify_repair_push_oid").get("remote_oid") or "")
    repo_value = pr.get("repository") if isinstance(pr, dict) else None
    actual_repo = repo_value.get("nameWithOwner") if isinstance(repo_value, dict) else repo_value
    actual_number = pr.get("number") if isinstance(pr, dict) else None
    try:
        actual_number_int = int(actual_number or 0)
    except (TypeError, ValueError):
        return fail("repair_pr_readback_mismatch", failure_class="terminal", retry_safe=False, operation="verify_existing_repair_pr", expected={"repo": context["repo"], "number": expected_number, "branch": context["branch"], "head": expected_oid}, actual=pr)
    if str(actual_repo or "") != context["repo"] or actual_number_int != expected_number or str(pr.get("headRefName") or "") != context["branch"] or (expected_oid and str(pr.get("headRefOid") or "") != expected_oid):
        return fail("repair_pr_readback_mismatch", failure_class="terminal", retry_safe=False, operation="verify_existing_repair_pr", expected={"repo": context["repo"], "number": expected_number, "branch": context["branch"], "head": expected_oid}, actual=pr)
    return ok(status="verified", operation="verify_existing_repair_pr", repo=context["repo"], pr_number=expected_number, branch=context["branch"], head_oid=expected_oid, pr=pr, mutated=False)


def _legacy_refresh_source(request: Request, *names: str) -> object:
    data, cfg = input_of(request), cfg_of(request)
    sources: list[object] = [data, cfg]
    for name in names:
        blob = cond_blob(request, name)
        if blob:
            sources.append(blob)
    for source in sources:
        if isinstance(source, dict):
            for key in ("pr", "attempt_state", "reservation", "repo", "pr_number", "number", "branch", "headRefName", "base_branch", "baseRefName", "verified_head", "headRefOid"):
                if key in source and source[key] not in (None, "", {}):
                    return source[key]
    return None


def _legacy_refresh_pr(request: Request) -> dict[str, object] | None:
    data = input_of(request)
    for source in (data.get("pr"), cond_blob(request, "load_pr_fields").get("pr"), cond_blob(request, "triage_load_pr_fields").get("pr"), cond_blob(request, "read_existing_repair_pr").get("pr"), cond_blob(request, "read_legacy_repair_pr").get("pr")):
        if isinstance(source, dict):
            return source
    return None

def _legacy_refresh_state(request: Request) -> dict[str, object] | None:
    data = input_of(request)
    conducted = cond_blob(request, "read_repair_attempt_state")
    if conducted:
        if str(conducted.get("status") or "") != "found" or conducted.get("ok") is False:
            return None
        source = conducted.get("attempt_state") or conducted.get("reservation") or conducted.get("state")
        if not isinstance(source, dict) or source.get("attempted") is not True or str(source.get("status") or "") not in {"reserved", "invoked"}:
            return None
        return source
    for source in (data.get("attempt_state"), data.get("repair_attempt"), data.get("reservation"), cond_blob(request, "read_legacy_repair_attempt_state").get("attempt_state")):
        if isinstance(source, dict):
            return source
    return None


def _legacy_refresh_value(request: Request, *keys: str) -> str:
    data, cfg = input_of(request), cfg_of(request)
    state = _legacy_refresh_state(request)
    pr = _legacy_refresh_pr(request)
    blobs = [data, cfg, state, pr, cond_blob(request, "read_repair_context"), cond_blob(request, "read_repair_remote_head"), cond_blob(request, "read_repair_base_head"), cond_blob(request, "decide_legacy_repair_head_refresh")]
    for blob in blobs:
        for key in keys:
            if isinstance(blob, dict) and blob.get(key) not in (None, ""):
                return str(blob[key]).strip()
    return ""


def read_repair_base_head(request: Request) -> Result:
    gated = _repair_decision_gate(request)
    if gated is not None:
        return gated
    upstream = _repair_upstream(request, "read_repair_base_head", "read_repair_attempt_state", "read_repair_context", "read_repair_remote_head")
    if upstream:
        return upstream
    data, cfg = input_of(request), cfg_of(request)
    repo = str(data.get("repo") or cfg.get("repo") or "")
    state_read = cond_blob(request, "read_repair_attempt_state")
    terminal = _atomic_terminal(request, "read_repair_base_head", "read_repair_attempt_state")
    if terminal is not None:
        return terminal
    if state_read.get("status") == "absent":
        return ok(status="inactive", operation="read_repair_base_head", reason="no_repair_reservation", mutated=False)
    if state_read.get("status") == "found":
        state = state_read.get("attempt_state")
        if not isinstance(state, dict) or state.get("attempted") is not True or str(state.get("status") or "") not in {"reserved", "invoked"}:
            return fail("legacy_repair_reservation_invalid_state", failure_class="terminal", retry_safe=False, operation="read_repair_base_head", mutated=False)
    base = str(data.get("base_branch") or cfg.get("base_branch") or "")
    if not repo:
        repo = _legacy_refresh_value(request, "repo")
    if not base:
        base = _legacy_refresh_value(request, "base_branch", "baseRefName")
    if not repo or not base:
        return fail("missing_repair_base_identity", failure_class="terminal", retry_safe=False, operation="read_repair_base_head")
    gh = str(cfg_of(request).get("gh_cli") or "gh")
    try:
        proc = run_cmd([gh, "api", f"repos/{repo}/git/ref/heads/{base}", "--jq", ".object.sha"], timeout=120)
        oid = (proc.stdout or "").strip()
    except (CommandError, OSError) as exc:
        return fail("base_ref_read_failed", failure_class="retryable_read", retry_safe=True, operation="read_repair_base_head", error=str(exc), repo=repo, base_branch=base)
    if not re.fullmatch(r"[0-9a-fA-F]{40}", oid):
        return fail("repair_base_head_invalid", failure_class="terminal", retry_safe=False, operation="read_repair_base_head", repo=repo, base_branch=base, output=oid)
    return ok(status="read", operation="read_repair_base_head", repo=repo, base_branch=base, base_ref_oid=oid, mutated=False)


def decide_legacy_repair_head_refresh(request: Request) -> Result:
    """Pure, exact authorization for refreshing a stale legacy PR base."""
    gated = _repair_decision_gate(request)
    if gated is not None:
        return gated
    terminal = _atomic_terminal(
        request,
        "decide_legacy_repair_head_refresh",
        "read_repair_attempt_state",
        "read_repair_base_head",
        "load_pr_fields",
    )
    if terminal is not None:
        return terminal
    action = str(input_of(request).get("action") or cond_blob(request, "decide_triage_action", "decide").get("action") or "")
    state_read = cond_blob(request, "read_repair_attempt_state")
    if state_read.get("status") == "found":
        nested = state_read.get("attempt_state")
        if not isinstance(nested, dict) or nested.get("attempted") is not True or str(nested.get("status") or "") not in {"reserved", "invoked"}:
            return fail("legacy_repair_reservation_invalid_state", failure_class="terminal", retry_safe=False, operation="decide_legacy_repair_head_refresh", mutated=False)
    state = _legacy_refresh_state(request)
    if action != "repair":
        return ok(status="inactive", operation="decide_legacy_repair_head_refresh", should_refresh=False, reason="not_repair", mutated=False)
    if state is None:
        return ok(status="inactive", operation="decide_legacy_repair_head_refresh", should_refresh=False, reason="no_legacy_reservation", mutated=False)
    keys = ("pre_head", "pre_status", "repo_branch", "local_branch", "worktree_path")
    present = [key for key in keys if key in state]
    if present and len(present) != len(keys):
        return fail("legacy_repair_reservation_partial_baseline", failure_class="terminal", retry_safe=False, operation="decide_legacy_repair_head_refresh", present_baseline=present, mutated=False)
    if present:
        return ok(status="inactive", operation="decide_legacy_repair_head_refresh", should_refresh=False, reason="nonlegacy_reservation", mutated=False)
    repo = str(state.get("repo") or _legacy_refresh_value(request, "repo"))
    try:
        number = int(state.get("pr_number") or _legacy_refresh_value(request, "pr_number", "number") or 0)
    except (TypeError, ValueError):
        number = 0
    branch = _legacy_refresh_value(request, "branch", "headRefName")
    base = _legacy_refresh_value(request, "base_branch", "baseRefName")
    old_head = str(state.get("verified_head") or _legacy_refresh_value(request, "verified_head", "headRefOid"))
    pr = _legacy_refresh_pr(request)
    base_read = cond_blob(request, "read_repair_base_head")
    live_base = str(base_read.get("base_ref_oid") or "")
    if not repo or number <= 0 or not branch or not base or not old_head or not isinstance(pr, dict) or not live_base:
        return fail("legacy_repair_identity_missing", failure_class="terminal", retry_safe=False, operation="decide_legacy_repair_head_refresh", mutated=False)
    repository = pr.get("repository")
    loaded = cond_blob(request, "load_pr_fields", "triage_load_pr_fields")
    actual_repo = repository.get("nameWithOwner") if isinstance(repository, dict) else repository or loaded.get("repo")
    try:
        actual_number = int(pr.get("number") or 0)
    except (TypeError, ValueError):
        actual_number = 0
    actual = {"repo": actual_repo or "", "number": actual_number, "state": str(pr.get("state") or "").upper(), "branch": pr.get("headRefName"), "base": pr.get("baseRefName"), "head": pr.get("headRefOid")}
    expected = {"repo": repo, "number": number, "state": "OPEN", "branch": branch, "base": base, "head": old_head}
    if actual != expected:
        return fail("legacy_repair_pr_identity_mismatch", failure_class="terminal", retry_safe=False, operation="decide_legacy_repair_head_refresh", expected=expected, actual=actual, mutated=False)
    observed_base = str(pr.get("baseRefOid") or "")
    if observed_base == live_base:
        return ok(status="inactive", operation="decide_legacy_repair_head_refresh", should_refresh=False, reason="base_current", mutated=False)
    return ok(status="refresh", operation="decide_legacy_repair_head_refresh", should_refresh=True, refresh_kind="legacy_base_synchronization", repo=repo, pr_number=number, branch=branch, base_branch=base, old_head=old_head, observed_base_ref_oid=observed_base, authoritative_base_ref_oid=live_base, mutated=False)


def update_legacy_repair_pr_branch(request: Request) -> Result:
    """Merge the live base into the legacy PR branch with optimistic head protection."""
    gated = _repair_decision_gate(request)
    if gated is not None:
        return gated
    terminal = _atomic_terminal(request, "update_legacy_repair_pr_branch", "decide_legacy_repair_head_refresh")
    if terminal:
        return terminal
    decision = cond_blob(request, "decide_legacy_repair_head_refresh")
    if decision.get("status") == "inactive" or decision.get("should_refresh") is not True:
        return noop(str(decision.get("reason") or "legacy_refresh_inactive"), operation="update_legacy_repair_pr_branch", refresh_kind="legacy_base_synchronization")
    if dry_run_flag(request):
        return planned(operation="update_legacy_repair_pr_branch", refresh_kind="legacy_base_synchronization", expected_head_sha=decision["old_head"], update_method="merge", repo=decision["repo"], pr_number=decision["pr_number"])
    gh = str(cfg_of(request).get("gh_cli") or "gh")
    cmd = [gh, "api", "--method", "PUT", f"repos/{decision['repo']}/pulls/{decision['pr_number']}/update-branch", "-f", f"expected_head_sha={decision['old_head']}", "-f", "update_method=merge"]
    try:
        proc = run_cmd(cmd, timeout=120)
    except (CommandError, OSError) as exc:
        return fail("legacy_repair_base_refresh_failed", failure_class="reconcile_then_retry", retry_safe=False, operation="update_legacy_repair_pr_branch", error=str(exc), mutated=True)
    return ok(status="updated", operation="update_legacy_repair_pr_branch", refresh_kind="legacy_base_synchronization", expected_head_sha=decision["old_head"], stdout_tail=(proc.stdout or "")[-400:], mutated=True)


def verify_legacy_repair_pr_head(request: Request) -> Result:
    """Separately read back the PR and prove a changed head and exact identity/base."""
    gated = _repair_decision_gate(request)
    if gated is not None:
        return gated
    terminal = _atomic_terminal(request, "verify_legacy_repair_pr_head", "decide_legacy_repair_head_refresh", "update_legacy_repair_pr_branch")
    if terminal:
        return terminal
    update = cond_blob(request, "update_legacy_repair_pr_branch")
    decision = cond_blob(request, "decide_legacy_repair_head_refresh")
    if update.get("status") == "noop" or decision.get("status") == "inactive":
        return ok(status="inactive", operation="verify_legacy_repair_pr_head", reason=str(decision.get("reason") or "legacy_refresh_inactive"), mutated=False)
    if update.get("status") == "planned" or dry_run_flag(request):
        return planned(operation="verify_legacy_repair_pr_head", refresh_kind="legacy_base_synchronization")
    gh = str(cfg_of(request).get("gh_cli") or "gh")
    try:
        proc = run_cmd([gh, "pr", "view", str(decision["pr_number"]), "--repo", str(decision["repo"]), "--json", "number,state,headRefName,headRefOid,baseRefName,baseRefOid"], timeout=120)
        pr = json.loads(proc.stdout or "")
    except (CommandError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("legacy_repair_pr_readback_failed", failure_class="reconcile_then_retry", retry_safe=False, operation="verify_legacy_repair_pr_head", error=str(exc), mutated=True)
    try:
        actual_number = int(pr.get("number") or 0) if isinstance(pr, dict) else 0
    except (TypeError, ValueError):
        actual_number = 0
    actual_head = str(pr.get("headRefOid") or "") if isinstance(pr, dict) else ""
    valid = isinstance(pr, dict) and actual_number == int(decision["pr_number"]) and str(pr.get("state") or "").upper() == "OPEN" and str(pr.get("headRefName") or "") == str(decision["branch"]) and str(pr.get("baseRefName") or "") == str(decision["base_branch"]) and actual_head and actual_head != str(decision["old_head"]) and str(pr.get("baseRefOid") or "") == str(decision["authoritative_base_ref_oid"])
    if not valid:
        return fail("legacy_repair_pr_readback_mismatch", failure_class="terminal", retry_safe=False, operation="verify_legacy_repair_pr_head", expected=decision, actual=pr, mutated=True)
    return ok(status="refreshed", operation="verify_legacy_repair_pr_head", refresh_kind="legacy_base_synchronization", repo=decision["repo"], pr_number=decision["pr_number"], branch=decision["branch"], base_branch=decision["base_branch"], old_head=decision["old_head"], new_head=actual_head, base_ref_oid=pr["baseRefOid"], pr=pr, mutated=False)


def _repair_attempt_receipt(request: Request, payload: dict[str, Any]) -> str:
    provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
    identity = {
        "repo": provenance.get("repo"),
        "pr_number": provenance.get("pr_number"),
        "verified_head": payload.get("before_oid"),
    }
    path = _repair_completed_receipt_path(request, identity)
    return str(path) if path is not None else ""

def build_repair_receipt(request: Request) -> Result:
    peers = ("verify_existing_repair_pr", "verify_repair_push_oid", "verify_repair_omp_postconditions", "invoke_repair_omp")
    gated = _repair_execution_gate(request, "build_repair_receipt", *peers)
    if gated:
        return gated
    upstream = _repair_upstream(request, "build_repair_receipt", *peers)
    if upstream:
        return upstream
    data, cfg = input_of(request), cfg_of(request)
    context = _repair_context(request)
    pr = cond_blob(request, "verify_existing_repair_pr")
    pushed = cond_blob(request, "verify_repair_push_oid")
    omp = cond_blob(request, "verify_repair_omp_postconditions")
    decision = cond_blob(request, "decide_repair_attempt")
    payload = {
        "phase": "REPAIR_COMPLETED",
        "before_oid": str(decision.get("verified_head") or omp.get("before_oid") or context.get("remote_oid") or ""),
        "after_oid": str(pushed.get("remote_oid") or pr.get("head_oid") or ""),
        "checks": decision.get("checks") or data.get("checks") or [],
        "omp": omp.get("omp") or cond_blob(request, "invoke_repair_omp").get("omp") or {},
        "run": {"run_id": decision.get("run_id") or data.get("run_id") or "", "status": "completed"},
        "omp_process_id": str(omp.get("omp_process_id") or cond_blob(request, "invoke_repair_omp").get("omp_process_id") or ""),
        "receipt_process_id": str(decision.get("process_id") or request.get("process_id") or ""),
        "candidate": decision.get("candidate") or data.get("candidate") or "",
        "config": dict(cfg),
        "provenance": {"repo": context["repo"], "pr_number": int(context["pr_number"]), "branch": context["branch"], "local_branch": context["local_branch"], "worktree_path": context["worktree_path"], "task_id": context["task_id"], "issue": context["issue"], "receipt": context["receipt"]},
        "pr": pr.get("pr") or {},
    }
    payload["run"]["receipt_process_id"] = payload.pop("receipt_process_id")
    payload["run"]["omp_process_id"] = payload.pop("omp_process_id")
    if not payload["run"]["omp_process_id"]:
        return fail("missing_repair_omp_provenance", failure_class="terminal", retry_safe=False, operation="build_repair_receipt", payload=payload)
    if not payload["run"]["run_id"] or not payload["run"]["receipt_process_id"] or not payload["candidate"]:
        return fail("missing_repair_receipt_provenance", failure_class="terminal", retry_safe=False, operation="build_repair_receipt", payload=payload)
    if not payload["before_oid"] or not payload["after_oid"] or payload["before_oid"] == payload["after_oid"]:
        return fail("invalid_repair_receipt_heads", failure_class="terminal", retry_safe=False, operation="build_repair_receipt", payload=payload)
    return ok(status="built", operation="build_repair_receipt", payload=payload, receipt_path=_repair_attempt_receipt(request, payload), ownership_receipt=context["receipt"], mutated=False)


def publish_repair_receipt(request: Request) -> Result:
    gated = _repair_execution_gate(request, "publish_repair_receipt", "build_repair_receipt")
    if gated:
        return gated
    upstream = _repair_upstream(request, "publish_repair_receipt", "build_repair_receipt")
    if upstream:
        return upstream
    built = cond_blob(request, "build_repair_receipt")
    payload = built.get("payload")
    path = str(built.get("receipt_path") or "")
    if not isinstance(payload, dict) or not path:
        return fail("missing_repair_receipt_inputs", failure_class="terminal", retry_safe=False, operation="publish_repair_receipt")
    if dry_run_flag(request):
        return planned(operation="publish_repair_receipt", receipt_path=path, payload=payload)
    try:
        with _receipt_directory_lock(Path(path).parent):
            return _publish_cleanup_receipt(Path(path), payload, path)
    except (OSError, ValueError) as exc:
        return fail("repair_receipt_write_failed", failure_class="terminal", retry_safe=False, operation="publish_repair_receipt", error=str(exc), receipt_path=path)

def verify_repair_receipt(request: Request) -> Result:
    peers = ("publish_repair_receipt", "build_repair_receipt")
    gated = _repair_execution_gate(request, "verify_repair_receipt", *peers)
    if gated:
        return gated
    upstream = _repair_upstream(request, "verify_repair_receipt", *peers)
    if upstream:
        return upstream
    publication = cond_blob(request, "publish_repair_receipt")
    path = str(publication.get("receipt_path") or cond_blob(request, "build_repair_receipt").get("receipt_path") or "")
    expected = cond_blob(request, "build_repair_receipt").get("payload")
    try:
        actual = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return fail("repair_receipt_readback_failed", failure_class="terminal", retry_safe=False, operation="verify_repair_receipt", error=str(exc), receipt_path=path)
    if not isinstance(expected, dict) or actual != expected:
        return fail("repair_receipt_readback_mismatch", failure_class="terminal", retry_safe=False, operation="verify_repair_receipt", receipt_path=path)
    return ok(status="verified", operation="verify_repair_receipt", receipt_path=path, payload=actual, mutated=False)