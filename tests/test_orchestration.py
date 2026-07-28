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


if __name__ == "__main__":
    unittest.main()
