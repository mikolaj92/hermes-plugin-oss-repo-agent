"""Structural checks for the Fala v2 package path manifest."""

from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
import unittest
from pathlib import Path


PACKAGE_PATH = Path(__file__).resolve().parents[1] / "fala-package.toml"
EXPECTED_PATH_IDS = {"issue_intake", "issue_to_pr", "pr_triage", "cleanup", "cleanup_reconcile", "auto_worker"}
LANE_ROOTS = {
    "issue_intake": ("select_issue_candidate",),
    "issue_to_pr": ("select_dispatch_task",),
    "pr_triage": ("select_fix_pr",),
    "auto_worker": (
        "intake_select_issue_candidate",
        "triage_read_open_prs",
        "lifecycle_read_lifecycle_github_state",
        "lifecycle_read_lifecycle_local_evidence",
    ),
}


class PackageStructureTests(unittest.TestCase):
    def test_v2_paths_have_unique_effectors_and_valid_conduction(self) -> None:
        """Every declared path is a self-contained, ordered atomic graph."""
        package = tomllib.loads(PACKAGE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(package["version"], "2")
        paths = package["correlation_paths"]
        self.assertEqual({path["id"] for path in paths}, EXPECTED_PATH_IDS)

        for path in paths:
            effectors = path["effectors"]
            path_effector_ids = {effector["id"] for effector in effectors}
            self.assertEqual(len(path_effector_ids), len(effectors))
            self.assertTrue(path_effector_ids)
            positions = {effector["id"]: index for index, effector in enumerate(effectors)}
            for effector in effectors:
                self.assertEqual(effector["adapter"]["kind"], "subprocess")
                self.assertTrue(effector["config"]["handler"].startswith("lokay.steps."))
                self.assertTrue(
                    set(effector.get("conduction", [])).issubset(path_effector_ids),
                    effector["id"],
                )
                self.assertTrue(
                    all(positions[predecessor] < positions[effector["id"]] for predecessor in effector.get("conduction", [])),
                    effector["id"],
                )

            lane_roots = LANE_ROOTS.get(path["id"])
            if lane_roots is None:
                continue
            reachable: set[str] = set()
            first_selector_position = min(positions[root] for root in lane_roots)
            for selector in lane_roots:
                lane_reachable = {selector}
                changed = True
                while changed:
                    changed = False
                    for effector in effectors:
                        if effector["id"] not in lane_reachable and lane_reachable.intersection(effector.get("conduction", [])):
                            lane_reachable.add(effector["id"])
                            changed = True
                reachable.update(lane_reachable)
            self.assertTrue(
                all(effector["id"] in reachable for effector in effectors[first_selector_position + 1:]),
                f"{path['id']} has an operation outside its declared selector lanes",
            )

            if path["id"] == "issue_intake":
                by_id = {effector["id"]: effector for effector in effectors}
                expected = {
                    "read_triage_receipt_index": ["normalize_issue_rows"],
                    "select_triage_candidate": ["normalize_issue_rows", "read_triage_receipt_index"],
                    "reserve_triage_run_budget": ["select_triage_candidate"],
                    "read_triage_issue_state": ["reserve_triage_run_budget", "select_triage_candidate"],
                    "read_triage_comments": ["reserve_triage_run_budget", "select_triage_candidate"],
                    "read_triage_repository_state": ["reserve_triage_run_budget", "select_triage_candidate"],
                    "build_triage_context": ["read_triage_repository_state", "reserve_triage_run_budget", "select_triage_candidate"],
                    "classify_triage_issue": ["read_triage_issue_state", "read_triage_comments", "build_triage_context", "reserve_triage_run_budget", "select_triage_candidate"],
                    "verify_triage_repository_unchanged": ["classify_triage_issue", "build_triage_context"],
                    "publish_triage_decision_receipt": ["verify_triage_repository_unchanged", "classify_triage_issue"],
                    "read_triage_canonical_issue": ["classify_triage_issue", "reserve_triage_run_budget", "select_triage_candidate"],
                    "read_triage_labels": ["publish_triage_decision_receipt", "reserve_triage_run_budget", "select_triage_candidate"],
                    "decide_triage_mutation": ["read_triage_labels", "classify_triage_issue", "read_triage_issue_state", "read_triage_comments", "read_triage_canonical_issue", "reserve_triage_run_budget", "select_triage_candidate"],
                    "ensure_triage_label": ["decide_triage_mutation", "read_triage_labels"],
                    "publish_triage_mutation_authorization": ["decide_triage_mutation", "ensure_triage_label"],
                    "mutate_triage_issue_labels": ["publish_triage_mutation_authorization", "ensure_triage_label", "decide_triage_mutation", "read_triage_labels"],
                    "post_triage_feedback": ["publish_triage_mutation_authorization", "mutate_triage_issue_labels", "decide_triage_mutation", "read_triage_labels", "classify_triage_issue"],
                    "verify_triage_feedback": ["post_triage_feedback", "mutate_triage_issue_labels", "read_triage_labels", "decide_triage_mutation"],
                    "observe_triage_feedback": ["verify_triage_feedback", "read_triage_labels", "decide_triage_mutation"],
                    "publish_triage_feedback_receipt": ["observe_triage_feedback", "verify_triage_feedback"],
                    "publish_triage_mutation_verification": ["mutate_triage_issue_labels", "verify_triage_feedback", "decide_triage_mutation"],
                    "publish_triage_close_authorization": ["decide_triage_mutation", "read_triage_issue_state", "read_triage_comments", "read_triage_canonical_issue", "read_triage_labels", "classify_triage_issue", "publish_triage_mutation_verification", "publish_triage_feedback_receipt"],
                    "close_triage_issue": ["publish_triage_close_authorization", "read_triage_labels", "decide_triage_mutation"],
                    "verify_triage_issue_closed": ["close_triage_issue", "read_triage_labels", "decide_triage_mutation"],
                    "publish_triage_close_verification": ["verify_triage_issue_closed"],
                    "verify_triage_receipt": ["publish_triage_close_verification", "publish_triage_close_authorization", "publish_triage_mutation_verification", "publish_triage_feedback_receipt", "publish_triage_decision_receipt"],
                    "build_triage_terminal": ["normalize_issue_rows", "select_triage_candidate", "reserve_triage_run_budget", "verify_triage_receipt", "decide_triage_mutation", "mutate_triage_issue_labels", "verify_triage_feedback", "verify_triage_issue_closed"],
                    "filter_issue_eligibility": ["build_triage_terminal"],
                    "select_issue_candidate": ["filter_issue_eligibility"],
                }
                for effector_id, conduction in expected.items():
                    self.assertEqual(by_id[effector_id]["conduction"], conduction, effector_id)
                auto_path = next(item for item in package["correlation_paths"] if item["id"] == "auto_worker")
                auto_by_id = {effector["id"]: effector for effector in auto_path["effectors"]}
                for effector_id, conduction in expected.items():
                    prefixed_id = f"intake_{effector_id}"
                    self.assertEqual(
                        auto_by_id[prefixed_id]["conduction"],
                        [f"intake_{upstream}" for upstream in conduction],
                        prefixed_id,
                    )
                self.assertLess(positions["normalize_issue_rows"], positions["read_triage_receipt_index"])

                self.assertIn("decide_issue_priority", by_id)
                self.assertLess(positions["select_issue_candidate"], positions["decide_issue_priority"])
                self.assertLess(positions["decide_issue_priority"], positions["decide_issue_action"])
                self.assertLess(positions["read_triage_receipt_index"], positions["select_triage_candidate"])
                self.assertLess(positions["select_triage_candidate"], positions["reserve_triage_run_budget"])
                self.assertLess(positions["reserve_triage_run_budget"], positions["classify_triage_issue"])
                self.assertLess(positions["classify_triage_issue"], positions["publish_triage_mutation_authorization"])
                self.assertLess(positions["publish_triage_mutation_authorization"], positions["mutate_triage_issue_labels"])
                self.assertLess(positions["publish_triage_mutation_verification"], positions["publish_triage_close_authorization"])
                self.assertLess(positions["publish_triage_close_authorization"], positions["close_triage_issue"])
                self.assertLess(positions["close_triage_issue"], positions["verify_triage_issue_closed"])
                self.assertLess(positions["verify_triage_issue_closed"], positions["publish_triage_close_verification"])
                self.assertLess(positions["publish_triage_close_verification"], positions["verify_triage_receipt"])
                self.assertLess(positions["verify_triage_receipt"], positions["build_triage_terminal"])
                self.assertLess(positions["build_triage_terminal"], positions["filter_issue_eligibility"])
                self.assertLess(positions["filter_issue_eligibility"], positions["select_issue_candidate"])
                for triage_id in expected:
                    if triage_id in {"filter_issue_eligibility", "select_issue_candidate"}:
                        continue
                    for legacy in ("select_issue_candidate", "decide_issue_action", "reserve_claim_file", "read_intake_tasks", "create_intake_task", "reconcile_intake_task"):
                        self.assertNotIn(triage_id, by_id[legacy].get("conduction", []))
                self.assertNotIn("select_triage_candidate", by_id["filter_issue_eligibility"]["conduction"])
                self.assertNotIn("normalize_issue_rows", by_id["filter_issue_eligibility"]["conduction"])

            if path["id"] in {"pr_triage", "auto_worker"}:
                prefix = "triage_" if path["id"] == "auto_worker" else ""
                by_id = {effector["id"]: effector for effector in effectors}
                self.assertEqual(
                    by_id[f"{prefix}read_pr_comments"]["conduction"],
                    [f"{prefix}decide_triage_action", f"{prefix}verify_pr_assignee", f"{prefix}load_pr_fields"],
                )
                self.assertEqual(
                    by_id[f"{prefix}decide_pr_comment"]["conduction"],
                    [f"{prefix}read_pr_comments", f"{prefix}decide_triage_action", f"{prefix}load_pr_fields"],
                )
                self.assertEqual(
                    by_id[f"{prefix}post_pr_comment"]["conduction"],
                    [f"{prefix}decide_pr_comment", f"{prefix}decide_triage_action", f"{prefix}load_pr_fields"],
                )
                self.assertEqual(
                    by_id[f"{prefix}verify_pr_comment"]["conduction"],
                    [f"{prefix}post_pr_comment", f"{prefix}decide_pr_comment", f"{prefix}load_pr_fields"],
                )
                self.assertEqual(
                    by_id[f"{prefix}find_review_marker"]["conduction"],
                    [f"{prefix}read_review_tasks", f"{prefix}load_pr_fields"],
                )
                self.assertEqual(
                    by_id[f"{prefix}create_review_task"]["conduction"],
                    [f"{prefix}find_review_marker", f"{prefix}load_pr_fields"],
                )
                self.assertEqual(
                    by_id[f"{prefix}read_review_tasks"]["conduction"],
                    [f"{prefix}decide_triage_action", f"{prefix}select_fix_pr"],
                )
                self.assertEqual(
                    by_id[f"{prefix}read_repair_context"]["conduction"],
                    [f"{prefix}build_repair_prompt", f"{prefix}decide_triage_action"],
                )
                self.assertEqual(by_id[f"{prefix}read_existing_repair_pr"]["conduction"], [f"{prefix}decide_repair_attempt", f"{prefix}verify_repair_push_oid"])
                self.assertEqual(
                    by_id[f"{prefix}build_repair_receipt"]["conduction"],
                    [
                        f"{prefix}decide_repair_attempt",
                        f"{prefix}verify_existing_repair_pr",
                        f"{prefix}verify_repair_push_oid",
                        f"{prefix}verify_repair_omp_postconditions",
                        f"{prefix}invoke_repair_omp",
                    ],
                )
                self.assertEqual(
                    by_id[f"{prefix}verify_repair_receipt"]["conduction"],
                    [f"{prefix}decide_repair_attempt", f"{prefix}publish_repair_receipt", f"{prefix}build_repair_receipt"],
                )
                self.assertEqual(by_id[f"{prefix}update_repair_branch_provenance"]["conduction"], [f"{prefix}decide_repair_attempt", f"{prefix}verify_repair_receipt", f"{prefix}verify_repair_push_oid"])
                self.assertEqual(by_id[f"{prefix}verify_updated_repair_branch_provenance"]["conduction"], [f"{prefix}update_repair_branch_provenance"])
                expected_completed = [f"{prefix}load_pr_fields"]
                if path["id"] == "auto_worker":
                    expected_completed.extend([f"{prefix}read_repair_context", f"{prefix}read_repair_remote_head", "lifecycle_decide_lifecycle_transition"])
                else:
                    expected_completed.append(f"{prefix}read_repair_remote_head")
                self.assertEqual(by_id[f"{prefix}read_repair_completed_receipt"]["conduction"], expected_completed)
                self.assertIn(f"{prefix}read_repair_completed_receipt", by_id[f"{prefix}decide_repair_attempt"]["conduction"])
                self.assertIn(f"{prefix}decide_triage_action", by_id[f"{prefix}decide_repair_attempt"]["conduction"])
                self.assertIn(f"{prefix}read_repair_attempt_reconciliation", by_id[f"{prefix}decide_repair_attempt"]["conduction"])
                self.assertEqual(
                    by_id[f"{prefix}decide_repair_worktree_ownership"]["conduction"],
                    [
                        f"{prefix}read_repair_context",
                        f"{prefix}read_repair_remote_head",
                        f"{prefix}read_repair_worktree_inventory",
                        f"{prefix}read_repair_branch_provenance",
                        f"{prefix}read_repair_creation_evidence",
                        f"{prefix}verify_legacy_repair_pr_head",
                        f"{prefix}read_repair_worktree_cleanliness",
                        f"{prefix}read_repair_remote_ancestry",
                        f"{prefix}fast_forward_repair_worktree",
                    ],
                )
                self.assertEqual(by_id[f"{prefix}read_repair_attempt_reconciliation"]["conduction"], [f"{prefix}read_repair_attempt_state", f"{prefix}read_repair_completed_receipt", f"{prefix}read_repair_remote_head", f"{prefix}read_repair_worktree_inventory", f"{prefix}read_repair_branch_provenance"])
                self.assertIn(f"{prefix}fetch_repair_remote_head", by_id)
                self.assertIn(f"{prefix}verify_fetched_repair_remote_head", by_id)
                self.assertEqual(by_id[f"{prefix}fetch_repair_remote_head"]["conduction"], [f"{prefix}read_repair_context", f"{prefix}read_repair_remote_head"])
                self.assertEqual(by_id[f"{prefix}verify_fetched_repair_remote_head"]["conduction"], [f"{prefix}fetch_repair_remote_head"])
                self.assertIn(f"{prefix}verify_fetched_repair_remote_head", by_id[f"{prefix}read_repair_remote_ancestry"]["conduction"])
                self.assertEqual(by_id[f"{prefix}read_repair_attempt_recovery_evidence"]["conduction"], [f"{prefix}read_repair_attempt_state", f"{prefix}read_repair_attempt_reconciliation"])
                self.assertEqual(by_id[f"{prefix}claim_repair_attempt_recovery"]["conduction"], [f"{prefix}read_repair_attempt_recovery_evidence"])
                self.assertEqual(by_id[f"{prefix}verify_repair_attempt_recovery"]["conduction"], [f"{prefix}claim_repair_attempt_recovery"])
                self.assertEqual(by_id[f"{prefix}read_repair_recovery_continuation_evidence"]["conduction"], [f"{prefix}verify_repair_attempt_recovery", f"{prefix}read_repair_attempt_state"])
                self.assertEqual(by_id[f"{prefix}claim_repair_recovery_continuation"]["conduction"], [f"{prefix}read_repair_recovery_continuation_evidence"])
                self.assertEqual(by_id[f"{prefix}verify_repair_recovery_continuation"]["conduction"], [f"{prefix}claim_repair_recovery_continuation", f"{prefix}verify_repair_attempt_recovery", f"{prefix}read_repair_attempt_state"])
                self.assertIn(f"{prefix}verify_repair_attempt_recovery", by_id[f"{prefix}decide_repair_attempt"]["conduction"])
                self.assertIn(f"{prefix}verify_repair_recovery_continuation", by_id[f"{prefix}decide_repair_attempt"]["conduction"])
                self.assertEqual(by_id[f"{prefix}read_repair_attempt_baseline"]["conduction"], [f"{prefix}verify_repair_worktree", f"{prefix}decide_repair_worktree_ownership", f"{prefix}read_repair_attempt_state", f"{prefix}read_repair_remote_head"])
                self.assertEqual(by_id[f"{prefix}reserve_repair_attempt"]["conduction"], [f"{prefix}decide_repair_attempt", f"{prefix}read_repair_context", f"{prefix}read_repair_attempt_baseline", f"{prefix}verify_repair_attempt_recovery", f"{prefix}verify_repair_recovery_continuation"])
                self.assertEqual(by_id[f"{prefix}verify_repair_attempt_reservation"]["conduction"], [f"{prefix}decide_repair_attempt", f"{prefix}reserve_repair_attempt", f"{prefix}read_repair_context", f"{prefix}verify_repair_attempt_recovery", f"{prefix}verify_repair_recovery_continuation"])
                self.assertEqual(
                    by_id[f"{prefix}read_repair_base_head"]["conduction"],
                    [f"{prefix}read_repair_attempt_state", f"{prefix}read_repair_context", f"{prefix}read_repair_remote_head"],
                )
                self.assertGreater(by_id[f"{prefix}invoke_repair_omp"]["adapter"]["timeout_seconds"], 7200)
                self.assertEqual(
                    by_id[f"{prefix}decide_legacy_repair_head_refresh"]["conduction"],
                    [f"{prefix}decide_triage_action", f"{prefix}read_repair_attempt_state", f"{prefix}load_pr_fields", f"{prefix}read_repair_base_head"],
                )
                self.assertEqual(by_id[f"{prefix}update_legacy_repair_pr_branch"]["conduction"], [f"{prefix}decide_legacy_repair_head_refresh"])
                self.assertEqual(by_id[f"{prefix}verify_legacy_repair_pr_head"]["conduction"], [f"{prefix}decide_legacy_repair_head_refresh", f"{prefix}update_legacy_repair_pr_branch"])
                self.assertIn(f"{prefix}verify_legacy_repair_pr_head", by_id[f"{prefix}decide_repair_worktree_ownership"]["conduction"])

            if path["id"] == "auto_worker":
                self.assertLess(positions["triage_decide_triage_action"], positions["intake_decide_issue_priority"])
                self.assertLess(positions["intake_select_issue_candidate"], positions["intake_decide_issue_priority"])
                self.assertLess(positions["intake_decide_issue_priority"], positions["intake_decide_issue_action"])
                self.assertEqual(
                    by_id["intake_decide_issue_priority"]["conduction"],
                    ["intake_select_issue_candidate", "triage_decide_triage_action"],
                )
                self.assertTrue(
                    all(
                        effector["id"].startswith(
                            ("intake_", "dispatch_", "triage_", "lifecycle_", "aggregate_", "cleanup_")
                        )
                        for effector in effectors
                    )
                )


if __name__ == "__main__":
    unittest.main()
