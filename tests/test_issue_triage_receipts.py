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
            published = out.get("payload") or payload
            if stage == "close-authorized":
                self.assertIs(published.get("authorized"), False, published)
                self.assertEqual(published.get("reason"), "classification_not_closeable", published)
            checked = receipts.verify_triage_receipt(self.req(receipt_path=out["receipt_path"], payload=published))
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

    def test_close_authorization_noops_for_feedback_without_closeable_class(self):
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
                    "classification": "needs_feedback",
                    "decision_digest": "f" * 64,
                }
            },
        )
        out = receipts.publish_triage_close_authorization(request)
        self.assertEqual(out["status"], "noop", out)
        self.assertEqual(out["reason"], "action_not_selected")
        self.assertFalse(list(self.root.rglob("close-authorized-*.json")))

    def test_close_authorization_evaluates_duplicate_policy_for_feedback(self):
        digest = "f" * 64
        request = self.req(
            payload=dict(self.payload, stage="close-authorized", decision_digest=digest),
            decision_digest=digest,
            auto_close_duplicates=True,
            conduction={
                "classify_triage_issue": {
                    "ok": True,
                    "status": "classified",
                    "repo": "owner/repo",
                    "number": 42,
                    "classification": {
                        "classification": "duplicate",
                        "canonical_issue": 12,
                        "reason": "same bug",
                        "question": "Confirm duplicate of #12?",
                    },
                    "decision_digest": digest,
                },
                "decide_triage_mutation": {
                    "ok": True,
                    "status": "mutation_decided",
                    "repo": "owner/repo",
                    "number": 42,
                    "action": "feedback",
                    "classification": "duplicate",
                    "decision_digest": digest,
                },
                "read_triage_issue_state": {
                    "ok": True,
                    "status": "issue_state_read",
                    "repo": "owner/repo",
                    "number": 42,
                    "issue": {
                        "repo": "owner/repo",
                        "number": 42,
                        "state": "OPEN",
                        "updatedAt": "2026-07-28T10:00:00Z",
                        "labels": ["duplicate"],
                    },
                    "classified_updatedAt": "2026-07-28T10:00:00Z",
                },
                "read_triage_labels": {
                    "ok": True,
                    "status": "triage_labels_read",
                    "repo": "owner/repo",
                    "number": 42,
                    "state": "OPEN",
                    "updatedAt": "2026-07-28T10:00:00Z",
                    "classified_updatedAt": "2026-07-28T10:00:00Z",
                    "labels": ["duplicate"],
                    "comments": [
                        {
                            "databaseId": 7,
                            "author": {"login": "maintainer"},
                            "authorAssociation": "MEMBER",
                            "createdAt": "2026-07-28T09:00:00Z",
                            "body": "This is a duplicate of #12.",
                        }
                    ],
                },
                "read_triage_comments": {
                    "ok": True,
                    "status": "comments_read",
                    "comments": [
                        {
                            "databaseId": 7,
                            "author": {"login": "maintainer"},
                            "authorAssociation": "MEMBER",
                            "createdAt": "2026-07-28T09:00:00Z",
                            "body": "This is a duplicate of #12.",
                        }
                    ],
                },
                "read_triage_canonical_issue": {
                    "ok": True,
                    "status": "canonical_read",
                    "canonical": {"number": 12, "state": "OPEN"},
                },
            },
        )
        out = receipts.publish_triage_close_authorization(request)
        self.assertTrue(out["ok"], out)
        self.assertIn(out["status"], {"written", "planned", "exists"}, out)
        payload = out.get("payload") or {}
        self.assertTrue(payload.get("authorized"), payload)
        self.assertEqual(payload.get("classification"), "duplicate")
        self.assertTrue(list(self.root.rglob("close-authorized-*.json")) or out["status"] == "planned")

    def test_close_authorization_denies_duplicate_when_auto_close_disabled(self):
        digest = "e" * 64
        request = self.req(
            payload=dict(self.payload, stage="close-authorized", decision_digest=digest),
            decision_digest=digest,
            auto_close_duplicates=False,
            conduction={
                "decide_triage_mutation": {
                    "ok": True,
                    "status": "mutation_decided",
                    "repo": "owner/repo",
                    "number": 42,
                    "action": "feedback",
                    "classification": "duplicate",
                    "decision_digest": digest,
                },
                "classify_triage_issue": {
                    "ok": True,
                    "status": "classified",
                    "classification": {"classification": "duplicate", "canonical_issue": 12},
                    "decision_digest": digest,
                },
                "read_triage_labels": {
                    "ok": True,
                    "status": "triage_labels_read",
                    "repo": "owner/repo",
                    "number": 42,
                    "state": "OPEN",
                    "updatedAt": "2026-07-28T10:00:00Z",
                    "classified_updatedAt": "2026-07-28T10:00:00Z",
                    "labels": ["duplicate"],
                    "comments": [],
                },
            },
        )
        out = receipts.publish_triage_close_authorization(request)
        self.assertTrue(out["ok"], out)
        payload = out.get("payload") or {}
        self.assertFalse(payload.get("authorized"), payload)
        self.assertEqual(payload.get("reason"), "auto_close_disabled")

    def test_close_authorization_rejects_injected_authorized_without_class(self):
        digest = "c" * 64
        request = self.req(
            payload=dict(
                self.payload,
                stage="close-authorized",
                decision_digest=digest,
                authorized=True,
                verified=True,
            ),
            decision_digest=digest,
        )
        out = receipts.publish_triage_close_authorization(request)
        self.assertTrue(out["ok"], out)
        payload = out.get("payload") or {}
        self.assertFalse(payload.get("authorized"), payload)
        self.assertEqual(payload.get("reason"), "classification_not_closeable")

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

    def test_mutation_verification_overwrites_pre_stamp_watermark(self):
        digest = "c" * 64
        post_stamp = "2026-08-01T17:52:37Z"
        request = {
            "input": {
                "triage_receipts": str(self.root),
                "dry_run": False,
                "issue_updated_at": "2026-07-29T14:23:58Z",
                "updated_at": "2026-07-29T14:23:58Z",
                "conduction": {
                    "intake_decide_triage_mutation": {
                        "ok": True,
                        "status": "mutation_decided",
                        "repo": "owner/repo",
                        "number": 42,
                        "action": "feedback",
                        "classification": "needs_feedback",
                        "decision_digest": digest,
                        "issue_updated_at": "2026-07-29T14:23:58Z",
                    },
                    "intake_mutate_triage_issue_labels": {
                        "ok": True,
                        "status": "labels_verified",
                        "repo": "owner/repo",
                        "number": 42,
                        "label": "ai:needs-feedback",
                        "verified": True,
                        "mutated": True,
                        "action": "feedback",
                        "decision_digest": digest,
                        "issue_updated_at": post_stamp,
                        "updatedAt": post_stamp,
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
        payload = out["payload"]
        self.assertEqual(payload["issue_updated_at"], post_stamp)
        self.assertEqual(payload["updated_at"], post_stamp)
        index = receipts.read_triage_receipt_index(self.req())["index"]
        self.assertEqual(index["feedback_watermark"], post_stamp)

    def test_index_advances_feedback_watermark_from_mutation_verified(self):
        decision = dict(
            self.payload,
            stage="decision",
            decision_digest="d" * 64,
            issue_updated_at="2026-07-29T14:23:58Z",
            selected={"repo": "owner/repo", "number": 42, "updatedAt": "2026-07-29T14:23:58Z"},
        )
        receipts.publish_triage_decision_receipt(self.req(payload=decision, updated_at="2026-07-29T14:23:58Z"))
        feedback = dict(
            decision,
            stage="feedback-verified",
            comment_id=5119067049,
            verified_readback_state="verified",
            issue_updated_at="2026-07-29T14:23:58Z",
        )
        receipts.publish_triage_feedback_receipt(self.req(payload=feedback, database_id=5119067049))
        mutation = dict(
            decision,
            stage="mutation-verified",
            verified_readback_state="verified",
            action="feedback",
            label="ai:needs-feedback",
            issue_updated_at="2026-08-01T17:52:37Z",
            updated_at="2026-08-01T17:52:37Z",
        )
        receipts.publish_triage_mutation_verification(self.req(payload=mutation, decision_digest="d" * 64))
        index = receipts.read_triage_receipt_index(self.req())["index"]
        self.assertTrue(index["triage_verified"])
        self.assertEqual(index["feedback_watermark"], "2026-08-01T17:52:37Z")

    def test_index_exposes_close_verified_from_close_stage(self):
        summary = receipts._reduce(
            [
                {
                    "stage": "decision",
                    "decision_digest": "a" * 64,
                    "issue_updated_at": "2026-07-28T10:00:00Z",
                    "receipt_path": "decision-a.json",
                },
                {
                    "stage": "mutation-verified",
                    "decision_digest": "a" * 64,
                    "verified_readback_state": "verified",
                    "issue_updated_at": "2026-08-01T17:52:37Z",
                    "receipt_path": "mutation-verified-a.json",
                },
                {
                    "stage": "close-verified",
                    "decision_digest": "a" * 64,
                    "verified_readback_state": "closed",
                    "issue_updated_at": "2026-08-01T18:00:00Z",
                    "receipt_path": "close-verified-a.json",
                },
            ]
        )
        self.assertTrue(summary["close_verified"])
        self.assertTrue(summary["triage_verified"])
        pending = receipts._reduce(
            [
                {
                    "stage": "decision",
                    "decision_digest": "b" * 64,
                    "issue_updated_at": "2026-07-28T10:00:00Z",
                    "receipt_path": "decision-b.json",
                },
                {
                    "stage": "mutation-verified",
                    "decision_digest": "b" * 64,
                    "verified_readback_state": "verified",
                    "issue_updated_at": "2026-08-01T17:52:37Z",
                    "receipt_path": "mutation-verified-b.json",
                },
            ]
        )
        self.assertFalse(pending["close_verified"])

    def test_mutation_feedback_and_close_coexist_on_action_identity(self):
        digest = "c" * 64
        feedback = dict(
            self.payload,
            stage="mutation-authorized",
            decision_digest=digest,
            action="feedback",
            classification="out_of_scope",
            label="ai:needs-feedback",
            status="mutation_decided",
            mutated=False,
        )
        first = receipts.publish_triage_mutation_authorization(
            self.req(payload=feedback, decision_digest=digest, action="feedback")
        )
        self.assertEqual(first["status"], "written", first)
        self.assertTrue(first["receipt_path"].endswith(f"mutation-authorized-{digest}-feedback.json"), first)
        close = dict(
            feedback,
            action="close",
            label="ai:out-of-scope",
        )
        second = receipts.publish_triage_mutation_authorization(
            self.req(payload=close, decision_digest=digest, action="close")
        )
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["status"], "written", second)
        self.assertTrue(second["receipt_path"].endswith(f"mutation-authorized-{digest}-close.json"), second)
        self.assertNotEqual(first["receipt_path"], second["receipt_path"])
        # Legacy pure-digest feedback receipt remains readable and untouched.
        legacy_path = receipts._receipt_path(self.root, "owner/repo", 42, "mutation-authorized", digest)
        legacy_payload = dict(feedback, stage="mutation-authorized")
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(json.dumps(legacy_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        legacy_path.chmod(0o600)
        again = receipts.publish_triage_mutation_authorization(
            self.req(payload=close, decision_digest=digest, action="close")
        )
        self.assertEqual(again["status"], "exists", again)
        self.assertEqual(json.loads(legacy_path.read_text(encoding="utf-8"))["action"], "feedback")
        self.assertEqual(json.loads(Path(second["receipt_path"]).read_text(encoding="utf-8"))["action"], "close")

    def test_close_verified_loads_digest_from_close_authorization(self):
        digest = "a" * 64
        out = receipts.publish_triage_close_verification(
            self.req(
                action="close",
                selected={"repo": "owner/repo", "number": 42},
                conduction={
                    "publish_triage_close_authorization": {
                        "ok": True,
                        "status": "written",
                        "decision_digest": digest,
                        "action": "close",
                        "payload": {
                            "decision_digest": digest,
                            "action": "close",
                            "authorized": True,
                            "repo": "owner/repo",
                            "issue": 42,
                            "number": 42,
                        },
                    },
                    "verify_triage_issue_closed": {
                        "ok": True,
                        "status": "triage_issue_closed_verified",
                        "repo": "owner/repo",
                        "number": 42,
                    },
                    "select_triage_candidate": {
                        "ok": True,
                        "status": "selected",
                        "selected": {"repo": "owner/repo", "number": 42},
                    },
                },
            )
        )
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "written", out)
        self.assertTrue(
            out["receipt_path"].endswith(f"close-verified-{digest}-close.json"),
            out,
        )
        payload = out.get("payload") or {}
        self.assertEqual(payload.get("decision_digest"), digest, payload)
        self.assertEqual(payload.get("action"), "close", payload)


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

    def test_verify_receipt_uses_published_mutation_path(self):
        digest = "e" * 64
        auth = dict(self.payload, stage="mutation-authorized", decision_digest=digest)
        written = receipts.publish_triage_mutation_authorization(self.req(payload=auth, decision_digest=digest))
        self.assertTrue(written["ok"], written)
        verified_payload = dict(
            self.payload,
            stage="mutation-verified",
            decision_digest=digest,
            verified=True,
            verified_readback_state="verified",
            label="ai:ready",
        )
        mutation = receipts.publish_triage_mutation_verification(
            self.req(payload=verified_payload, decision_digest=digest)
        )
        self.assertTrue(mutation["ok"], mutation)
        request = {
            "input": {
                "triage_receipts": str(self.root),
                "dry_run": False,
                "conduction": {
                    "intake_publish_triage_mutation_verification": {
                        "ok": True,
                        "status": "written",
                        "receipt_path": mutation["receipt_path"],
                        "payload": mutation["payload"],
                    },
                    "intake_publish_triage_feedback_receipt": {
                        "ok": True,
                        "status": "noop",
                        "reason": "action_not_selected",
                    },
                    "intake_publish_triage_close_authorization": {
                        "ok": True,
                        "status": "noop",
                        "reason": "action_not_selected",
                    },
                    "intake_publish_triage_close_verification": {
                        "ok": True,
                        "status": "noop",
                        "reason": "action_not_selected",
                    },
                    "intake_publish_triage_decision_receipt": {
                        "ok": True,
                        "status": "written",
                        "receipt_path": written["receipt_path"],
                        "payload": written["payload"],
                    },
                },
            },
            "config": {},
        }
        out = receipts.verify_triage_receipt(request)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "verified", out)
        self.assertTrue(out.get("verified"))
        self.assertEqual(out["receipt_path"], mutation["receipt_path"])
        self.assertEqual(out["payload"]["stage"], "mutation-verified")

    def test_verify_ignores_dispatch_receipt_path(self):
        digest = "f" * 64
        verified_payload = dict(
            self.payload,
            stage="mutation-verified",
            decision_digest=digest,
            verified=True,
            verified_readback_state="verified",
            label="ai:ready",
        )
        mutation = receipts.publish_triage_mutation_verification(
            self.req(payload=verified_payload, decision_digest=digest)
        )
        self.assertTrue(mutation["ok"], mutation)
        request = {
            "input": {
                "triage_receipts": str(self.root),
                "receipt_path": "/Users/mini-m4-main/.hermes/state/lokay-dispatch-live/auto-worker-dispatch-example.json",
                "paths": {"triage_receipts": str(self.root)},
                "dry_run": False,
                "conduction": {
                    "intake_publish_triage_mutation_verification": {
                        "ok": True,
                        "status": "written",
                        "receipt_path": mutation["receipt_path"],
                        "payload": mutation["payload"],
                    },
                    "intake_publish_triage_feedback_receipt": {
                        "ok": True,
                        "status": "noop",
                        "reason": "action_not_selected",
                    },
                    "intake_publish_triage_close_authorization": {
                        "ok": True,
                        "status": "noop",
                        "reason": "action_not_selected",
                    },
                    "intake_publish_triage_close_verification": {
                        "ok": True,
                        "status": "noop",
                        "reason": "action_not_selected",
                    },
                    "intake_publish_triage_decision_receipt": {
                        "ok": True,
                        "status": "noop",
                        "reason": "action_not_selected",
                    },
                },
            },
            "config": {},
        }
        out = receipts.verify_triage_receipt(request)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "verified", out)
        self.assertEqual(out["receipt_path"], mutation["receipt_path"])

    def test_verify_noops_without_selection_despite_dispatch_path(self):
        request = {
            "input": {
                "triage_receipts": str(self.root),
                "receipt_path": "/Users/mini-m4-main/.hermes/state/lokay-dispatch-live/auto-worker-dispatch-example.json",
                "paths": {"triage_receipts": str(self.root)},
                "dry_run": False,
                "conduction": {
                    "intake_publish_triage_mutation_verification": {
                        "ok": True,
                        "status": "noop",
                        "reason": "no_triage_selection",
                    },
                    "intake_publish_triage_feedback_receipt": {
                        "ok": True,
                        "status": "noop",
                        "reason": "no_triage_selection",
                    },
                    "intake_publish_triage_close_authorization": {
                        "ok": True,
                        "status": "noop",
                        "reason": "no_triage_selection",
                    },
                    "intake_publish_triage_close_verification": {
                        "ok": True,
                        "status": "noop",
                        "reason": "no_triage_selection",
                    },
                    "intake_publish_triage_decision_receipt": {
                        "ok": True,
                        "status": "noop",
                        "reason": "triage_candidate_missing",
                    },
                },
            },
            "config": {},
        }
        out = receipts.verify_triage_receipt(request)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "noop", out)
        self.assertIn(out.get("reason"), {"no_triage_selection", "triage_candidate_missing"}, out)

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

    def test_reconcile_publish_reuses_existing_decision_without_rewrite(self) -> None:
        classification = {
            "schema_version": 1,
            "classification": "needs_feedback",
            "reason": "Need maintainer intent",
            "question": "What should happen?",
            "canonical_issue": 0,
            "evidence": [{"kind": "issue", "identity": "issue:42", "quote": "local worker"}],
        }
        digest = "e" * 64
        selected = {
            "repo": "owner/repo",
            "number": 42,
            "candidate_class": "reconcile_decision",
            "updatedAt": "2026-07-28T10:00:00Z",
        }
        payload = {
            "schema_version": 1,
            "stage": "decision",
            "repo": "owner/repo",
            "issue": 42,
            "number": 42,
            "action": "needs_feedback",
            "classification": classification,
            "decision_digest": digest,
            "issue_updated_at": "2026-07-28T10:00:00Z",
            "question": "What should happen?",
            "status": "classified",
            "stdout_sha256": "f" * 64,
            "candidate_class": "untriaged",
            "selected": dict(selected),
            "extra_residual_field": "must-not-rebuild",
        }
        issue_dir = self.root / "owner__repo" / "42"
        issue_dir.mkdir(parents=True)
        path = issue_dir / ("decision-" + "a" * 64 + ".json")
        path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
        before = path.read_text(encoding="utf-8")
        req = self.req(
            selected=selected,
            candidate_class="reconcile_decision",
            conduction={
                "select_triage_candidate": {
                    "ok": True,
                    "status": "selected",
                    "selected": selected,
                    "candidate_class": "reconcile_decision",
                    "repo": "owner/repo",
                    "number": 42,
                },
                "reserve_triage_run_budget": {
                    "ok": True,
                    "status": "exists",
                    "selected": selected,
                    "repo": "owner/repo",
                    "number": 42,
                    "issue": 42,
                },
                "classify_triage_issue": {
                    "ok": True,
                    "status": "classified",
                    "reason": "decision_reused",
                    "selected": selected,
                    "classification": classification,
                    "action": "needs_feedback",
                    "decision_digest": digest,
                    "question": "What should happen?",
                },
            },
        )
        out = receipts.publish_triage_decision_receipt(req)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "exists")
        self.assertEqual(out["reason"], "decision_reused")
        self.assertEqual(out["decision_digest"], digest)
        self.assertEqual(Path(out["receipt_path"]), path)
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        loaded = receipts.load_latest_triage_decision(req)
        self.assertTrue(loaded["ok"], loaded)
        self.assertEqual(loaded["decision_digest"], digest)
        self.assertEqual(loaded["action"], "needs_feedback")

    def test_frozen_ready_publish_skips_durable_decision(self) -> None:
        selected = {
            "repo": "owner/repo",
            "number": 31,
            "candidate_class": "frozen_ready_conflict",
            "labels": ["frozen", "ai:ready"],
        }
        digest = "b" * 64
        classification = {
            "schema_version": 1,
            "classification": "ready",
            "reason": "frozen_ready_reconciliation",
            "question": "",
            "canonical_issue": 0,
            "evidence": [{"kind": "issue", "identity": "issue:31", "quote": "frozen+ready"}],
        }
        req = self.req(
            selected=selected,
            candidate_class="frozen_ready_conflict",
            conduction={
                "select_triage_candidate": {
                    "ok": True,
                    "status": "selected",
                    "selected": selected,
                    "candidate_class": "frozen_ready_conflict",
                    "repo": "owner/repo",
                    "number": 31,
                },
                "reserve_triage_run_budget": {
                    "ok": True,
                    "status": "exists",
                    "selected": selected,
                    "repo": "owner/repo",
                    "number": 31,
                    "issue": 31,
                },
                "classify_triage_issue": {
                    "ok": True,
                    "status": "classified",
                    "reason": "frozen_ready_reconciliation",
                    "selected": selected,
                    "classification": classification,
                    "action": "remove_ready",
                    "decision_digest": digest,
                },
            },
        )
        out = receipts.publish_triage_decision_receipt(req)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "exists")
        self.assertEqual(out["reason"], "frozen_ready_reconciliation")
        self.assertIsNone(out.get("receipt_path"))
        self.assertEqual(out["decision_digest"], digest)
        self.assertFalse(list(self.root.rglob("*.json")))



if __name__ == "__main__":
    unittest.main()
