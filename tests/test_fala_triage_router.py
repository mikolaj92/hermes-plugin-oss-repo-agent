from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from repo_agent.config import AgentConfig, RepoEntry
from repo_agent.flows.common import PathRunResult
from repo_agent.flows.intake import IntakeRunResult
from repo_agent.tick_all import run_all
from repo_agent.flows.triage import run_follow_up_path, run_triage_with_router


class TriageRouterAggregationTests(unittest.TestCase):
    def _result(self, *, status: str, path_id: str, action: str | None, step: str) -> PathRunResult:
        process = {"id": f"{path_id}-process", "step_id": step, "status": status}
        return PathRunResult(
            run_id=f"{path_id}-run",
            path_id=path_id,
            dry_run=True,
            ticks=1,
            stopped_reason="idle",
            completed=[process] if status == "completed" else [],
            failed=[process] if status in {"failed", "timed_out", "cancelled"} else [],
            processes=[process],
            summary={"run_status": status},
            status=status,
            action=action,
        )

    def test_waiting_follow_up_is_returned_and_propagated(self) -> None:
        decide = self._result(status="completed", path_id="pr_triage", action="merge", step="decide")
        follow = self._result(status="waiting", path_id="pr_merge", action="merge", step="merge")
        cfg = AgentConfig(
            mode="dry-run",
            repos=(RepoEntry(repo="o/r", board="board", clone_path="/tmp/o-r"),),
        )

        async def scenario() -> PathRunResult:
            with mock.patch("repo_agent.flows.triage.run_pr_triage_decide", new=mock.AsyncMock(return_value=decide)), mock.patch(
                "repo_agent.flows.triage.run_follow_up_path", new=mock.AsyncMock(return_value=follow)
            ):
                return await run_triage_with_router(
                    db_path=Path(tempfile.mktemp()), config=cfg, dry_run=True
                )

        result = asyncio.run(scenario())
        self.assertEqual(result.status, "waiting")
        self.assertEqual(result.summary["follow_up_status"], "waiting")
        self.assertEqual(result.follow_up["status"], "waiting")
        self.assertEqual([p["step_id"] for p in result.processes], ["decide", "merge"])
        self.assertEqual([p["step_id"] for p in result.completed], ["decide"])

    def test_timed_out_follow_up_is_failed_result(self) -> None:
        decide = self._result(status="completed", path_id="pr_triage", action="merge", step="decide")
        follow = self._result(status="timed_out", path_id="pr_merge", action="merge", step="merge")
        cfg = AgentConfig(
            mode="dry-run",
            repos=(RepoEntry(repo="o/r", board="board", clone_path="/tmp/o-r"),),
        )

        async def scenario() -> PathRunResult:
            with mock.patch("repo_agent.flows.triage.run_pr_triage_decide", new=mock.AsyncMock(return_value=decide)), mock.patch(
                "repo_agent.flows.triage.run_follow_up_path", new=mock.AsyncMock(return_value=follow)
            ):
                return await run_triage_with_router(
                    db_path=Path(tempfile.mktemp()), config=cfg, dry_run=True
                )

        result = asyncio.run(scenario())
        self.assertEqual(result.status, "timed_out")
        self.assertEqual(result.summary["run_status"], "timed_out")
        self.assertEqual(result.follow_up["status"], "timed_out")
        self.assertEqual([p["step_id"] for p in result.failed], ["merge"])

    def test_successful_follow_up_keeps_success_status(self) -> None:
        decide = self._result(status="completed", path_id="pr_triage", action="merge", step="decide")
        follow = self._result(status="completed", path_id="pr_merge", action="merge", step="merge")
        cfg = AgentConfig(
            mode="dry-run",
            repos=(RepoEntry(repo="o/r", board="board", clone_path="/tmp/o-r"),),
        )

        async def scenario() -> PathRunResult:
            with mock.patch("repo_agent.flows.triage.run_pr_triage_decide", new=mock.AsyncMock(return_value=decide)), mock.patch(
                "repo_agent.flows.triage.run_follow_up_path", new=mock.AsyncMock(return_value=follow)
            ):
                return await run_triage_with_router(
                    db_path=Path(tempfile.mktemp()), config=cfg, dry_run=True
                )

        result = asyncio.run(scenario())
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.summary["follow_up_status"], "completed")
        self.assertFalse(result.failed)
        self.assertEqual([p["step_id"] for p in result.completed], ["decide", "merge"])

    def test_completed_follow_up_with_failed_process_promotes_parent(self) -> None:
        decide = self._result(status="completed", path_id="pr_triage", action="merge", step="decide")
        follow = self._result(status="completed", path_id="pr_merge", action="merge", step="merge")
        follow.processes[0]["status"] = "failed"
        follow.completed = []
        follow.failed = []
        follow.summary["failed_steps"] = ["merge"]
        cfg = AgentConfig(
            mode="dry-run",
            repos=(RepoEntry(repo="o/r", board="board", clone_path="/tmp/o-r"),),
        )

        async def scenario() -> PathRunResult:
            with mock.patch("repo_agent.flows.triage.run_pr_triage_decide", new=mock.AsyncMock(return_value=decide)), mock.patch(
                "repo_agent.flows.triage.run_follow_up_path", new=mock.AsyncMock(return_value=follow)
            ):
                return await run_triage_with_router(
                    db_path=Path(tempfile.mktemp()), config=cfg, dry_run=True
                )

        result = asyncio.run(scenario())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.summary["run_status"], "failed")
        self.assertIn("merge", result.summary["failed_steps"])
        self.assertEqual([p["step_id"] for p in result.failed], ["merge"])

    def test_router_does_not_seed_followup_from_transient_decide_run(self) -> None:
        decide = self._result(status="completed", path_id="pr_triage", action="merge", step="decide")
        decide.run_id = "decide-run"
        follow = self._result(status="completed", path_id="pr_merge", action="merge", step="merge")
        cfg = AgentConfig(
            mode="dry-run",
            repos=(RepoEntry(repo="o/r", board="board", clone_path="/tmp/o-r"),),
        )

        async def scenario() -> tuple[PathRunResult, mock.AsyncMock]:
            follow_up = mock.AsyncMock(return_value=follow)
            with mock.patch("repo_agent.flows.triage.run_pr_triage_decide", new=mock.AsyncMock(return_value=decide)), mock.patch(
                "repo_agent.flows.triage.run_follow_up_path", new=follow_up
            ):
                result = await run_triage_with_router(
                    db_path=Path(tempfile.mktemp()), config=cfg, dry_run=True
                )
            return result, follow_up

        result, follow_up = asyncio.run(scenario())
        self.assertEqual(result.follow_up["status"], "completed")
        self.assertNotIn("run_id", follow_up.call_args.kwargs)

    def test_follow_up_id_is_stable_across_worker_ticks(self) -> None:
        from types import SimpleNamespace

        cfg = AgentConfig(
            mode="dry-run",
            repos=(RepoEntry(repo="o/r", board="board", clone_path="/tmp/o-r"),),
        )
        runtime_result = SimpleNamespace(
            outcome=SimpleNamespace(ticks=1, stopped_reason="idle", completed=[], failed=[]),
            status="completed",
            processes=[],
        )

        async def scenario() -> tuple[PathRunResult, PathRunResult, mock.AsyncMock]:
            runner = mock.AsyncMock(return_value=runtime_result)
            with mock.patch("repo_agent.flows.triage.run_repo_agent_path", new=runner):
                first = await run_follow_up_path(
                    action="merge",
                    db_path=Path(tempfile.mktemp()),
                    config=cfg,
                    dry_run=True,
                    repo="o/r",
                    pr={"number": 7, "headRefOid": "head-1"},
                    number=7,
                    worker_id="worker-one",
                )
                second = await run_follow_up_path(
                    action="merge",
                    db_path=Path(tempfile.mktemp()),
                    config=cfg,
                    dry_run=True,
                    repo="o/r",
                    pr={"number": 7, "headRefOid": "head-1"},
                    number=7,
                    worker_id="worker-two",
                )
            assert first is not None and second is not None
            return first, second, runner

        first, second, runner = asyncio.run(scenario())
        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(
            runner.call_args_list[0].kwargs["run"].id,
            runner.call_args_list[1].kwargs["run"].id,
        )
    def test_tick_all_marks_waiting_triage_as_unresolved(self) -> None:
        cfg = AgentConfig(
            mode="dry-run",
            repos=(RepoEntry(repo="o/r", board="board", clone_path="/tmp/o-r"),),
        )
        intake = IntakeRunResult("i", True, 0, "idle", [], [], [], {}, status="completed")
        completed = self._result(status="completed", path_id="dispatch", action=None, step="dispatch")
        waiting = self._result(status="waiting", path_id="pr_triage", action="merge", step="merge")
        cleanup = self._result(status="completed", path_id="cleanup", action=None, step="cleanup")

        async def scenario() -> dict:
            with mock.patch("repo_agent.tick_all.run_intake_flow", new=mock.AsyncMock(return_value=intake)), mock.patch(
                "repo_agent.tick_all.run_issue_to_pr_flow", new=mock.AsyncMock(return_value=completed)
            ), mock.patch(
                "repo_agent.tick_all.run_triage_with_router", new=mock.AsyncMock(return_value=waiting)
            ), mock.patch(
                "repo_agent.tick_all.run_cleanup_flow", new=mock.AsyncMock(return_value=cleanup)
            ):
                return await run_all(db_path=Path(tempfile.mktemp()), config=cfg, dry_run=True)

        self.assertTrue(asyncio.run(scenario())["any_failed"])


if __name__ == "__main__":
    unittest.main()
