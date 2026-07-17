"""Unit tests for Fala correlation path composition (specs + conduction helpers)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from repo_agent.envelope import cond_blob, cond_get
from repo_agent.flows import (
    ALL_PATHS,
    CLEANUP_PATH,
    INTAKE_PATH,
    ISSUE_TO_PR_PATH,
    PR_COMMENT_PATH,
    PR_MERGE_PATH,
    PR_REPAIR_PATH,
    PR_TRIAGE_PATH,
    path_conduction_graph,
    path_ids,
)
from repo_agent.flows.common import effector
from repo_agent.steps import issue_to_pr, repair, triage
from repo_agent.steps.cleanup import parse_issue_from_branch, remove_worktree


def req(input_data=None, config=None):
    return SimpleNamespace(
        input=input_data or {},
        config=config or {},
        process_id="t1",
        impulse_id=None,
        work_dir=None,
        adapter=None,
    )


class PathSpecTests(unittest.TestCase):
    def test_all_paths_present(self) -> None:
        ids = {p.id for p in ALL_PATHS}
        for needed in (
            "issue_intake",
            "issue_to_pr",
            "pr_triage",
            "pr_merge",
            "pr_comment_block",
            "pr_repair",
            "cleanup",
        ):
            self.assertIn(needed, ids)

    def test_issue_to_pr_chain(self) -> None:
        ids = path_ids(ISSUE_TO_PR_PATH)
        self.assertEqual(
            ids,
            [
                "load_kanban_task",
                "parse_issue_ref",
                "prepare_worktree",
                "run_omp",
                "verify_branch",
                "push_branch",
                "open_pull_request",
                "apply_pr_labels",
                "write_dispatch_receipt",
                "complete_kanban_task",
            ],
        )
        graph = path_conduction_graph(ISSUE_TO_PR_PATH)
        self.assertEqual(graph["parse_issue_ref"], ["load_kanban_task"])
        self.assertIn("prepare_worktree", graph["run_omp"])
        self.assertIn("open_pull_request", graph["apply_pr_labels"])

    def test_triage_decide_chain(self) -> None:
        ids = path_ids(PR_TRIAGE_PATH)
        self.assertEqual(
            ids,
            [
                "list_ai_fix_prs",
                "load_pr_fields",
                "evaluate_checks",
                "evaluate_test_evidence",
                "decide_triage_action",
            ],
        )
        graph = path_conduction_graph(PR_TRIAGE_PATH)
        self.assertIn("evaluate_checks", graph["decide_triage_action"])
        self.assertIn("evaluate_test_evidence", graph["decide_triage_action"])

    def test_merge_repair_cleanup_chains(self) -> None:
        self.assertEqual(
            path_ids(PR_MERGE_PATH),
            ["claim_pr", "merge", "write_merge_receipt", "close_linked_issue"],
        )
        self.assertEqual(path_ids(PR_COMMENT_PATH), ["comment_pr"])
        self.assertEqual(
            path_ids(PR_REPAIR_PATH),
            [
                "create_review_fix_task",
                "build_repair_prompt",
                "prepare_worktree",
                "run_omp",
                "push_branch",
            ],
        )
        self.assertEqual(
            path_ids(CLEANUP_PATH),
            [
                "parse_issue_from_branch",
                "check_issue_closed",
                "check_no_open_pr",
                "remove_worktree",
                "delete_local_fix_branch",
                "release_active_issue_claim",
            ],
        )

    def test_intake_still_three(self) -> None:
        self.assertEqual(path_ids(INTAKE_PATH), ["poll", "claim", "kanban"])

    def test_paths_are_acyclic_and_refs_known(self) -> None:
        # CorrelationPathSpec validates on construction; re-assert unique ids
        for path in ALL_PATHS:
            seen: set[str] = set()
            known = {e.id for e in path.effectors}
            for e in path.effectors:
                self.assertNotIn(e.id, seen)
                seen.add(e.id)
                for dep in e.conduction:
                    self.assertIn(dep, known)
                self.assertTrue(e.adapter.ref.startswith("repo_agent.steps."))


class EnvelopeConductionTests(unittest.TestCase):
    def test_cond_get_prefers_input(self) -> None:
        r = req(
            {
                "repo": "from-input",
                "conduction": {"parse_issue_ref": {"repo": "from-cond"}},
            }
        )
        self.assertEqual(cond_get(r, "repo", "parse_issue_ref"), "from-input")

    def test_cond_get_falls_back(self) -> None:
        r = req({"conduction": {"parse_issue_ref": {"repo": "acme/app", "issue": 9}}})
        self.assertEqual(cond_get(r, "repo", "parse_issue_ref"), "acme/app")
        self.assertEqual(cond_get(r, "issue", "parse_issue_ref"), 9)
        self.assertIsNone(cond_get(r, "missing", "parse_issue_ref"))

    def test_cond_blob(self) -> None:
        r = req({"conduction": {"a": {}, "b": {"x": 1}}})
        self.assertEqual(cond_blob(r, "a", "b"), {"x": 1})


class ConductionAwareEffectorTests(unittest.TestCase):
    def test_parse_issue_ref_from_conduction_task(self) -> None:
        out = issue_to_pr.parse_issue_ref_from_task(
            req(
                {
                    "conduction": {
                        "load_kanban_task": {
                            "task": {
                                "title": "[fix-pr] acme/app#42: crash",
                                "id": "t1",
                            }
                        }
                    }
                },
                {"branch_prefix": "ai/fix"},
            )
        ).output
        self.assertTrue(out["ok"])
        self.assertEqual(out["repo"], "acme/app")
        self.assertEqual(out["issue"], 42)

    def test_decide_pulls_checks_and_evidence(self) -> None:
        pr = {
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "labels": [],
        }
        out = triage.decide_triage_action(
            req(
                {
                    "automerge": True,
                    "conduction": {
                        "load_pr_fields": {"pr": pr, "number": 3},
                        "evaluate_checks": {"pass_": True, "status": "checks_passed"},
                        "evaluate_test_evidence": {
                            "pass_": True,
                            "status": "evidence_optional",
                        },
                    },
                }
            )
        ).output
        self.assertEqual(out["action"], "merge")
        self.assertEqual(out["reason"], "ready")

    def test_decide_repair_on_failed_checks(self) -> None:
        out = triage.decide_triage_action(
            req(
                {
                    "conduction": {
                        "load_pr_fields": {
                            "pr": {"state": "OPEN", "mergeable": "MERGEABLE", "labels": []}
                        },
                        "evaluate_checks": {"pass_": False, "status": "checks_failed"},
                        "evaluate_test_evidence": {"pass_": True},
                    }
                }
            )
        ).output
        self.assertEqual(out["action"], "repair")

    def test_build_repair_prompt_from_conduction(self) -> None:
        out = repair.build_repair_prompt(
            req(
                {
                    "conduction": {
                        "load_pr_fields": {
                            "pr": {"number": 7, "title": "fix", "headRefName": "ai/fix/7-x"}
                        },
                        "decide_triage_action": {"reason": "checks_not_green"},
                        "evaluate_checks": {"failures": ["ci"]},
                    }
                }
            )
        ).output
        self.assertTrue(out["ok"])
        self.assertIn("Repair PR #7", out["prompt"])
        self.assertEqual(out["reason"], "checks_not_green")

    def test_prepare_worktree_dry_from_parse(self) -> None:
        out = issue_to_pr.prepare_worktree(
            req(
                {
                    "dry_run": True,
                    "worktree_root": "/tmp/wt",
                    "clone_path": "/tmp/clone",
                    "conduction": {
                        "parse_issue_ref": {"branch": "ai/fix/1-x", "repo": "o/r"}
                    },
                }
            )
        ).output
        self.assertEqual(out["status"], "planned")
        self.assertIn("ai/fix/1-x", out["worktree_path"])

    def test_remove_worktree_guards_open_issue(self) -> None:
        out = remove_worktree(
            req(
                {
                    "dry_run": True,
                    "clone_path": "/c",
                    "worktree_path": "/w",
                    "require_safe": True,
                    "conduction": {
                        "check_issue_closed": {"closed": False, "issue": 1},
                        "check_no_open_pr": {"safe_to_cleanup": True},
                    },
                }
            )
        ).output
        self.assertEqual(out["status"], "noop")
        self.assertEqual(out["reason"], "issue_still_open")

    def test_parse_issue_from_branch(self) -> None:
        out = parse_issue_from_branch(
            req({"branch": "ai/fix/99-something"})
        ).output
        self.assertEqual(out["issue"], 99)

    def test_write_dispatch_receipt_payload_from_conduction(self) -> None:
        out = issue_to_pr.write_dispatch_receipt(
            req(
                {
                    "dry_run": True,
                    "receipt_path": "/tmp/r.json",
                    "conduction": {
                        "parse_issue_ref": {"repo": "o/r", "issue": 2, "branch": "b"},
                        "open_pull_request": {"number": 5, "url": "u"},
                        "prepare_worktree": {"worktree_path": "/wt"},
                    },
                }
            )
        ).output
        self.assertEqual(out["status"], "planned")
        self.assertEqual(out["payload"]["pr_number"], 5)
        self.assertEqual(out["payload"]["repo"], "o/r")


class EffectorHelperTests(unittest.TestCase):
    def test_effector_factory(self) -> None:
        e = effector("x", "repo_agent.steps.poll.poll_eligible_issues", conduction=["y"])
        self.assertEqual(e.id, "x")
        self.assertEqual(e.conduction, ["y"])
        self.assertEqual(e.adapter.kind, "python_function")


if __name__ == "__main__":
    unittest.main()
