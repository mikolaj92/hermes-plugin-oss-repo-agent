"""Unit tests for mega-atomic effectors — drive real shipped handlers."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from repo_agent.adapters_cli import CommandError
from repo_agent.catalog import EFFECTORS, domains, list_effectors, load_all
from repo_agent.steps import cleanup, issue_to_pr, repair, triage
from repo_agent.steps.claim import claim_github_issue
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
    def test_claim_rejects_malformed_selected(self) -> None:
        out = claim_github_issue(
            req(
                {
                    "dry_run": True,
                    "conduction": {"poll": {"selected": ["not-an-issue"]}},
                }
            )
        ).output
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "invalid_selected_issue")
        self.assertEqual(out["failure_class"], "terminal")
        self.assertFalse(out["retry_safe"])
        self.assertFalse(out["mutated"])

    def test_claim_already_complete_is_not_mutated(self) -> None:
        read_back = json.dumps(
            {
                "assignees": [{"login": "mikolaj92"}],
                "labels": [{"name": "ai:ready"}, {"name": "ai:in-progress"}],
            }
        )
        with mock.patch(
            "repo_agent.steps.claim.run_cmd",
            return_value=mock.Mock(stdout=read_back),
        ) as run:
            out = claim_github_issue(
                req(
                    {
                        "dry_run": False,
                        "conduction": {
                            "poll": {
                                "selected": {
                                    "repo": "o/r",
                                    "number": 3,
                                    "labels": ["ai:ready", "ai:in-progress"],
                                    "assignees": ["mikolaj92"],
                                }
                            }
                        }
                    },
                    {"assignee": "mikolaj92"},
                )
            ).output
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "claimed")
        self.assertFalse(out["mutated"])
        self.assertEqual(run.call_count, 2)
    def test_claim_initial_read_classifies_command_and_malformed_failures(self) -> None:
        with mock.patch(
            "repo_agent.steps.claim.run_cmd",
            return_value=mock.Mock(stdout="not-json"),
        ):
            malformed = claim_github_issue(
                req(
                    {
                        "dry_run": False,
                        "conduction": {"poll": {"selected": {"repo": "o/r", "number": 3}}},
                    },
                    {"assignee": "mikolaj92"},
                )
            ).output
        self.assertEqual(malformed["reason"], "claim_readback_failed")
        self.assertEqual(malformed["failure_class"], "terminal")
        self.assertFalse(malformed["retry_safe"])
        self.assertFalse(malformed["mutated"])
        with mock.patch(
            "repo_agent.steps.claim.run_cmd",
            side_effect=CommandError(["gh"], 1, "", "unavailable"),
        ):
            command_error = claim_github_issue(
                req(
                    {
                        "dry_run": False,
                        "conduction": {"poll": {"selected": {"repo": "o/r", "number": 3}}},
                    },
                    {"assignee": "mikolaj92"},
                )
            ).output
        self.assertEqual(command_error["reason"], "claim_readback_failed")
        self.assertEqual(command_error["failure_class"], "retryable_read")
        self.assertTrue(command_error["retry_safe"])
        self.assertFalse(command_error["mutated"])

    def test_claim_final_readback_failure_after_skips_is_not_mutated(self) -> None:
        read_back = json.dumps(
            {
                "assignees": [{"login": "mikolaj92"}],
                "labels": [{"name": "ai:ready"}, {"name": "ai:in-progress"}],
            }
        )
        with mock.patch(
            "repo_agent.steps.claim.run_cmd",
            side_effect=[
                mock.Mock(stdout=read_back),
                CommandError(["gh", "issue", "view"], 1, "", "unavailable"),
            ],
        ):
            out = claim_github_issue(
                req(
                    {
                        "dry_run": False,
                        "conduction": {
                            "poll": {
                                "selected": {
                                    "repo": "o/r",
                                    "number": 3,
                                    "labels": ["ai:ready", "ai:in-progress"],
                                    "assignees": ["mikolaj92"],
                                }
                            }
                        }
                    },
                    {"assignee": "mikolaj92"},
                )
            ).output
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "claim_readback_failed")
        self.assertFalse(out["mutated"])
    def test_claim_nonzero_mutation_reconciles_state_delta(self) -> None:
        initial = json.dumps({"assignees": [], "labels": []})
        reconciled = json.dumps(
            {
                "assignees": [{"login": "mikolaj92"}],
                "labels": [{"name": "ai:ready"}, {"name": "ai:in-progress"}],
            }
        )
        with mock.patch(
            "repo_agent.steps.claim.run_cmd",
            side_effect=[
                mock.Mock(stdout=initial),
                CommandError(["gh", "issue", "edit"], 1, "", "remote applied"),
                mock.Mock(stdout=reconciled),
            ],
        ):
            out = claim_github_issue(
                req(
                    {
                        "dry_run": False,
                        "conduction": {
                            "poll": {
                                "selected": {"repo": "o/r", "number": 3}
                            }
                        },
                    },
                    {"assignee": "mikolaj92"},
                )
            ).output
        self.assertTrue(out["ok"])
        self.assertTrue(out["reconciled"])
        self.assertTrue(out["mutated"])


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
                req({"worktree_path": "/wt", "clone_path": "/c", "base_branch": "main", "dry_run": False})
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
    def test_verify_branch_dry_run_plans_without_reading_paths(self) -> None:
        with mock.patch(
            "repo_agent.steps.issue_to_pr.rev_parse",
            side_effect=AssertionError("dry-run must not call rev_parse"),
        ) as rev_parse:
            out = issue_to_pr.verify_branch_has_commits(
                req(
                    {
                        "worktree_path": "/planned/worktree",
                        "clone_path": "/planned/clone",
                        "base_branch": "main",
                        "dry_run": True,
                    }
                )
            ).output
        self.assertEqual(out["status"], "planned")
        self.assertTrue(out["dry_run"])
        self.assertFalse(out["mutated"])
        rev_parse.assert_not_called()

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
        with mock.patch(
            "repo_agent.steps.issue_to_pr.run_omp",
            return_value={"status": "completed", "returncode": 0, "stdout_tail": "ok"},
        ):
            ok_out = issue_to_pr.run_omp_worker(
                req(
                    {
                        "worktree_path": "/wt",
                        "prompt": "fix",
                        "dry_run": False,
                    }
                )
            ).output
        self.assertTrue(ok_out["ok"])
        self.assertEqual(ok_out["status"], "omp_finished")
        self.assertTrue(ok_out["mutated"])
        with mock.patch(
            "repo_agent.steps.issue_to_pr.run_omp",
            side_effect=CommandError(["omp"], 1, "", "oom"),
        ):
            bad = issue_to_pr.run_omp_worker(
                req({"worktree_path": "/wt", "prompt": "fix", "dry_run": False})
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
            return_value=[{"id": "t1", "status": "in_progress"}],
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
            return_value=[{"id": "t1", "status": "in_progress"}],
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
        with tempfile.TemporaryDirectory() as worktree:
            with mock.patch(
                "repo_agent.steps.issue_to_pr.rev_parse", return_value="abc123"
            ), mock.patch(
                "repo_agent.steps.issue_to_pr.git_push_branch", return_value="pushed"
            ), mock.patch(
                "repo_agent.steps.issue_to_pr.git", return_value="abc123\n"
            ):
                pushed = issue_to_pr.push_branch(
                    req(
                        {
                            "worktree_path": worktree,
                            "branch": "ai/fix/1",
                            "dry_run": False,
                        }
                    )
                ).output
            self.assertEqual(pushed["status"], "pushed")
            self.assertTrue(pushed["mutated"])
            with mock.patch(
                "repo_agent.steps.issue_to_pr.rev_parse", return_value="def456"
            ), mock.patch(
                "repo_agent.steps.issue_to_pr.git_push_branch",
                side_effect=CommandError(["git"], 1, "", "rejected"),
            ) as push_mock:
                bad = issue_to_pr.push_branch(
                    req(
                        {
                            "worktree_path": worktree,
                            "branch": "b",
                            "dry_run": False,
                        }
                    )
                ).output
            push_mock.assert_called_once_with(worktree, "b", set_upstream=True)
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
                        "labels": ["ai:blocked"],
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
    def test_evaluate_checks_rejects_malformed_rollups_and_unknown_states(self) -> None:
        malformed = (None, {}, "not-a-rollup", ["bad"], [{}], [{"name": "ci"}], [{"name": "ci", "conclusion": "MAYBE"}])
        for rollup in malformed:
            with self.subTest(rollup=rollup):
                output = triage.evaluate_checks(req({"pr": {"statusCheckRollup": rollup}})).output
                self.assertEqual(output["reason"], "invalid_checks_read")
                self.assertEqual(output["failure_class"], "terminal")
                self.assertFalse(output["retry_safe"])
                self.assertFalse(output["mutated"])
    def test_claim_and_comment_pr_view_command_errors_are_retryable_reads(self) -> None:
        error = CommandError(["gh"], 1, "", "unavailable")
        for call, payload in (
            (triage.claim_pr_assignee, {"repo": "o/r", "number": 5, "assignee": "owner", "dry_run": False}),
            (triage.comment_pr_once, {"repo": "o/r", "number": 5, "body": "blocked", "dry_run": False}),
        ):
            with self.subTest(call=call.__name__), mock.patch("repo_agent.steps.triage.run_cmd", side_effect=error):
                output = call(req(payload)).output
            self.assertEqual(output["failure_class"], "retryable_read")
            self.assertTrue(output["retry_safe"])
            self.assertFalse(output["mutated"])
    def test_merge_command_or_readback_uncertainty_is_reconcile_then_retry(self) -> None:
        error = CommandError(["gh"], 1, "", "timeout")
        for readback in (error, SimpleNamespace(stdout="{}", stderr="", returncode=0)):
            with self.subTest(readback=readback), mock.patch(
                "repo_agent.steps.triage.run_cmd", side_effect=[error, readback]
            ):
                output = triage.merge_pull_request(
                    req({"repo": "o/r", "number": 5, "head_oid": "abc", "dry_run": False})
                ).output
            self.assertEqual(output["failure_class"], "reconcile_then_retry")
            self.assertFalse(output["retry_safe"])
            self.assertTrue(output["mutated"])

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
        dry = triage.comment_pr_once(req({"repo": "o/r", "number": 5, "body": "blocked", "dry_run": True})).output
        self.assertEqual(dry["status"], "planned")
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            side_effect=[
                SimpleNamespace(stdout=json.dumps({"comments": []}), stderr="", returncode=0),
                SimpleNamespace(stdout="", stderr="", returncode=0),
            ],
        ):
            out = triage.comment_pr_once(req({"repo": "o/r", "number": 5, "body": "blocked", "dry_run": False})).output
        self.assertEqual(out["status"], "commented")
        self.assertTrue(out["mutated"])
        with mock.patch("repo_agent.steps.triage.run_cmd", side_effect=CommandError(["gh"], 1, "", "fail")):
            failed = triage.comment_pr_once(req({"repo": "o/r", "number": 5, "body": "x", "dry_run": False})).output
        self.assertEqual(failed["reason"], "comment_read_failed")

        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            return_value=SimpleNamespace(stdout="", stderr="", returncode=0),
        ):
            blank = triage.comment_pr_once(req({"repo": "o/r", "number": 5, "body": "x", "dry_run": False})).output
        self.assertEqual(blank["reason"], "comment_read_failed")
        self.assertEqual(blank["failure_class"], "terminal")

    def test_close_issue_blank_or_malformed_readback_fails_closed(self) -> None:
        for stdout in ("", "{}", "not-json"):
            with self.subTest(stdout=stdout), mock.patch(
                "repo_agent.steps.triage.run_cmd",
                return_value=SimpleNamespace(stdout=stdout, stderr="", returncode=0),
            ):
                output = triage.close_linked_issue(
                    req({"repo": "o/r", "issue": 7, "dry_run": False})
                ).output
            self.assertEqual(output["reason"], "close_read_failed")
            self.assertEqual(output["failure_class"], "terminal")
            self.assertFalse(output["retry_safe"])
            self.assertFalse(output["mutated"])

    def test_assignee_blank_readback_fails_closed(self) -> None:
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            return_value=SimpleNamespace(stdout="", stderr="", returncode=0),
        ):
            output = triage.claim_pr_assignee(
                req({"repo": "o/r", "number": 5, "assignee": "owner", "dry_run": False})
            ).output
        self.assertEqual(output["reason"], "assignee_read_failed")
        self.assertFalse(output["retry_safe"])
    def test_comment_marker_reconciles_and_conflicts(self) -> None:
        marker = "<!-- repo-agent:o/r:5:triage -->"
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            return_value=SimpleNamespace(stdout=json.dumps({"comments": [{"body": f"old\n{marker}"}]}), stderr="", returncode=0),
        ) as command:
            out = triage.comment_pr_once(req({"repo": "o/r", "number": 5, "body": "new", "dry_run": False})).output
        self.assertTrue(out["reconciled"])
        command.assert_called_once()
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            return_value=SimpleNamespace(stdout=json.dumps({"comments": [{"body": marker}, {"body": marker}]}), stderr="", returncode=0),
        ):
            conflict = triage.comment_pr_once(req({"repo": "o/r", "number": 5, "body": "new", "dry_run": False})).output
        self.assertEqual(conflict["reason"], "comment_marker_conflict")

    def test_assignee_invalid_readback_fails_closed(self) -> None:
        with mock.patch("repo_agent.steps.triage.run_cmd", return_value=SimpleNamespace(stdout="not-json", stderr="", returncode=0)):
            output = triage.claim_pr_assignee(req({"repo": "o/r", "number": 5, "assignee": "owner", "dry_run": False})).output
        self.assertEqual(output["reason"], "assignee_read_failed")
        self.assertEqual(output["failure_class"], "terminal")

    def test_merge_success_and_missing_head(self) -> None:
        dry = triage.merge_pull_request(req({"repo": "o/r", "number": 5, "head_oid": "abc", "dry_run": True})).output
        self.assertEqual(dry["status"], "planned")
        with mock.patch("repo_agent.steps.triage.run_cmd", return_value=SimpleNamespace(stdout="", stderr="", returncode=0)):
            merged = triage.merge_pull_request(req({"repo": "o/r", "number": 5, "head_oid": "abc", "dry_run": False})).output
        self.assertEqual(merged["status"], "merged")
        missing = triage.merge_pull_request(req({"repo": "o/r", "number": 5, "dry_run": False})).output
        self.assertEqual(missing["reason"], "missing_head_oid")

    def test_merge_command_error_reconciles_verified_merge(self) -> None:
        error = CommandError(["gh"], 1, "", "timeout")
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            side_effect=[error, SimpleNamespace(stdout=json.dumps({"state": "MERGED", "mergedAt": "now", "headRefOid": "abc", "mergeCommit": {"oid": "merge"}}), stderr="", returncode=0)],
        ):
            output = triage.merge_pull_request(req({"repo": "o/r", "number": 5, "head_oid": "abc", "dry_run": False})).output
        self.assertEqual(output["status"], "merge_verified")
        self.assertTrue(output["reconciled"])

    def test_list_ai_fix_prs_and_load_pr_fields(self) -> None:
        prs = [{"number": 1, "headRefName": "ai/fix/1-x", "title": "t", "url": "u", "author": {"login": "m"}, "labels": []}]
        with mock.patch("repo_agent.steps.triage.run_cmd", return_value=SimpleNamespace(stdout=json.dumps(prs), stderr="", returncode=0)):
            listed = triage.list_ai_fix_prs(req({"repo": "o/r"})).output
        self.assertEqual(listed["count"], 1)
        with mock.patch("repo_agent.steps.triage.run_cmd", return_value=SimpleNamespace(stdout=json.dumps({"number": 9, "title": "t", "state": "OPEN"}), stderr="", returncode=0)):
            loaded = triage.load_pr_fields(req({"repo": "o/r", "number": 9})).output
        self.assertEqual(loaded["status"], "loaded")
        with mock.patch("repo_agent.steps.triage.run_cmd", side_effect=CommandError(["gh"], 1, "", "404")):
            failed = triage.load_pr_fields(req({"repo": "o/r", "number": 9})).output
        self.assertEqual(failed["reason"], "pr_view_failed")

    def test_claim_close_and_receipt(self) -> None:
        dry = triage.claim_pr_assignee(req({"repo": "o/r", "number": 3, "dry_run": True}, {"assignee": "me"})).output
        self.assertEqual(dry["status"], "planned")
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            side_effect=[
                SimpleNamespace(stdout=json.dumps({"assignees": []}), stderr="", returncode=0),
                SimpleNamespace(stdout="", stderr="", returncode=0),
            ],
        ):
            claimed = triage.claim_pr_assignee(req({"repo": "o/r", "number": 3, "dry_run": False}, {"assignee": "me"})).output
        self.assertEqual(claimed["status"], "claimed")
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            side_effect=[
                SimpleNamespace(stdout=json.dumps({"state": "OPEN"}), stderr="", returncode=0),
                SimpleNamespace(stdout="", stderr="", returncode=0),
                SimpleNamespace(stdout=json.dumps({"state": "CLOSED"}), stderr="", returncode=0),
            ],
        ):
            closed = triage.close_linked_issue(req({"repo": "o/r", "issue": 7, "dry_run": False})).output
        self.assertEqual(closed["status"], "closed")
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "merge.json")
            written = triage.write_merge_receipt(req({"receipt_path": path, "payload": {"pr": 1, "phase": "MERGED"}, "dry_run": False})).output
        self.assertEqual(written["status"], "written")
        self.assertEqual(triage.write_merge_receipt(req({"payload": {}})).output["reason"], "missing_receipt_path")
    def test_triage_read_failures_classify_command_and_malformed_payloads(self) -> None:
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            side_effect=CommandError(["gh"], 1, "", "unavailable"),
        ):
            listed = triage.list_ai_fix_prs(req({"repo": "o/r"})).output
        self.assertEqual(listed["reason"], "pr_list_failed")
        self.assertEqual(listed["failure_class"], "retryable_read")
        self.assertTrue(listed["retry_safe"])
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            return_value=SimpleNamespace(stdout="not-json", stderr="", returncode=0),
        ):
            listed = triage.list_ai_fix_prs(req({"repo": "o/r"})).output
        self.assertEqual(listed["reason"], "invalid_pr_list")
        self.assertEqual(listed["failure_class"], "terminal")
        self.assertFalse(listed["retry_safe"])
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            side_effect=CommandError(["gh"], 1, "", "unavailable"),
        ):
            loaded = triage.load_pr_fields(req({"repo": "o/r", "number": 9})).output
        self.assertEqual(loaded["reason"], "pr_view_failed")
        self.assertEqual(loaded["failure_class"], "retryable_read")
        self.assertTrue(loaded["retry_safe"])
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            return_value=SimpleNamespace(stdout="{}", stderr="", returncode=0),
        ):
            loaded = triage.load_pr_fields(req({"repo": "o/r", "number": 9})).output
        self.assertEqual(loaded["reason"], "invalid_pr_readback")
        self.assertEqual(loaded["failure_class"], "terminal")
        self.assertFalse(loaded["retry_safe"])
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
        list_calls = {"n": 0}

        def list_side_effect(*_a, **_k):
            list_calls["n"] += 1
            if list_calls["n"] == 1:
                return []
            return [{"id": "t_rev_2", "title": title, "body": "Idempotency-Key: fix-pr-review:o/r:2", "status": "ready"}]

        with mock.patch(
            "repo_agent.steps.repair.hermes_kanban_json", side_effect=list_side_effect
        ), mock.patch(
            "repo_agent.steps.issue_to_pr.hermes_kanban_json",
            return_value=[{"id": "t_rev_2", "title": title, "body": "Idempotency-Key: fix-pr-review:o/r:2", "status": "ready"}],
        ), mock.patch(
            "repo_agent.steps.repair.run_cmd",
            return_value=SimpleNamespace(stdout="Created t_rev_2\n", stderr="", returncode=0),
        ):
            out = repair.create_review_fix_task(
                req({"board": "b", "repo": "o/r", "number": 2, "reason": "conflict", "dry_run": False})
            ).output
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "created")
        self.assertEqual(out["task_id"], "t_rev_2")
        self.assertTrue(out["mutated"])

    def test_create_review_fix_task_reconciles_command_error(self) -> None:
        with mock.patch(
            "repo_agent.steps.repair.hermes_kanban_json",
            side_effect=[
                [],
                [{"id": "t_done", "title": "[fix-pr-review] o/r#2: conflict", "body": "Idempotency-Key: fix-pr-review:o/r:2", "status": "archived"}],
            ],
        ), mock.patch(
            "repo_agent.steps.repair.run_cmd",
            side_effect=CommandError(["hermes"], 1, "", "timeout"),
        ):
            out = repair.create_review_fix_task(req({"board": "b", "repo": "o/r", "number": 2, "reason": "conflict", "dry_run": False})).output
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "reconciled")
        self.assertEqual(out["task_id"], "t_done")

    def test_create_review_fix_task_ambiguous_command_error_fails_closed(self) -> None:
        tasks = [
            {"id": "t1", "title": "[fix-pr-review] o/r#2: conflict", "body": "Idempotency-Key: fix-pr-review:o/r:2"},
            {"id": "t2", "title": "[fix-pr-review] o/r#2: conflict", "body": "Idempotency-Key: fix-pr-review:o/r:2"},
        ]
        with mock.patch("repo_agent.steps.repair.hermes_kanban_json", side_effect=[[], tasks]), mock.patch(
            "repo_agent.steps.repair.run_cmd", side_effect=CommandError(["hermes"], 1, "", "timeout")
        ):
            out = repair.create_review_fix_task(req({"board": "b", "repo": "o/r", "number": 2, "reason": "conflict", "dry_run": False})).output
        self.assertFalse(out["ok"])
        self.assertEqual(out["failure_class"], "terminal")
        self.assertFalse(out["retry_safe"])

    def test_create_review_fix_task_command_error_absence_fails_closed(self) -> None:
        with mock.patch("repo_agent.steps.repair.hermes_kanban_json", side_effect=[[], []]), mock.patch(
            "repo_agent.steps.repair.run_cmd", side_effect=CommandError(["hermes"], 1, "", "timeout")
        ):
            out = repair.create_review_fix_task(req({"board": "b", "repo": "o/r", "number": 2, "reason": "conflict", "dry_run": False})).output
        self.assertFalse(out["ok"])
        self.assertEqual(out["failure_class"], "terminal")
        self.assertFalse(out["retry_safe"])

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
                req({"board": "b", "task_id": "t1", "reason": "stuck", "dry_run": False})
            ).output
        self.assertEqual(ok_out["status"], "blocked")
        self.assertTrue(ok_out["mutated"])
        with mock.patch(
            "repo_agent.steps.repair.hermes_kanban_json",
            side_effect=[[{"id": "t1", "status": "ready"}], []],
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

    def test_check_no_open_pr_blank_or_malformed_readback_fails_closed(self) -> None:
        for stdout in ("", "{}", "not-json"):
            with self.subTest(stdout=stdout), mock.patch(
                "repo_agent.steps.cleanup.run_cmd",
                return_value=SimpleNamespace(stdout=stdout, stderr="", returncode=0),
            ):
                out = cleanup.check_no_open_pr_for_branch(
                    req({"repo": "o/r", "branch": "ai/fix/1-x"})
                ).output
            self.assertFalse(out["ok"])
            self.assertEqual(out["reason"], "invalid_pr_list")
            self.assertEqual(out["failure_class"], "terminal")
            self.assertFalse(out["retry_safe"])
            self.assertFalse(out["mutated"])
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
            path.write_text(json.dumps({"repo": "o/r", "issue": 3}), encoding="utf-8")
            out = cleanup.release_active_issue_claim(
                req(
                    {
                        "claim_path": str(path),
                        "repo": "o/r",
                        "issue": "3",
                        "dry_run": False,
                    }
                )
            ).output
            self.assertTrue(out["mutated"])
            self.assertFalse(path.exists())

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
