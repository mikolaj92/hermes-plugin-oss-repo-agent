from __future__ import annotations

import unittest
import unittest.mock

from lokay.steps import cleanup
from lokay.steps.orchestration import aggregate_lane_results



def request(conduction: dict[str, dict], **values: object) -> dict:
    return {"input": {"conduction": conduction, **values}, "config": {}}


class AggregateLaneResultsTests(unittest.TestCase):
    def test_idle_intake_does_not_hide_repairable_triage(self) -> None:
        intake = {"status": "noop", "ok": True, "mutated": False, "reason": "no_selected_issue"}
        triage = {"status": "decided", "ok": True, "mutated": False, "action": "repair", "repo": "o/r", "number": 7}
        out = aggregate_lane_results(request({
            "auto_worker_intake_select_issue_candidate": intake,
            "auto_worker_triage_decide_triage_action": triage,
        }))
        self.assertEqual(out["status"], "aggregated")
        self.assertIs(out["ok"], True)
        self.assertEqual(out["lanes"]["intake"], intake)
        self.assertEqual(out["lanes"]["triage"], triage)
        self.assertFalse(out["cleanup_authorized"])

    def test_dispatch_failure_keeps_triage_result_and_attributes_lane(self) -> None:
        dispatch = {"status": "failed", "ok": False, "mutated": False, "reason": "push_failed", "failure_class": "terminal"}
        triage = {"status": "decided", "ok": True, "mutated": False, "action": "repair", "repo": "o/r"}
        out = aggregate_lane_results(request({
            "auto_worker_dispatch_verify_task_completed": dispatch,
            "auto_worker_triage_decide_triage_action": triage,
        }))
        self.assertEqual(out["status"], "failed")
        self.assertIs(out["ok"], False)
        self.assertEqual(out["terminal_failures"], [{"lane": "dispatch", "reason": "push_failed", "failure_class": "terminal"}])
        self.assertEqual(out["lanes"]["dispatch"], dispatch)
        self.assertEqual(out["lanes"]["triage"], triage)
    def test_adapter_failure_is_terminal(self) -> None:
        adapter_failure = {"code": "adapter_failed", "message": "subprocess adapter failed"}
        out = aggregate_lane_results(request({"auto_worker_lifecycle_decide_lifecycle_transition": adapter_failure}))
        self.assertFalse(out["ok"])
        self.assertEqual(out["terminal_failures"], [{"lane": "lifecycle", "reason": "adapter_failed", "failure_class": "terminal"}])


    def test_pending_repair_has_no_cleanup_identity(self) -> None:
        pending = {"status": "decided", "ok": True, "mutated": False, "outcome": "wait_pending_checks", "identity": {"repo": "o/r", "issue": 7, "pr_number": 8, "branch": "ai/fix/7", "head_oid": "abc"}}
        triage = {"status": "noop", "ok": True, "mutated": False, "reason": "not_selected"}
        out = aggregate_lane_results(request({
            "auto_worker_lifecycle_decide_lifecycle_transition": pending,
            "auto_worker_triage_verify_merge_receipt": triage,
        }))
        self.assertFalse(out["cleanup_authorized"])
        self.assertNotIn("cleanup_identity", out)

    def test_verified_merge_requires_terminal_lifecycle_identity(self) -> None:
        provenance = {"source": "github_pr_readback", "repo": "o/r", "number": 8, "head_ref": "ai/fix/7", "head_oid": "abc"}
        verified = {"status": "merge_receipt_verified", "ok": True, "mutated": False, "verified_provenance": provenance}
        triage = {"status": "verified", "ok": True, "mutated": False, "repo": "o/r", "board": "b", "clone_path": "/repo", "priority": 3, "issue": 7, "pr_number": 8, "branch": "ai/fix/7", "head_oid": "abc"}
        pending = {"status": "decided", "ok": True, "mutated": False, "outcome": "wait_pending_checks", "identity": {"repo": "o/r", "issue": 7, "pr_number": 8, "branch": "ai/fix/7", "head_oid": "abc"}}
        out = aggregate_lane_results(request({
            "auto_worker_triage_verify_merge_receipt": verified,
            "auto_worker_triage_decide_triage_action": triage,
            "auto_worker_lifecycle_decide_lifecycle_transition": pending,
        }))
        self.assertFalse(out["cleanup_authorized"])

    def test_verified_merge_exposes_exact_cleanup_identity(self) -> None:
        provenance = {"source": "github_pr_readback", "repo": "o/r", "number": 8, "head_ref": "ai/fix/7", "head_oid": "abc"}
        verified = {"status": "merge_receipt_verified", "ok": True, "mutated": False, "verified_provenance": provenance}
        identity = {"repo": "o/r", "issue": 7, "pr_number": 8, "branch": "ai/fix/7", "head_oid": "abc"}
        lifecycle = {"status": "decided", "ok": True, "mutated": False, "outcome": "finalize_merged", "identity": identity}
        triage = {"status": "verified", "ok": True, "mutated": False, "repo": "o/r", "board": "b", "clone_path": "/repo", "priority": 3, **identity}
        out = aggregate_lane_results(request({
            "auto_worker_triage_verify_merge_receipt": verified,
            "auto_worker_triage_decide_triage_action": triage,
            "auto_worker_lifecycle_decide_lifecycle_transition": lifecycle,
        }))
        self.assertTrue(out["cleanup_authorized"])
        self.assertEqual(out["cleanup_identity"], {"repo": "o/r", "board": "b", "clone_path": "/repo", "priority": 3, "issue": 7, "pr_number": 8, "branch": "ai/fix/7", "head_oid": "abc"})

    def test_terminal_lifecycle_resolves_exact_repository_metadata(self) -> None:
        identity = {"repo": "o/r", "issue": 7, "pr_number": 8, "branch": "ai/fix/7", "head_oid": "abc"}
        lifecycle = {"status": "decided", "ok": True, "mutated": False, "outcome": "finalize_merged", "identity": identity}
        out = aggregate_lane_results(request(
            {"auto_worker_lifecycle_decide_lifecycle_transition": lifecycle},
            repos=[{"repo": "other/r", "board": "wrong", "clone_path": "/wrong", "priority": 1},
                   {"repo": "o/r", "board": "b", "clone_path": "/repo", "priority": 3}],
        ))
        self.assertTrue(out["cleanup_authorized"], out)
        self.assertEqual(out["cleanup_identity"], {**identity, "board": "b", "clone_path": "/repo", "priority": 3})

    def test_terminal_lifecycle_rejects_ambiguous_repository_metadata(self) -> None:
        identity = {"repo": "o/r", "issue": 7, "pr_number": 8, "branch": "ai/fix/7", "head_oid": "abc"}
        lifecycle = {"status": "decided", "ok": True, "mutated": False, "outcome": "finalize_merged", "identity": identity}
        out = aggregate_lane_results(request(
            {"auto_worker_lifecycle_decide_lifecycle_transition": lifecycle},
            repos=[{"repo": "o/r", "board": "a", "clone_path": "/a", "priority": 1},
                   {"repo": "o/r", "board": "b", "clone_path": "/b", "priority": 2}],
        ))
        self.assertFalse(out["cleanup_authorized"])

    def test_terminal_lifecycle_identity_must_match_merge_provenance(self) -> None:
        provenance = {"source": "github_pr_readback", "repo": "o/r", "number": 8, "head_ref": "ai/fix/7", "head_oid": "abc"}
        verified = {"status": "merge_receipt_verified", "ok": True, "mutated": False, "verified_provenance": provenance}
        lifecycle = {"status": "decided", "ok": True, "mutated": False, "outcome": "finalize_merged", "identity": {"repo": "o/r", "issue": 7, "pr_number": 8, "branch": "ai/fix/7", "head_oid": "different"}}
        out = aggregate_lane_results(request({
            "auto_worker_triage_verify_merge_receipt": verified,
            "auto_worker_lifecycle_decide_lifecycle_transition": lifecycle,
        }))
        self.assertFalse(out["cleanup_authorized"])
        self.assertNotIn("cleanup_identity", out)
    def test_closed_unmerged_lifecycle_does_not_authorize_cleanup(self) -> None:
        identity = {"repo": "o/r", "issue": 7, "pr_number": 8, "branch": "ai/fix/7", "head_oid": "abc"}
        lifecycle = {"status": "decided", "ok": True, "outcome": "finalize_closed", "identity": identity}
        out = aggregate_lane_results(request({"lifecycle_decide_lifecycle_transition": lifecycle}))
        self.assertFalse(out["cleanup_authorized"])
        self.assertNotIn("cleanup_identity", out)


    def test_repair_cleanup_preserves_distinct_local_branch(self) -> None:
        repo, issue, pr, branch, head = "o/r", 7, 8, "ai/fix/7", "a" * 40
        import hashlib
        digest = hashlib.sha256(f"{repo}\0{pr}\0{branch}".encode()).hexdigest()
        local = f"lokay/repair/{digest}"
        lifecycle = {"status": "decided", "ok": True, "outcome": "finalize_merged", "identity": {"repo": repo, "issue": issue, "pr_number": pr, "branch": branch, "head_oid": head}}
        repair = {"status": "verified", "ok": True, "payload": {"config": {"repo": repo, "issue": issue, "pr_number": pr, "branch": branch, "target_branch": branch, "local_branch": local, "worktree_path": f"/worktrees/{local}", "receipt": "/state/repair.json", "remote_oid": head, "clone_path": "/repo"}}}
        conduction = {
            "triage_verify_repair_receipt": repair,
            "lifecycle_decide_lifecycle_transition": lifecycle,
        }
        out = aggregate_lane_results(request(conduction, repos=[{"repo": repo, "board": "b", "clone_path": "/repo", "priority": 3}]))
        self.assertTrue(out["cleanup_authorized"], out)
        self.assertEqual(out["cleanup_identity"]["branch"], branch)
        self.assertEqual(out["cleanup_identity"]["local_branch"], local)
        conduction["aggregate_lane_results"] = out
        with unittest.mock.patch.object(cleanup, "git", return_value="\n".join([
            f"branch.{cleanup.branch_config_section(local)}.lokay-task task-7",
            f"branch.{cleanup.branch_config_section(local)}.lokay-issue 7",
            f"branch.{cleanup.branch_config_section(local)}.lokay-receipt /state/repair.json",
            f"branch.{cleanup.branch_config_section(local)}.lokay-repo o/r",
            f"branch.{cleanup.branch_config_section(local)}.lokay-local-oid {head}",
        ])):
            owned = cleanup.read_branch_ownership({"input": {"conduction": conduction}, "config": {}})
        self.assertTrue(owned["ok"], owned)
        self.assertEqual(owned["branch"], local)
        conduction["read_branch_ownership"] = owned
        conduction["derive_cleanup_paths"] = {"ok": True, "status": "derived", "branch": branch, "local_branch": local, "worktree_path": f"/trusted/{local}"}
        rejected = cleanup.validate_cleanup_identity({"input": {"conduction": conduction}, "config": {}})
        self.assertEqual(rejected["reason"], "cleanup_identity_mismatch")
        self.assertEqual(rejected["field"], "worktree_path")

    def test_verified_triage_mutation_counts_as_work(self) -> None:
        out = aggregate_lane_results(request({
            "intake_mutate_triage_issue_labels": {
                "status": "labels_verified", "ok": True, "verified": True, "mutated": True,
            },
        }))
        self.assertTrue(out["worked"])
        self.assertFalse(out["idle"])


    def test_verified_triage_feedback_and_closure_count_as_work(self) -> None:
        for atom, receipt in (
            ("intake_verify_triage_feedback", {"status": "feedback_verified", "ok": True, "verified": True}),
            ("intake_verify_triage_issue_closed", {"status": "triage_issue_closed_verified", "ok": True, "verified": True}),
        ):
            with self.subTest(atom=atom):
                out = aggregate_lane_results(request({atom: receipt}))
                self.assertTrue(out["worked"])
                self.assertFalse(out["idle"])
    def test_selection_only_triage_is_idle(self) -> None:
        out = aggregate_lane_results(request({
            "intake_select_triage_candidate": {
                "status": "selected", "ok": True, "selected": {"repo": "o/r", "number": 7},
            },
        }))
        self.assertFalse(out["worked"])
        self.assertFalse(out["mutated"])
        self.assertTrue(out["idle"])

    def test_ordinary_intake_and_dispatch_selection_count_as_work(self) -> None:
        for atom, lane in (
            ("intake_select_issue_candidate", "intake"),
            ("dispatch_select_dispatch_task", "dispatch"),
        ):
            with self.subTest(lane=lane):
                out = aggregate_lane_results(request({
                    atom: {"status": "selected", "ok": True, "selected": {"repo": "o/r", "number": 7}},
                }))
                self.assertTrue(out["worked"])
                self.assertFalse(out["idle"])

    def test_pending_lifecycle_is_not_idle_but_repair_selection_is(self) -> None:
        waiting = aggregate_lane_results(request({
            "auto_worker_lifecycle_decide_lifecycle_transition": {
                "status": "decided", "ok": True, "outcome": "wait_pending_checks",
            },
        }))
        self.assertFalse(waiting["idle"])
        self.assertTrue(waiting["pending"])

        selected_repair = aggregate_lane_results(request({
            "auto_worker_triage_decide_triage_action": {
                "status": "decided", "ok": True, "action": "repair",
            },
        }))
        self.assertFalse(selected_repair["worked"])
        self.assertTrue(selected_repair["idle"])
        self.assertFalse(selected_repair["pending"])

    def test_incomplete_cleanup_identity_does_not_authorize(self) -> None:
        identity = {"repo": "o/r", "issue": 7, "pr_number": 8, "branch": "ai/fix/7", "head_oid": "abc"}
        lifecycle = {"status": "decided", "ok": True, "mutated": False, "outcome": "finalize_merged", "identity": identity}
        out = aggregate_lane_results(request({"auto_worker_lifecycle_decide_lifecycle_transition": lifecycle}))
        self.assertFalse(out["cleanup_authorized"])
        self.assertNotIn("cleanup_identity", out)

    def test_expected_triage_noops_do_not_fail_closed(self) -> None:
        out = aggregate_lane_results(request({
            "intake_select_triage_candidate": {"status": "noop", "ok": True, "reason": "no_candidate"},
            "intake_reserve_triage_run_budget": {"status": "failed", "ok": False, "reason": "claim_busy"},
        }))
        self.assertTrue(out["ok"])
        self.assertEqual(out["terminal_failures"], [])

    def test_expected_noop_reason_cannot_mask_terminal_failure(self) -> None:
        out = aggregate_lane_results(request({
            "intake_mutate_triage_issue_labels": {
                "status": "failed", "ok": False, "reason": "no_candidate", "code": "adapter_failed",
            },
        }))
        self.assertFalse(out["ok"])
        self.assertEqual(out["terminal_failures"][0]["reason"], "no_candidate")

    def test_genuine_triage_failure_propagates(self) -> None:
        out = aggregate_lane_results(request({
            "intake_mutate_triage_issue_labels": {
                "status": "failed", "ok": False, "reason": "label_mutation_failed",
            },
        }))
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["terminal_failures"][0]["lane"], "triage")


if __name__ == "__main__":
    unittest.main()
