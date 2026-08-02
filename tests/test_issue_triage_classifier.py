from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lokay.steps import issue_triage_classifier


def request(tmp: str):
    issue = {"repo": "owner/repo", "number": 42, "title": "Dropped response", "body": "The local worker drops a response.", "updatedAt": "2026-07-28T10:00:00Z"}
    return {
        "input": {
            "selected": issue,
            "repo_goal": "Reliable issue automation",
            "context": {"README.md": "Reliable issue automation for local workers."},
            "comments": [],
            "sources": {"issue:42": "Dropped response\nThe local worker drops a response.", "repository_context:README.md": "Reliable issue automation for local workers."},
            "command": "omp",
            "model": "model",
            "thinking": "medium",
            "timeout_seconds": 30,
            "context_max_bytes": 131072,
            "sandbox_root": tmp,
        },
        "config": {"dry_run": False},
    }


class ClassifierSandboxTests(unittest.TestCase):
    @mock.patch("lokay.steps.issue_triage_classifier.run_omp")
    def test_parses_complete_stdout_and_uses_empty_sandbox(self, run_omp) -> None:
        value = {"schema_version": 1, "classification": "ready", "reason": "Matches goal", "question": "", "canonical_issue": 0, "evidence": [{"kind": "issue", "identity": "issue:42", "quote": "local worker"}]}
        run_omp.return_value = {"status": "completed", "stdout": json.dumps(value), "stdout_tail": "irrelevant"}
        with tempfile.TemporaryDirectory() as tmp:
            out = issue_triage_classifier.classify_triage_issue(request(tmp))
            self.assertEqual(out["status"], "classified")
            self.assertEqual(out["classification"]["classification"], "ready")
            kwargs = run_omp.call_args.kwargs
            self.assertTrue(kwargs["classification"])
            self.assertNotEqual(Path(kwargs["cwd"]), Path(tmp))
            self.assertFalse(Path(kwargs["cwd"]).exists())
            self.assertIn("UNTRUSTED_GITHUB_CONTENT", kwargs["prompt"])
            self.assertIn('"schema_version":1', kwargs["prompt"])
            self.assertIn("integer 1", kwargs["prompt"])
            self.assertEqual(out["stdout_sha256"], __import__("hashlib").sha256(json.dumps(value).encode()).hexdigest())
            self.assertIn("issue:<number>", kwargs["prompt"])
            self.assertIn("comment:<databaseId>", kwargs["prompt"])
            self.assertIn("repository_context:<path>", kwargs["prompt"])
            self.assertIn('"evidence_source_identities":["issue:42","repository_context:README.md"]', kwargs["prompt"])

    @mock.patch("lokay.steps.issue_triage_classifier.run_omp")
    def test_prompt_lists_exact_source_identities(self, run_omp) -> None:
        value = {
            "schema_version": 1,
            "classification": "ready",
            "reason": "Matches goal",
            "question": "",
            "canonical_issue": 0,
            "evidence": [{"kind": "issue", "identity": "issue:42", "quote": "local worker"}],
        }
        run_omp.return_value = {"status": "completed", "stdout": json.dumps(value)}
        with tempfile.TemporaryDirectory() as tmp:
            req = request(tmp)
            req["input"]["comments"] = [{"databaseId": 7, "body": "This is a duplicate of #12."}]
            req["input"]["sources"] = {
                "issue:42": "Dropped response\nThe local worker drops a response.",
                "comment:7": "This is a duplicate of #12.",
                "repository_context:README.md": "Reliable issue automation for local workers.",
            }
            out = issue_triage_classifier.classify_triage_issue(req)
        self.assertEqual(out["status"], "classified", out)
        prompt = run_omp.call_args.kwargs["prompt"]
        self.assertIn(
            '"evidence_source_identities":["comment:7","issue:42","repository_context:README.md"]',
            prompt,
        )
        self.assertIn("Bare numbers, bare paths, or unprefixed labels are invalid", prompt)

    @mock.patch("lokay.steps.issue_triage_classifier.run_omp")
    def test_rejects_bare_identity_stdout(self, run_omp) -> None:
        value = {
            "schema_version": 1,
            "classification": "ready",
            "reason": "Matches goal",
            "question": "",
            "canonical_issue": 0,
            "evidence": [{"kind": "issue", "identity": "42", "quote": "local worker"}],
        }
        run_omp.return_value = {"status": "completed", "stdout": json.dumps(value)}
        with tempfile.TemporaryDirectory() as tmp:
            out = issue_triage_classifier.classify_triage_issue(request(tmp))
        self.assertEqual(out["status"], "failed")
        self.assertIn("classifier_failed:invalid_evidence_identity", out["reason"])
        self.assertTrue(out["retry_safe"])

    @mock.patch("lokay.steps.issue_triage_classifier.run_omp")
    def test_rejects_tail_only_or_wrapped_output(self, run_omp) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for result in ({"status": "completed", "stdout_tail": "{}"}, {"status": "completed", "stdout": "```json\n{}\n```"}):
                with self.subTest(result=result):
                    run_omp.return_value = result
                    out = issue_triage_classifier.classify_triage_issue(request(tmp))
                    self.assertEqual(out["status"], "failed")
                    self.assertFalse(out["mutated"])

    @mock.patch("lokay.steps.issue_triage_classifier.run_omp")
    def test_ambiguous_becomes_feedback_decision(self, run_omp) -> None:
        value = {"schema_version": 1, "classification": "ambiguous", "reason": "Missing behavior", "question": "", "canonical_issue": 0, "evidence": [{"kind": "issue", "identity": "issue:42", "quote": "local worker"}]}
        run_omp.return_value = {"status": "completed", "stdout": json.dumps(value)}
        with tempfile.TemporaryDirectory() as tmp:
            out = issue_triage_classifier.classify_triage_issue(request(tmp))
        self.assertEqual(out["action"], "needs_feedback")
        self.assertTrue(out["question"])

    @mock.patch("lokay.steps.issue_triage_classifier.run_omp")
    def test_dry_run_does_not_claim_business_classification(self, run_omp) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            req = request(tmp)
            req["config"]["dry_run"] = True
            out = issue_triage_classifier.classify_triage_issue(req)
        self.assertEqual(out["status"], "planned")
        run_omp.assert_not_called()
    @mock.patch("lokay.steps.issue_triage_classifier.run_omp")
    def test_forwards_committed_context_packet_and_builds_sources(self, run_omp) -> None:
        value = {"schema_version": 1, "classification": "ready", "reason": "Matches repository", "question": "", "canonical_issue": 0, "evidence": [{"kind": "repository_context", "identity": "repository_context:README.md", "quote": "Reliable issue automation"}]}
        run_omp.return_value = {"status": "completed", "stdout": json.dumps(value)}
        with tempfile.TemporaryDirectory() as tmp:
            req = request(tmp)
            req["input"].pop("context")
            req["input"].pop("sources")
            req["input"]["conduction"] = {
                "build_triage_context": {"packet": {"context": [{"path": "README.md", "sha256": "0" * 64, "bytes": 26, "content": "Reliable issue automation"}]}},
                "read_triage_issue_state": {"issue": req["input"]["selected"]},
                "read_triage_comments": {"comments": []},
            }
            out = issue_triage_classifier.classify_triage_issue(req)
        self.assertEqual(out["status"], "classified", out)
        self.assertIn('"repository_context":{"README.md":"Reliable issue automation"}', run_omp.call_args.kwargs["prompt"])
        prompt = run_omp.call_args.kwargs["prompt"]
        self.assertIn('"evidence_source_identities":["issue:42","repository_context:README.md"]', prompt)

    @mock.patch("lokay.steps.issue_triage_classifier.run_omp")
    def test_reconcile_reuses_durable_decision_without_omp(self, run_omp) -> None:
        classification = {
            "schema_version": 1,
            "classification": "out_of_scope",
            "reason": "Already shipped",
            "question": "",
            "canonical_issue": 0,
            "evidence": [{"kind": "issue", "identity": "issue:42", "quote": "local worker"}],
        }
        digest = "c" * 64
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            issue_dir = root / "owner__repo" / "42"
            issue_dir.mkdir(parents=True)
            payload = {
                "schema_version": 1,
                "stage": "decision",
                "repo": "owner/repo",
                "issue": 42,
                "number": 42,
                "action": "out_of_scope",
                "classification": classification,
                "decision_digest": digest,
                "issue_updated_at": "2026-07-28T10:00:00Z",
                "question": "",
                "status": "classified",
                "selected": {"repo": "owner/repo", "number": 42, "candidate_class": "reconcile_decision"},
            }
            path = issue_dir / ("decision-" + "d" * 64 + ".json")
            path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            path.chmod(0o600)
            selected = {
                "repo": "owner/repo",
                "number": 42,
                "candidate_class": "reconcile_decision",
                "updatedAt": "2026-07-28T10:00:00Z",
            }
            req = {
                "input": {
                    "selected": selected,
                    "repo": "owner/repo",
                    "number": 42,
                    "triage_receipts": str(root),
                    "candidate_class": "reconcile_decision",
                    "conduction": {
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
                        "read_triage_issue_state": {
                            "ok": True,
                            "status": "issue_read",
                            "issue": selected,
                            "selected": selected,
                            "repo": "owner/repo",
                            "number": 42,
                        },
                        "read_triage_comments": {
                            "ok": True,
                            "status": "comments_read",
                            "comments": [],
                            "selected": selected,
                        },
                        "build_triage_context": {
                            "ok": True,
                            "status": "context_packet",
                            "reason": "decision_reused",
                            "selected": selected,
                            "packet": {"context": []},
                            "pre_snapshot": {},
                            "post_snapshot": {},
                        },
                    },
                },
                "config": {"dry_run": False, "triage_receipts": str(root)},
            }
            out = issue_triage_classifier.classify_triage_issue(req)
            self.assertEqual(out["status"], "classified", out)
            self.assertEqual(out["reason"], "decision_reused")
            self.assertEqual(out["action"], "out_of_scope")
            self.assertEqual(out["decision_digest"], digest)
            self.assertEqual(out["classification"], classification)
            run_omp.assert_not_called()

    @mock.patch("lokay.steps.issue_triage_classifier.run_omp")
    def test_reconcile_reuses_durable_decision_under_dry_run(self, run_omp) -> None:
        classification = {
            "schema_version": 1,
            "classification": "needs_feedback",
            "reason": "Need maintainer intent",
            "question": "What should happen?",
            "canonical_issue": 0,
            "evidence": [{"kind": "issue", "identity": "issue:42", "quote": "local worker"}],
        }
        digest = "a" * 64
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            issue_dir = root / "owner__repo" / "42"
            issue_dir.mkdir(parents=True)
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
                "selected": {"repo": "owner/repo", "number": 42, "candidate_class": "reconcile_decision"},
            }
            path = issue_dir / ("decision-" + "b" * 64 + ".json")
            path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            path.chmod(0o600)
            selected = {
                "repo": "owner/repo",
                "number": 42,
                "candidate_class": "reconcile_decision",
                "updatedAt": "2026-07-28T10:00:00Z",
            }
            req = {
                "input": {
                    "selected": selected,
                    "repo": "owner/repo",
                    "number": 42,
                    "triage_receipts": str(root),
                    "candidate_class": "reconcile_decision",
                    "conduction": {
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
                        "read_triage_issue_state": {
                            "ok": True,
                            "status": "issue_read",
                            "issue": selected,
                            "selected": selected,
                            "repo": "owner/repo",
                            "number": 42,
                        },
                        "read_triage_comments": {
                            "ok": True,
                            "status": "comments_read",
                            "comments": [],
                            "selected": selected,
                        },
                        "build_triage_context": {
                            "ok": True,
                            "status": "context_packet",
                            "reason": "decision_reused",
                            "selected": selected,
                            "packet": {"context": []},
                            "pre_snapshot": {},
                            "post_snapshot": {},
                        },
                    },
                },
                # Default dry_run is True; reuse must still return classification/action.
                "config": {"triage_receipts": str(root)},
            }
            out = issue_triage_classifier.classify_triage_issue(req)
            self.assertEqual(out["status"], "classified", out)
            self.assertEqual(out["reason"], "decision_reused")
            self.assertEqual(out["action"], "needs_feedback")
            self.assertEqual(out["decision_digest"], digest)
            self.assertEqual(out["classification"], classification)
            run_omp.assert_not_called()

    @mock.patch("lokay.steps.issue_triage_classifier.run_omp")
    def test_frozen_ready_conflict_classifies_without_omp(self, run_omp) -> None:
        selected = {
            "repo": "owner/repo",
            "number": 31,
            "candidate_class": "frozen_ready_conflict",
            "labels": ["frozen", "ai:ready"],
        }
        req = {
            "input": {
                "selected": selected,
                "repo": "owner/repo",
                "number": 31,
                "candidate_class": "frozen_ready_conflict",
                "current_labels": ["frozen", "ai:ready"],
                "conduction": {
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
                    "read_triage_issue_state": {
                        "ok": True,
                        "status": "issue_read",
                        "issue": selected,
                        "selected": selected,
                        "repo": "owner/repo",
                        "number": 31,
                    },
                    "read_triage_comments": {
                        "ok": True,
                        "status": "comments_read",
                        "comments": [],
                        "selected": selected,
                    },
                    "build_triage_context": {
                        "ok": True,
                        "status": "context_packet",
                        "reason": "frozen_ready_reconciliation",
                        "selected": selected,
                        "packet": {"context": []},
                        "pre_snapshot": {},
                        "post_snapshot": {},
                    },
                    "read_triage_labels": {
                        "ok": True,
                        "status": "triage_labels_read",
                        "labels": ["frozen", "ai:ready"],
                        "selected": selected,
                    },
                },
            },
            "config": {"dry_run": False},
        }
        out = issue_triage_classifier.classify_triage_issue(req)
        self.assertEqual(out["status"], "classified", out)
        self.assertEqual(out["reason"], "frozen_ready_reconciliation")
        self.assertEqual(out["action"], "remove_ready")
        self.assertEqual(out["classification"]["classification"], "ready")
        self.assertRegex(str(out["decision_digest"]), r"^[0-9a-f]{64}$")
        run_omp.assert_not_called()



if __name__ == "__main__":
    unittest.main()
