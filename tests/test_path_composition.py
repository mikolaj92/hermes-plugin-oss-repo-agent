"""Structural checks for the Fala v2 package path manifest."""

from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
import unittest
from pathlib import Path


PACKAGE_PATH = Path(__file__).resolve().parents[1] / "fala-package.toml"
EXPECTED_PATH_ORDER = (
    "repo_issue_poll",
    "issue_triage",
    "issue_feedback",
    "issue_split",
    "issue_close",
    "issue_ready",
    "issue_to_pr",
    "pr_triage",
    "pr_repair",
    "pr_merge",
    "cleanup",
    "cleanup_reconcile",
)
EXPECTED_PATH_IDS = set(EXPECTED_PATH_ORDER)
FORBIDDEN_PATH_IDS = {"issue_intake", "auto_worker", "tick_all"}

# Exact process-owned effector sets (path_id == process_id).
OWNED_EFFECTORS: dict[str, tuple[str, ...]] = {
    "repo_issue_poll": (
        "read_open_issues",
        "normalize_issue_rows",
    ),
    "issue_triage": (
        "read_triage_receipt_index",
        "select_triage_candidate",
        "reserve_triage_run_budget",
        "read_triage_issue_state",
        "read_triage_comments",
        "read_triage_repository_state",
        "build_triage_context",
        "classify_triage_issue",
        "verify_triage_repository_unchanged",
        "publish_triage_decision_receipt",
        "read_triage_canonical_issue",
        "read_triage_labels",
        "decide_triage_mutation",
    ),
    "issue_feedback": (
        "ensure_triage_label",
        "publish_triage_mutation_authorization",
        "mutate_triage_issue_labels",
        "post_triage_feedback",
        "verify_triage_feedback",
        "observe_triage_feedback",
        "publish_triage_feedback_receipt",
        "publish_triage_mutation_verification",
    ),
    "issue_split": (
        "split_mixed_triage_issue",
    ),
    "issue_close": (
        "publish_triage_close_authorization",
        "close_triage_issue",
        "verify_triage_issue_closed",
        "publish_triage_close_verification",
        "verify_triage_receipt",
    ),
    "issue_ready": (
        "build_triage_terminal",
        "filter_issue_eligibility",
        "select_issue_candidate",
        "decide_issue_priority",
        "decide_issue_action",
        "read_issue_comments",
        "decide_issue_comment",
        "post_issue_comment",
        "verify_issue_comment",
        "reserve_claim_file",
        "read_issue_claim_state",
        "assign_issue",
        "intake_add_issue_label",
        "verify_issue_claim",
        "build_issue_claim_result",
        "read_intake_tasks",
        "find_intake_marker",
        "create_intake_task",
        "reconcile_intake_task",
    ),
    "pr_triage": (
        "read_open_prs",
        "filter_fix_prs",
        "select_fix_pr",
        "load_pr_fields",
        "evaluate_checks",
        "evaluate_test_evidence",
        "decide_triage_action",
    ),
    "pr_repair": (
        "read_review_tasks",
        "find_review_marker",
        "create_review_task",
        "reconcile_review_task",
        "build_repair_prompt",
        "read_repair_context",
        "read_repair_remote_head",
        "fetch_repair_remote_head",
        "verify_fetched_repair_remote_head",
        "read_repair_worktree_inventory",
        "read_repair_branch_provenance",
        "read_repair_worktree_cleanliness",
        "read_repair_remote_ancestry",
        "decide_repair_worktree_fast_forward",
        "read_repair_worktree_branch_before_fast_forward",
        "read_repair_worktree_head_before_fast_forward",
        "read_repair_worktree_cleanliness_before_fast_forward",
        "decide_repair_worktree_fast_forward_execution",
        "read_repair_creation_evidence",
        "read_repair_attempt_state",
        "read_repair_base_head",
        "decide_legacy_repair_head_refresh",
        "update_legacy_repair_pr_branch",
        "verify_legacy_repair_pr_head",
        "fast_forward_repair_worktree",
        "decide_repair_worktree_ownership",
        "create_repair_branch",
        "write_repair_branch_provenance",
        "add_repair_worktree",
        "verify_repair_worktree",
        "read_repair_attempt_baseline",
        "read_repair_completed_receipt",
        "read_repair_attempt_reconciliation",
        "read_repair_attempt_recovery_evidence",
        "claim_repair_attempt_recovery",
        "verify_repair_attempt_recovery",
        "read_repair_recovery_continuation_evidence",
        "claim_repair_recovery_continuation",
        "verify_repair_recovery_continuation",
        "decide_repair_attempt",
        "reserve_repair_attempt",
        "verify_repair_attempt_reservation",
        "read_repair_omp_preconditions",
        "invoke_repair_omp",
        "verify_repair_omp_postconditions",
        "read_repair_worktree_head",
        "decide_repair_push",
        "push_repair_branch",
        "read_repair_pushed_ref",
        "verify_repair_push_oid",
        "read_existing_repair_pr",
        "verify_existing_repair_pr",
        "build_repair_receipt",
        "publish_repair_receipt",
        "verify_repair_receipt",
        "update_repair_branch_provenance",
        "verify_updated_repair_branch_provenance",
    ),
    "pr_merge": (
        "read_pr_assignees",
        "decide_pr_assignee",
        "assign_pr",
        "verify_pr_assignee",
        "read_pr_comments",
        "decide_pr_comment",
        "post_pr_comment",
        "verify_pr_comment",
        "read_merge_preconditions",
        "merge_pr",
        "read_merge_postcondition",
        "verify_merge_provenance",
        "verify_linked_merge_provenance",
        "read_linked_issue_state",
        "close_linked_issue",
        "verify_linked_issue_closed",
        "build_merge_receipt",
        "read_receipt_merge_provenance",
        "publish_merge_receipt",
        "verify_merge_receipt",
    ),
}

# Sibling exclusivity: each process forbids exclusive effectors owned by siblings.
FORBIDDEN_EFFECTORS: dict[str, frozenset[str]] = {
    "issue_triage": frozenset(
        {
            "post_triage_feedback",
            "publish_triage_feedback_receipt",
            "split_mixed_triage_issue",
            "close_triage_issue",
            "verify_triage_issue_closed",
            "publish_triage_close_authorization",
            "publish_triage_close_verification",
            "verify_triage_receipt",
            "build_triage_terminal",
            "reserve_claim_file",
            "assign_issue",
            "filter_issue_eligibility",
            "select_issue_candidate",
            "create_intake_task",
        }
    ),
    "issue_feedback": frozenset(
        {
            "close_triage_issue",
            "verify_triage_issue_closed",
            "publish_triage_close_authorization",
            "publish_triage_close_verification",
            "verify_triage_receipt",
            "split_mixed_triage_issue",
            "build_triage_terminal",
            "reserve_claim_file",
            "assign_issue",
            "build_issue_claim_result",
            "filter_issue_eligibility",
            "select_issue_candidate",
            "create_intake_task",
        }
    ),
    "issue_split": frozenset(
        {
            "close_triage_issue",
            "verify_triage_issue_closed",
            "post_triage_feedback",
            "publish_triage_feedback_receipt",
            "build_triage_terminal",
            "reserve_claim_file",
            "assign_issue",
            "filter_issue_eligibility",
            "select_issue_candidate",
        }
    ),
    "issue_close": frozenset(
        {
            "post_triage_feedback",
            "publish_triage_feedback_receipt",
            "split_mixed_triage_issue",
            "build_triage_terminal",
            "reserve_claim_file",
            "assign_issue",
            "create_intake_task",
            "filter_issue_eligibility",
            "select_issue_candidate",
        }
    ),
    "issue_ready": frozenset(
        {
            "close_triage_issue",
            "verify_triage_issue_closed",
            "post_triage_feedback",
            "publish_triage_feedback_receipt",
            "split_mixed_triage_issue",
            "publish_triage_close_authorization",
            "publish_triage_close_verification",
            "verify_triage_receipt",
        }
    ),
    "pr_triage": frozenset(
        {
            "merge_pr",
            "publish_merge_receipt",
            "verify_merge_receipt",
            "invoke_repair_omp",
            "publish_repair_receipt",
            "verify_repair_receipt",
            "reserve_repair_attempt",
            "decide_repair_attempt",
        }
    ),
    "pr_repair": frozenset(
        {
            "merge_pr",
            "publish_merge_receipt",
            "verify_merge_receipt",
            "close_linked_issue",
            "read_merge_preconditions",
        }
    ),
    "pr_merge": frozenset(
        {
            "invoke_repair_omp",
            "publish_repair_receipt",
            "verify_repair_receipt",
            "reserve_repair_attempt",
            "decide_repair_attempt",
            "create_repair_branch",
        }
    ),
}

LANE_ROOTS = {
    "repo_issue_poll": ("read_open_issues",),
    "issue_triage": ("select_triage_candidate",),
    "issue_feedback": ("ensure_triage_label",),
    "issue_split": ("split_mixed_triage_issue",),
    "issue_close": ("publish_triage_close_authorization",),
    "issue_ready": ("build_triage_terminal",),
    "issue_to_pr": ("select_dispatch_task",),
    "pr_triage": ("select_fix_pr",),
    "pr_repair": ("read_review_tasks",),
    "pr_merge": ("read_pr_assignees",),
    "cleanup": ("resolve_cleanup_branch_source",),
    "cleanup_reconcile": ("validate_reconcile_identity",),
}


class PackageStructureTests(unittest.TestCase):
    def test_v2_paths_have_unique_effectors_and_valid_conduction(self) -> None:
        """Every declared path is a self-contained, ordered atomic graph."""
        package = tomllib.loads(PACKAGE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(package["version"], "2")
        paths = package["correlation_paths"]
        path_ids = [path["id"] for path in paths]
        self.assertEqual(path_ids, list(EXPECTED_PATH_ORDER))
        self.assertEqual(set(path_ids), EXPECTED_PATH_IDS)
        self.assertEqual(len(path_ids), 12)
        for forbidden in FORBIDDEN_PATH_IDS:
            self.assertNotIn(forbidden, path_ids)

        by_path = {path["id"]: path for path in paths}

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

            owned = OWNED_EFFECTORS.get(path["id"])
            if owned is not None:
                self.assertEqual(tuple(effector["id"] for effector in effectors), owned)

            forbidden = FORBIDDEN_EFFECTORS.get(path["id"], frozenset())
            self.assertFalse(path_effector_ids & forbidden, path["id"])

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
                all(effector["id"] in reachable for effector in effectors[first_selector_position + 1 :]),
                f"{path['id']} has an operation outside its declared selector lanes",
            )

        # Disjoint sibling ownership for issue and PR process families.
        issue_siblings = ("issue_feedback", "issue_split", "issue_close", "issue_ready")
        for left in issue_siblings:
            left_ids = {effector["id"] for effector in by_path[left]["effectors"]}
            for right in issue_siblings:
                if left >= right:
                    continue
                right_ids = {effector["id"] for effector in by_path[right]["effectors"]}
                self.assertFalse(left_ids & right_ids, f"{left}/{right}")

        pr_siblings = ("pr_triage", "pr_repair", "pr_merge")
        for left in pr_siblings:
            left_ids = {effector["id"] for effector in by_path[left]["effectors"]}
            for right in pr_siblings:
                if left >= right:
                    continue
                right_ids = {effector["id"] for effector in by_path[right]["effectors"]}
                self.assertFalse(left_ids & right_ids, f"{left}/{right}")

        # Bounded process-local conduction contracts retained from the prior package.
        issue_triage = {effector["id"]: effector for effector in by_path["issue_triage"]["effectors"]}
        self.assertEqual(issue_triage["select_triage_candidate"]["conduction"], ["read_triage_receipt_index"])
        self.assertEqual(issue_triage["reserve_triage_run_budget"]["conduction"], ["select_triage_candidate"])
        self.assertEqual(
            issue_triage["decide_triage_mutation"]["conduction"],
            [
                "read_triage_labels",
                "classify_triage_issue",
                "read_triage_issue_state",
                "read_triage_comments",
                "read_triage_canonical_issue",
                "reserve_triage_run_budget",
                "select_triage_candidate",
            ],
        )
        self.assertNotIn("post_triage_feedback", issue_triage)
        self.assertNotIn("close_triage_issue", issue_triage)

        issue_feedback = {effector["id"]: effector for effector in by_path["issue_feedback"]["effectors"]}
        self.assertEqual(
            issue_feedback["publish_triage_feedback_receipt"]["conduction"],
            ["observe_triage_feedback", "verify_triage_feedback"],
        )
        self.assertNotIn("close_triage_issue", issue_feedback)
        self.assertNotIn("split_mixed_triage_issue", issue_feedback)

        issue_close = {effector["id"]: effector for effector in by_path["issue_close"]["effectors"]}
        self.assertEqual(
            issue_close["verify_triage_receipt"]["conduction"],
            ["publish_triage_close_verification", "publish_triage_close_authorization"],
        )
        self.assertNotIn("post_triage_feedback", issue_close)

        issue_ready = {effector["id"]: effector for effector in by_path["issue_ready"]["effectors"]}
        self.assertEqual(issue_ready["filter_issue_eligibility"]["conduction"], ["build_triage_terminal"])
        self.assertEqual(issue_ready["select_issue_candidate"]["conduction"], ["filter_issue_eligibility"])
        self.assertNotIn("normalize_issue_rows", issue_ready["filter_issue_eligibility"]["conduction"])
        self.assertNotIn("select_triage_candidate", issue_ready["filter_issue_eligibility"]["conduction"])

        pr_triage = {effector["id"]: effector for effector in by_path["pr_triage"]["effectors"]}
        self.assertEqual(
            pr_triage["decide_triage_action"]["conduction"],
            [
                "evaluate_checks",
                "evaluate_test_evidence",
                "read_open_prs",
                "filter_fix_prs",
                "select_fix_pr",
                "load_pr_fields",
            ],
        )
        self.assertNotIn("merge_pr", pr_triage)
        self.assertNotIn("invoke_repair_omp", pr_triage)

        pr_repair = {effector["id"]: effector for effector in by_path["pr_repair"]["effectors"]}
        self.assertEqual(
            pr_repair["build_repair_receipt"]["conduction"],
            [
                "decide_repair_attempt",
                "verify_existing_repair_pr",
                "verify_repair_push_oid",
                "verify_repair_omp_postconditions",
                "invoke_repair_omp",
            ],
        )
        self.assertEqual(
            pr_repair["verify_repair_receipt"]["conduction"],
            ["decide_repair_attempt", "publish_repair_receipt", "build_repair_receipt"],
        )
        self.assertEqual(
            pr_repair["decide_repair_worktree_ownership"]["conduction"],
            [
                "read_repair_context",
                "read_repair_remote_head",
                "read_repair_worktree_inventory",
                "read_repair_branch_provenance",
                "read_repair_creation_evidence",
                "verify_legacy_repair_pr_head",
                "read_repair_worktree_cleanliness",
                "read_repair_remote_ancestry",
                "fast_forward_repair_worktree",
            ],
        )
        self.assertEqual(
            pr_repair["reserve_repair_attempt"]["conduction"],
            [
                "decide_repair_attempt",
                "read_repair_context",
                "read_repair_attempt_baseline",
                "verify_repair_attempt_recovery",
                "verify_repair_recovery_continuation",
            ],
        )
        self.assertGreater(pr_repair["invoke_repair_omp"]["adapter"]["timeout_seconds"], 7200)
        self.assertNotIn("merge_pr", pr_repair)

        pr_merge = {effector["id"]: effector for effector in by_path["pr_merge"]["effectors"]}
        self.assertEqual(
            pr_merge["read_merge_preconditions"]["conduction"],
            ["verify_pr_assignee", "verify_pr_comment"],
        )
        self.assertEqual(pr_merge["verify_merge_receipt"]["conduction"], ["publish_merge_receipt"])
        self.assertNotIn("invoke_repair_omp", pr_merge)
        self.assertNotIn("create_repair_branch", pr_merge)

        issue_to_pr = {effector["id"]: effector for effector in by_path["issue_to_pr"]["effectors"]}
        self.assertGreater(issue_to_pr["invoke_omp"]["adapter"]["timeout_seconds"], 7200)
        self.assertEqual(
            issue_to_pr["complete_task"]["conduction"],
            [
                "decide_task_completion",
                "read_task_for_completion",
                "select_dispatch_task",
                "decide_held_issue_already_merged",
                "read_merged_closing_prs",
                "invoke_omp",
                "verify_omp_postconditions",
            ],
        )
        self.assertEqual(
            issue_to_pr["read_task_for_completion"]["conduction"],
            [
                "select_dispatch_task",
                "decide_held_issue_already_merged",
                "read_merged_closing_prs",
                "verify_dispatch_receipt",
                "invoke_omp",
                "verify_omp_postconditions",
            ],
        )
        self.assertEqual(issue_to_pr["update_branch_local_oid"]["conduction"], ["verify_push_oid"])
        self.assertEqual(issue_to_pr["verify_updated_branch_local_oid"]["conduction"], ["update_branch_local_oid"])
        self.assertEqual(issue_to_pr["read_open_pr_for_branch"]["conduction"], ["verify_updated_branch_local_oid"])

        cleanup = {effector["id"]: effector for effector in by_path["cleanup"]["effectors"]}
        self.assertEqual(
            cleanup["verify_claim_release_evidence"]["conduction"],
            [
                "verify_cleanup_guards",
                "verify_local_branch_absent",
                "verify_worktree_absent",
                "remove_worktree",
                "delete_local_branch",
                "check_issue_closed",
                "check_no_open_pr_for_branch",
            ],
        )
        self.assertEqual(
            cleanup["collect_cleanup_receipt_evidence"]["conduction"],
            [
                "verify_claim_absent",
                "parse_cleanup_issue_number",
                "check_issue_closed",
                "check_no_open_pr_for_branch",
                "remove_worktree",
                "delete_local_branch",
                "release_claim_file",
            ],
        )

        cleanup_reconcile = {effector["id"]: effector for effector in by_path["cleanup_reconcile"]["effectors"]}
        self.assertEqual(
            tuple(effector["id"] for effector in by_path["cleanup_reconcile"]["effectors"]),
            (
                "validate_reconcile_identity",
                "read_local_receipts",
                "read_claim_process_evidence",
                "read_github_terminal_state",
                "read_remote_provenance",
                "read_reconcile_worktree_state",
                "decide_no_target_reconciliation",
                "update_task_receipt",
                "publish_reconcile_receipt",
                "verify_no_target_reconciliation",
            ),
        )
        self.assertEqual(
            cleanup_reconcile["verify_no_target_reconciliation"]["conduction"],
            ["publish_reconcile_receipt"],
        )


if __name__ == "__main__":
    unittest.main()
