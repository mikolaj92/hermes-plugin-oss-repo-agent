"""Unit tests for mega-atomic effectors — drive real shipped handlers."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from repo_agent.adapters_cli import CommandError
from repo_agent.catalog import EFFECTORS, domains, list_effectors, load_all
from repo_agent.steps import cleanup, issue_to_pr, repair, triage
from repo_agent.steps.claim import _reserve_claim, claim_github_issue
from repo_agent.steps.kanban_intake import ensure_kanban_intake
from repo_agent.steps.poll import poll_eligible_issues


def req(input_data=None, config=None):
    return SimpleNamespace(
        input=input_data or {},
        config=config or {},
        process_id="t1",
        impulse_id=None,
        work_dir=None,
        adapter=None,
    )


class CatalogTests(unittest.TestCase):
    def test_catalog_spans_all_domains(self) -> None:
        d = domains()
        for needed in ("intake", "issue_to_pr", "triage", "repair", "cleanup"):
            self.assertIn(needed, d)
        self.assertGreaterEqual(len(EFFECTORS), 30)
        # every ref loads
        loaded = load_all()
        self.assertEqual(len(loaded), len(EFFECTORS))
        for e in EFFECTORS:
            self.assertTrue(callable(loaded[e.id]), e.id)
        # skeptic-required bricks for composition
        for eid in (
            "check_worktree_dirty",
            "list_controlled_worktrees",
            "push_branch",
            "apply_issue_labels",
            "complete_kanban_task",
            "refresh_clone_base",
            "prepare_worktree",
            "apply_pr_labels",
            "list_ai_fix_prs",
            "load_pr_fields",
            "claim_pr_assignee",
            "close_linked_issue",
            "write_merge_receipt",
            "block_kanban_task",
            "check_issue_closed",
            "delete_local_fix_branch",
        ):
            self.assertIn(eid, loaded)


class IntakeAlignedTests(unittest.TestCase):
    def test_poll_success_and_filter(self) -> None:
        issues = [
            {
                "number": 1,
                "title": "a",
                "url": "u",
                "labels": [{"name": "ai:ready"}],
                "assignees": [],
            },
            {
                "number": 2,
                "title": "b",
                "url": "u",
                "labels": [{"name": "ai:ready"}, {"name": "ai:blocked"}],
                "assignees": [],
            },
        ]
        with mock.patch("repo_agent.steps.poll.gh_json", return_value=issues):
            out = poll_eligible_issues(
                req(
                    {"repos": [{"repo": "o/r", "board": "b"}], "dry_run": True},
                    {"assignee": "mikolaj92", "ready_label": "ai:ready"},
                )
            ).output
        self.assertTrue(out["ok"])
        self.assertEqual(out["eligible_count"], 1)
        self.assertEqual(out["selected"]["number"], 1)

    def test_claim_dry_run_and_noop(self) -> None:
        noop = claim_github_issue(
            req({"conduction": {"poll": {"selected": None}}, "dry_run": True})
        ).output
        self.assertEqual(noop["status"], "noop")
        planned = claim_github_issue(
            req(
                {
                    "dry_run": True,
                    "conduction": {
                        "poll": {
                            "selected": {
                                "repo": "o/r",
                                "number": 3,
                                "labels": [],
                                "assignees": [],
                            }
                        }
                    },
                },
                {"assignee": "mikolaj92"},
            )
        ).output
        self.assertEqual(planned["status"], "planned")
        self.assertFalse(planned["mutated"])
    def test_claim_rejects_bool_and_string_issue_values(self) -> None:
        for value in (True, "3"):
            with self.subTest(value=value):
                out = claim_github_issue(req({"dry_run": True, "selected": {"repo": "o/r", "board": "b", "number": value}})).output
                self.assertEqual(out["reason"], "invalid_selected_issue")

    def test_claim_rejects_malformed_unrelated_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "other.json"
            path.write_text("not json", encoding="utf-8")
            out = claim_github_issue(req({"dry_run": False, "selected": {"repo": "o/r", "board": "b", "number": 3}}, {"active_issue_path": tmp})).output
            self.assertEqual(out["reason"], "claim_malformed")

    def test_claim_rejects_invalid_existing_identity_fields(self) -> None:
        invalid_claims = [
            {"version": 2, "repo": "o/r", "issue": 1, "board": "b", "assignee": "a", "claimedAt": "now"},
            {"version": 1, "repo": "", "issue": 1, "board": "b", "assignee": "a", "claimedAt": "now"},
            {"version": 1, "repo": "o/r", "issue": True, "board": "b", "assignee": "a", "claimedAt": "now"},
            {"version": 1, "repo": "o/r", "issue": 1, "board": "b", "assignee": "", "claimedAt": "now"},
            {"version": 1, "repo": "o/r", "issue": 1, "board": "b", "assignee": "a", "claimedAt": ""},
        ]
        for payload in invalid_claims:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "claim.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                out = claim_github_issue(req({"dry_run": False, "selected": {"repo": "o/r", "board": "b", "number": 1}}, {"assignee": "a", "active_issue_path": path})).output
                self.assertEqual(out["reason"], "claim_malformed")

    def test_claim_capacity_and_same_identity_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _reserve_claim(Path(tmp) / "first.json", repo="o/r", issue=1, board="b", assignee="a")
            out = claim_github_issue(req({"dry_run": False, "selected": {"repo": "o/r", "board": "b", "number": 2}}, {"assignee": "a", "active_issue_path": tmp, "max_active_issues": 1})).output
            self.assertEqual(out["reason"], "claim_capacity_exhausted")
            with mock.patch("repo_agent.steps.claim.run_cmd", return_value=SimpleNamespace(stdout='{"assignees": [{"login": "a"}], "labels": [{"name": "ai:ready"}, {"name": "ai:in-progress"}]}')):
                reused = claim_github_issue(req({"dry_run": False, "selected": {"repo": "o/r", "board": "b", "number": 1}}, {"assignee": "a", "active_issue_path": tmp, "max_active_issues": 1})).output
            self.assertTrue(reused["ok"])
            self.assertTrue(reused["reused"])

    def test_claim_exclusive_conflict_and_fsync_failure_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claim.json"
            _reserve_claim(path, repo="o/r", issue=1, board="b", assignee="a")
            existing, error, reused = _reserve_claim(path, repo="o/r", issue=2, board="b", assignee="a")
            self.assertEqual(error, "claim_busy")
            self.assertFalse(reused)
            with mock.patch("repo_agent.steps.claim.os.fsync", side_effect=OSError("disk full")):
                claim, fsync_error, reused = _reserve_claim(Path(tmp) / "second.json", repo="o/r", issue=2, board="b", assignee="a")
            self.assertTrue(fsync_error.startswith("claim_uncertain:"))
            self.assertFalse(reused)
            self.assertIsNotNone(claim)

    def test_claim_command_timeout_and_readback_mismatch_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            selected = {"repo": "o/r", "board": "b", "number": 3}
            with mock.patch("repo_agent.steps.claim.run_cmd", side_effect=CommandError(["gh"], 1, "", "denied")):
                failed = claim_github_issue(req({"dry_run": False, "selected": selected}, {"active_issue_path": tmp})).output
            self.assertEqual(failed["reason"], "claim_uncertain")
            self.assertTrue(failed["mutated"])

        with tempfile.TemporaryDirectory() as tmp:
            selected = {"repo": "o/r", "board": "b", "number": 4}
            with mock.patch("repo_agent.steps.claim.run_cmd", side_effect=subprocess.TimeoutExpired(["gh"], 1)):
                timed = claim_github_issue(req({"dry_run": False, "selected": selected}, {"active_issue_path": tmp})).output
            self.assertEqual(timed["reason"], "claim_uncertain")
            self.assertEqual(timed["failure_class"], "reconcile_then_retry")

        with tempfile.TemporaryDirectory() as tmp:
            selected = {"repo": "o/r", "board": "b", "number": 5}
            with mock.patch("repo_agent.steps.claim.run_cmd", return_value=SimpleNamespace(stdout='{"assignees": [], "labels": []}')):
                mismatch = claim_github_issue(req({"dry_run": False, "selected": selected}, {"active_issue_path": tmp})).output
            self.assertEqual(mismatch["reason"], "claim_readback_mismatch")
            self.assertTrue(mismatch["mutated"])

    def test_kanban_dry_run(self) -> None:
        out = ensure_kanban_intake(
            req(
                {
                    "dry_run": True,
                    "conduction": {
                        "claim": {
                            "status": "planned",
                            "selected": {
                                "repo": "o/r",
                                "number": 1,
                                "title": "t",
                                "url": "u",
                                "board": "b",
                                "labels": [],
                            },
                        }
                    },
                }
            )
        ).output
        self.assertEqual(out["status"], "planned")


class IssueToPrTests(unittest.TestCase):
    def test_parse_issue_ref(self) -> None:
        out = issue_to_pr.parse_issue_ref_from_task(
            req(
                {
                    "task": {
                        "title": "[issue] acme/app#42: fix crash",
                        "id": "t1",
                    }
                },
                {"branch_prefix": "ai/fix"},
            )
        ).output
        self.assertTrue(out["ok"])
        self.assertEqual(out["repo"], "acme/app")
        self.assertEqual(out["issue"], 42)
        self.assertTrue(out["branch"].startswith("ai/fix/42-"))

    def test_parse_issue_ref_failure(self) -> None:
        out = issue_to_pr.parse_issue_ref_from_task(
            req({"task": {"title": "no ref here"}})
        ).output
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "unparseable_issue_ref")

    def test_create_fix_pr_task_dry_run(self) -> None:
        out = issue_to_pr.create_fix_pr_task(
            req(
                {"board": "b", "repo": "o/r", "issue": 9, "title": "x", "dry_run": True},
                {"fixer_assignee": "fixer"},
            )
        ).output
        self.assertEqual(out["status"], "planned")
        self.assertIn("[fix-pr]", out["title"])

    def test_verify_branch_has_commits(self) -> None:
        with mock.patch(
            "repo_agent.steps.issue_to_pr.rev_parse", side_effect=["aaa", "bbb"]
        ):
            out = issue_to_pr.verify_branch_has_commits(
                req(
                    {
                        "worktree_path": "/wt",
                        "clone_path": "/c",
                        "base_branch": "main",
                        "dry_run": False,
                    }
                )
            ).output
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "has_commits")

    def test_verify_branch_no_commits(self) -> None:
        with mock.patch(
            "repo_agent.steps.issue_to_pr.rev_parse", side_effect=["same", "same"]
        ):
            out = issue_to_pr.verify_branch_has_commits(
                req({"worktree_path": "/wt", "clone_path": "/c", "dry_run": False})
            ).output
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "no_new_commits")

    def test_open_pr_dry_run(self) -> None:
        out = issue_to_pr.open_pull_request(
            req(
                {
                    "repo": "o/r",
                    "branch": "ai/fix/1-x",
                    "title": "t",
                    "dry_run": True,
                }
            )
        ).output
        self.assertEqual(out["status"], "planned")

    def test_write_dispatch_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "r.json")
            out = issue_to_pr.write_dispatch_receipt(
                req(
                    {
                        "receipt_path": path,
                        "payload": {"phase": "CLAIMED", "issue": 1},
                        "dry_run": False,
                    }
                )
            ).output
            self.assertTrue(out["ok"])
            self.assertTrue(out["mutated"])
            self.assertEqual(json.loads(Path(path).read_text())["phase"], "CLAIMED")

    def test_run_omp_dry_run(self) -> None:
        out = issue_to_pr.run_omp_worker(
            req(
                {
                    "worktree_path": "/wt",
                    "prompt": "fix it",
                    "dry_run": True,
                },
                {"model": "omniroute/omp/default"},
            )
        ).output
        self.assertEqual(out["status"], "planned")
        self.assertFalse(out["mutated"])

    def test_run_omp_success_and_failure(self) -> None:
        def git_side_effect(cmd, cwd=None, **_kwargs):
            if cmd[:2] == ["rev-parse", "--is-inside-work-tree"]:
                return "true"
            if cmd[:2] == ["rev-parse", "--show-toplevel"]:
                return str(cwd)
            if cmd[:2] == ["branch", "--show-current"]:
                return "ai/fix/1"
            raise AssertionError(cmd)

        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp)
            with mock.patch(
                "repo_agent.steps.issue_to_pr.git", side_effect=git_side_effect
            ), mock.patch(
                "repo_agent.steps.issue_to_pr.rev_parse",
                side_effect=["head-before", "base", "head-after"],
            ), mock.patch(
                "repo_agent.steps.issue_to_pr.run_omp",
                return_value={
                    "status": "completed",
                    "returncode": 0,
                    "stdout_tail": "ok",
                },
            ), mock.patch(
                "repo_agent.steps.issue_to_pr._omp_diff_paths",
                return_value=[],
            ), mock.patch(
                "repo_agent.steps.issue_to_pr._escaped_omp_paths",
                return_value=[],
            ):
                ok_out = issue_to_pr.run_omp_worker(
                    req(
                        {
                            "worktree_path": str(wt),
                            "prompt": "fix",
                            "branch": "ai/fix/1",
                            "clone_path": str(wt),
                            "dry_run": False,
                        }
                    )
                ).output
        self.assertTrue(ok_out["ok"])
        self.assertEqual(ok_out["status"], "omp_finished")
        self.assertTrue(ok_out["mutated"])
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "repo_agent.steps.issue_to_pr.git", side_effect=git_side_effect
            ), mock.patch(
                "repo_agent.steps.issue_to_pr.rev_parse",
                side_effect=["head-before", "base"],
            ), mock.patch(
                "repo_agent.steps.issue_to_pr.run_omp",
                side_effect=CommandError(["omp"], 1, "", "oom"),
            ):
                bad = issue_to_pr.run_omp_worker(
                    req(
                        {
                            "worktree_path": tmp,
                            "prompt": "fix",
                            "branch": "ai/fix/1",
                            "dry_run": False,
                        }
                    )
                ).output
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["reason"], "omp_failed")

    def test_create_fix_pr_task_success_and_failure(self) -> None:
        # create: empty list first (no exists), then re-list with matching title+id
        list_calls = {"n": 0}

        def list_side_effect(*_a, **_k):
            list_calls["n"] += 1
            if list_calls["n"] == 1:
                return []
            return [
                {
                    "id": "t_fix_9",
                    "title": "[fix-pr] o/r#9: x",
                    "status": "ready",
                }
            ]

        with mock.patch(
            "repo_agent.steps.issue_to_pr.hermes_kanban_json",
            side_effect=list_side_effect,
        ), mock.patch(
            "repo_agent.steps.issue_to_pr.run_cmd",
            return_value=SimpleNamespace(
                stdout="Created task t_fix_9\n", stderr="", returncode=0
            ),
        ):
            out = issue_to_pr.create_fix_pr_task(
                req(
                    {
                        "board": "b",
                        "repo": "o/r",
                        "issue": 9,
                        "title": "x",
                        "dry_run": False,
                    }
                )
            ).output
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "created")
        self.assertTrue(out["mutated"])
        self.assertEqual(out["task_id"], "t_fix_9")
        with mock.patch(
            "repo_agent.steps.issue_to_pr.hermes_kanban_json",
            side_effect=CommandError(["hermes"], 1, "", "no board"),
        ):
            bad = issue_to_pr.create_fix_pr_task(
                req({"board": "b", "repo": "o/r", "issue": 1, "dry_run": False})
            ).output
        self.assertEqual(bad["reason"], "kanban_list_failed")

    def test_complete_kanban_task_paths(self) -> None:
        dry = issue_to_pr.complete_kanban_task(
            req({"board": "b", "task_id": "t1", "result": "done", "dry_run": True})
        ).output
        self.assertEqual(dry["status"], "planned")
        with mock.patch(
            "repo_agent.steps.issue_to_pr.hermes_kanban_json",
            return_value=[{"id": "t1", "status": "ready"}],
        ), mock.patch(
            "repo_agent.steps.issue_to_pr.run_cmd",
            return_value=SimpleNamespace(stdout="", stderr="", returncode=0),
        ):
            ok_out = issue_to_pr.complete_kanban_task(
                req({"board": "b", "task_id": "t1", "dry_run": False})
            ).output
        self.assertEqual(ok_out["status"], "completed")
        self.assertTrue(ok_out["mutated"])
        with mock.patch(
            "repo_agent.steps.issue_to_pr.hermes_kanban_json",
            return_value=[{"id": "t1", "status": "ready"}],
        ), mock.patch(
            "repo_agent.steps.issue_to_pr.run_cmd",
            side_effect=CommandError(["hermes"], 1, "", "gone"),
        ):
            bad = issue_to_pr.complete_kanban_task(
                req({"board": "b", "task_id": "t1", "dry_run": False})
            ).output
        self.assertEqual(bad["reason"], "complete_failed")
        miss = issue_to_pr.complete_kanban_task(req({"board": "b"})).output
        self.assertEqual(miss["reason"], "missing_board_or_task_id")

    def test_refresh_clone_base_paths(self) -> None:
        dry = issue_to_pr.refresh_clone_base(
            req({"clone_path": "/c", "base_branch": "main", "dry_run": True})
        ).output
        self.assertEqual(dry["status"], "planned")
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".git").mkdir()
            with mock.patch(
                "repo_agent.steps.issue_to_pr.git", return_value="ok"
            ):
                ok_out = issue_to_pr.refresh_clone_base(
                    req({"clone_path": tmp, "base_branch": "main", "dry_run": False})
                ).output
            self.assertEqual(ok_out["status"], "refreshed")
        with mock.patch(
            "repo_agent.steps.issue_to_pr.git",
            side_effect=CommandError(["git"], 1, "", "fetch fail"),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                Path(tmp, ".git").mkdir()
                bad = issue_to_pr.refresh_clone_base(
                    req({"clone_path": tmp, "dry_run": False})
                ).output
        self.assertEqual(bad["reason"], "refresh_fetch_failed")

    def test_prepare_worktree_paths(self) -> None:
        dry = issue_to_pr.prepare_worktree(
            req(
                {
                    "clone_path": "/c",
                    "branch": "ai/fix/1-x",
                    "worktree_root": "/wt",
                    "dry_run": True,
                }
            )
        ).output
        self.assertEqual(dry["status"], "planned")
        self.assertIn("worktree_path", dry)
        with mock.patch(
            "repo_agent.steps.issue_to_pr.branch_exists", return_value=False
        ), mock.patch(
            "repo_agent.steps.issue_to_pr.git"
        ), mock.patch(
            "repo_agent.steps.issue_to_pr.worktree_add"
        ), mock.patch(
            "repo_agent.steps.issue_to_pr.rev_parse", return_value="deadbeef"
        ):
            with tempfile.TemporaryDirectory() as tmp:
                out = issue_to_pr.prepare_worktree(
                    req(
                        {
                            "clone_path": tmp,
                            "branch": "ai/fix/1-x",
                            "worktree_root": str(Path(tmp) / "wts"),
                            "base_branch": "main",
                            "dry_run": False,
                        }
                    )
                ).output
        self.assertEqual(out["status"], "prepared")
        self.assertEqual(out["head"], "deadbeef")
        with mock.patch(
            "repo_agent.steps.issue_to_pr.branch_exists", return_value=False
        ), mock.patch(
            "repo_agent.steps.issue_to_pr.git",
            side_effect=CommandError(["git"], 1, "", "no base"),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                bad = issue_to_pr.prepare_worktree(
                    req(
                        {
                            "clone_path": tmp,
                            "branch": "ai/fix/1-x",
                            "worktree_root": str(Path(tmp) / "wts"),
                            "dry_run": False,
                        }
                    )
                ).output
        self.assertEqual(bad["reason"], "worktree_prepare_failed")

    def test_open_pull_request_success_structured_and_failure(self) -> None:
        def fake_run(cmd, **kwargs):
            if "create" in cmd:
                return SimpleNamespace(
                    stdout="https://github.com/o/r/pull/42\n",
                    stderr="",
                    returncode=0,
                )
            if "list" in cmd:
                return SimpleNamespace(
                    stdout=json.dumps([{"number": 42, "url": "https://github.com/o/r/pull/42"}]),
                    stderr="",
                    returncode=0,
                )
            return SimpleNamespace(stdout="[]", stderr="", returncode=0)

        with mock.patch("repo_agent.steps.issue_to_pr.run_cmd", side_effect=fake_run):
            # first list empty then create then list with number
            calls = {"n": 0}

            def side_effect(cmd, **kwargs):
                calls["n"] += 1
                if cmd[1] == "pr" and cmd[2] == "list" and calls["n"] == 1:
                    return SimpleNamespace(stdout="[]", stderr="", returncode=0)
                if cmd[1] == "pr" and cmd[2] == "create":
                    return SimpleNamespace(
                        stdout="https://github.com/o/r/pull/42\n",
                        stderr="",
                        returncode=0,
                    )
                if cmd[1] == "pr" and cmd[2] == "list":
                    return SimpleNamespace(
                        stdout=json.dumps(
                            [{"number": 42, "url": "https://github.com/o/r/pull/42"}]
                        ),
                        stderr="",
                        returncode=0,
                    )
                raise AssertionError(cmd)

            with mock.patch(
                "repo_agent.steps.issue_to_pr.run_cmd", side_effect=side_effect
            ):
                out = issue_to_pr.open_pull_request(
                    req(
                        {
                            "repo": "o/r",
                            "branch": "ai/fix/1-x",
                            "title": "t",
                            "dry_run": False,
                        }
                    )
                ).output
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "created")
        self.assertEqual(out["number"], 42)
        self.assertIn("pull/42", out["url"])
        with mock.patch(
            "repo_agent.steps.issue_to_pr.run_cmd",
            side_effect=CommandError(["gh"], 1, "", "denied"),
        ):
            bad = issue_to_pr.open_pull_request(
                req({"repo": "o/r", "branch": "ai/fix/1", "dry_run": False})
            ).output
        self.assertEqual(bad["reason"], "pr_create_failed")

    def test_apply_pr_labels_paths(self) -> None:
        dry = issue_to_pr.apply_pr_labels(
            req({"repo": "o/r", "number": 3, "labels": ["ai:generated"], "dry_run": True})
        ).output
        self.assertEqual(dry["status"], "planned")
        with mock.patch(
            "repo_agent.steps.issue_to_pr.run_cmd",
            return_value=SimpleNamespace(stdout="", stderr="", returncode=0),
        ):
            ok_out = issue_to_pr.apply_pr_labels(
                req(
                    {
                        "repo": "o/r",
                        "number": 3,
                        "labels": ["ai:generated"],
                        "dry_run": False,
                    }
                )
            ).output
        self.assertEqual(ok_out["status"], "labeled")
        with mock.patch(
            "repo_agent.steps.issue_to_pr.run_cmd",
            side_effect=CommandError(["gh"], 1, "", "no label"),
        ):
            bad = issue_to_pr.apply_pr_labels(
                req(
                    {
                        "repo": "o/r",
                        "number": 3,
                        "labels": ["missing"],
                        "dry_run": False,
                    }
                )
            ).output
        self.assertEqual(bad["reason"], "all_labels_failed")

    def test_check_worktree_dirty_and_list_and_push(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            miss = issue_to_pr.check_worktree_dirty(
                req({"worktree_path": str(Path(tmp) / "nope")})
            ).output
            self.assertEqual(miss["reason"], "worktree_missing")
            Path(tmp, "f").write_text("x")
            with mock.patch(
                "repo_agent.steps.issue_to_pr.is_dirty", return_value=True
            ):
                dirty = issue_to_pr.check_worktree_dirty(
                    req({"worktree_path": tmp})
                ).output
            self.assertTrue(dirty["dirty"])
        with mock.patch(
            "repo_agent.steps.issue_to_pr.worktree_list",
            return_value="worktree /c\nHEAD abc\nbranch refs/heads/main\n\n"
            "worktree /c/wts/ai-fix-1\nHEAD def\nbranch refs/heads/ai/fix/1\n",
        ):
            listed = issue_to_pr.list_controlled_worktrees(
                req({"clone_path": "/c", "worktree_root": "/c/wts"})
            ).output
        self.assertEqual(listed["status"], "listed")
        self.assertEqual(listed["count"], 1)
        dry = issue_to_pr.push_branch(
            req({"worktree_path": "/wt", "branch": "ai/fix/1", "dry_run": True})
        ).output
        self.assertEqual(dry["status"], "planned")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "repo_agent.steps.issue_to_pr.rev_parse", return_value="abc123"
            ), mock.patch(
                "repo_agent.steps.issue_to_pr.git_push_branch", return_value="pushed"
            ), mock.patch(
                "repo_agent.steps.issue_to_pr.git",
                return_value="abc123\trefs/heads/ai/fix/1",
            ):
                pushed = issue_to_pr.push_branch(
                    req(
                        {
                            "worktree_path": tmp,
                            "branch": "ai/fix/1",
                            "dry_run": False,
                        }
                    )
                ).output
            self.assertEqual(pushed["status"], "pushed")
            self.assertTrue(pushed["mutated"])
            with mock.patch(
                "repo_agent.steps.issue_to_pr.rev_parse", return_value="abc123"
            ), mock.patch(
                "repo_agent.steps.issue_to_pr.git_push_branch",
                side_effect=CommandError(["git"], 1, "", "rejected"),
            ):
                bad = issue_to_pr.push_branch(
                    req({"worktree_path": tmp, "branch": "b", "dry_run": False})
                ).output
            self.assertEqual(bad["reason"], "push_failed")

    def test_apply_issue_labels_paths(self) -> None:
        dry = issue_to_pr.apply_issue_labels(
            req(
                {
                    "repo": "o/r",
                    "issue": 1,
                    "labels": ["ai:blocked"],
                    "dry_run": True,
                }
            )
        ).output
        self.assertEqual(dry["status"], "planned")
        with mock.patch(
            "repo_agent.steps.issue_to_pr.run_cmd",
            return_value=SimpleNamespace(stdout="", stderr="", returncode=0),
        ):
            ok_out = issue_to_pr.apply_issue_labels(
                req(
                    {
                        "repo": "o/r",
                        "issue": 1,
                        "labels": ["ai:in-progress"],
                        "dry_run": False,
                    }
                )
            ).output
        self.assertEqual(ok_out["status"], "labeled")
        miss = issue_to_pr.apply_issue_labels(req({"repo": "o/r", "issue": 1})).output
        self.assertEqual(miss["reason"], "missing_labels")


class TriageTests(unittest.TestCase):
    def test_evaluate_checks_pass_and_fail(self) -> None:
        good = triage.evaluate_checks(
            req(
                {
                    "pr": {
                        "statusCheckRollup": [
                            {"name": "ci", "conclusion": "SUCCESS", "state": "SUCCESS"}
                        ]
                    }
                }
            )
        ).output
        self.assertTrue(good["pass_"])
        bad = triage.evaluate_checks(
            req(
                {
                    "pr": {
                        "statusCheckRollup": [
                            {"name": "ci", "conclusion": "FAILURE", "state": "FAILURE"}
                        ]
                    }
                }
            )
        ).output
        self.assertFalse(bad["pass_"])
        self.assertEqual(bad["status"], "checks_failed")

    def test_evaluate_test_evidence(self) -> None:
        miss = triage.evaluate_test_evidence(
            req({"pr": {"body": "no plan"}, "require_test_evidence": True})
        ).output
        self.assertFalse(miss["pass_"])
        hit = triage.evaluate_test_evidence(
            req({"pr": {"body": "Test plan: ran pytest"}, "require_test_evidence": True})
        ).output
        self.assertTrue(hit["pass_"])

    def test_decide_triage_action_routes(self) -> None:
        merge = triage.decide_triage_action(
            req(
                {
                    "pr": {"state": "OPEN", "mergeable": "MERGEABLE", "labels": []},
                    "checks_pass": True,
                    "evidence_pass": True,
                    "automerge": True,
                }
            )
        ).output
        self.assertEqual(merge["action"], "merge")
        repair = triage.decide_triage_action(
            req(
                {
                    "pr": {"state": "OPEN", "mergeable": "MERGEABLE", "labels": []},
                    "checks_pass": False,
                    "evidence_pass": True,
                    "automerge": True,
                }
            )
        ).output
        self.assertEqual(repair["action"], "repair")
        block = triage.decide_triage_action(
            req(
                {
                    "pr": {"state": "OPEN", "mergeable": "MERGEABLE", "labels": []},
                    "checks_pass": True,
                    "evidence_pass": False,
                    "automerge": True,
                }
            )
        ).output
        self.assertEqual(block["action"], "comment_block")

    def test_comment_pr_dry_run_success_failure(self) -> None:
        dry = triage.comment_pr_once(
            req(
                {
                    "repo": "o/r",
                    "number": 5,
                    "body": "blocked: missing tests",
                    "dry_run": True,
                }
            )
        ).output
        self.assertEqual(dry["status"], "planned")
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            side_effect=[
                SimpleNamespace(stdout=json.dumps({"comments": []}), stderr="", returncode=0),
                SimpleNamespace(stdout="", stderr="", returncode=0),
                SimpleNamespace(stdout=json.dumps({"comments": [{"body": "blocked\n\n<!-- repo-agent:o/r:5:triage -->"}]}), stderr="", returncode=0),
            ],
        ):
            ok_out = triage.comment_pr_once(
                req(
                    {
                        "repo": "o/r",
                        "number": 5,
                        "body": "blocked",
                        "dry_run": False,
                    }
                )
            ).output
        self.assertEqual(ok_out["status"], "commented")
        self.assertTrue(ok_out["mutated"])
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            side_effect=CommandError(["gh"], 1, "", "fail"),
        ):
            bad = triage.comment_pr_once(
                req({"repo": "o/r", "number": 5, "body": "x", "dry_run": False})
            ).output
        self.assertEqual(bad["reason"], "comment_read_failed")

    def test_merge_success_and_failure(self) -> None:
        dry = triage.merge_pull_request(
            req(
                {
                    "repo": "o/r",
                    "number": 5,
                    "head_oid": "abc",
                    "dry_run": True,
                }
            )
        ).output
        self.assertEqual(dry["status"], "planned")
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            side_effect=[
                SimpleNamespace(stdout=json.dumps({"state": "OPEN", "headRefOid": "abc"}), stderr="", returncode=0),
                SimpleNamespace(stdout="", stderr="", returncode=0),
                SimpleNamespace(stdout=json.dumps({"state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z", "headRefOid": "abc", "headRefName": "ai/fix/5-x", "mergeCommit": {"oid": "merge-1"}}), stderr="", returncode=0),
            ],
        ):
            ok_out = triage.merge_pull_request(
                req(
                    {
                        "repo": "o/r",
                        "number": 5,
                        "head_oid": "abc",
                        "dry_run": False,
                    }
                )
            ).output
        self.assertEqual(ok_out["status"], "merged")
        self.assertTrue(ok_out["mutated"])
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            side_effect=[
                SimpleNamespace(stdout=json.dumps({"state": "OPEN", "headRefOid": "abc"}), stderr="", returncode=0),
                CommandError(["gh"], 1, "", "not mergeable"),
                SimpleNamespace(stdout="", stderr="", returncode=0),
            ],
        ):
            bad = triage.merge_pull_request(
                req({"repo": "o/r", "number": 5, "head_oid": "abc", "dry_run": False})
            ).output
        self.assertEqual(bad["reason"], "merge_failed")

    def test_list_ai_fix_prs_and_load_pr_fields(self) -> None:
        prs = [
            {
                "number": 1,
                "headRefName": "ai/fix/1-x",
                "title": "t",
                "url": "u",
                "author": {"login": "m"},
                "labels": [],
            },
            {
                "number": 2,
                "headRefName": "feature/other",
                "title": "t2",
                "url": "u2",
                "author": {"login": "m"},
                "labels": [],
            },
        ]
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            return_value=SimpleNamespace(
                stdout=json.dumps(prs), stderr="", returncode=0
            ),
        ):
            listed = triage.list_ai_fix_prs(req({"repo": "o/r"})).output
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["prs"][0]["number"], 1)
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            return_value=SimpleNamespace(
                stdout=json.dumps({"number": 9, "title": "t", "state": "OPEN"}),
                stderr="",
                returncode=0,
            ),
        ):
            loaded = triage.load_pr_fields(
                req({"repo": "o/r", "number": 9})
            ).output
        self.assertEqual(loaded["status"], "loaded")
        self.assertEqual(loaded["pr"]["number"], 9)
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            side_effect=CommandError(["gh"], 1, "", "404"),
        ):
            bad = triage.load_pr_fields(req({"repo": "o/r", "number": 9})).output
        self.assertEqual(bad["reason"], "pr_view_failed")

    def test_claim_pr_close_issue_merge_receipt(self) -> None:
        dry = triage.claim_pr_assignee(
            req({"repo": "o/r", "number": 3, "dry_run": True}, {"assignee": "me"})
        ).output
        self.assertEqual(dry["status"], "planned")
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            side_effect=[
                SimpleNamespace(stdout=json.dumps({"assignees": []}), stderr="", returncode=0),
                SimpleNamespace(stdout="", stderr="", returncode=0),
                SimpleNamespace(stdout=json.dumps({"assignees": [{"login": "me"}]}), stderr="", returncode=0),
            ],
        ):
            claimed = triage.claim_pr_assignee(
                req({"repo": "o/r", "number": 3, "dry_run": False}, {"assignee": "me"})
            ).output
        self.assertEqual(claimed["status"], "claimed")
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            side_effect=[
                SimpleNamespace(stdout=json.dumps({"assignees": []}), stderr="", returncode=0),
                CommandError(["gh"], 1, "", "nope"),
            ],
        ):
            bad_claim = triage.claim_pr_assignee(
                req({"repo": "o/r", "number": 3, "dry_run": False})
            ).output
        self.assertEqual(bad_claim["reason"], "claim_failed")

        provenance = {
            "source": "github_pr_readback", "state": "MERGED", "repo": "o/r", "number": 7,
            "head_oid": "head-7", "head_ref": "ai/fix/7-x", "merge_oid": "merge-7", "merged_at": "2026-01-01T00:00:00Z",
        }
        dry_c = triage.close_linked_issue(
            req({"repo": "o/r", "issue": 7, "dry_run": True})
        ).output
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            side_effect=[
                SimpleNamespace(stdout=json.dumps({"state": "MERGED", "mergedAt": provenance["merged_at"], "headRefOid": provenance["head_oid"], "headRefName": provenance["head_ref"], "mergeCommit": {"oid": provenance["merge_oid"]}}), stderr="", returncode=0),
                SimpleNamespace(stdout=json.dumps({"state": "OPEN"}), stderr="", returncode=0),
                SimpleNamespace(stdout="", stderr="", returncode=0),
                SimpleNamespace(stdout=json.dumps({"state": "CLOSED"}), stderr="", returncode=0),
            ],
        ):
            closed = triage.close_linked_issue(
                req({"repo": "o/r", "issue": 7, "dry_run": False, "verified_provenance": provenance})
            ).output
        self.assertEqual(closed["status"], "closed")
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            side_effect=[
                SimpleNamespace(stdout=json.dumps({"state": "MERGED", "mergedAt": provenance["merged_at"], "headRefOid": provenance["head_oid"], "headRefName": provenance["head_ref"], "mergeCommit": {"oid": provenance["merge_oid"]}}), stderr="", returncode=0),
                SimpleNamespace(stdout=json.dumps({"state": "OPEN"}), stderr="", returncode=0),
                CommandError(["gh"], 1, "", "x"),
                SimpleNamespace(stdout=json.dumps({"state": "OPEN"}), stderr="", returncode=0),
            ],
        ):
            bad_close = triage.close_linked_issue(
                req({"repo": "o/r", "issue": 7, "dry_run": False, "verified_provenance": provenance})
            ).output
        self.assertEqual(bad_close["reason"], "close_failed")

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "merge.json")
            with mock.patch(
                "repo_agent.steps.triage.run_cmd",
                return_value=SimpleNamespace(
                    stdout=json.dumps({"state": "MERGED", "mergedAt": provenance["merged_at"], "headRefOid": provenance["head_oid"], "headRefName": provenance["head_ref"], "mergeCommit": {"oid": provenance["merge_oid"]}}),
                    stderr="",
                    returncode=0,
                ),
            ):
                written = triage.write_merge_receipt(
                    req(
                        {
                            "receipt_path": path,
                            "payload": {"repo": "o/r", "pr": 7, "phase": "MERGED"},
                            "verified_provenance": provenance,
                            "dry_run": False,
                        }
                    )
                ).output
            self.assertEqual(written["status"], "written")
            self.assertEqual(json.loads(Path(path).read_text())["phase"], "MERGED")
    def test_merge_preconditions_and_provenance_fail_closed(self) -> None:
        base = {"repo": "o/r", "number": 4, "head_oid": "head-4", "dry_run": False}
        for view, expected in (
            ({"state": "OPEN", "headRefOid": "other"}, "merge_head_mismatch"),
            ({"state": "CLOSED", "headRefOid": "head-4"}, "merge_precondition_failed"),
            ({"state": "OPEN", "headRefOid": "head-4"}, "merge_precondition_read_failed"),
        ):
            with self.subTest(expected=expected):
                stdout = "" if expected.endswith("read_failed") else json.dumps(view)
                with mock.patch(
                    "repo_agent.steps.triage.run_cmd",
                    return_value=SimpleNamespace(stdout=stdout, stderr="", returncode=0),
                ):
                    out = triage.merge_pull_request(req(base)).output
                self.assertEqual(out["reason"], expected)
        already = {
            "state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z", "headRefOid": "head-4",
            "headRefName": "ai/fix/4-x", "mergeCommit": {"oid": "merge-4"},
        }
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            return_value=SimpleNamespace(stdout=json.dumps(already), stderr="", returncode=0),
        ):
            out = triage.merge_pull_request(req(base)).output
        self.assertEqual(out["status"], "already_merged")
        self.assertFalse(out["mutated"])
        for provenance in ({}, {"source": "forged", "repo": "o/r", "number": 4}):
            with self.subTest(provenance=provenance):
                out = triage.close_linked_issue(
                    req({"repo": "o/r", "issue": 4, "dry_run": False, "verified_provenance": provenance})
                ).output
                self.assertEqual(out["reason"], "merge_provenance_unverified")
        valid = {
            "source": "github_pr_readback", "state": "MERGED", "repo": "o/r", "number": 4,
            "head_oid": "head-4", "head_ref": "ai/fix/4-x", "merge_oid": "merge-4", "merged_at": "2026-01-01T00:00:00Z",
        }
        mismatched = dict(valid, merge_oid="forged-merge")
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            return_value=SimpleNamespace(stdout=json.dumps(already), stderr="", returncode=0),
        ):
            out = triage.close_linked_issue(
                req({"repo": "o/r", "issue": 4, "dry_run": False, "verified_provenance": mismatched})
            ).output
        self.assertEqual(out["reason"], "merge_provenance_unverified")
    def test_assignee_post_readback_absent_and_already_claimed(self) -> None:
        base = {"repo": "o/r", "number": 4, "assignee": "agent", "dry_run": False}
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            side_effect=[
                SimpleNamespace(stdout=json.dumps({"assignees": []}), stderr="", returncode=0),
                SimpleNamespace(stdout="", stderr="", returncode=0),
                SimpleNamespace(stdout=json.dumps({"assignees": []}), stderr="", returncode=0),
            ],
        ):
            out = triage.claim_pr_assignee(req(base)).output
        self.assertEqual(out["reason"], "assignee_readback_mismatch")
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            return_value=SimpleNamespace(stdout=json.dumps({"assignees": [{"login": "agent"}]}), stderr="", returncode=0),
        ):
            out = triage.claim_pr_assignee(req(base)).output
        self.assertEqual(out["status"], "already_claimed")

    def test_comment_post_readback_absent_and_marker_idempotency(self) -> None:
        marker = "<!-- repo-agent:o/r:4:triage -->"
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            side_effect=[
                SimpleNamespace(stdout=json.dumps({"comments": []}), stderr="", returncode=0),
                SimpleNamespace(stdout="", stderr="", returncode=0),
                SimpleNamespace(stdout=json.dumps({"comments": []}), stderr="", returncode=0),
            ],
        ):
            out = triage.comment_pr_once(req({"repo": "o/r", "number": 4, "body": "blocked", "dry_run": False})).output
        self.assertEqual(out["reason"], "comment_readback_mismatch")
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            return_value=SimpleNamespace(stdout=json.dumps({"comments": [{"body": f"blocked\\n\\n{marker}"}]}), stderr="", returncode=0),
        ):
            out = triage.comment_pr_once(req({"repo": "o/r", "number": 4, "body": "blocked", "dry_run": False})).output
        self.assertTrue(out["reconciled"])

    def test_assignee_and_comment_blank_or_malformed_readback_fail_closed(self) -> None:
        for handler, payload, reason in (
            (triage.claim_pr_assignee, "", "assignee_read_failed"),
            (triage.claim_pr_assignee, "not-json", "assignee_read_failed"),
            (triage.comment_pr_once, "", "comment_read_failed"),
            (triage.comment_pr_once, "not-json", "comment_read_failed"),
        ):
            with self.subTest(handler=handler.__name__, payload=payload):
                with mock.patch(
                    "repo_agent.steps.triage.run_cmd",
                    return_value=SimpleNamespace(stdout=payload, stderr="", returncode=0),
                ):
                    data = {"repo": "o/r", "number": 4, "dry_run": False}
                    if handler is triage.claim_pr_assignee:
                        data["assignee"] = "agent"
                    else:
                        data["body"] = "blocked"
                    out = handler(req(data)).output
                self.assertEqual(out["reason"], reason)
        dry_r = triage.write_merge_receipt(
            req({"receipt_path": "/tmp/x", "payload": {}, "dry_run": True})
        ).output
        self.assertEqual(dry_r["status"], "planned")
        miss = triage.write_merge_receipt(req({"payload": {}})).output
        self.assertEqual(miss["reason"], "missing_receipt_path")


class RepairTests(unittest.TestCase):
    def test_build_repair_prompt(self) -> None:
        out = repair.build_repair_prompt(
            req(
                {
                    "pr": {"number": 8, "title": "fix"},
                    "failures": ["ci"],
                    "reason": "checks_failed",
                }
            )
        ).output
        self.assertTrue(out["ok"])
        self.assertIn("PR #8", out["prompt"])
        self.assertIn("ci", out["prompt"])

    def test_create_review_fix_task_dry(self) -> None:
        out = repair.create_review_fix_task(
            req(
                {
                    "board": "b",
                    "repo": "o/r",
                    "number": 2,
                    "reason": "conflict",
                    "dry_run": True,
                }
            )
        ).output
        self.assertEqual(out["status"], "planned")
        self.assertIn("[fix-pr-review]", out["title"])

    def test_create_review_fix_task_success_returns_task_id(self) -> None:
        title = "[fix-pr-review] o/r#2: conflict"
        marker = "fix-pr-review:o/r:2"
        body = (
            "Repository: o/r\nPR: #2\nReason: conflict\n"
            f"Idempotency-Key: {marker}\n"
        )
        list_calls = {"n": 0}

        def list_side_effect(*_a, **_k):
            list_calls["n"] += 1
            if list_calls["n"] == 1:
                return []
            return [
                {
                    "id": "t_rev_2",
                    "title": title,
                    "status": "ready",
                    "body": body,
                }
            ]

        with mock.patch(
            "repo_agent.steps.repair.hermes_kanban_json", side_effect=list_side_effect
        ), mock.patch(
            "repo_agent.steps.repair.run_cmd",
            return_value=SimpleNamespace(
                stdout="Created t_rev_2\n", stderr="", returncode=0
            ),
        ):
            out = repair.create_review_fix_task(
                req(
                    {
                        "board": "b",
                        "repo": "o/r",
                        "number": 2,
                        "reason": "conflict",
                        "dry_run": False,
                    }
                )
            ).output
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "created")
        self.assertEqual(out["task_id"], "t_rev_2")
        self.assertTrue(out["mutated"])

    def test_block_kanban_task_paths(self) -> None:
        dry = repair.block_kanban_task(
            req({"board": "b", "task_id": "t1", "reason": "stuck", "dry_run": True})
        ).output
        self.assertEqual(dry["status"], "planned")
        with mock.patch(
            "repo_agent.steps.repair.hermes_kanban_json",
            side_effect=[
                [{"id": "t1", "status": "ready"}],
                [{"id": "t1", "status": "blocked"}],
            ],
        ), mock.patch(
            "repo_agent.steps.repair.run_cmd",
            return_value=SimpleNamespace(stdout="", stderr="", returncode=0),
        ):
            ok_out = repair.block_kanban_task(
                req(
                    {
                        "board": "b",
                        "task_id": "t1",
                        "reason": "stuck",
                        "dry_run": False,
                    }
                )
            ).output
        self.assertEqual(ok_out["status"], "blocked")
        self.assertTrue(ok_out["mutated"])
        with mock.patch(
            "repo_agent.steps.repair.hermes_kanban_json",
            side_effect=[
                [{"id": "t1", "status": "ready"}],
                [{"id": "t1", "status": "ready"}],
            ],
        ), mock.patch(
            "repo_agent.steps.repair.run_cmd",
            side_effect=CommandError(["hermes"], 1, "", "no"),
        ):
            bad = repair.block_kanban_task(
                req({"board": "b", "task_id": "t1", "dry_run": False})
            ).output
        self.assertEqual(bad["reason"], "block_failed")
        miss = repair.block_kanban_task(req({"board": "b"})).output
        self.assertEqual(miss["reason"], "missing_board_or_task_id")


class CleanupTests(unittest.TestCase):
    def test_parse_issue_from_branch(self) -> None:
        out = cleanup.parse_issue_from_branch(
            req({"branch": "ai/fix/17-fix-login"})
        ).output
        self.assertEqual(out["issue"], 17)

    def test_parse_issue_fail(self) -> None:
        out = cleanup.parse_issue_from_branch(req({"branch": "feature/foo"})).output
        self.assertFalse(out["ok"])

    def test_check_no_open_pr(self) -> None:
        with mock.patch(
            "repo_agent.steps.cleanup.run_cmd",
            return_value=SimpleNamespace(stdout="[]", stderr="", returncode=0),
        ):
            out = cleanup.check_no_open_pr_for_branch(
                req({"repo": "o/r", "branch": "ai/fix/1-x"})
            ).output
        self.assertTrue(out["safe_to_cleanup"])

    def test_remove_worktree_dry_and_absent(self) -> None:
        dry = cleanup.remove_worktree(
            req(
                {
                    "clone_path": "/c",
                    "worktree_path": "/nope",
                    "dry_run": True,
                    "conduction": {
                        "check_issue_closed": {"closed": True},
                        "check_no_open_pr": {"safe_to_cleanup": True},
                    },
                }
            )
        ).output
        self.assertEqual(dry["status"], "planned")
        with tempfile.TemporaryDirectory() as tmp:
            # path does not exist
            out = cleanup.remove_worktree(
                req(
                    {
                        "clone_path": tmp,
                        "worktree_path": str(Path(tmp) / "missing-wt"),
                        "dry_run": False,
                        "conduction": {
                            "check_issue_closed": {"closed": True},
                            "check_no_open_pr": {"safe_to_cleanup": True},
                        },
                    }
                )
            ).output
        self.assertEqual(out["reason"], "worktree_missing")

    def test_release_active_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claim.json"
            path.write_text(json.dumps({"version": 1, "repo": "o/r", "issue": 3, "board": "b", "claimedAt": "2024-01-01T00:00:00Z"}), encoding="utf-8")
            out = cleanup.release_active_issue_claim(
                req(
                    {
                        "claim_path": str(path),
                        "repo": "o/r",
                        "issue": 3,
                        "dry_run": False,
                        "conduction": {
                            "remove_worktree": {"status": "already_absent", "ok": True},
                            "check_issue_closed": {"closed": True, "ok": True},
                            "check_no_open_pr": {"safe_to_cleanup": True, "ok": True},
                            "delete_local_fix_branch": {"status": "already_absent", "ok": True},
                        },
                    }
                )
            ).output
            self.assertEqual(out["status"], "released")
            self.assertTrue(out["mutated"])
            self.assertFalse(path.exists())

    def test_release_active_claim_noops_without_branch(self) -> None:
        out = cleanup.release_active_issue_claim(
            req(
                {
                    "dry_run": False,
                    "conduction": {
                        "parse_issue_from_branch": {"ok": True, "status": "noop", "reason": "no_branch"},
                    },
                }
            )
        ).output
        self.assertEqual(out["status"], "noop")
        self.assertEqual(out["reason"], "no_branch")

    def test_create_maintenance_dry(self) -> None:
        out = cleanup.create_maintenance_task(
            req(
                {
                    "board": "b",
                    "worktree_path": "/wt/dirty",
                    "reason": "dirty",
                    "dry_run": True,
                }
            )
        ).output
        self.assertEqual(out["status"], "planned")

    def test_check_issue_closed_paths(self) -> None:
        with mock.patch(
            "repo_agent.steps.cleanup.run_cmd",
            return_value=SimpleNamespace(
                stdout=json.dumps({"state": "CLOSED"}), stderr="", returncode=0
            ),
        ):
            closed = cleanup.check_issue_closed(
                req({"repo": "o/r", "issue": 3})
            ).output
        self.assertTrue(closed["closed"])
        self.assertEqual(closed["state"], "CLOSED")
        with mock.patch(
            "repo_agent.steps.cleanup.run_cmd",
            return_value=SimpleNamespace(
                stdout=json.dumps({"state": "OPEN"}), stderr="", returncode=0
            ),
        ):
            open_ = cleanup.check_issue_closed(
                req({"repo": "o/r", "issue": 3})
            ).output
        self.assertFalse(open_["closed"])
        with mock.patch(
            "repo_agent.steps.cleanup.run_cmd",
            side_effect=CommandError(["gh"], 1, "", "404"),
        ):
            bad = cleanup.check_issue_closed(req({"repo": "o/r", "issue": 3})).output
        self.assertEqual(bad["reason"], "issue_view_failed")

    def test_delete_local_fix_branch_paths(self) -> None:
        dry = cleanup.delete_local_fix_branch(
            req({"clone_path": "/c", "branch": "ai/fix/1", "dry_run": True})
        ).output
        self.assertEqual(dry["status"], "planned")
        with mock.patch("repo_agent.steps.cleanup.delete_local_branch"):
            ok_out = cleanup.delete_local_fix_branch(
                req(
                    {
                        "clone_path": "/c",
                        "branch": "ai/fix/1",
                        "dry_run": False,
                    }
                )
            ).output
        self.assertEqual(ok_out["status"], "deleted")
        self.assertTrue(ok_out["mutated"])
        with mock.patch(
            "repo_agent.steps.cleanup.delete_local_branch",
            side_effect=CommandError(["git"], 1, "", "not found"),
        ):
            bad = cleanup.delete_local_fix_branch(
                req({"clone_path": "/c", "branch": "ai/fix/1", "dry_run": False})
            ).output
        self.assertEqual(bad["reason"], "delete_failed")


class AdapterFailurePathTests(unittest.TestCase):
    def test_load_kanban_task_list_failure(self) -> None:
        with mock.patch(
            "repo_agent.steps.issue_to_pr.hermes_kanban_json",
            side_effect=CommandError(["hermes"], 1, "", "boom"),
        ):
            out = issue_to_pr.load_kanban_task(req({"board": "b"})).output
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "kanban_list_failed")


if __name__ == "__main__":
    unittest.main()
