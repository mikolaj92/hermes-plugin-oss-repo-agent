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
            self.assertEqual(out["stdout_sha256"], __import__("hashlib").sha256(json.dumps(value).encode()).hexdigest())

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


if __name__ == "__main__":
    unittest.main()
