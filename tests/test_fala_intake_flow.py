from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fala

from lokay.config import AgentConfig, PathConfig, RepoEntry
from lokay.flows.intake import run_intake_flow
from lokay.steps import claim, kanban_intake, poll


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




class TickAllHostPathTests(unittest.TestCase):
    @staticmethod
    def _host(*, failed: bool = False):
        from lokay.flows.runtime import HostPathRunResult, JournalProcess

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
        from lokay.tick_all import run_all

        cfg = AgentConfig(
            mode="dry-run",
            repos=(RepoEntry(repo="o/r", board="board-r", clone_path="/tmp/o-r"),),
        )
        runner = mock.AsyncMock(return_value=self._host())
        with mock.patch("lokay.tick_all.run_package_path_async", new=runner):
            result = asyncio.run(
                run_all(db_path=Path("/tmp/auto-worker.sqlite"), config=cfg, dry_run=True, limit=7)
            )

        runner.assert_awaited_once()
        self.assertEqual(runner.await_args.kwargs["path_id"], "auto_worker")
        self.assertEqual(runner.await_args.kwargs["max_ticks"], 256)
        effector_inputs = runner.await_args.kwargs["effector_inputs"]
        self.assertEqual(effector_inputs["triage_read_open_prs"]["limit"], 7)
        self.assertTrue(effector_inputs["triage_decide_triage_action"]["require_human_approval"])
        self.assertTrue(effector_inputs["cleanup_remove_worktree"]["require_safe"])
        self.assertTrue(effector_inputs["dispatch_read_clone_preconditions"]["dry_run"])
        self.assertNotIn("clone_path", effector_inputs["dispatch_read_clone_preconditions"])
        self.assertIn("cleanup_build_cleanup_receipt", effector_inputs)
        self.assertIn("cleanup_receipt_path", effector_inputs["cleanup_collect_cleanup_receipt_evidence"])
        self.assertEqual(result["path_id"], "auto_worker")
        self.assertFalse(result["any_failed"])
        self.assertEqual(result["processes"][0]["id"], "intake_poll")
        self.assertEqual(result["processes"][0]["output"]["status"], "planned")
    def test_multi_repo_auto_worker_does_not_inject_first_repo_context(self) -> None:
        from lokay.tick_all import run_all

        cfg = AgentConfig(
            mode="dry-run",
            repos=(
                RepoEntry(repo="o/first", board="first-board", clone_path="/tmp/first"),
                RepoEntry(repo="o/temida", board="temida-board", clone_path="/tmp/temida"),
            ),
        )
        runner = mock.AsyncMock(return_value=self._host())
        with mock.patch("lokay.tick_all.run_package_path_async", new=runner):
            asyncio.run(run_all(db_path=Path("/tmp/auto-worker.sqlite"), config=cfg, dry_run=True, limit=7))
        inputs = runner.await_args.kwargs["effector_inputs"]
        for value in inputs.values():
            self.assertNotEqual(value.get("repo"), "o/first")
            self.assertNotEqual(value.get("board"), "first-board")
            self.assertNotEqual(value.get("clone_path"), "/tmp/first")
        self.assertEqual(runner.await_args.kwargs["inputs"]["repos"][1]["repo"], "o/temida")


    def test_empty_auto_worker_is_idle_and_not_worked(self) -> None:
        from lokay.flows.runtime import HostPathRunResult, JournalProcess
        from lokay.tick_all import run_all

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
        with mock.patch("lokay.tick_all.run_package_path_async", new=runner):
            result = asyncio.run(
                run_all(db_path=Path("/tmp/auto-worker-idle.sqlite"), config=AgentConfig(mode="dry-run"), dry_run=True)
            )

        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["stopped_reason"], "idle")
        self.assertFalse(result["summary"]["worked"])
        self.assertFalse(result["any_failed"])

    def test_run_all_preserves_failed_process_evidence_and_nonzero_marker(self) -> None:
        from lokay.tick_all import run_all

        cfg = AgentConfig(mode="dry-run")
        runner = mock.AsyncMock(return_value=self._host(failed=True))
        with mock.patch("lokay.tick_all.run_package_path_async", new=runner):
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
                "updatedAt": "2026-07-28T10:00:00Z",
                "labels": [{"name": "ai:ready"}],
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            gh = Path(tmp) / "gh"
            gh.write_text(
                "#!/bin/sh\ncase \"$*\" in\n  *\"--json comments\"*) printf '%s\\n' '{\"comments\":[]}' ;;\n  *\"--json assignees,labels\"*) printf '%s\\n' '{\"assignees\":[],\"labels\":[{\"name\":\"ai:ready\"}]}' ;;\n  *) printf '%s\\n' '[{\"number\":9,\"title\":\"ship it\",\"url\":\"https://example/9\",\"updatedAt\":\"2026-07-28T10:00:00Z\",\"labels\":[{\"name\":\"ai:ready\"}],\"assignees\":[]}]' ;;\nesac\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)
            hermes = Path(tmp) / "hermes"
            hermes.write_text("#!/bin/sh\nprintf '%s\\n' '[]'\n", encoding="utf-8")
            hermes.chmod(0o755)
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
            fala_home = Path(fala.__file__).resolve().parents[2]
            if not (fala_home / "mojo" / "fala").is_dir():
                self.skipTest("installed Fala package does not include its Mojo source checkout")
            with mock.patch.dict(os.environ, {"FALA_HOME": str(fala_home), "PATH": f"{tmp}:{os.environ.get('PATH', '')}"}, clear=False):
                result = asyncio.run(
                    run_intake_flow(
                        db_path=db,
                        config=cfg,
                        dry_run=True,
                        limit=5,
                        run_id="test-intake-1",
                        max_ticks=47,
                    )
                )

        self.assertEqual(result.failed, [], msg=str(result.processes))
        self.assertEqual(result.stopped_reason, "worked")
        self.assertGreaterEqual(result.ticks, len(result.processes))
        self.assertEqual(len(result.failed), 0)
        steps = {p["step_id"]: p for p in result.processes}
        for step in ("read_open_issues", "decide_issue_action", "post_issue_comment", "build_issue_claim_result", "reconcile_intake_task"):
            self.assertEqual(steps[step]["status"], "succeeded")
        self.assertEqual(result.summary["eligible_count"], 1)
        self.assertEqual(result.summary["issue_action"], "accept")
        # dry-run claim/kanban use status planned (envelope)
        self.assertIn(result.summary["claim_status"], ("planned", "claimed"))
        self.assertEqual(result.summary["kanban_status"], "intake_reconciled")

        self.assertEqual(result.fala_version, "0.7.15")

    def _assert_auto_worker_repair_lane(self, conclusion: str) -> None:
        from lokay.flows.common import process_values
        from lokay.flows.runtime import read_journal_processes
        from lokay.tick_all import run_all

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = root / "gh-calls"
            remote = root / "remote.git"
            seed = root / "seed"
            clone = root / "clone"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(seed), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test"], check=True)
            (seed / "README").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(seed), "add", "README"], check=True)
            subprocess.run(["git", "-C", str(seed), "commit", "-m", "fixture"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(seed), "branch", "ai/fix/9"], check=True)
            subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(remote)], check=True)
            subprocess.run(["git", "-C", str(seed), "push", "origin", "main", "ai/fix/9"], check=True, capture_output=True)
            subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
            subprocess.run(["git", "clone", str(remote), str(clone)], check=True, capture_output=True)
            head_oid = subprocess.run(["git", "-C", str(seed), "rev-parse", "ai/fix/9"], check=True, capture_output=True, text=True).stdout.strip()
            hermes = root / "hermes"
            hermes.write_text("#!/bin/sh\nprintf '%s\\n' '[]'\n", encoding="utf-8")
            hermes.chmod(0o755)
            gh = root / "gh"
            gh.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> {calls}\n"
                "case \"$1 $2\" in\n"
                "  \"issue list\") printf '%s\\n' '[]' ;;\n"
                f"  \"pr list\") printf '%s\\n' '[{{\"number\":9,\"title\":\"review me\",\"url\":\"https://example/9\",\"body\":\"Needs automated repair\",\"state\":\"OPEN\",\"isDraft\":false,\"headRefName\":\"ai/fix/9\",\"headRefOid\":\"{head_oid}\",\"baseRefName\":\"main\",\"baseRefOid\":\"{head_oid}\",\"author\":{{\"login\":\"o\"}},\"labels\":[],\"mergeable\":\"MERGEABLE\",\"reviewDecision\":\"APPROVED\",\"statusCheckRollup\":[{{\"name\":\"ci\",\"conclusion\":\"{conclusion}\"}}],\"commits\":[],\"closingIssuesReferences\":[{{\"number\":10}}]}}]' ;;\n"
                "  \"issue view\") printf '%s\\n' '{\"number\":10,\"state\":\"OPEN\",\"labels\":[],\"assignees\":[]}' ;;\n"
                "  \"pr view\") case \"$*\" in\n"
                "    *\"--json comments\"*) printf '%s\\n' '{\"comments\":[]}' ;;\n"
                f"    *) printf '%s\\n' '{{\"number\":9,\"title\":\"review me\",\"url\":\"https://example/9\",\"body\":\"Needs automated repair\",\"state\":\"OPEN\",\"isDraft\":false,\"headRefName\":\"ai/fix/9\",\"headRefOid\":\"{head_oid}\",\"baseRefName\":\"main\",\"baseRefOid\":\"{head_oid}\",\"author\":{{\"login\":\"o\"}},\"labels\":[],\"mergeable\":\"MERGEABLE\",\"reviewDecision\":\"APPROVED\",\"statusCheckRollup\":[{{\"name\":\"ci\",\"conclusion\":\"{conclusion}\"}}],\"commits\":[],\"closingIssuesReferences\":[{{\"number\":10}}]}}' ;;\n"
                "  esac ;;\n"
                "  *) printf '%s\\n' 'unexpected gh mutation' >&2; exit 97 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)
            cfg = AgentConfig(
                mode="dry-run",
                gh_cli=str(gh),
                raw={"candidate": "a" * 64},
                paths=PathConfig(
                    worktree_root=str(root / "worktrees"),
                    dispatch_receipts=str(root / "dispatch"),
                    task_receipts=str(root / "receipts"),
                    merge_receipts=str(root / "merge"),
                    active_issue=str(root / "active"),
                ),
                repos=(RepoEntry(repo="o/r", board="board-r", clone_path=str(clone), priority=1),),
            )
            db = root / "state.sqlite"
            fala_home = Path(fala.__file__).resolve().parents[2]
            if not (fala_home / "mojo" / "fala").is_dir():
                self.skipTest("installed Fala package does not include its Mojo source checkout")
            with mock.patch.dict(os.environ, {"FALA_HOME": str(fala_home), "PATH": f"{root}:{os.environ.get('PATH', '')}"}, clear=False):
                result = asyncio.run(run_all(db_path=db, config=cfg, dry_run=True, limit=1))
            processes = read_journal_processes(db, result["run_id"])
            call_log = calls.read_text(encoding="utf-8")
        by_step = {process.step_id: process for process in processes}
        self.assertEqual(result["path_id"], "auto_worker")
        self.assertEqual(by_step["intake_read_open_issues"].status, "succeeded")
        self.assertIsNone(process_values({"output": by_step["intake_select_issue_candidate"].output})["selected"])
        self.assertEqual(by_step["triage_read_open_prs"].status, "succeeded")
        selected = process_values({"output": by_step["triage_select_fix_pr"].output})
        self.assertEqual((selected["repo"], selected["number"]), ("o/r", 9))
        checks = process_values({"output": by_step["triage_evaluate_checks"].output})
        evidence = process_values({"output": by_step["triage_evaluate_test_evidence"].output})
        self.assertEqual(checks["status"], "checks_passed" if conclusion == "SUCCESS" else "checks_failed")
        self.assertEqual(evidence["status"], "evidence_missing")
        decision = process_values({"output": by_step["triage_decide_triage_action"].output})
        self.assertEqual(decision["action"], "repair")
        lifecycle = process_values({"output": by_step["lifecycle_decide_lifecycle_transition"].output})
        self.assertEqual(lifecycle["action"], "resume_repair")
        repair_context = process_values({"output": by_step["triage_read_repair_context"].output})
        remote_head = process_values({"output": by_step["triage_read_repair_remote_head"].output})
        self.assertEqual(repair_context["status"], "read")
        self.assertEqual(remote_head["status"], "read")
        self.assertEqual(remote_head["remote_oid"], head_oid)
        preparation = process_values({"output": by_step["triage_add_repair_worktree"].output})
        self.assertEqual(preparation["status"], "planned")
        attempt = process_values({"output": by_step["triage_decide_repair_attempt"].output})
        self.assertEqual(attempt["status"], "noop")
        self.assertFalse(attempt["authorize"])
        self.assertEqual(attempt["reason"], "executor_disabled")
        self.assertEqual(process_values({"output": by_step["triage_post_pr_comment"].output})["status"], "noop")
        failed = [
            (process.step_id, process.status, process.error, process_values({"output": process.output}))
            for process in processes
            if process.status != "succeeded"
        ]
        self.assertFalse(failed, msg=str(failed))
        self.assertFalse(result["any_failed"])
        self.assertNotIn("pr comment", call_log)

    def test_auto_worker_runs_pr_lane_when_issue_intake_is_idle(self) -> None:
        self._assert_auto_worker_repair_lane("FAILURE")

    def test_auto_worker_repairs_green_pr_with_missing_test_evidence(self) -> None:
        self._assert_auto_worker_repair_lane("SUCCESS")


if __name__ == "__main__":
    unittest.main()
