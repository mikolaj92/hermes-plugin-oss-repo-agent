from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fala.adapters import EffectorRunResult

from repo_agent.config import AgentConfig, RepoEntry
from repo_agent.flows.intake import run_intake_flow
from repo_agent.steps.claim import claim_github_issue
from repo_agent.steps.kanban_intake import ensure_kanban_intake
from repo_agent.steps.poll import poll_eligible_issues


class _Req:
    def __init__(self, input_data=None, config=None):
        self.input = input_data or {}
        self.config = config or {}
        self.process_id = "p1"
        self.impulse_id = None
        self.work_dir = None
        # EffectorRunRequest also carries adapter; steps ignore it when using .input
        self.adapter = None


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

        self.assertIsInstance(result, EffectorRunResult)
        self.assertEqual(result.output["eligible_count"], 1)
        self.assertEqual(result.output["selected"]["number"], 1)
        self.assertEqual(result.output["skipped_count"], 2)


class ClaimKanbanDryRunTests(unittest.TestCase):
    def test_claim_noop_without_selection(self) -> None:
        result = claim_github_issue(
            _Req({"conduction": {"poll": {"selected": None, "dry_run": True}}})
        )
        # Also support needs-style if conduction empty — empty selected via conduction
        self.assertEqual(result.output["status"], "noop")
        self.assertFalse(result.output["mutated"])

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
        self.assertEqual(result.output["status"], "planned")
        self.assertFalse(result.output["mutated"])

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
        self.assertEqual(result.output["status"], "planned")
        self.assertIn("planned", result.output)


class EmptyTickAllTests(unittest.TestCase):
    def test_empty_tick_all_is_controlled_noop(self) -> None:
        from repo_agent.tick_all import run_all

        cfg = AgentConfig(
            mode="dry-run",
            repos=(RepoEntry(repo="o/r", board="board-r", clone_path="/tmp/o-r"),),
        )
        with mock.patch("repo_agent.steps.poll.gh_json", return_value=[]), mock.patch(
            "repo_agent.steps.issue_to_pr.hermes_kanban_json", return_value=[]
        ), mock.patch(
            "repo_agent.steps.triage.run_cmd",
            return_value=mock.Mock(stdout="[]", stderr="", returncode=0),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                result = asyncio.run(
                    run_all(db_path=Path(tmp) / "state.sqlite", config=cfg, dry_run=True)
                )

        self.assertFalse(result["any_failed"])
        self.assertEqual(result["dispatch"]["summary"]["load_status"], "noop")
        self.assertEqual(result["triage"]["summary"]["reason"], "no_open_prs")
        self.assertEqual(result["cleanup"]["status"], "noop")
        self.assertEqual(result["cleanup"]["stopped_reason"], "no_branch")


    def test_nonempty_live_config_forces_explicit_dry_run_across_all_flows(self) -> None:
        from repo_agent.flows.common import PathRunResult
        from repo_agent.flows.intake import IntakeRunResult
        from repo_agent.tick_all import run_all

        cfg = AgentConfig(
            mode="live",
            repos=(RepoEntry(repo="o/r", board="board-r", clone_path="/tmp/o-r"),),
        )
        calls: list[tuple[str, bool]] = []

        def path_result(name: str) -> PathRunResult:
            return PathRunResult(
                run_id=f"{name}-run",
                path_id=name,
                dry_run=True,
                ticks=1,
                stopped_reason="idle",
                completed=[{"id": f"{name}-process", "status": "succeeded"}],
                processes=[{"id": f"{name}-process", "step_id": name, "status": "succeeded"}],
                summary={"result": "planned"},
                status="succeeded",
            )

        async def fake_intake(**kwargs):
            calls.append(("intake", kwargs["dry_run"]))
            return IntakeRunResult(
                run_id="intake-run",
                dry_run=True,
                ticks=1,
                stopped_reason="idle",
                completed=[{"id": "intake-process", "status": "succeeded"}],
                failed=[],
                processes=[{"id": "intake-process", "step_id": "poll", "status": "succeeded"}],
                summary={"result": "planned"},
                status="succeeded",
            )

        async def fake_dispatch(**kwargs):
            calls.append(("dispatch", kwargs["dry_run"]))
            return path_result("dispatch")

        async def fake_triage(**kwargs):
            calls.append(("triage", kwargs["dry_run"]))
            return path_result("triage")

        async def fake_cleanup(**kwargs):
            calls.append(("cleanup", kwargs["dry_run"]))
            return path_result("cleanup")

        with mock.patch("repo_agent.tick_all.run_intake_flow", side_effect=fake_intake), mock.patch(
            "repo_agent.tick_all.run_issue_to_pr_flow", side_effect=fake_dispatch
        ), mock.patch(
            "repo_agent.tick_all.run_triage_with_router", side_effect=fake_triage
        ), mock.patch("repo_agent.tick_all.run_cleanup_flow", side_effect=fake_cleanup):
            result = asyncio.run(
                run_all(
                    db_path=Path("/tmp/nonempty-live-dry-run.sqlite"),
                    config=cfg,
                    dry_run=True,
                    limit=7,
                )
            )

        self.assertEqual(calls, [("intake", True), ("dispatch", True), ("triage", True), ("cleanup", True)])
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["any_failed"])
        for name in ("intake", "dispatch", "triage", "cleanup"):
            self.assertIn(name, result)
            self.assertIsInstance(result[name], dict)
            self.assertTrue(result[name]["dry_run"])
            self.assertIn("run_id", result[name])
            self.assertIn("summary", result[name])

    def test_run_all_aggregates_failed_flow_without_skipping_later_dry_run_flows(self) -> None:
        from repo_agent.flows.common import PathRunResult
        from repo_agent.flows.intake import IntakeRunResult
        from repo_agent.tick_all import run_all

        cfg = AgentConfig(
            mode="live",
            repos=(RepoEntry(repo="o/r", board="board-r", clone_path="/tmp/o-r"),),
        )
        calls: list[tuple[str, bool]] = []

        def intake_result() -> IntakeRunResult:
            return IntakeRunResult(
                run_id="intake-run",
                dry_run=True,
                ticks=1,
                stopped_reason="idle",
                completed=[],
                failed=[],
                processes=[],
                summary={"result": "planned"},
                status="succeeded",
            )

        def path_result(name: str, *, failed: bool = False) -> PathRunResult:
            status = "failed" if failed else "succeeded"
            process = {"id": f"{name}-process", "step_id": name, "status": status}
            return PathRunResult(
                run_id=f"{name}-run",
                path_id=name,
                dry_run=True,
                ticks=1,
                stopped_reason="failed" if failed else "idle",
                completed=[] if failed else [process],
                failed=[process] if failed else [],
                processes=[process],
                summary={"result": "failed" if failed else "planned"},
                status=status,
            )

        async def fake_intake(**kwargs):
            calls.append(("intake", kwargs["dry_run"]))
            return intake_result()

        async def fake_dispatch(**kwargs):
            calls.append(("dispatch", kwargs["dry_run"]))
            return path_result("dispatch", failed=True)

        async def fake_triage(**kwargs):
            calls.append(("triage", kwargs["dry_run"]))
            return path_result("triage")

        async def fake_cleanup(**kwargs):
            calls.append(("cleanup", kwargs["dry_run"]))
            return path_result("cleanup")

        with mock.patch("repo_agent.tick_all.run_intake_flow", side_effect=fake_intake), mock.patch(
            "repo_agent.tick_all.run_issue_to_pr_flow", side_effect=fake_dispatch
        ), mock.patch(
            "repo_agent.tick_all.run_triage_with_router", side_effect=fake_triage
        ), mock.patch("repo_agent.tick_all.run_cleanup_flow", side_effect=fake_cleanup):
            result = asyncio.run(
                run_all(
                    db_path=Path("/tmp/nonempty-live-failed-dry-run.sqlite"),
                    config=cfg,
                    dry_run=True,
                )
            )

        self.assertEqual(calls, [("intake", True), ("dispatch", True), ("triage", True), ("cleanup", True)])
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["any_failed"])
        self.assertEqual(result["dispatch"]["status"], "failed")
        self.assertTrue(result["dispatch"]["failed"])


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
        cfg = AgentConfig(
            mode="dry-run",
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

        with mock.patch("repo_agent.steps.poll.gh_json", return_value=issues):
            with tempfile.TemporaryDirectory() as tmp:
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

        self.assertEqual(result.stopped_reason, "idle")
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
        self.assertEqual(result.fala_version, "0.2.1")


if __name__ == "__main__":
    unittest.main()
