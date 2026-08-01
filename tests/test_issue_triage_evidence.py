from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lokay.steps import issue_triage_evidence as evidence


OID = "a" * 40


def proc(stdout: str = "", returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


class GitHubReadTests(unittest.TestCase):
    def test_issue_state_rejects_malformed_payload(self):
        with mock.patch.object(evidence, "run_cmd", return_value=proc("{\"number\": 4}")):
            result = evidence.read_triage_issue_state({"input": {"repo": "o/r", "number": 4}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "malformed_issue_payload")

    def test_comments_read_failure_is_retryable(self):
        with mock.patch.object(evidence, "run_cmd", side_effect=evidence.CommandError(["gh"], 1, "", "offline")):
            result = evidence.read_triage_comments({"input": {"repo": "o/r", "number": 4}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_class"], "retryable_read")
        self.assertFalse(result["mutated"])

    def test_comments_shape_accepts_gh_issue_view_ids(self):
        from lokay.steps import issue_triage_evidence as evidence

        comments = evidence._comments_shape(
            [
                {
                    "id": "IC_kwDOExample",
                    "url": "https://github.com/owner/repo/issues/4#issuecomment-99",
                    "author": {"login": "maintainer"},
                    "authorAssociation": "MEMBER",
                    "createdAt": "2026-07-29T09:00:00Z",
                    "body": "Need more detail",
                }
            ]
        )
        self.assertEqual(comments[0]["databaseId"], 99)
        self.assertEqual(comments[0]["id"], "IC_kwDOExample")

    def test_repository_state_pins_exact_oid(self):
        repository = {"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}}
        with mock.patch.object(evidence, "run_cmd", side_effect=[proc(json.dumps(repository)), proc(OID + "\n")]) as run:
            result = evidence.read_triage_repository_state({"input": {"repo": "o/r"}})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["default_branch_oid"], OID)
        self.assertEqual(run.call_args_list[1].args[0], ["gh", "api", "repos/o/r/git/ref/heads/main", "--jq", ".object.sha"])

    def test_canonical_issue_rejects_other_repository(self):
        payload = {"number": 9, "title": "x", "body": "x", "url": "https://github.com/other/r/issues/9", "state": "OPEN", "updatedAt": "2026-07-28T10:00:00Z", "labels": [], "repository": {"nameWithOwner": "other/r"}}
        with mock.patch.object(evidence, "run_cmd", return_value=proc(json.dumps(payload))):
            result = evidence.read_triage_canonical_issue({"input": {"repo": "o/r", "number": 4, "canonical_issue": 9}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "canonical_repository_mismatch")

    def test_canonical_issue_is_skipped_for_non_duplicate_classification(self):
        result = evidence.read_triage_canonical_issue({
            "input": {
                "repo": "o/r",
                "number": 4,
                "conduction": {
                    "classify_triage_issue": {
                        "ok": True,
                        "status": "classified",
                        "classification": {
                            "classification": "needs_feedback",
                            "canonical_issue": 0,
                        },
                    },
                },
            },
        })
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "noop")
        self.assertEqual(result["reason"], "canonical_issue_not_required")

    def test_canonical_issue_uses_nested_classifier_target(self):
        payload = {"number": 9, "title": "x", "body": "x", "url": "https://github.com/o/r/issues/9", "state": "OPEN", "updatedAt": "2026-07-28T10:00:00Z", "labels": [], "repository": {"nameWithOwner": "o/r"}}
        request = {
            "input": {
                "repo": "o/r",
                "number": 4,
                "conduction": {
                    "classify_triage_issue": {
                        "ok": True,
                        "status": "classified",
                        "classification": {
                            "classification": "duplicate",
                            "canonical_issue": 9,
                        },
                    },
                },
            },
        }
        with mock.patch.object(evidence, "run_cmd", return_value=proc(json.dumps(payload))) as run:
            result = evidence.read_triage_canonical_issue(request)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["canonical_issue"], 9)
        self.assertIn("9", run.call_args.args[0])

    def test_duplicate_without_canonical_target_fails_closed(self):
        result = evidence.read_triage_canonical_issue({
            "input": {
                "repo": "o/r",
                "number": 4,
                "conduction": {
                    "classify_triage_issue": {
                        "ok": True,
                        "status": "classified",
                        "classification": {
                            "classification": "duplicate",
                            "canonical_issue": 0,
                        },
                    },
                },
            },
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid_canonical_issue")



class PinnedContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.remote = self.root / "remote.git"
        self.repo.mkdir()
        self._git("init", "--bare", str(self.remote), cwd=self.root)
        self._git("init", cwd=self.repo)
        self._git("config", "user.email", "test@example.com", cwd=self.repo)
        self._git("config", "user.name", "Test", cwd=self.repo)
        (self.repo / "README.md").write_text("committed\n")
        self._git("add", "README.md", cwd=self.repo)
        self._git("commit", "-m", "initial", cwd=self.repo)
        self._git("branch", "-M", "main", cwd=self.repo)
        self._git("remote", "add", "origin", str(self.remote), cwd=self.repo)
        self._git("push", "-u", "origin", "main", cwd=self.repo)
        self.head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _git(*args, cwd):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)

    def request(self, **extra):
        return {"input": {"repo": "o/r", "clone_path": str(self.repo), "context_paths": ["README.md"], "head_oid": self.head, **extra}}

    def remote_state(self):
        return {"ok": True, "status": "repository_read", "repo": "o/r", "default_branch_oid": self.head, "default_branch": "main"}

    def test_build_uses_committed_git_show_and_excludes_dirty_bytes(self):
        (self.repo / "README.md").write_text("DIRTY SECRET\n")
        with mock.patch.object(evidence, "read_triage_repository_state", return_value=self.remote_state()):
            result = evidence.build_triage_context(self.request())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["packet"]["context"][0]["content"], "committed\n")
        self.assertNotIn("DIRTY SECRET", json.dumps(result))

    def test_build_accepts_production_triage_context_keys(self):
        request = self.request()
        request["input"].pop("context_paths")
        request["input"]["triage_context_paths"] = ["README.md"]
        request["input"]["triage_context_max_bytes"] = 1024
        with mock.patch.object(evidence, "read_triage_repository_state", return_value=self.remote_state()):
            result = evidence.build_triage_context(request)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["packet"]["context_paths"], ["README.md"])
        self.assertEqual(result["packet"]["context"][0]["content"], "committed\n")

    def test_invalid_path_traversal_and_cap_fail_closed(self):
        with mock.patch.object(evidence, "read_triage_repository_state", return_value=self.remote_state()):
            traversal = evidence.build_triage_context(self.request(context_paths=["../README.md"]))
            oversized = evidence.build_triage_context(self.request(context_max_bytes=2))
        self.assertFalse(traversal["ok"])
        self.assertEqual(traversal["reason"], "invalid_context_path")
        self.assertFalse(oversized["ok"])
        self.assertEqual(oversized["reason"], "context_oversized")

    def test_hash_manifest_and_exact_snapshot_mismatch(self):
        with mock.patch.object(evidence, "read_triage_repository_state", return_value=self.remote_state()):
            result = evidence.build_triage_context(self.request())
        self.assertTrue(result["ok"], result)
        item = result["packet"]["context"][0]
        self.assertEqual(item["sha256"], __import__("hashlib").sha256(b"committed\n").hexdigest())
        (self.repo / "untracked.txt").write_text("change")
        changed = evidence.verify_triage_repository_unchanged({"input": {"clone_path": str(self.repo), "pre_snapshot": result["pre_snapshot"]}})
        self.assertFalse(changed["ok"])
        self.assertEqual(changed["reason"], "repository_changed")

    def test_missing_upstream_is_fail_closed(self):
        self._git("config", "--unset", "branch.main.remote", cwd=self.repo)
        result = evidence.verify_triage_repository_unchanged({"input": {"clone_path": str(self.repo), "pre_snapshot": {}}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "missing_pre_snapshot")

    def test_reconcile_skips_local_snapshot(self):
        # Detached / missing clone must not fail reconcile; reuse only needs labels.
        selected = {
            "repo": "o/r",
            "number": 7,
            "candidate_class": "reconcile_decision",
            "clone_path": str(self.root / "missing-clone"),
        }
        request = {
            "input": {
                "selected": selected,
                "repo": "o/r",
                "number": 7,
                "clone_path": selected["clone_path"],
                "candidate_class": "reconcile_decision",
                "conduction": {
                    "select_triage_candidate": {
                        "ok": True,
                        "status": "selected",
                        "selected": selected,
                        "candidate_class": "reconcile_decision",
                        "repo": "o/r",
                        "number": 7,
                    },
                    "reserve_triage_run_budget": {
                        "ok": True,
                        "status": "exists",
                        "selected": selected,
                        "repo": "o/r",
                        "number": 7,
                        "issue": 7,
                    },
                    "read_triage_repository_state": {
                        "ok": True,
                        "status": "repository_read",
                        "repo": "o/r",
                        "selected": selected,
                    },
                },
            },
            "config": {},
        }
        built = evidence.build_triage_context(request)
        self.assertTrue(built["ok"], built)
        self.assertEqual(built["status"], "context_packet")
        self.assertEqual(built["reason"], "decision_reused")
        self.assertEqual(built["packet"]["context"], [])
        request["input"]["conduction"]["build_triage_context"] = built
        verified = evidence.verify_triage_repository_unchanged(request)
        self.assertTrue(verified["ok"], verified)
        self.assertEqual(verified["status"], "snapshot_unchanged")
        self.assertEqual(verified["reason"], "decision_reused")



if __name__ == "__main__":
    unittest.main()
