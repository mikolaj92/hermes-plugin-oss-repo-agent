"""Outcome-aware lifecycle for Fala 0.2.1 correlation paths.

Fala's public driver treats every non-raising adapter result as success and
retries exceptions without a policy or backoff hook.  Keep Fala as the source
of models, persistence, leasing, instantiation, and atomic transitions, but
own this small claim/adapter/transition loop so effector outcomes are handled
explicitly.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from fala.adapters import EffectorRunRequest, EffectorRunResult, create_effector_adapter
from fala.correlation_paths import CorrelationPathInstance, instantiate_correlation_path
from fala.driver import (
    RunUntilIdleResult,
    process_effector_request_parts,
    _run_correlation_path_status,
)
from fala.models import CorrelationPathSpec
from fala.runtime_backend import Homeostat, Process, ProcessStatus, Run, RunStatus, RuntimeBackendService


class FailurePolicy(str, Enum):
    terminal = "terminal"
    retryable_read = "retryable_read"
    reconcile_then_retry = "reconcile_then_retry"


@dataclass(frozen=True)
class EffectorFailure:
    reason: str
    error: dict[str, Any]
    retry_safe: bool = False
    declared_policy: FailurePolicy | None = None

@dataclass(frozen=True)
class RuntimePathRunResult:
    run: Run
    correlation_path: CorrelationPathInstance
    outcome: RunUntilIdleResult
    status: RunStatus
    processes: list[Process]


def _safe_message(value: Any) -> str:
    message = str(value)
    # Do not persist common credential-shaped values in runtime diagnostics.
    import re

    message = re.sub(
        r"(?i)(token|password|secret|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        message,
    )
    return message[:2000]


def _redact(value: Any, *, key: str = "") -> Any:
    lowered = key.lower()
    if any(marker in lowered for marker in ("token", "password", "secret", "api_key", "authorization")):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item, key=key) for item in value]
    if isinstance(value, str):
        return _safe_message(value)
    return value


def _result_output(result: EffectorRunResult | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(result, Mapping):
        return dict(result)
    return dict(result.output or {})

def _dry_run_type_error(field: str, value: Any, *, effector_id: str = "") -> dict[str, Any]:
    return {
        "type": "EffectorOutputContractError",
        "message": f"{field} dry_run metadata must be a bool",
        "reason": "dry_run_type_mismatch",
        "field": field,
        "effector_id": effector_id,
        "reported_dry_run": _redact(value),
        "mutated": False,
        "failure_class": FailurePolicy.terminal.value,
        "retry_safe": False,
    }


def _expected_dry_run(
    effector_input: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    effector_id: str = "",
) -> tuple[bool | None, dict[str, Any] | None]:
    for field, values in (("request", effector_input), ("config", config)):
        if "dry_run" in values and type(values["dry_run"]) is not bool:
            return None, _dry_run_type_error(field, values["dry_run"], effector_id=effector_id)
    if "dry_run" in effector_input:
        return effector_input["dry_run"], None
    if "dry_run" in config:
        return config["dry_run"], None
    return None, None


def _normalize_output_dry_run(
    output: Mapping[str, Any],
    *,
    expected: bool | None,
    effector_id: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    normalized = dict(output)
    if "dry_run" in normalized and type(normalized["dry_run"]) is not bool:
        return normalized, _dry_run_type_error(
            "output",
            normalized["dry_run"],
            effector_id=effector_id,
        )
    if expected is None:
        return normalized, None
    explicit = normalized.get("dry_run")
    if explicit is not None and explicit is not expected:
        return normalized, {
            "type": "EffectorOutputContractError",
            "message": "effector dry_run metadata conflicts with request",
            "reason": "output_dry_run_mismatch",
            "effector_id": effector_id,
            "expected_dry_run": expected,
            "reported_dry_run": explicit,
            "mutated": bool(normalized.get("mutated", False)),
            "failure_class": FailurePolicy.terminal.value,
            "retry_safe": False,
            "output": _redact(normalized),
        }
    normalized["dry_run"] = expected
    return normalized, None


def _contract_failure(error: dict[str, Any]) -> EffectorFailure:
    return EffectorFailure(
        reason=str(error["reason"]),
        error=error,
        retry_safe=False,
        declared_policy=FailurePolicy.terminal,
    )

def classify_effector_result(
    result: EffectorRunResult | Mapping[str, Any],
    *,
    effector_id: str = "",
) -> EffectorFailure | None:
    """Classify adapter output, failing closed on undeclared/unknown outcomes."""
    output = _result_output(result)
    raw_status = output.get("status")
    status = str(raw_status or "").strip().lower()
    ok = output.get("ok", True)
    allowed_success = {
        "ok", "planned", "noop", "success", "succeeded",
        "polled", "claimed", "created", "exists", "loaded", "parsed",
        "checked", "listed", "decided", "no_checks", "checks_failed",
        "checks_pending", "checks_passed", "evidence_optional",
        "evidence_present", "evidence_missing", "already_completed",
        "completed", "refreshed", "reused", "prepared", "built", "omp_finished",
        "has_commits", "blocked", "removed", "already_absent", "deleted",
        "released", "absent", "closed", "already_closed", "merged",
        "merge_verified", "commented", "confirmed", "labeled", "written",
        "pushed", "already_claimed", "already_completed", "already_exists",
        "reconciled", "verified", "already_released",
    }
    if ok is True and status in allowed_success:
        return None
    declared = output.get("failure_class")
    policy: FailurePolicy | None = None
    if declared is not None:
        try:
            policy = FailurePolicy(str(declared))
        except ValueError:
            policy = None
    if ok is True and status not in allowed_success:
        reason = "unknown_success_status"
    elif declared is not None and policy is None:
        reason = "unknown_failure_class"
    else:
        reason = _safe_message(output.get("reason") or output.get("error") or status or "effector_failed")
    error = {
        "type": "EffectorFailure",
        "message": _safe_message(reason),
        "reason": reason,
        "effector_id": effector_id,
        "failure_class": policy.value if policy is not None else "terminal",
        "declared_failure_class": _redact(declared),
        "retry_safe": bool(output.get("retry_safe", False)),
        "output": _redact(output),
    }
    return EffectorFailure(
        reason=reason,
        error=error,
        retry_safe=bool(output.get("retry_safe", False)),
        declared_policy=policy,
    )




def _exception_failure(exc: BaseException, effector_id: str) -> EffectorFailure:
    message = _safe_message(exc)
    error = {
        "type": type(exc).__name__,
        "message": message,
        "reason": "adapter_exception",
        "effector_id": effector_id,
        "failure_class": FailurePolicy.terminal.value,
        "retry_safe": False,
    }
    return EffectorFailure(reason="adapter_exception", error=error)
def _instance_from_processes(
    run_id: str,
    correlation_path: CorrelationPathSpec,
    correlation_path_id: str | None,
    processes: list[Process],
) -> CorrelationPathInstance:
    marker_ids = [
        (process.metadata or {}).get("correlation_path", {}).get("correlation_path_id")
        for process in processes
    ]
    resolved = correlation_path_id or next(
        (value for value in marker_ids if isinstance(value, str) and value),
        f"{run_id}:{correlation_path.id}",
    )
    return CorrelationPathInstance(
        correlation_path_id=resolved,
        run_id=run_id,
        processes=list(processes),
    )


async def _reconcile_homeostats(
    service: RuntimeBackendService,
    *,
    run_id: str,
    actor: str | None,
) -> None:
    """Close waiting processes whose externally-owned homeostat already resolved.

    Fala's ``complete_homeostat`` deliberately only transitions the homeostat.  The
    plugin-owned linkage below makes a resumed tick converge the corresponding
    waiting process without changing Fala's runtime.
    """
    homeostats = await service.list_homeostats(run_id=run_id)
    processes = await service.list_processes(run_id=run_id)
    by_id = {process.id: process for process in processes}
    for homeostat in homeostats:
        if homeostat.status.value == "open":
            continue
        metadata = homeostat.metadata or {}
        process_id = metadata.get("process_id")
        if not isinstance(process_id, str):
            candidate = f"homeostat:{metadata.get('process_id')}"
            process_id = candidate if candidate in by_id else None
        process = by_id.get(process_id) if process_id else None
        if process is None or process.status != ProcessStatus.waiting:
            continue
        if homeostat.status.value == "completed":
            values = _redact(homeostat.values)
            if not isinstance(values, dict):
                values = {"value": values}
            await service.complete_process(
                run_id=run_id,
                process_id=process.id,
                output={**_redact(process.output), **values, "homeostat_id": homeostat.id},
                idempotency_key=f"process.homeostat.complete:{homeostat.id}",
                actor=actor,
            )
        else:
            await service.fail_process(
                run_id=run_id,
                process_id=process.id,
                error={
                    "type": "HomeostatResolvedWithoutApproval",
                    "reason": f"homeostat_{homeostat.status.value}",
                    "homeostat_id": homeostat.id,
                    "values": _redact(homeostat.values),
                },
                idempotency_key=f"process.homeostat.fail:{homeostat.id}",
                actor=actor,
            )


def _default_policy(effector_id: str) -> FailurePolicy:
    """Undeclared effectors fail closed; retry requires an explicit policy."""
    return FailurePolicy.terminal


def _default_attempts(effector_id: str) -> int:
    """Undeclared effectors receive a single terminal attempt."""
    return 1

def _policy_for(
    effector_id: str,
    failure_policy_by_effector: Mapping[str, FailurePolicy | str],
) -> FailurePolicy:
    value = failure_policy_by_effector.get(effector_id, _default_policy(effector_id))
    try:
        return value if isinstance(value, FailurePolicy) else FailurePolicy(str(value))
    except ValueError:
        return FailurePolicy.terminal


def _attempts_for(effector_id: str, max_attempts_by_effector: Mapping[str, int]) -> int:
    value = int(max_attempts_by_effector.get(effector_id, _default_attempts(effector_id)))
    if value < 1:
        raise ValueError(f"max attempts for {effector_id!r} must be at least one")
    return value


async def _cancel_dead_dependents(
    service: RuntimeBackendService,
    *,
    run_id: str,
    correlation_path_id: str,
    actor: str | None,
) -> None:
    """Cancel pending downstreams after an upstream permanently fails."""
    while True:
        processes = await service.list_processes(run_id=run_id)
        members: dict[str, Process] = {}
        for process in processes:
            marker = (process.metadata or {}).get("correlation_path") or {}
            if marker.get("correlation_path_id") == correlation_path_id:
                members[str(marker.get("effector_id") or process.process_type)] = process
        dead = {
            effector_id
            for effector_id, process in members.items()
            if process.status in {
                ProcessStatus.failed,
                ProcessStatus.cancelled,
                ProcessStatus.timed_out,
            }
        }
        cancellations: list[tuple[Process, list[str]]] = []
        allowed_downstream = {
            ProcessStatus.pending,
            ProcessStatus.ready,
            ProcessStatus.retry_wait,
            ProcessStatus.waiting,
        }
        for process in members.values():
            if process.status not in allowed_downstream:
                continue
            marker = (process.metadata or {}).get("correlation_path") or {}
            upstream = [str(item) for item in marker.get("conduction") or []]
            dead_upstreams = [item for item in upstream if item in dead]
            if dead_upstreams:
                cancellations.append((process, dead_upstreams))
        if not cancellations:
            return
        for process, dead_upstreams in cancellations:
            await service.cancel_process(
                run_id=run_id,
                process_id=process.id,
                error={
                    "type": "DeadUpstream",
                    "message": "upstream effector permanently failed; downstream cannot proceed",
                    "dead_upstreams": dead_upstreams,
                },
                idempotency_key=f"process.cancel:{process.id}:dead-upstream",
                actor=actor,
            )


async def run_repo_agent_path(
    service: RuntimeBackendService,
    *,
    run: Run,
    correlation_path: CorrelationPathSpec,
    worker_id: str,
    failure_policy_by_effector: Mapping[str, FailurePolicy | str] | None = None,
    max_attempts_by_effector: Mapping[str, int] | None = None,
    retry_backoff_seconds: float = 0.0,
    correlation_path_id: str | None = None,
    effector_inputs: Mapping[str, Mapping[str, Any]] | None = None,
    effector_configs: Mapping[str, Mapping[str, Any]] | None = None,
    capability_output_schemas: Mapping[str, dict[str, Any]] | None = None,
    accepted_reaction_kinds_by_effector: Mapping[str, list[str]] | None = None,
    regulation_by_effector: Mapping[str, dict[str, Any]] | None = None,
    work_dir: str | Path | None = None,
    max_ticks: int = 100,
    lease_seconds: float = 300.0,
    actor: str | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> RuntimePathRunResult:
    """Run one path while classifying output and applying explicit retry policy."""
    if max_ticks < 1:
        raise ValueError("max_ticks must be greater than zero")
    if retry_backoff_seconds < 0:
        raise ValueError("retry_backoff_seconds must not be negative")
    clock = clock or (lambda: datetime.now(timezone.utc))
    sleep = sleep or asyncio.sleep
    failure_policy_by_effector = dict(failure_policy_by_effector or {})
    max_attempts_by_effector = dict(max_attempts_by_effector or {})
    known = {effector.id for effector in correlation_path.effectors}
    unknown_policy = set(failure_policy_by_effector) - known
    unknown_attempts = set(max_attempts_by_effector) - known
    if unknown_policy or unknown_attempts:
        raise ValueError(f"unknown effector policy/attempt keys: {sorted(unknown_policy | unknown_attempts)}")

    stored_run, _ = await service.create_run(run, idempotency_key="run.create", actor=actor)
    terminal_runs = {RunStatus.completed, RunStatus.failed, RunStatus.cancelled, RunStatus.timed_out}
    existing_processes = await service.list_processes(run_id=run.id)
    if stored_run.status in terminal_runs:
        instance = _instance_from_processes(run.id, correlation_path, correlation_path_id, existing_processes)
        outcome = RunUntilIdleResult(
            stored_run.status == RunStatus.completed,
            0,
            "already_terminal",
            [p for p in existing_processes if p.status == ProcessStatus.succeeded],
            [p for p in existing_processes if p.status in {ProcessStatus.failed, ProcessStatus.cancelled, ProcessStatus.timed_out}],
            [p for p in existing_processes if p.status not in {ProcessStatus.succeeded, ProcessStatus.failed, ProcessStatus.cancelled, ProcessStatus.timed_out}],
        )
        return RuntimePathRunResult(stored_run, instance, outcome, stored_run.status, existing_processes)
    await _reconcile_homeostats(service, run_id=run.id, actor=actor)
    existing_processes = await service.list_processes(run_id=run.id)
    if existing_processes:
        instance = _instance_from_processes(run.id, correlation_path, correlation_path_id, existing_processes)
    else:
        instance = await instantiate_correlation_path(
            service,
            run_id=run.id,
            correlation_path=correlation_path,
            correlation_path_id=correlation_path_id,
            effector_inputs=effector_inputs,
            effector_configs=effector_configs,
            actor=actor,
            capability_output_schemas=capability_output_schemas,
            accepted_reaction_kinds_by_effector=accepted_reaction_kinds_by_effector,
            regulation_by_effector=regulation_by_effector,
            max_attempts_by_effector=max_attempts_by_effector,
        )

    ticks = 0
    stopped_reason = "idle"
    while ticks < max_ticks:
        await _reconcile_homeostats(service, run_id=run.id, actor=actor)
        process = await service.claim_next_ready_process(worker_id=worker_id, run_id=run.id, lease_seconds=lease_seconds)
        if process is None:
            # Expired leases can fail an upstream between ticks; cancel dependents
            # before deriving an idle outcome, then reconcile newly resolved waits.
            await _cancel_dead_dependents(
                service,
                run_id=run.id,
                correlation_path_id=instance.correlation_path_id,
                actor=actor,
            )
            await _reconcile_homeostats(service, run_id=run.id, actor=actor)
            processes = await service.list_processes(run_id=run.id)
            if any(p.status == ProcessStatus.ready for p in processes):
                continue
            now = clock()
            future = min(
                (
                    p for p in processes
                    if p.status == ProcessStatus.retry_wait
                    and p.available_at is not None
                    and p.available_at > now
                ),
                key=lambda p: p.available_at,
                default=None,
            )
            if future is not None:
                await sleep(max(0.0, (future.available_at - now).total_seconds()))
                continue
            break
        ticks += 1
        marker = (process.metadata or {}).get("correlation_path") or {}
        effector_id = str(marker.get("effector_id") or process.process_type)
        try:
            adapter_spec, effector_input, config = process_effector_request_parts(process)
            expected_dry_run, input_contract_error = _expected_dry_run(
                effector_input,
                config,
                effector_id=effector_id,
            )
            if input_contract_error is not None:
                failure = _contract_failure(input_contract_error)
            else:
                work_root = Path(work_dir).expanduser() if work_dir else None
                effector_work_dir = work_root / process.id if work_root else None
                if effector_work_dir:
                    effector_work_dir.mkdir(parents=True, exist_ok=True)
                request = EffectorRunRequest(
                    process_id=process.id,
                    impulse_id=process.impulse_id,
                    adapter=adapter_spec,
                    input=effector_input,
                    config=config,
                    work_dir=effector_work_dir,
                )
                result = await create_effector_adapter(adapter_spec.kind).run(request)
                raw_output = _result_output(result)
                normalized_output, output_contract_error = _normalize_output_dry_run(
                    raw_output,
                    expected=expected_dry_run,
                    effector_id=effector_id,
                )
                if output_contract_error is not None:
                    failure = _contract_failure(output_contract_error)
                elif result.waiting:
                    if result.homeostat_id is not None:
                        homeostat_metadata = {
                            **(result.metadata or {}),
                            "process_id": process.id,
                            "correlation_path_id": instance.correlation_path_id,
                            "effector_id": effector_id,
                        }
                        await service.save_homeostat(
                            Homeostat(
                                id=result.homeostat_id,
                                run_id=process.run_id,
                                impulse_id=process.impulse_id,
                                kind=adapter_spec.kind,
                                values=_redact(normalized_output),
                                metadata=_redact(homeostat_metadata),
                                max_attempts=process.max_attempts,
                            ),
                            idempotency_key=f"homeostat.open:{result.homeostat_id}",
                            actor=actor,
                        )
                    await service.wait_process(
                        run_id=process.run_id,
                        process_id=process.id,
                        output=_redact(normalized_output),
                        idempotency_key=f"process.wait:{process.id}:{process.attempt}",
                        actor=actor,
                    )
                    continue
                else:
                    failure = classify_effector_result(normalized_output, effector_id=effector_id)
                    if failure is None:
                        output = {
                            **normalized_output,
                            "adapter": {
                                "returncode": result.returncode,
                                "stdout": result.stdout,
                                "stderr": result.stderr,
                            },
                        }
                        await service.complete_process(
                            run_id=process.run_id,
                            process_id=process.id,
                            output=output,
                            idempotency_key=f"process.complete:{process.id}:{process.attempt}",
                            actor=actor,
                        )
                        continue
        except Exception as exc:
            failure = _exception_failure(exc, effector_id)

        policy = _policy_for(effector_id, failure_policy_by_effector)
        max_attempts = _attempts_for(effector_id, max_attempts_by_effector)
        declared_policy = failure.declared_policy
        # Reconciliation is a safe ceiling: it may observe terminal outcomes,
        # retry-safe reads, or reconciliation-required mutations. A terminal
        # configuration may never broaden into a retry, and missing or unknown
        # declarations never inherit retry behavior.
        policy_mismatch = (
            declared_policy is None
            or (
                policy == FailurePolicy.terminal
                and declared_policy != FailurePolicy.terminal
            )
            or (
                policy == FailurePolicy.retryable_read
                and declared_policy not in {
                    FailurePolicy.retryable_read,
                    FailurePolicy.terminal,
                }
            )
            or (
                policy == FailurePolicy.reconcile_then_retry
                and declared_policy not in {
                    FailurePolicy.reconcile_then_retry,
                    FailurePolicy.retryable_read,
                    FailurePolicy.terminal,
                }
            )
            or (
                declared_policy == FailurePolicy.retryable_read
                and not failure.retry_safe
            )
        )
        if policy_mismatch:
            failure = EffectorFailure(
                reason="failure_policy_mismatch",
                error={
                    **failure.error,
                    "reason": "failure_policy_mismatch",
                    "configured_failure_class": policy.value,
                    "declared_failure_class": (
                        declared_policy.value if declared_policy is not None else "unknown"
                    ),
                    "failure_class": FailurePolicy.terminal.value,
                    "retry_safe": False,
                },
                retry_safe=False,
                declared_policy=FailurePolicy.terminal,
            )
            declared_policy = FailurePolicy.terminal
        can_retry = (
            not policy_mismatch
            and declared_policy != FailurePolicy.terminal
            and process.attempt < max_attempts
            and failure.retry_safe
            and (
                declared_policy == FailurePolicy.retryable_read
                or (
                    declared_policy == FailurePolicy.reconcile_then_retry
                    and failure.retry_safe
                )
            )
        )
        if can_retry:
            available_at = clock() + timedelta(seconds=retry_backoff_seconds)
            await service.retry_process(
                run_id=process.run_id,
                process_id=process.id,
                available_at=available_at,
                error=failure.error,
                idempotency_key=f"process.retry:{process.id}:{process.attempt}",
                actor=actor,
            )
            delay = max(0.0, (available_at - clock()).total_seconds())
            if delay:
                await sleep(delay)
            continue
        await service.fail_process(
            run_id=process.run_id,
            process_id=process.id,
            error=failure.error,
            idempotency_key=f"process.fail:{process.id}:{process.attempt}",
            actor=actor,
        )
        await _cancel_dead_dependents(
            service,
            run_id=process.run_id,
            correlation_path_id=instance.correlation_path_id,
            actor=actor,
        )
    if ticks >= max_ticks:
        stopped_reason = "max_ticks"
    processes = await service.list_processes(run_id=run.id)
    status, reason = _run_correlation_path_status(processes, stopped_reason=stopped_reason, max_ticks=max_ticks)
    finalized, _ = await service.set_run_status(run_id=run.id, status=status, idempotency_key=f"run.{status.value}", reason=reason, actor=actor)
    completed = [p for p in processes if p.status == ProcessStatus.succeeded]
    failed = [p for p in processes if p.status in {ProcessStatus.failed, ProcessStatus.cancelled, ProcessStatus.timed_out}]
    waiting = [p for p in processes if p.status not in {ProcessStatus.succeeded, ProcessStatus.failed, ProcessStatus.cancelled, ProcessStatus.timed_out}]
    outcome = RunUntilIdleResult(status == RunStatus.completed, ticks, stopped_reason, completed, failed, waiting)
    return RuntimePathRunResult(finalized, instance, outcome, status, processes)
