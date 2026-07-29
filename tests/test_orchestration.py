from __future__ import annotations

import unittest

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

    def test_verified_merge_exposes_exact_cleanup_identity(self) -> None:
        provenance = {"source": "github_pr_readback", "repo": "o/r", "number": 8, "head_ref": "ai/fix/7", "head_oid": "abc"}
        verified = {"status": "merge_receipt_verified", "ok": True, "mutated": False, "verified_provenance": provenance}
        triage = {"status": "verified", "ok": True, "mutated": False, "repo": "o/r", "board": "b", "clone_path": "/repo", "priority": 3, "issue": 7, "pr_number": 8, "branch": "ai/fix/7", "head_oid": "abc"}
        out = aggregate_lane_results(request({
            "auto_worker_triage_verify_merge_receipt": verified,
            "auto_worker_triage_decide_triage_action": triage,
        }))
        self.assertTrue(out["cleanup_authorized"])
        self.assertEqual(out["cleanup_identity"], {"repo": "o/r", "board": "b", "clone_path": "/repo", "priority": 3, "issue": 7, "pr_number": 8, "branch": "ai/fix/7", "head_oid": "abc"})

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

        selected_repair = aggregate_lane_results(request({
            "auto_worker_triage_decide_triage_action": {
                "status": "decided", "ok": True, "action": "repair",
            },
        }))
        self.assertFalse(selected_repair["worked"])
        self.assertTrue(selected_repair["idle"])

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
