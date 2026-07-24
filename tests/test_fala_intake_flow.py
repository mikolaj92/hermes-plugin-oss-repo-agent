from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock


from repo_agent.config import AgentConfig, RepoEntry
from repo_agent.flows.intake import run_intake_flow
from repo_agent.steps.claim import claim_github_issue
from repo_agent.steps.kanban_intake import ensure_kanban_intake
from repo_agent.steps.poll import poll_eligible_issues


class _Req(dict):
    def __init__(self, input_data=None, config=None):
        super().__init__(input=input_data or {}, config=config or {})


class PollStepTests(unittest.TestCase):
    def test_filters_ready_and_foreign(self) -> None:
        issues = [
            {
                "number": 1,
                "title": "ready one",
                "url": "https://example/1",
                "labels": [{"name": "ai:ready"}],
                "assignees": [],
            },
            {
                "number": 2,
                "title": "blocked",
                "url": "https://example/2",
                "labels": [{"name": "ai:ready"}, {"name": "ai:blocked"}],
                "assignees": [],
            },
            {
                "number": 3,
                "title": "foreign",
                "url": "https://example/3",
                "labels": [{"name": "ai:ready"}],
                "assignees": [{"login": "someone-else"}],
            },
        ]

        with mock.patch("repo_agent.steps.poll.gh_json", return_value=issues):
            result = poll_eligible_issues(
                _Req(
                    {
                        "repos": [{"repo": "o/r", "board": "board-r"}],
                        "dry_run": True,
                    },
                    config={"assignee": "mikolaj92", "ready_label": "ai:ready"},
                )
            )

        self.assertIsInstance(result, dict)
        self.assertEqual(result["eligible_count"], 1)
        self.assertEqual(result["selected"]["number"], 1)
        self.assertEqual(result["skipped_count"], 2)


class ClaimKanbanDryRunTests(unittest.TestCase):
    def test_claim_noop_without_selection(self) -> None:
        result = claim_github_issue(
            _Req({"conduction": {"poll": {"selected": None, "dry_run": True}}})
        )
        # Also support needs-style if conduction empty — empty selected via conduction
        self.assertEqual(result["status"], "noop")
        self.assertFalse(result["mutated"])

    def test_claim_dry_run_plans(self) -> None:
        result = claim_github_issue(
            _Req(
                {
                    "dry_run": True,
                    "conduction": {
                        "poll": {
                            "selected": {
                                "repo": "o/r",
                                "number": 7,
                                "title": "t",
                                "board": "b",
                                "labels": ["ai:ready"],
                                "assignees": [],
                            },
                            "dry_run": True,
                        }
                    },
                },
                config={"assignee": "mikolaj92"},
            )
        )
        self.assertEqual(result["status"], "planned")
        self.assertFalse(result["mutated"])

    def test_kanban_dry_run_plans(self) -> None:
        result = ensure_kanban_intake(
            _Req(
                {
                    "dry_run": True,
                    "conduction": {
                        "claim": {
                            "status": "planned",
                            "selected": {
                                "repo": "o/r",
                                "number": 7,
                                "title": "t",
                                "url": "https://x",
                                "board": "b",
                                "labels": [],
                            },
                            "dry_run": True,
                        }
                    },
                }
            )
        )
        self.assertEqual(result["status"], "planned")
        self.assertIn("planned", result)


class TickAllHostPathTests(unittest.TestCase):
    @staticmethod
    def _host(*, failed: bool = False):
        from repo_agent.flows.runtime import HostPathRunResult, JournalProcess

        run_status = "failed" if failed else "completed"
        process_status = "failed" if failed else "succeeded"
        process = JournalProcess(
            id="intake_poll" if not failed else "dispatch_open_pull_request",
            status=process_status,
            attempt=1,
            max_attempts=1,
            output={"status": "planned" if not failed else "error"},
            error={} if not failed else {"message": "dispatch failed"},
        )
        return HostPathRunResult(
            run_id="auto-worker-run",
            path_id="auto_worker",
            run_status=run_status,
            replayed=False,
            ticks=1,
            processes=(process,),
        )

    def test_run_all_makes_one_auto_worker_host_call(self) -> None:
        from repo_agent.tick_all import run_all

        cfg = AgentConfig(
            mode="dry-run",
            repos=(RepoEntry(repo="o/r", board="board-r", clone_path="/tmp/o-r"),),
        )
        runner = mock.AsyncMock(return_value=self._host())
        with mock.patch("repo_agent.tick_all.run_package_path_async", new=runner):
            result = asyncio.run(
                run_all(db_path=Path("/tmp/auto-worker.sqlite"), config=cfg, dry_run=True, limit=7)
            )

        runner.assert_awaited_once()
        self.assertEqual(runner.await_args.kwargs["path_id"], "auto_worker")
        self.assertEqual(runner.await_args.kwargs["max_ticks"], 40)
        effector_inputs = runner.await_args.kwargs["effector_inputs"]
        self.assertEqual(effector_inputs["triage_list_ai_fix_prs"]["limit"], 7)
        self.assertTrue(effector_inputs["cleanup_remove_worktree"]["require_safe"])
        self.assertTrue(effector_inputs["dispatch_prepare_worktree"]["dry_run"])
        self.assertNotIn("clone_path", effector_inputs["dispatch_prepare_worktree"])
        self.assertIn("cleanup_write_cleanup_receipt", effector_inputs)
        self.assertIn("cleanup_receipt_path", effector_inputs["cleanup_parse_issue_from_branch"])
        self.assertEqual(result["path_id"], "auto_worker")
        self.assertFalse(result["any_failed"])
        self.assertEqual(result["processes"][0]["id"], "intake_poll")
        self.assertEqual(result["processes"][0]["output"]["status"], "planned")
    def test_multi_repo_auto_worker_does_not_inject_first_repo_context(self) -> None:
        from repo_agent.tick_all import run_all

        cfg = AgentConfig(
            mode="dry-run",
            repos=(
                RepoEntry(repo="o/first", board="first-board", clone_path="/tmp/first"),
                RepoEntry(repo="o/temida", board="temida-board", clone_path="/tmp/temida"),
            ),
        )
        runner = mock.AsyncMock(return_value=self._host())
        with mock.patch("repo_agent.tick_all.run_package_path_async", new=runner):
            asyncio.run(run_all(db_path=Path("/tmp/auto-worker.sqlite"), config=cfg, dry_run=True, limit=7))
        inputs = runner.await_args.kwargs["effector_inputs"]
        for value in inputs.values():
            self.assertNotEqual(value.get("repo"), "o/first")
            self.assertNotEqual(value.get("board"), "first-board")
            self.assertNotEqual(value.get("clone_path"), "/tmp/first")
        self.assertEqual(runner.await_args.kwargs["inputs"]["repos"][1]["repo"], "o/temida")


    def test_empty_auto_worker_is_idle_and_not_worked(self) -> None:
        from repo_agent.flows.runtime import HostPathRunResult, JournalProcess
        from repo_agent.tick_all import run_all

        host = HostPathRunResult(
            run_id="auto-worker-idle",
            path_id="auto_worker",
            run_status="completed",
            replayed=False,
            ticks=1,
            processes=(
                JournalProcess(
                    id="intake_poll",
                    status="succeeded",
                    attempt=1,
                    max_attempts=1,
                    output={"status": "noop", "reason": "no_eligible_issues", "mutated": False},
                    error={},
                ),
            ),
        )
        runner = mock.AsyncMock(return_value=host)
        with mock.patch("repo_agent.tick_all.run_package_path_async", new=runner):
            result = asyncio.run(
                run_all(db_path=Path("/tmp/auto-worker-idle.sqlite"), config=AgentConfig(mode="dry-run"), dry_run=True)
            )

        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["stopped_reason"], "idle")
        self.assertFalse(result["summary"]["worked"])
        self.assertFalse(result["any_failed"])

    def test_run_all_preserves_failed_process_evidence_and_nonzero_marker(self) -> None:
        from repo_agent.tick_all import run_all

        cfg = AgentConfig(mode="dry-run")
        runner = mock.AsyncMock(return_value=self._host(failed=True))
        with mock.patch("repo_agent.tick_all.run_package_path_async", new=runner):
            result = asyncio.run(
                run_all(db_path=Path("/tmp/auto-worker-failed.sqlite"), config=cfg, dry_run=True)
            )

        runner.assert_awaited_once()
        self.assertTrue(result["any_failed"])
        self.assertEqual(result["failed"][0]["id"], "dispatch_open_pull_request")
        self.assertEqual(result["failed"][0]["error"]["message"], "dispatch failed")


class IntakeFlowE2ETests(unittest.TestCase):
    def test_flow_runs_direction_then_claim_kanban_dry(self) -> None:
        issues = [
            {
                "number": 9,
                "title": "ship it",
                "url": "https://example/9",
                "labels": [{"name": "ai:ready"}],
                "assignees": [],
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            gh = Path(tmp) / "gh"
            gh.write_text(
                "#!/bin/sh\nprintf '%s\\n' '[{\"number\":9,\"title\":\"ship it\",\"url\":\"https://example/9\",\"labels\":[{\"name\":\"ai:ready\"}],\"assignees\":[]}]'\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)
            cfg = AgentConfig(
                mode="dry-run",
                gh_cli=str(gh),
                assignee="mikolaj92",
                repos=(
                    RepoEntry(
                        repo="o/r",
                        board="board-r",
                        clone_path="/tmp/o-r",
                        priority=1,
                    ),
                ),
            )
            db = Path(tmp) / "state.sqlite"
            result = asyncio.run(
                run_intake_flow(
                    db_path=db,
                    config=cfg,
                    dry_run=True,
                    limit=5,
                    run_id="test-intake-1",
                )
            )

        self.assertEqual(result.failed, [], msg=str(result.processes))
        self.assertEqual(result.stopped_reason, "worked")
        self.assertEqual(result.ticks, 5)
        self.assertEqual(len(result.failed), 0)
        steps = {p["step_id"]: p for p in result.processes}
        self.assertEqual(steps["poll"]["status"], "succeeded")
        self.assertEqual(steps["decide_issue_action"]["status"], "succeeded")
        self.assertEqual(steps["comment_issue"]["status"], "succeeded")
        self.assertEqual(steps["claim"]["status"], "succeeded")
        self.assertEqual(steps["kanban"]["status"], "succeeded")
        self.assertEqual(result.summary["eligible_count"], 1)
        self.assertEqual(result.summary["issue_action"], "accept")
        # dry-run claim/kanban use status planned (envelope)
        self.assertIn(result.summary["claim_status"], ("planned", "claimed"))
        self.assertIn(result.summary["kanban_status"], ("planned", "created", "exists"))
        self.assertEqual(result.fala_version, "0.7.9")


if __name__ == "__main__":
    unittest.main()
