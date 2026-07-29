from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lokay.steps import issue_triage_receipts as receipts


class ReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.payload = {
            "schema_version": 1,
            "stage": "decision",
            "repo": "owner/repo",
            "issue": 42,
            "updated_at": "2026-07-28T10:00:00Z",
            "decision_digest": "a" * 64,
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def req(self, **extra):
        value = {"triage_receipts": str(self.root), "repo": "owner/repo", "issue": 42, "dry_run": False, **extra}
        return {"input": value, "config": {}}

    def test_first_identical_and_conflicting_publish(self):
        first = receipts.publish_triage_decision_receipt(self.req(payload=self.payload, updated_at=self.payload["updated_at"]))
        self.assertEqual(first["status"], "written")
        same = receipts.publish_triage_decision_receipt(self.req(payload=self.payload, updated_at=self.payload["updated_at"]))
        self.assertEqual(same["status"], "exists")
        conflict = dict(self.payload, classification="ready")
        out = receipts.publish_triage_decision_receipt(self.req(payload=conflict, updated_at=self.payload["updated_at"]))
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "receipt_conflict")

    def test_dry_run_does_not_write(self):
        out = receipts.publish_triage_decision_receipt(self.req(payload=self.payload, dry_run=True, updated_at=self.payload["updated_at"]))
        self.assertEqual(out["status"], "planned")
        self.assertFalse(list(self.root.rglob("*.json")))

    def test_layout_rejects_unsafe_identity(self):
        with self.assertRaises(ValueError):
            receipts._receipt_path(self.root, "owner/../repo", 42, "decision", "x")
        out = receipts.reserve_triage_run_budget(self.req(run_id="../escape"))
        self.assertFalse(out["ok"])
        out = receipts.reserve_triage_run_budget(self.req(run_id="run-1"))
        self.assertEqual(out["status"], "written")
        conflict = receipts.reserve_triage_run_budget(self.req(run_id="run-1", repo="other/repo"))
        self.assertEqual(conflict["reason"], "run_budget_conflict")

    def test_private_regular_single_link_and_symlink_rejected(self):
        out = receipts.publish_triage_decision_receipt(self.req(payload=self.payload, updated_at=self.payload["updated_at"]))
        path = Path(out["receipt_path"])
        self.assertTrue(stat.S_ISREG(path.lstat().st_mode))
        self.assertEqual(path.stat().st_mode & 0o077, 0)
        other = self.root / "other.json"
        other.write_text(path.read_text(), encoding="utf-8")
        path.unlink()
        path.symlink_to(other)
        result = receipts.verify_triage_receipt(self.req(receipt_path=str(path), payload=self.payload))
        self.assertFalse(result["ok"])

    def test_fsync_failure_is_reported(self):
        with mock.patch.object(receipts.os, "fsync", side_effect=OSError("disk")):
            out = receipts.publish_triage_decision_receipt(self.req(payload=self.payload, updated_at=self.payload["updated_at"]))
        self.assertFalse(out["ok"])
        self.assertIn("write_failed", out["reason"])

    def test_index_reduces_pending_stage(self):
        auth = dict(self.payload, stage="mutation-authorized", decision_digest="b" * 64, mutation_attempt_state="attempted")
        receipts.publish_triage_mutation_authorization(self.req(payload=auth, decision_digest="b" * 64))
        index = receipts.read_triage_receipt_index(self.req())
        self.assertTrue(index["index"]["reconcile_pending"])
        self.assertEqual(len(index["index"]["pending"]), 1)

    def test_index_records_verified_decision_revision(self):
        decision = dict(self.payload, selected={"repo": "owner/repo", "number": 42, "updatedAt": self.payload["updated_at"]})
        receipts.publish_triage_decision_receipt(self.req(payload=decision, updated_at=self.payload["updated_at"]))
        before = receipts.read_triage_receipt_index(self.req())["index"]
        self.assertTrue(before["decision_recorded"])
        self.assertFalse(before["triage_verified"])
        self.assertEqual(before["decision_watermark"], self.payload["updated_at"])
        verified = dict(decision, stage="feedback-verified", comment_id=123, verified_readback_state="verified")
        receipts.publish_triage_feedback_receipt(self.req(payload=verified, database_id=123))
        after = receipts.read_triage_receipt_index(self.req())["index"]
        self.assertTrue(after["triage_verified"])
        self.assertEqual(after["feedback_watermark"], self.payload["updated_at"])

    def test_all_stage_atoms_publish_and_verify(self):
        funcs = (
            (receipts.publish_triage_decision_receipt, "decision", "c" * 64),
            (receipts.publish_triage_mutation_authorization, "mutation-authorized", "d" * 64),
            (receipts.publish_triage_mutation_verification, "mutation-verified", "e" * 64),
            (receipts.publish_triage_close_authorization, "close-authorized", "f" * 64),
            (receipts.publish_triage_close_verification, "close-verified", "0" * 64),
        )
        for func, stage, digest in funcs:
            payload = dict(self.payload, stage=stage, decision_digest=digest)
            out = func(self.req(payload=payload, decision_digest=digest, updated_at=self.payload["updated_at"]))
            self.assertTrue(out["ok"], out)
            checked = receipts.verify_triage_receipt(self.req(receipt_path=out["receipt_path"], payload=payload))
            self.assertTrue(checked["ok"], checked)

    def test_flat_index_covers_all_normalized_rows_and_nested_compatibility(self):
        rows = [{"repo": "owner/repo", "number": 42}, {"repo": "other/repo", "number": 7}]
        request = {"input": {"triage_receipts": str(self.root), "triage_enabled": True, "conduction": {"normalize_issue_rows": {"ok": True, "status": "normalized", "rows": rows}}}, "config": {}}
        out = receipts.read_triage_receipt_index(request)
        self.assertTrue(out["ok"], out)
        self.assertIn("owner/repo#42", out["index"])
        self.assertIn("other/repo#7", out["index"])
        nested = receipts.read_triage_receipt_index(self.req())
        self.assertFalse(nested["index"]["reconcile_pending"])

    def test_prefixed_selected_identity_and_budget_continuity(self):
        selected = {"repo": "owner/repo", "number": 42, "title": "candidate"}
        request = {"input": {"triage_receipts": str(self.root), "run_id": "run-prefixed", "dry_run": False, "conduction": {"intake_select_triage_candidate": {"ok": True, "status": "selected", "selected": selected}}}, "config": {}}
        budget = receipts.reserve_triage_run_budget(request)
        self.assertTrue(budget["ok"], budget)
        self.assertEqual((budget["repo"], budget["issue"], budget["selected"]), ("owner/repo", 42, selected))
        same = receipts.reserve_triage_run_budget(request)
        self.assertEqual(same["status"], "exists")
        conflict = receipts.reserve_triage_run_budget({**request, "input": {**request["input"], "conduction": {"intake_select_triage_candidate": {"ok": True, "status": "selected", "selected": {**selected, "number": 43}}}}})
        self.assertEqual(conflict["reason"], "run_budget_conflict")

    def test_prefixed_conducted_repo_number_is_selected_identity(self):
        request = self.req(
            conduction={
                "intake_decide_triage_mutation": {
                    "ok": True,
                    "status": "mutation_decided",
                    "repo": "owner/repo",
                    "number": 42,
                    "action": "add_ready",
                    "decision_digest": "d" * 64,
                }
            }
        )
        request["input"].pop("repo")
        request["input"].pop("issue")
        out = receipts.publish_triage_mutation_authorization(request)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "written")

    def test_close_authorization_noops_for_feedback_action(self):
        request = self.req(
            payload=dict(self.payload, stage="close-authorized", decision_digest="f" * 64),
            decision_digest="f" * 64,
            conduction={
                "decide_triage_mutation": {
                    "ok": True,
                    "status": "mutation_decided",
                    "repo": "owner/repo",
                    "number": 42,
                    "action": "feedback",
                    "decision_digest": "f" * 64,
                }
            },
        )
        out = receipts.publish_triage_close_authorization(request)
        self.assertEqual(out["status"], "noop", out)
        self.assertEqual(out["reason"], "action_not_selected")
        self.assertFalse(list(self.root.rglob("close-authorized-*.json")))

    def test_mutation_verification_noops_for_feedback_without_decide(self):
        request = {
            "input": {
                "triage_receipts": str(self.root),
                "dry_run": False,
                "conduction": {
                    "intake_mutate_triage_issue_labels": {
                        "ok": True,
                        "status": "noop",
                        "reason": "action_not_selected",
                        "repo": "owner/repo",
                        "number": 42,
                    },
                    "intake_verify_triage_feedback": {
                        "ok": True,
                        "status": "feedback_verified",
                        "repo": "owner/repo",
                        "number": 42,
                        "comment_id": 5116451350,
                        "decision_digest": "f" * 64,
                        "verified_readback_state": "verified",
                    },
                },
            },
            "config": {},
        }
        out = receipts.publish_triage_mutation_verification(request)
        self.assertEqual(out["status"], "noop", out)
        self.assertEqual(out["reason"], "action_not_selected")
        self.assertFalse(list(self.root.rglob("mutation-verified-*.json")))

    def test_mutation_verification_publishes_from_conducted_decide(self):
        digest = "a" * 64
        request = {
            "input": {
                "triage_receipts": str(self.root),
                "dry_run": False,
                "conduction": {
                    "intake_decide_triage_mutation": {
                        "ok": True,
                        "status": "mutation_decided",
                        "repo": "owner/repo",
                        "number": 42,
                        "action": "add_ready",
                        "classification": "ready",
                        "decision_digest": digest,
                    },
                    "intake_mutate_triage_issue_labels": {
                        "ok": True,
                        "status": "labels_verified",
                        "repo": "owner/repo",
                        "number": 42,
                        "label": "ai:ready",
                        "verified": True,
                        "decision_digest": digest,
                    },
                    "intake_verify_triage_feedback": {
                        "ok": True,
                        "status": "noop",
                        "reason": "action_not_selected",
                    },
                },
            },
            "config": {},
        }
        out = receipts.publish_triage_mutation_verification(request)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "written", out)
        payload = out["payload"]
        self.assertEqual(payload["stage"], "mutation-verified")
        self.assertEqual(payload["decision_digest"], digest)
        self.assertEqual(payload["label"], "ai:ready")
        self.assertEqual(payload["verified_readback_state"], "verified")
        self.assertTrue(list(self.root.rglob("mutation-verified-*.json")))

    def test_mutation_verification_publishes_already_labeled(self):
        digest = "b" * 64
        request = {
            "input": {
                "triage_receipts": str(self.root),
                "dry_run": False,
                "conduction": {
                    "intake_decide_triage_mutation": {
                        "ok": True,
                        "status": "mutation_decided",
                        "repo": "owner/repo",
                        "number": 42,
                        "action": "add_ready",
                        "classification": "ready",
                        "decision_digest": digest,
                    },
                    "intake_mutate_triage_issue_labels": {
                        "ok": True,
                        "status": "labels_verified",
                        "reason": "already_labeled",
                        "repo": "owner/repo",
                        "number": 42,
                        "label": "ai:ready",
                        "verified": True,
                        "mutated": False,
                        "action": "add_ready",
                        "decision_digest": digest,
                    },
                    "intake_verify_triage_feedback": {
                        "ok": True,
                        "status": "noop",
                        "reason": "action_not_selected",
                    },
                },
            },
            "config": {},
        }
        out = receipts.publish_triage_mutation_verification(request)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "written", out)
        self.assertEqual(out["payload"]["decision_digest"], digest)
        self.assertEqual(out["payload"]["verified_readback_state"], "verified")
        self.assertEqual(out["payload"]["label"], "ai:ready")

    def test_feedback_receipt_identity_scopes_by_digest(self):
        comment_id = 5117775811
        old_digest = "c" * 64
        new_digest = "d" * 64
        first = self.req(
            payload=dict(
                self.payload,
                stage="feedback-verified",
                comment_id=comment_id,
                decision_digest=old_digest,
                verified_readback_state="verified",
                issue_updated_at=self.payload["updated_at"],
            ),
            database_id=comment_id,
            decision_digest=old_digest,
        )
        out1 = receipts.publish_triage_feedback_receipt(first)
        self.assertTrue(out1["ok"], out1)
        self.assertTrue(out1["receipt_path"].endswith(f"feedback-verified-{comment_id}-{old_digest}.json"), out1)
        second = self.req(
            payload=dict(
                self.payload,
                stage="feedback-verified",
                comment_id=comment_id,
                decision_digest=new_digest,
                verified_readback_state="verified",
                issue_updated_at="2026-07-29T12:46:45Z",
            ),
            database_id=comment_id,
            decision_digest=new_digest,
        )
        out2 = receipts.publish_triage_feedback_receipt(second)
        self.assertTrue(out2["ok"], out2)
        self.assertEqual(out2["status"], "written", out2)
        self.assertTrue(out2["receipt_path"].endswith(f"feedback-verified-{comment_id}-{new_digest}.json"), out2)
        self.assertNotEqual(out1["receipt_path"], out2["receipt_path"])

    def test_superseded_decision_pending_is_dropped(self):
        old = dict(
            self.payload,
            stage="decision",
            decision_digest="1" * 64,
            issue_updated_at="2026-07-29T12:00:00Z",
            updated_at="2026-07-29T12:00:00Z",
        )
        new = dict(
            self.payload,
            stage="decision",
            decision_digest="2" * 64,
            issue_updated_at="2026-07-29T12:30:00Z",
            updated_at="2026-07-29T12:30:00Z",
        )
        auth_old = dict(
            self.payload,
            stage="mutation-authorized",
            decision_digest="1" * 64,
            mutation_attempt_state="attempted",
            issue_updated_at="2026-07-29T12:00:00Z",
        )
        feedback_new = dict(
            self.payload,
            stage="feedback-verified",
            decision_digest="2" * 64,
            comment_id=99,
            verified_readback_state="verified",
            issue_updated_at="2026-07-29T12:30:00Z",
        )
        receipts.publish_triage_decision_receipt(self.req(payload=old, decision_digest=old["decision_digest"], updated_at=old["updated_at"]))
        receipts.publish_triage_mutation_authorization(self.req(payload=auth_old, decision_digest=auth_old["decision_digest"]))
        receipts.publish_triage_decision_receipt(self.req(payload=new, decision_digest=new["decision_digest"], updated_at=new["updated_at"]))
        receipts.publish_triage_feedback_receipt(
            self.req(payload=feedback_new, decision_digest=feedback_new["decision_digest"], database_id=99)
        )
        index = receipts.read_triage_receipt_index(self.req())["index"]
        self.assertFalse(index["reconcile_pending"], index)
        self.assertTrue(index["triage_verified"], index)

    def test_disabled_and_no_candidate_no_write_and_terminal_failure_propagates(self):
        disabled = receipts.publish_triage_decision_receipt(self.req(triage_enabled=False, payload=self.payload))
        self.assertEqual(disabled["status"], "noop")
        self.assertFalse(list(self.root.rglob("*.json")))
        no_candidate = receipts.publish_triage_decision_receipt({"input": {"triage_receipts": str(self.root), "triage_enabled": True, "dry_run": False}, "config": {}})
        self.assertEqual(no_candidate["status"], "noop")
        self.assertFalse(list(self.root.rglob("*.json")))
        terminal = receipts.publish_triage_decision_receipt(self.req(payload=self.payload, conduction={"select_triage_candidate": {"ok": False, "status": "failed", "reason": "upstream"}}))
        self.assertEqual((terminal["ok"], terminal["reason"]), (False, "upstream_failed"))
        self.assertFalse(list(self.root.rglob("*.json")))


if __name__ == "__main__":
    unittest.main()
