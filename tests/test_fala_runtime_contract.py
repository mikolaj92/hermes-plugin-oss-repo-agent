from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from repo_agent.adapters_cli import CommandError
from fala.adapters import EffectorRunRequest
from fala.models import CorrelationPathSpec, EffectorAdapterSpec, EffectorSpec
from fala.runtime_backend import (
    HomeostatStatus,
    ProcessStatus,
    Run,
    RunStatus,
    RuntimeBackendService,
)

from repo_agent.flows.runtime import FailurePolicy, classify_effector_result, run_repo_agent_path


def _effector(
    effector_id: str,
    ref: str,
    *,
    conduction: list[str] | None = None,
) -> EffectorSpec:
    return EffectorSpec(
        id=effector_id,
        capability="python_function",
        adapter=EffectorAdapterSpec(kind="python_function", ref=ref),
        conduction=conduction or [],
    )


def semantic_failure(request: EffectorRunRequest) -> dict[str, object]:
    return {
        "ok": False,
        "status": "failed",
        "reason": "semantic failure",
        "failure_class": "terminal",
    }


def count_and_fail(request: EffectorRunRequest) -> dict[str, object]:
    counter = Path(str(request.input["counter"]))
    calls = int(counter.read_text(encoding="utf-8") or "0") + 1
    counter.write_text(str(calls), encoding="utf-8")
    return {
        "ok": False,
        "status": "failed",
        "reason": "permanent input error",
        "failure_class": "terminal",
    }


def flaky_read(request: EffectorRunRequest) -> dict[str, object]:
    counter = Path(str(request.input["counter"]))
    calls = int(counter.read_text(encoding="utf-8") or "0") + 1
    counter.write_text(str(calls), encoding="utf-8")
    if calls == 1:
        return {
            "ok": False,
            "status": "failed",
            "reason": "temporary read error",
            "failure_class": "retryable_read",
            "retry_safe": True,
        }
    return {"ok": True, "status": "planned", "value": "read-back"}


def ambiguous_mutation(request: EffectorRunRequest) -> dict[str, object]:
    counter = Path(str(request.input["counter"]))
    calls = int(counter.read_text(encoding="utf-8") or "0") + 1
    counter.write_text(str(calls), encoding="utf-8")
    return {
        "ok": False,
        "status": "failed",
        "reason": "mutation outcome ambiguous",
        "failure_class": "reconcile_then_retry",
        # Deliberately absent: an ambiguous mutation is not retry-safe.
    }


def idempotent_mutation(request: EffectorRunRequest) -> dict[str, object]:
    marker = Path(str(request.input["marker"]))
    if marker.exists():
        return {"ok": True, "status": "planned", "reused": True}
    marker.write_text("created", encoding="utf-8")
    return {
        "ok": False,
        "status": "failed",
        "reason": "mutation response lost",
        "failure_class": "reconcile_then_retry",
        "retry_safe": True,
    }


def successful_effector(request: EffectorRunRequest) -> dict[str, object]:
    return {"ok": True, "status": "planned"}
def counting_success(request: EffectorRunRequest) -> dict[str, object]:
    counter = Path(str(request.input["counter"]))
    calls = int(counter.read_text(encoding="utf-8") or "0") + 1
    counter.write_text(str(calls), encoding="utf-8")
    return {"ok": True, "status": "planned"}


def malformed_dry_run_output(request: EffectorRunRequest) -> dict[str, object]:
    return {"ok": True, "status": "planned", "dry_run": "false"}


def conflicting_dry_run_output(request: EffectorRunRequest) -> dict[str, object]:
    return {"ok": True, "status": "planned", "dry_run": False, "mutated": True}





def undeclared_failure(request: EffectorRunRequest) -> dict[str, object]:
    return {"ok": False, "status": "failed", "reason": "undeclared failure"}

def unknown_failure(request: EffectorRunRequest) -> dict[str, object]:
    return {"ok": False, "status": "failed", "reason": "unknown failure", "failure_class": "typo"}
class RuntimeContractTests(unittest.TestCase):
    def test_all_declared_success_statuses_are_accepted(self) -> None:
        for status in ("labeled", "written", "pushed", "already_claimed"):
            with self.subTest(status=status):
                self.assertIsNone(classify_effector_result({"ok": True, "status": status}))
    def test_malformed_request_dry_run_fails_before_adapter_invocation(self) -> None:
        async def scenario(root: Path):
            counter = root / "calls"
            counter.write_text("0", encoding="utf-8")
            service = RuntimeBackendService.sqlite(root / "state.sqlite")
            path = CorrelationPathSpec(
                id="malformed_request_dry_run_path",
                effectors=[_effector("work", "tests.test_fala_runtime_contract.counting_success")],
            )
            result = await run_repo_agent_path(
                service,
                run=Run(id="runtime_malformed_request_dry_run"),
                correlation_path=path,
                worker_id="contract-test",
                actor="contract-test",
                effector_inputs={"work": {"counter": str(counter), "dry_run": "false"}},
            )
            process = await service.backend.get_process(
                run_id="runtime_malformed_request_dry_run",
                process_id="runtime_malformed_request_dry_run:malformed_request_dry_run_path:work",
            )
            return result, process, counter.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            result, process, calls = asyncio.run(scenario(Path(tmp)))

        self.assertEqual(result.status, RunStatus.failed)
        self.assertEqual(process.status, ProcessStatus.failed)
        self.assertEqual(calls, "0")
        self.assertEqual(process.error["reason"], "dry_run_type_mismatch")
        self.assertEqual(process.error["failure_class"], "terminal")
        self.assertFalse(process.error["retry_safe"])
        self.assertFalse(process.error["mutated"])

    def test_malformed_config_dry_run_fails_before_adapter_invocation(self) -> None:
        async def scenario(root: Path):
            counter = root / "calls"
            counter.write_text("0", encoding="utf-8")
            service = RuntimeBackendService.sqlite(root / "state.sqlite")
            path = CorrelationPathSpec(
                id="malformed_config_dry_run_path",
                effectors=[_effector("work", "tests.test_fala_runtime_contract.counting_success")],
            )
            result = await run_repo_agent_path(
                service,
                run=Run(id="runtime_malformed_config_dry_run"),
                correlation_path=path,
                worker_id="contract-test",
                actor="contract-test",
                effector_inputs={"work": {"counter": str(counter)}},
                effector_configs={"work": {"dry_run": 1}},
            )
            process = await service.backend.get_process(
                run_id="runtime_malformed_config_dry_run",
                process_id="runtime_malformed_config_dry_run:malformed_config_dry_run_path:work",
            )
            return result, process, counter.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            result, process, calls = asyncio.run(scenario(Path(tmp)))

        self.assertEqual(result.status, RunStatus.failed)
        self.assertEqual(process.status, ProcessStatus.failed)
        self.assertEqual(calls, "0")
        self.assertEqual(process.error["reason"], "dry_run_type_mismatch")
        self.assertEqual(process.error["field"], "config")
        self.assertFalse(process.error["mutated"])

    def test_malformed_output_dry_run_is_terminal(self) -> None:
        async def scenario(root: Path):
            service = RuntimeBackendService.sqlite(root / "state.sqlite")
            path = CorrelationPathSpec(
                id="malformed_output_dry_run_path",
                effectors=[_effector("work", "tests.test_fala_runtime_contract.malformed_dry_run_output")],
            )
            result = await run_repo_agent_path(
                service,
                run=Run(id="runtime_malformed_output_dry_run"),
                correlation_path=path,
                worker_id="contract-test",
                actor="contract-test",
                effector_inputs={"work": {"dry_run": True}},
                failure_policy_by_effector={"work": FailurePolicy.retryable_read},
                max_attempts_by_effector={"work": 3},
            )
            process = await service.backend.get_process(
                run_id="runtime_malformed_output_dry_run",
                process_id="runtime_malformed_output_dry_run:malformed_output_dry_run_path:work",
            )
            return result, process

        with tempfile.TemporaryDirectory() as tmp:
            result, process = asyncio.run(scenario(Path(tmp)))

        self.assertEqual(result.status, RunStatus.failed)
        self.assertEqual(process.status, ProcessStatus.failed)
        self.assertEqual(process.attempt, 1)
        self.assertEqual(process.error["reason"], "dry_run_type_mismatch")
        self.assertEqual(process.error["field"], "output")
        self.assertEqual(process.error["failure_class"], "terminal")
        self.assertFalse(process.error["retry_safe"])

    def test_conflicting_output_dry_run_is_terminal_and_truthful(self) -> None:
        async def scenario(root: Path):
            service = RuntimeBackendService.sqlite(root / "state.sqlite")
            path = CorrelationPathSpec(
                id="conflicting_output_dry_run_path",
                effectors=[_effector("work", "tests.test_fala_runtime_contract.conflicting_dry_run_output")],
            )
            result = await run_repo_agent_path(
                service,
                run=Run(id="runtime_conflicting_output_dry_run"),
                correlation_path=path,
                worker_id="contract-test",
                actor="contract-test",
                effector_inputs={"work": {"dry_run": True}},
                failure_policy_by_effector={"work": FailurePolicy.retryable_read},
                max_attempts_by_effector={"work": 3},
            )
            process = await service.backend.get_process(
                run_id="runtime_conflicting_output_dry_run",
                process_id="runtime_conflicting_output_dry_run:conflicting_output_dry_run_path:work",
            )
            return result, process

        with tempfile.TemporaryDirectory() as tmp:
            result, process = asyncio.run(scenario(Path(tmp)))

        self.assertEqual(result.status, RunStatus.failed)
        self.assertEqual(process.status, ProcessStatus.failed)
        self.assertEqual(process.attempt, 1)
        self.assertEqual(process.error["reason"], "output_dry_run_mismatch")
        self.assertEqual(process.error["expected_dry_run"], True)
        self.assertEqual(process.error["reported_dry_run"], False)
        self.assertTrue(process.error["mutated"])
        self.assertEqual(process.error["failure_class"], "terminal")
        self.assertFalse(process.error["retry_safe"])

    def test_omitted_output_dry_run_is_normalized_and_persisted(self) -> None:
        async def scenario(root: Path):
            service = RuntimeBackendService.sqlite(root / "state.sqlite")
            path = CorrelationPathSpec(
                id="normalized_output_dry_run_path",
                effectors=[_effector("work", "tests.test_fala_runtime_contract.successful_effector")],
            )
            result = await run_repo_agent_path(
                service,
                run=Run(id="runtime_normalized_output_dry_run"),
                correlation_path=path,
                worker_id="contract-test",
                actor="contract-test",
                effector_inputs={"work": {"dry_run": True}},
            )
            process = await service.backend.get_process(
                run_id="runtime_normalized_output_dry_run",
                process_id="runtime_normalized_output_dry_run:normalized_output_dry_run_path:work",
            )
            return result, process

        with tempfile.TemporaryDirectory() as tmp:
            result, process = asyncio.run(scenario(Path(tmp)))

        self.assertEqual(result.status, RunStatus.completed)
        self.assertEqual(process.status, ProcessStatus.succeeded)
        self.assertIs(process.output["dry_run"], True)


    def test_undeclared_failure_does_not_inherit_retry_policy(self) -> None:
        async def scenario(root: Path):
            service = RuntimeBackendService.sqlite(root / "state.sqlite")
            path = CorrelationPathSpec(
                id="undeclared_failure_path",
                effectors=[_effector("read", "tests.test_fala_runtime_contract.undeclared_failure")],
            )
            result = await run_repo_agent_path(
                service,
                run=Run(id="runtime_undeclared_failure"),
                correlation_path=path,
                worker_id="contract-test",
                actor="contract-test",
                failure_policy_by_effector={"read": FailurePolicy.retryable_read},
                max_attempts_by_effector={"read": 3},
            )
            process = await service.backend.get_process(
                run_id="runtime_undeclared_failure",
                process_id="runtime_undeclared_failure:undeclared_failure_path:read",
            )
            return result, process

        with tempfile.TemporaryDirectory() as tmp:
            result, process = asyncio.run(scenario(Path(tmp)))

        self.assertEqual(result.status, RunStatus.failed)
        self.assertEqual(process.status, ProcessStatus.failed)
        self.assertEqual(process.attempt, 1)

    def test_unknown_declared_failure_does_not_inherit_retry_policy(self) -> None:
        async def scenario(root: Path):
            service = RuntimeBackendService.sqlite(root / "state.sqlite")
            path = CorrelationPathSpec(
                id="unknown_failure_path",
                effectors=[_effector("read", "tests.test_fala_runtime_contract.unknown_failure")],
            )
            result = await run_repo_agent_path(
                service,
                run=Run(id="runtime_unknown_failure"),
                correlation_path=path,
                worker_id="contract-test",
                actor="contract-test",
                failure_policy_by_effector={"read": FailurePolicy.retryable_read},
                max_attempts_by_effector={"read": 3},
            )
            process = await service.backend.get_process(
                run_id="runtime_unknown_failure",
                process_id="runtime_unknown_failure:unknown_failure_path:read",
            )
            return result, process

        with tempfile.TemporaryDirectory() as tmp:
            result, process = asyncio.run(scenario(Path(tmp)))

        self.assertEqual(result.status, RunStatus.failed)
        self.assertEqual(process.status, ProcessStatus.failed)
        self.assertEqual(process.attempt, 1)
        self.assertEqual(process.error["reason"], "failure_policy_mismatch")

    def _clock_patch(self):
        current = [datetime(2030, 1, 1, tzinfo=timezone.utc)]

        def now() -> datetime:
            return current[0]

        async def sleep(seconds: float) -> None:
            current[0] += timedelta(seconds=seconds)

        return current, mock.patch("fala.runtime_backend._now", side_effect=now), now, sleep

    def test_semantic_failure_fails_process_and_run_and_blocks_dependent(self) -> None:
        async def scenario(root: Path):
            service = RuntimeBackendService.sqlite(root / "state.sqlite")
            path = CorrelationPathSpec(
                id="semantic_failure_path",
                effectors=[
                    _effector("fail", "tests.test_fala_runtime_contract.semantic_failure"),
                    _effector(
                        "dependent",
                        "tests.test_fala_runtime_contract.successful_effector",
                        conduction=["fail"],
                    ),
                ],
            )
            result = await run_repo_agent_path(
                service,
                run=Run(id="runtime_semantic_failure"),
                correlation_path=path,
                worker_id="contract-test",
                actor="contract-test",
                failure_policy_by_effector={"fail": FailurePolicy.terminal},
                max_attempts_by_effector={"fail": 3, "dependent": 1},
            )
            processes = {p.id.rsplit(":", 1)[-1]: p for p in result.processes}
            stored_run = await service.backend.get_run(run_id="runtime_semantic_failure")
            return result, processes, stored_run

        with tempfile.TemporaryDirectory() as tmp:
            result, processes, stored_run = asyncio.run(scenario(Path(tmp)))

        self.assertEqual(result.status, RunStatus.failed)
        self.assertEqual(stored_run.status, RunStatus.failed)
        self.assertFalse(result.outcome.ok)
        # Terminal policy failure cancels pending dependents immediately so no
        # unclaimable pending process is left behind.
        self.assertEqual(processes["dependent"].status, ProcessStatus.cancelled)
        self.assertNotIn(processes["dependent"], result.outcome.completed)
        self.assertIn(processes["dependent"], result.outcome.failed)
        self.assertEqual(processes["fail"].error["reason"], "semantic failure")
        self.assertEqual(processes["fail"].error["failure_class"], "terminal")

    def test_retryable_read_records_backoff_and_succeeds_on_second_attempt(self) -> None:
        async def scenario(root: Path):
            counter = root / "read_calls"
            counter.write_text("0", encoding="utf-8")
            service = RuntimeBackendService.sqlite(root / "state.sqlite")
            path = CorrelationPathSpec(
                id="retryable_read_path",
                effectors=[_effector("read", "tests.test_fala_runtime_contract.flaky_read")],
            )
            current, clock_patch, clock, sleep = self._clock_patch()
            with clock_patch:
                result = await run_repo_agent_path(
                    service,
                    run=Run(id="runtime_retryable_read"),
                    correlation_path=path,
                    worker_id="contract-test",
                    actor="contract-test",
                    failure_policy_by_effector={"read": FailurePolicy.retryable_read},
                    max_attempts_by_effector={"read": 2},
                    retry_backoff_seconds=60,
                    effector_inputs={"read": {"counter": str(counter)}},
                    clock=clock,
                    sleep=sleep,
                )
            process = await service.backend.get_process(
                run_id="runtime_retryable_read",
                process_id="runtime_retryable_read:retryable_read_path:read",
            )
            events = await service.backend.list_events(run_id="runtime_retryable_read")
            return result, process, events, current[0], int(counter.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as tmp:
            result, process, events, current, calls = asyncio.run(scenario(Path(tmp)))

        self.assertEqual(result.status, RunStatus.completed)
        self.assertEqual(process.status, ProcessStatus.succeeded)
        self.assertEqual(process.attempt, 2)
        self.assertEqual(process.max_attempts, 2)
        self.assertEqual(calls, 2)
        retry_events = [e for e in events if e.event_type == "process.retry_scheduled"]
        self.assertEqual(len(retry_events), 1)
        self.assertEqual(process.available_at, datetime(2030, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=60))
        self.assertEqual(current, process.available_at)
    def test_verify_branch_retries_read_then_preserves_terminal_reason(self) -> None:
        async def scenario(root: Path):
            service = RuntimeBackendService.sqlite(root / "state.sqlite")
            path = CorrelationPathSpec(
                id="verify_branch_policy_path",
                effectors=[
                    _effector(
                        "verify",
                        "repo_agent.steps.issue_to_pr.verify_branch_has_commits",
                    )
                ],
            )
            with mock.patch(
                "repo_agent.steps.issue_to_pr.rev_parse",
                side_effect=[
                    CommandError(["git", "rev-parse"], 1, "", "temporary read error"),
                    "same",
                    "same",
                ],
            ):
                result = await run_repo_agent_path(
                    service,
                    run=Run(id="runtime_verify_branch_policy"),
                    correlation_path=path,
                    worker_id="contract-test",
                    actor="contract-test",
                    failure_policy_by_effector={
                        "verify": FailurePolicy.retryable_read,
                    },
                    max_attempts_by_effector={"verify": 3},
                    effector_inputs={
                        "verify": {
                            "worktree_path": "/wt",
                            "clone_path": "/c",
                            "base_branch": "main",
                            "dry_run": False,
                        }
                    },
                    retry_backoff_seconds=0,
                )
            process = await service.backend.get_process(
                run_id="runtime_verify_branch_policy",
                process_id="runtime_verify_branch_policy:verify_branch_policy_path:verify",
            )
            return result, process

        with tempfile.TemporaryDirectory() as tmp:
            result, process = asyncio.run(scenario(Path(tmp)))

        self.assertEqual(result.status, RunStatus.failed)
        self.assertEqual(process.status, ProcessStatus.failed)
        self.assertEqual(process.attempt, 2)
        self.assertEqual(process.error["reason"], "no_new_commits")
        self.assertEqual(process.error["failure_class"], "terminal")
        self.assertFalse(process.error["retry_safe"])
        self.assertNotEqual(process.error["reason"], "failure_policy_mismatch")


    def test_terminal_failure_uses_one_attempt_even_when_budget_is_three(self) -> None:
        async def scenario(root: Path):
            counter = root / "calls"
            counter.write_text("0", encoding="utf-8")
            service = RuntimeBackendService.sqlite(root / "state.sqlite")
            path = CorrelationPathSpec(
                id="terminal_budget_path",
                effectors=[_effector("mutate", "tests.test_fala_runtime_contract.count_and_fail")],
            )
            result = await run_repo_agent_path(
                service,
                run=Run(id="runtime_terminal_budget"),
                correlation_path=path,
                worker_id="contract-test",
                actor="contract-test",
                failure_policy_by_effector={"mutate": FailurePolicy.terminal},
                max_attempts_by_effector={"mutate": 3},
                effector_inputs={"mutate": {"counter": str(counter)}},
            )
            process = await service.backend.get_process(
                run_id="runtime_terminal_budget",
                process_id="runtime_terminal_budget:terminal_budget_path:mutate",
            )
            return result, process, int(counter.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as tmp:
            result, process, calls = asyncio.run(scenario(Path(tmp)))

        self.assertEqual(result.status, RunStatus.failed)
        self.assertEqual(process.status, ProcessStatus.failed)
        self.assertEqual(process.attempt, 1)
        self.assertEqual(process.max_attempts, 3)
        self.assertEqual(calls, 1)

    def test_ambiguous_mutation_without_retry_safe_is_terminal(self) -> None:
        async def scenario(root: Path):
            counter = root / "calls"
            counter.write_text("0", encoding="utf-8")
            service = RuntimeBackendService.sqlite(root / "state.sqlite")
            path = CorrelationPathSpec(
                id="ambiguous_mutation_path",
                effectors=[_effector("mutate", "tests.test_fala_runtime_contract.ambiguous_mutation")],
            )
            result = await run_repo_agent_path(
                service,
                run=Run(id="runtime_ambiguous_mutation"),
                correlation_path=path,
                worker_id="contract-test",
                actor="contract-test",
                failure_policy_by_effector={
                    "mutate": FailurePolicy.reconcile_then_retry,
                },
                max_attempts_by_effector={"mutate": 3},
                effector_inputs={"mutate": {"counter": str(counter)}},
            )
            process = await service.backend.get_process(
                run_id="runtime_ambiguous_mutation",
                process_id="runtime_ambiguous_mutation:ambiguous_mutation_path:mutate",
            )
            events = await service.backend.list_events(run_id="runtime_ambiguous_mutation")
            return result, process, events, int(counter.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as tmp:
            result, process, events, calls = asyncio.run(scenario(Path(tmp)))

        self.assertEqual(result.status, RunStatus.failed)
        self.assertEqual(process.status, ProcessStatus.failed)
        self.assertEqual(process.attempt, 1)
        self.assertEqual(calls, 1)
        self.assertFalse(any(e.event_type == "process.retry_scheduled" for e in events))

    def test_retry_safe_mutation_retries_without_duplicate_side_effect(self) -> None:
        async def scenario(root: Path):
            marker = root / "mutation-marker"
            service = RuntimeBackendService.sqlite(root / "state.sqlite")
            path = CorrelationPathSpec(
                id="idempotent_mutation_path",
                effectors=[_effector("mutate", "tests.test_fala_runtime_contract.idempotent_mutation")],
            )
            current, clock_patch, clock, sleep = self._clock_patch()
            with clock_patch:
                result = await run_repo_agent_path(
                    service,
                    run=Run(id="runtime_idempotent_mutation"),
                    correlation_path=path,
                    worker_id="contract-test",
                    actor="contract-test",
                    failure_policy_by_effector={
                        "mutate": FailurePolicy.reconcile_then_retry,
                    },
                    max_attempts_by_effector={"mutate": 3},
                    retry_backoff_seconds=15,
                    effector_inputs={"mutate": {"marker": str(marker)}},
                    clock=clock,
                    sleep=sleep,
                )
            process = await service.backend.get_process(
                run_id="runtime_idempotent_mutation",
                process_id="runtime_idempotent_mutation:idempotent_mutation_path:mutate",
            )
            events = await service.backend.list_events(run_id="runtime_idempotent_mutation")
            return result, process, events, marker.read_text(encoding="utf-8"), current[0]

        with tempfile.TemporaryDirectory() as tmp:
            result, process, events, marker, current = asyncio.run(scenario(Path(tmp)))

        self.assertEqual(result.status, RunStatus.completed)
        self.assertEqual(process.status, ProcessStatus.succeeded)
        self.assertEqual(process.attempt, 2)
        self.assertEqual(marker, "created")
        self.assertEqual(len([e for e in events if e.event_type == "process.retry_scheduled"]), 1)
        self.assertEqual(current, datetime(2030, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=15))

    def test_runner_propagates_per_effector_attempt_budgets(self) -> None:
        async def scenario(root: Path):
            service = RuntimeBackendService.sqlite(root / "state.sqlite")
            path = CorrelationPathSpec(
                id="attempt_propagation_path",
                effectors=[
                    _effector("read", "tests.test_fala_runtime_contract.successful_effector"),
                    _effector("write", "tests.test_fala_runtime_contract.successful_effector"),
                ],
            )
            result = await run_repo_agent_path(
                service,
                run=Run(id="runtime_attempt_propagation"),
                correlation_path=path,
                worker_id="contract-test",
                actor="contract-test",
                max_attempts_by_effector={"read": 3, "write": 1},
            )
            processes = await service.list_processes(run_id="runtime_attempt_propagation")
            return result, {p.id.rsplit(":", 1)[-1]: p for p in processes}

        with tempfile.TemporaryDirectory() as tmp:
            result, processes = asyncio.run(scenario(Path(tmp)))

        self.assertEqual(result.status, RunStatus.completed)
        self.assertEqual(processes["read"].max_attempts, 3)
        self.assertEqual(processes["write"].max_attempts, 1)

    def test_waiting_homeostat_is_persisted_and_run_remains_resumable(self) -> None:
        async def scenario(root: Path):
            service = RuntimeBackendService.sqlite(root / "state.sqlite")
            path = CorrelationPathSpec(
                id="homeostat_path",
                effectors=[
                    EffectorSpec(
                        id="approval",
                        capability="manual_homeostat",
                        adapter=EffectorAdapterSpec(kind="manual_homeostat"),
                    )
                ],
            )
            result = await run_repo_agent_path(
                service,
                run=Run(id="runtime_homeostat"),
                correlation_path=path,
                worker_id="contract-test",
                actor="contract-test",
                max_attempts_by_effector={"approval": 3},
            )
            process = await service.backend.get_process(
                run_id="runtime_homeostat",
                process_id="runtime_homeostat:homeostat_path:approval",
            )
            homeostats = await service.list_homeostats(run_id="runtime_homeostat")
            stored_run = await service.backend.get_run(run_id="runtime_homeostat")
            return result, process, homeostats, stored_run

        with tempfile.TemporaryDirectory() as tmp:
            result, process, homeostats, stored_run = asyncio.run(scenario(Path(tmp)))

        self.assertEqual(result.status, RunStatus.waiting)
        self.assertEqual(stored_run.status, RunStatus.waiting)
        self.assertEqual(process.status, ProcessStatus.waiting)
        self.assertEqual(len(homeostats), 1)
        self.assertEqual(homeostats[0].status, HomeostatStatus.open)
        self.assertEqual(homeostats[0].values["status"], "waiting")


if __name__ == "__main__":
    unittest.main()
