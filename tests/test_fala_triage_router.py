from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from repo_agent.config import AgentConfig, RepoEntry
from repo_agent.flows.common import PathRunResult
from repo_agent.flows.runtime import HostPathRunResult, JournalProcess
from repo_agent.flows.triage import run_pr_triage_decide, run_triage_flow


def _process(
    step_id: str,
    *,
    status: str = "succeeded",
    output: dict | None = None,
    error: dict | None = None,
) -> JournalProcess:
    return JournalProcess(
        id=f"run:{step_id}",
        status=status,
        attempt=1,
        max_attempts=1,
        output=output or {},
        error=error or {},
    )


def _host(
    *,
    run_status: str = "completed",
    processes: list[JournalProcess] | None = None,
    ticks: int = 3,
    run_id: str = "pr-triage-run",
) -> HostPathRunResult:
    return HostPathRunResult(
        run_id=run_id,
        path_id="pr_triage",
        run_status=run_status,
        replayed=False,
        ticks=ticks,
        processes=tuple(processes or ()),
    )


class TriagePackageFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = AgentConfig(
            mode="dry-run",
            repos=(RepoEntry(repo="o/r", board="board", clone_path="/tmp/o-r"),),
        )

    def test_single_package_path_invocation(self) -> None:
        host = _host(
            processes=[
                _process(
                    "list_ai_fix_prs",
                    output={"status": "listed", "count": 0, "prs": [], "reason": "no_open_prs"},
                ),
                _process(
                    "decide_triage_action",
                    output={"status": "noop", "action": "skip", "reason": "no_open_prs"},
                ),
            ]
        )

        async def scenario() -> tuple[PathRunResult, mock.AsyncMock]:
            runner = mock.AsyncMock(return_value=host)
            with mock.patch("repo_agent.flows.triage.run_package_path_async", new=runner):
                result = await run_triage_flow(
                    db_path=Path(tempfile.mktemp()),
                    config=self.cfg,
                    dry_run=True,
                )
            return result, runner

        result, runner = asyncio.run(scenario())
        self.assertEqual(runner.await_count, 1)
        kwargs = runner.await_args.kwargs
        self.assertEqual(kwargs["path_id"], "pr_triage")
        self.assertTrue(str(kwargs["package_path"]).endswith("fala-package.toml"))
        self.assertEqual(result.path_id, "pr_triage")
        self.assertEqual(result.status, "idle")
        self.assertFalse(result.summary.get("worked"))
        self.assertEqual(result.action, "skip")

    def test_failed_process_evidence_is_preserved(self) -> None:
        host = _host(
            run_status="failed",
            processes=[
                _process(
                    "list_ai_fix_prs",
                    output={"status": "listed", "count": 1, "prs": [{"number": 9}]},
                ),
                _process(
                    "decide_triage_action",
                    status="failed",
                    output={"status": "failed", "ok": False, "reason": "invalid_pr"},
                    error={"reason": "invalid_pr"},
                ),
                _process(
                    "claim_pr",
                    status="cancelled",
                    output={"status": "noop", "reason": "not_selected", "worked": False},
                ),
            ],
        )

        async def scenario() -> PathRunResult:
            with mock.patch(
                "repo_agent.flows.triage.run_package_path_async",
                new=mock.AsyncMock(return_value=host),
            ):
                return await run_pr_triage_decide(
                    db_path=Path(tempfile.mktemp()),
                    config=self.cfg,
                    dry_run=True,
                )

        result = asyncio.run(scenario())
        self.assertEqual(result.status, "failed")
        self.assertEqual([p["step_id"] for p in result.failed], ["decide_triage_action", "claim_pr"])
        self.assertIn("decide_triage_action", result.summary["failed_steps"])
        self.assertIn("claim_pr", result.summary["failed_steps"])

    def test_timed_out_process_keeps_exact_status(self) -> None:
        host = _host(
            run_status="timed_out",
            processes=[
                _process(
                    "decide_triage_action",
                    output={"status": "decided", "action": "merge", "reason": "ready"},
                ),
                _process(
                    "merge",
                    status="timed_out",
                    output={},
                    error={"reason": "lease_timeout"},
                ),
            ],
        )

        async def scenario() -> PathRunResult:
            with mock.patch(
                "repo_agent.flows.triage.run_package_path_async",
                new=mock.AsyncMock(return_value=host),
            ):
                return await run_triage_flow(
                    db_path=Path(tempfile.mktemp()),
                    config=self.cfg,
                    dry_run=True,
                )

        result = asyncio.run(scenario())
        self.assertEqual(result.status, "timed_out")
        self.assertEqual(result.summary["run_status"], "timed_out")
        self.assertEqual([p["step_id"] for p in result.failed], ["merge"])

    def test_selected_merge_action_is_surfaced(self) -> None:
        host = _host(
            processes=[
                _process(
                    "load_pr_fields",
                    output={
                        "status": "loaded",
                        "number": 7,
                        "pr": {"number": 7, "headRefOid": "abc"},
                    },
                ),
                _process(
                    "decide_triage_action",
                    output={"status": "decided", "action": "merge", "reason": "ready"},
                ),
                _process(
                    "claim_pr",
                    output={"status": "planned", "mutated": False},
                ),
                _process(
                    "merge",
                    output={"status": "planned", "mutated": False},
                ),
                _process(
                    "comment_pr",
                    output={"status": "noop", "reason": "not_selected", "worked": False},
                ),
            ]
        )

        async def scenario() -> PathRunResult:
            with mock.patch(
                "repo_agent.flows.triage.run_package_path_async",
                new=mock.AsyncMock(return_value=host),
            ):
                return await run_triage_flow(
                    db_path=Path(tempfile.mktemp()),
                    config=self.cfg,
                    dry_run=True,
                    pr_number=7,
                )

        result = asyncio.run(scenario())
        self.assertEqual(result.action, "merge")
        self.assertEqual(result.summary["pr_number"], 7)
        self.assertEqual(result.status, "completed")
        self.assertFalse(result.failed)

    def test_empty_repo_config_is_idle_without_host(self) -> None:
        cfg = AgentConfig(mode="dry-run", repos=())

        async def scenario() -> tuple[PathRunResult, mock.AsyncMock]:
            runner = mock.AsyncMock()
            with mock.patch("repo_agent.flows.triage.run_package_path_async", new=runner):
                result = await run_triage_flow(
                    db_path=Path(tempfile.mktemp()),
                    config=cfg,
                    dry_run=True,
                )
            return result, runner

        result, runner = asyncio.run(scenario())
        runner.assert_not_called()
        self.assertEqual(result.status, "idle")
        self.assertEqual(result.action, "skip")
        self.assertEqual(result.summary["reason"], "no_repositories")


class BranchDecisionGateTests(unittest.TestCase):
    def test_merge_handlers_noop_when_comment_selected(self) -> None:
        from repo_agent.steps import triage

        request = {
            "input": {
                "repo": "o/r",
                "number": 3,
                "dry_run": True,
                "conduction": {
                    "decide_triage_action": {
                        "status": "decided",
                        "action": "comment_block",
                        "reason": "missing_test_evidence",
                    }
                },
            },
            "config": {"assignee": "me"},
        }
        for handler in (
            triage.claim_pr_assignee,
            triage.merge_pull_request,
            triage.write_merge_receipt,
            triage.close_linked_issue,
        ):
            with self.subTest(handler=handler.__name__):
                out = handler(request)
                self.assertEqual(out["status"], "noop")
                self.assertEqual(out["reason"], "not_selected")
                self.assertFalse(out.get("mutated"))
                self.assertFalse(out.get("worked"))

    def test_comment_handler_noop_when_merge_selected(self) -> None:
        from repo_agent.steps import triage

        out = triage.comment_pr_once(
            {
                "input": {
                    "repo": "o/r",
                    "number": 3,
                    "body": "blocked",
                    "dry_run": True,
                    "conduction": {
                        "decide_triage_action": {
                            "status": "decided",
                            "action": "merge",
                            "reason": "ready",
                        }
                    },
                },
                "config": {},
            }
        )
        self.assertEqual(out["status"], "noop")
        self.assertEqual(out["reason"], "not_selected")
        self.assertFalse(out.get("worked"))

    def test_failed_decision_blocks_branch_mutation(self) -> None:
        from repo_agent.steps import triage

        out = triage.claim_pr_assignee(
            {
                "input": {
                    "repo": "o/r",
                    "number": 3,
                    "dry_run": False,
                    "conduction": {
                        "decide_triage_action": {
                            "status": "failed",
                            "ok": False,
                            "reason": "invalid_pr",
                        }
                    },
                },
                "config": {"assignee": "me"},
            }
        )
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["reason"], "upstream_failed")
        self.assertFalse(out.get("mutated"))

    def test_repair_handlers_noop_when_merge_selected(self) -> None:
        from repo_agent.steps import repair

        request = {
            "input": {
                "repo": "o/r",
                "number": 8,
                "board": "b",
                "dry_run": True,
                "conduction": {
                    "decide_triage_action": {
                        "status": "decided",
                        "action": "merge",
                        "reason": "ready",
                    },
                    "load_pr_fields": {
                        "pr": {"number": 8, "title": "x", "headRefName": "ai/fix/8"},
                    },
                },
            },
            "config": {},
        }
        for handler in (repair.build_repair_prompt, repair.create_review_fix_task):
            with self.subTest(handler=handler.__name__):
                out = handler(request)
                self.assertEqual(out["status"], "noop")
                self.assertEqual(out["reason"], "not_selected")
                self.assertFalse(out.get("worked"))


if __name__ == "__main__":
    unittest.main()
