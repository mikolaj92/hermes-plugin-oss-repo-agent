from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest import mock

from lokay.config import AgentConfig, RepoEntry
from lokay.flows.intake import run_intake_flow
from lokay.flows.runtime import HostPathRunResult, JournalProcess
from lokay.flows.triage import run_pr_triage_decide
from lokay.process import build_effective_run
from lokay.process_contracts import FORBIDDEN_PATH_ALIASES, PROCESS_CONTRACTS
from lokay.registry import PROCESS_IDS
from lokay.steps import claim, kanban_intake, poll
from lokay.tick_all import run_all


class _Req(dict):
    def __init__(self, input_data=None, config=None):
        super().__init__(input=input_data or {}, config=config or {})


class PollStepTests(unittest.TestCase):
    def test_filters_ready_and_foreign(self) -> None:
        issues = [
            {"number": 1, "title": "ready one", "url": "https://example/1", "labels": [{"name": "ai:ready"}], "assignees": []},
            {"number": 2, "title": "blocked", "url": "https://example/2", "labels": [{"name": "ai:ready"}, {"name": "ai:blocked"}], "assignees": []},
            {"number": 3, "title": "foreign", "url": "https://example/3", "labels": [{"name": "ai:ready"}], "assignees": [{"login": "someone-else"}]},
        ]
        with mock.patch("lokay.steps.poll.gh_json", return_value=issues):
            read = poll.read_open_issues(_Req({"repo": "o/r", "board": "board-r", "dry_run": True}, {"assignee": "mikolaj92", "ready_label": "ai:ready"}))
        normalized = poll.normalize_issue_rows(_Req({"read_open_issues": read, "conduction": {"read_open_issues": read}, "dry_run": True}))
        filtered = poll.filter_issue_eligibility(_Req({"conduction": {"normalize_issue_rows": normalized}, "dry_run": True}, {"assignee": "mikolaj92", "ready_label": "ai:ready"}))
        result = poll.select_issue_candidate(_Req({"conduction": {"filter_issue_eligibility": filtered}, "dry_run": True}))
        self.assertEqual(result["eligible_count"], 1)
        self.assertEqual(result["selected"]["number"], 1)
        self.assertEqual(result["skipped_count"], 2)


class ClaimKanbanDryRunTests(unittest.TestCase):
    def _selected(self):
        return {"repo": "o/r", "number": 7, "title": "t", "url": "https://x", "board": "b", "labels": [], "assignees": []}

    def test_claim_noop_without_selection(self) -> None:
        result = claim.reserve_claim_file(_Req({"conduction": {"select_issue_candidate": {"selected": None, "status": "noop"}}, "dry_run": True}))
        self.assertEqual(result["status"], "noop")
        self.assertFalse(result["mutated"])

    def test_claim_dry_run_plans(self) -> None:
        result = claim.reserve_claim_file(_Req({"dry_run": True, "conduction": {"select_issue_candidate": {"selected": self._selected()}}}, {"assignee": "mikolaj92"}))
        self.assertEqual(result["status"], "planned")
        self.assertFalse(result["mutated"])

    def test_kanban_dry_run_plans(self) -> None:
        selected = self._selected()
        found = {"status": "intake_marker_absent", "found": False, "selected": selected, "board": "b", "marker": "github-issue:o/r:7"}
        result = kanban_intake.create_intake_task(_Req({"dry_run": True, "selected": selected, "conduction": {"find_intake_marker": found}}, {"kanban_intake_assignee": "lokay-fixer"}))
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["status"], "planned")


class ProcessSpecificHostPathTests(unittest.TestCase):
    """Canonical process path contracts replace retired auto_worker/run_all host composition."""

    @staticmethod
    def _effective(process_id: str, cfg: AgentConfig, *, run_id: str = "process-run") -> dict:
        contract = PROCESS_CONTRACTS[process_id]
        return build_effective_run(
            contract=contract,
            process_id=process_id,
            run_id=run_id,
            db_path=Path("/tmp/process.sqlite"),
            cfg=cfg,
            generation="test-generation",
            candidate_id="a" * 64,
            config_sha256="b" * 64,
            command=f"lokay-process-{process_id}",
        )

    @staticmethod
    def _host(
        *,
        path_id: str,
        failed: bool = False,
        processes: tuple[JournalProcess, ...] | None = None,
        run_id: str = "process-run",
    ) -> HostPathRunResult:
        if processes is None:
            step_id = "open_pull_request" if failed else "read_open_prs"
            process = JournalProcess(
                id=f"{run_id}:{path_id}:{step_id}",
                status="failed" if failed else "succeeded",
                attempt=1,
                max_attempts=1,
                output={"status": "error" if failed else "planned"},
                error={"message": "dispatch failed"} if failed else {},
                metadata={"correlation_path_id": path_id, "effector_id": step_id},
                correlation_path_id=path_id,
                effector_id=step_id,
            )
            processes = (process,)
        return HostPathRunResult(
            run_id=run_id,
            path_id=path_id,
            run_status="failed" if failed else "completed",
            replayed=False,
            ticks=1,
            processes=processes,
        )

    def test_twelve_process_paths_are_canonical_and_disjoint(self) -> None:
        self.assertEqual(tuple(PROCESS_CONTRACTS), PROCESS_IDS)
        self.assertEqual(len(PROCESS_IDS), 12)
        for process_id in PROCESS_IDS:
            contract = PROCESS_CONTRACTS[process_id]
            self.assertEqual(contract.process_id, process_id)
            self.assertEqual(contract.path_id, process_id)
            self.assertNotIn(contract.path_id, FORBIDDEN_PATH_ALIASES)
            self.assertNotIn(contract.path_id, {"auto_worker", "tick_all", "issue_intake"})
            self.assertTrue(contract.allowed_effectors)
            self.assertFalse(set(contract.allowed_effectors) & FORBIDDEN_PATH_ALIASES)

        pr_group = ("pr_triage", "pr_repair", "pr_merge")
        for index, left in enumerate(pr_group):
            left_set = set(PROCESS_CONTRACTS[left].allowed_effectors)
            for right in pr_group[index + 1 :]:
                right_set = set(PROCESS_CONTRACTS[right].allowed_effectors)
                self.assertFalse(left_set & right_set, msg=f"ownership overlap {left}/{right}")

    def test_pr_triage_effective_run_is_one_owned_host_path(self) -> None:
        cfg = AgentConfig(
            mode="dry-run",
            repos=(RepoEntry(repo="o/r", board="board-r", clone_path="/tmp/o-r"),),
            raw={"github": {"default_limit": 7}},
        )
        effective = self._effective("pr_triage", cfg)
        contract = PROCESS_CONTRACTS["pr_triage"]

        self.assertEqual(effective["inputs"]["path_id"], "pr_triage")
        self.assertEqual(effective["inputs"]["process_id"], "pr_triage")
        self.assertEqual(effective["max_ticks"], contract.max_ticks)
        self.assertEqual(set(effective["effector_inputs"]), set(contract.allowed_effectors))
        self.assertEqual(set(effective["effector_configs"]), set(contract.allowed_effectors))
        self.assertEqual(effective["effector_inputs"]["read_open_prs"]["limit"], 7)
        self.assertTrue(effective["effector_inputs"]["decide_triage_action"]["require_human_approval"])
        self.assertNotIn("auto_worker", effective["effector_inputs"])
        self.assertFalse(any(step.startswith("triage_") for step in effective["effector_inputs"]))
        for sibling in PROCESS_CONTRACTS["pr_repair"].allowed_effectors[:5]:
            self.assertNotIn(sibling, effective["effector_inputs"])
        for sibling in ("merge_pr", "remove_worktree", "read_open_issues"):
            self.assertNotIn(sibling, effective["effector_inputs"])

    def test_cleanup_and_poll_effective_runs_stay_process_local(self) -> None:
        cfg = AgentConfig(
            mode="dry-run",
            repos=(RepoEntry(repo="o/r", board="board-r", clone_path="/tmp/o-r"),),
            raw={"github": {"default_limit": 7}},
        )
        cleanup = self._effective("cleanup", cfg)
        poll_run = self._effective("repo_issue_poll", cfg)

        self.assertEqual(cleanup["inputs"]["path_id"], "cleanup")
        self.assertTrue(all(value.get("require_safe") is True for value in cleanup["effector_inputs"].values()))
        self.assertIn("remove_worktree", cleanup["effector_inputs"])
        self.assertNotIn("decide_triage_action", cleanup["effector_inputs"])

        self.assertEqual(poll_run["inputs"]["path_id"], "repo_issue_poll")
        self.assertEqual(set(poll_run["effector_inputs"]), {"read_open_issues", "normalize_issue_rows"})
        self.assertEqual(poll_run["effector_inputs"]["read_open_issues"]["limit"], 7)
        self.assertEqual(
            [entry["repo"] for entry in poll_run["effector_inputs"]["read_open_issues"]["repos"]],
            ["o/r"],
        )

    def test_multi_repo_effective_run_carries_full_catalog_without_first_repo_injection(self) -> None:
        cfg = AgentConfig(
            mode="dry-run",
            repos=(
                RepoEntry(repo="o/first", board="first-board", clone_path="/tmp/first"),
                RepoEntry(
                    repo="o/temida",
                    board="temida-board",
                    clone_path="/tmp/temida",
                    triage_context_paths=("CONTRIBUTING.md",),
                ),
            ),
            raw={"github": {"default_limit": 7}},
        )
        effective = self._effective("pr_triage", cfg)
        inputs = effective["effector_inputs"]
        repos = effective["inputs"]["repos"]

        for value in inputs.values():
            self.assertNotEqual(value.get("repo"), "o/first")
            self.assertNotEqual(value.get("board"), "first-board")
            self.assertNotEqual(value.get("clone_path"), "/tmp/first")
        self.assertEqual([entry["repo"] for entry in repos], ["o/first", "o/temida"])
        self.assertEqual(repos[1]["triage_context_paths"], ["CONTRIBUTING.md"])
        self.assertEqual(repos[0]["triage_context_paths"], ["README.md"])
        self.assertEqual(inputs["read_open_prs"]["repos"][1]["repo"], "o/temida")

    def test_idle_pr_triage_host_is_not_worked(self) -> None:
        host = self._host(
            path_id="pr_triage",
            processes=(
                JournalProcess(
                    id="process-run:pr_triage:read_open_prs",
                    status="succeeded",
                    attempt=1,
                    max_attempts=1,
                    output={"status": "noop", "reason": "no_open_prs", "mutated": False},
                    error={},
                    metadata={"correlation_path_id": "pr_triage", "effector_id": "read_open_prs"},
                    correlation_path_id="pr_triage",
                    effector_id="read_open_prs",
                ),
                JournalProcess(
                    id="process-run:pr_triage:decide_triage_action",
                    status="succeeded",
                    attempt=1,
                    max_attempts=1,
                    output={"status": "noop", "action": "skip", "reason": "no_open_prs", "mutated": False},
                    error={},
                    metadata={"correlation_path_id": "pr_triage", "effector_id": "decide_triage_action"},
                    correlation_path_id="pr_triage",
                    effector_id="decide_triage_action",
                ),
            ),
        )
        cfg = AgentConfig(
            mode="dry-run",
            repos=(RepoEntry(repo="o/r", board="board-r", clone_path="/tmp/o-r"),),
        )

        async def scenario():
            runner = mock.AsyncMock(return_value=host)
            with mock.patch("lokay.flows.triage.run_package_path_async", new=runner):
                result = await run_pr_triage_decide(
                    db_path=Path("/tmp/process-idle.sqlite"),
                    config=cfg,
                    dry_run=True,
                )
            return result, runner

        result, runner = asyncio.run(scenario())
        runner.assert_awaited_once()
        self.assertEqual(runner.await_args.kwargs["path_id"], "pr_triage")
        self.assertEqual(result.path_id, "pr_triage")
        self.assertEqual(result.status, "idle")
        self.assertEqual(result.stopped_reason, "no_open_prs")
        self.assertFalse(result.summary["worked"])
        self.assertEqual(result.failed, [])

    def test_failed_process_evidence_is_preserved(self) -> None:
        host = self._host(
            path_id="pr_triage",
            failed=True,
            processes=(
                JournalProcess(
                    id="process-run:pr_triage:read_open_prs",
                    status="succeeded",
                    attempt=1,
                    max_attempts=1,
                    output={"status": "listed", "count": 1},
                    error={},
                    metadata={"correlation_path_id": "pr_triage", "effector_id": "read_open_prs"},
                    correlation_path_id="pr_triage",
                    effector_id="read_open_prs",
                ),
                JournalProcess(
                    id="process-run:pr_triage:decide_triage_action",
                    status="failed",
                    attempt=1,
                    max_attempts=1,
                    output={"status": "error"},
                    error={"message": "dispatch failed"},
                    metadata={"correlation_path_id": "pr_triage", "effector_id": "decide_triage_action"},
                    correlation_path_id="pr_triage",
                    effector_id="decide_triage_action",
                ),
            ),
        )
        cfg = AgentConfig(
            mode="dry-run",
            repos=(RepoEntry(repo="o/r", board="board-r", clone_path="/tmp/o-r"),),
        )

        async def scenario():
            runner = mock.AsyncMock(return_value=host)
            with mock.patch("lokay.flows.triage.run_package_path_async", new=runner):
                return await run_pr_triage_decide(
                    db_path=Path("/tmp/process-failed.sqlite"),
                    config=cfg,
                    dry_run=True,
                )

        result = asyncio.run(scenario())
        self.assertEqual(result.status, "failed")
        self.assertEqual([item["step_id"] for item in result.failed], ["decide_triage_action"])
        self.assertEqual(result.failed[0]["error"]["message"], "dispatch failed")
        self.assertIn("decide_triage_action", result.summary["failed_steps"])


class ProcessRepairLaneContractTests(unittest.TestCase):
    """Repair ownership lives on pr_repair after pr_triage decides action=repair."""

    def test_pr_triage_decides_repair_without_owning_repair_effectors(self) -> None:
        for conclusion, expected_checks in (
            ("FAILURE", "checks_failed"),
            ("SUCCESS", "checks_passed"),
        ):
            with self.subTest(conclusion=conclusion):
                host = HostPathRunResult(
                    run_id=f"pr-triage-{conclusion.lower()}",
                    path_id="pr_triage",
                    run_status="completed",
                    replayed=False,
                    ticks=4,
                    processes=(
                        JournalProcess(
                            id="run:pr_triage:read_open_prs",
                            status="succeeded",
                            attempt=1,
                            max_attempts=1,
                            output={"status": "listed", "count": 1, "prs": [{"number": 9, "repo": "o/r"}]},
                            error={},
                            metadata={"correlation_path_id": "pr_triage", "effector_id": "read_open_prs"},
                            correlation_path_id="pr_triage",
                            effector_id="read_open_prs",
                        ),
                        JournalProcess(
                            id="run:pr_triage:select_fix_pr",
                            status="succeeded",
                            attempt=1,
                            max_attempts=1,
                            output={"status": "selected", "repo": "o/r", "number": 9},
                            error={},
                            metadata={"correlation_path_id": "pr_triage", "effector_id": "select_fix_pr"},
                            correlation_path_id="pr_triage",
                            effector_id="select_fix_pr",
                        ),
                        JournalProcess(
                            id="run:pr_triage:evaluate_checks",
                            status="succeeded",
                            attempt=1,
                            max_attempts=1,
                            output={"status": expected_checks},
                            error={},
                            metadata={"correlation_path_id": "pr_triage", "effector_id": "evaluate_checks"},
                            correlation_path_id="pr_triage",
                            effector_id="evaluate_checks",
                        ),
                        JournalProcess(
                            id="run:pr_triage:evaluate_test_evidence",
                            status="succeeded",
                            attempt=1,
                            max_attempts=1,
                            output={"status": "evidence_missing"},
                            error={},
                            metadata={"correlation_path_id": "pr_triage", "effector_id": "evaluate_test_evidence"},
                            correlation_path_id="pr_triage",
                            effector_id="evaluate_test_evidence",
                        ),
                        JournalProcess(
                            id="run:pr_triage:decide_triage_action",
                            status="succeeded",
                            attempt=1,
                            max_attempts=1,
                            output={"status": "decided", "action": "repair", "reason": "missing_test_evidence"},
                            error={},
                            metadata={"correlation_path_id": "pr_triage", "effector_id": "decide_triage_action"},
                            correlation_path_id="pr_triage",
                            effector_id="decide_triage_action",
                        ),
                    ),
                )
                cfg = AgentConfig(
                    mode="dry-run",
                    repos=(RepoEntry(repo="o/r", board="board-r", clone_path="/tmp/o-r"),),
                )

                async def scenario():
                    runner = mock.AsyncMock(return_value=host)
                    with mock.patch("lokay.flows.triage.run_package_path_async", new=runner):
                        result = await run_pr_triage_decide(
                            db_path=Path("/tmp/process-repair.sqlite"),
                            config=cfg,
                            dry_run=True,
                            limit=1,
                        )
                    return result, runner

                result, runner = asyncio.run(scenario())
                kwargs = runner.await_args.kwargs
                self.assertEqual(kwargs["path_id"], "pr_triage")
                self.assertEqual(set(kwargs["effector_inputs"]), set(PROCESS_CONTRACTS["pr_triage"].allowed_effectors))
                self.assertNotIn("invoke_repair_omp", kwargs["effector_inputs"])
                self.assertNotIn("add_repair_worktree", kwargs["effector_inputs"])
                self.assertEqual(result.path_id, "pr_triage")
                self.assertEqual(result.action, "repair")
                self.assertTrue(result.summary["worked"])
                self.assertEqual(result.failed, [])
                steps = {item["step_id"]: item for item in result.processes}
                self.assertEqual(steps["evaluate_checks"]["output"]["status"], expected_checks)
                self.assertEqual(steps["evaluate_test_evidence"]["output"]["status"], "evidence_missing")
                self.assertEqual(steps["decide_triage_action"]["output"]["action"], "repair")

    def test_pr_repair_contract_owns_repair_lane_and_forbids_triage_siblings(self) -> None:
        contract = PROCESS_CONTRACTS["pr_repair"]
        self.assertEqual(contract.path_id, "pr_repair")
        for effector_id in (
            "read_repair_context",
            "read_repair_remote_head",
            "add_repair_worktree",
            "decide_repair_attempt",
            "invoke_repair_omp",
            "build_repair_receipt",
        ):
            self.assertIn(effector_id, contract.allowed_effectors)
        for sibling in (
            "read_open_prs",
            "decide_triage_action",
            "merge_pr",
            "post_pr_comment",
        ):
            self.assertIn(sibling, contract.forbidden_sibling_effectors)
            self.assertNotIn(sibling, contract.allowed_effectors)

        cfg = AgentConfig(
            mode="dry-run",
            repos=(RepoEntry(repo="o/r", board="board-r", clone_path="/tmp/o-r"),),
            raw={"candidate": "a" * 64},
        )
        effective = build_effective_run(
            contract=contract,
            process_id="pr_repair",
            run_id="repair-run",
            db_path=Path("/tmp/repair.sqlite"),
            cfg=cfg,
            generation="test-generation",
            candidate_id="a" * 64,
            config_sha256="b" * 64,
            command="lokay-process-pr_repair",
            predecessor_evidence={
                "groups": [["pr_decision"]],
                "receipts": {
                    "pr_decision": {"digest": "c" * 64, "status": "ready"},
                },
                "pr_decision": "c" * 64,
            },
        )
        self.assertEqual(effective["inputs"]["path_id"], "pr_repair")
        self.assertEqual(set(effective["effector_inputs"]), set(contract.allowed_effectors))
        self.assertIn("decide_repair_attempt", effective["effector_inputs"])
        self.assertNotIn("decide_triage_action", effective["effector_inputs"])
        self.assertEqual(effective["max_ticks"], contract.max_ticks)
        self.assertEqual(effective["inputs"]["pr_decision"], "c" * 64)

    def test_repair_attempt_stays_noop_when_executor_disabled(self) -> None:
        from lokay.steps import repair

        request = {
            "input": {
                "dry_run": True,
                "path_id": "pr_repair",
                "executor_enabled": False,
                "conduction": {
                    "verify_repair_worktree": {
                        "ok": True,
                        "status": "ready",
                        "worktree_path": "/tmp/wt",
                        "verified_head": "abc",
                    },
                    "read_repair_omp_preconditions": {
                        "ok": True,
                        "status": "ready",
                        "worktree_path": "/tmp/wt",
                        "pre_head": "abc",
                    },
                },
            },
            "config": {"executor_enabled": False},
        }
        out = repair.decide_repair_attempt(request)
        self.assertEqual(out["status"], "noop")
        self.assertFalse(out.get("authorize", True))
        self.assertIn(out.get("reason"), {"executor_disabled", "dry_run", "disabled"})


class RetiredAggregateActivationTests(unittest.TestCase):
    def test_run_all_is_retired_for_aggregate_auto_worker(self) -> None:
        with self.assertRaises(RuntimeError) as raised:
            asyncio.run(
                run_all(
                    db_path=Path("/tmp/auto-worker.sqlite"),
                    config=AgentConfig(mode="dry-run"),
                    dry_run=True,
                )
            )
        message = str(raised.exception)
        self.assertIn("auto_worker", message)
        self.assertIn("lokay.process", message)
        for process_id in ("repo_issue_poll", "pr_triage", "pr_repair", "cleanup"):
            self.assertIn(process_id, message)

    def test_run_intake_flow_is_retired_for_aggregate_issue_intake(self) -> None:
        with self.assertRaises(RuntimeError) as raised:
            asyncio.run(
                run_intake_flow(
                    db_path=Path("/tmp/issue-intake.sqlite"),
                    config=AgentConfig(mode="dry-run"),
                    dry_run=True,
                )
            )
        message = str(raised.exception)
        self.assertIn("issue_intake", message)
        self.assertIn("lokay.process", message)
        self.assertIn("repo_issue_poll", message)


if __name__ == "__main__":
    unittest.main()
