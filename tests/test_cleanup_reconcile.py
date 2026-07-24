from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from repo_agent.steps.cleanup_reconcile import reconcile_no_target_cleanup


SHA_BASE = "1" * 40
SHA_HEAD = "2" * 40
SHA_MERGE = "3" * 40
SHA_MAIN = "4" * 40


class CleanupReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clone = self.root / "clone"
        self.clone.mkdir()
        self.worktree = self.root / "worktrees" / "ai" / "fix" / "8-test"
        self.active = self.root / "active"
        self.active.mkdir()
        self.task_receipt = self.root / "receipts" / "task.json"
        self.task_receipt.parent.mkdir()
        self.task_receipt.write_text(json.dumps({
            "version": 1, "phase": "PR_OPEN", "outcome": "pr-open", "repo": "owner/repo",
            "issue": "8", "task_id": "task-8", "branch": "ai/fix/8-test",
            "clone_path": str(self.clone), "base_sha": SHA_BASE, "worker_pid": "99999999",
            "worker_pgid": "99999999", "next_retry_after": "",
        }), encoding="utf-8")
        os.chmod(self.task_receipt, 0o600)
        self.task_lock = Path(str(self.task_receipt) + ".lock")
        self.task_lock.touch()
        self.merge_receipt = self.root / "merge.json"
        self.merge_receipt.write_text(json.dumps({
            "phase": "ISSUE_CLOSED_CONFIRMED", "repo": "owner/repo", "issue": 8, "pr": 9,
            "branch": "ai/fix/8-test", "baseSha": SHA_BASE, "headSha": SHA_HEAD,
            "mergeSha": SHA_MERGE, "originMainSha": SHA_MERGE,
        }), encoding="utf-8")
        self.output = self.root / "cleanup.json"
        self.db = self.root / "state.sqlite"
        with sqlite3.connect(self.db) as connection:
            connection.execute("CREATE TABLE processes (run_id TEXT, id TEXT, status TEXT, lease_owner TEXT, lease_expires_at TEXT, input_json TEXT, output_json TEXT, metadata TEXT)")
        self.data = {
            "repo": "owner/repo", "issue": 8, "pr_number": 9, "task_id": "task-8",
            "branch": "ai/fix/8-test", "clone_path": str(self.clone), "worktree_path": str(self.worktree),
            "claim_path": str(self.active / "claim.json"), "task_receipt_path": str(self.task_receipt),
            "merge_receipt_path": str(self.merge_receipt), "receipt_path": str(self.output), "db_path": str(self.db),
            "base_sha": SHA_BASE, "head_oid": SHA_HEAD, "merge_oid": SHA_MERGE,
            "origin_main_sha": SHA_MAIN, "remote_retention_authorized": True, "dry_run": False,
        }
        self.config = {
            "repo": "owner/repo", "clone_path": str(self.clone),
            "worktree_root": str(self.root / "worktrees"), "claim_root": str(self.active),
            "db_path": str(self.db), "task_receipt_root": str(self.task_receipt.parent),
            "merge_receipt_root": str(self.merge_receipt.parent), "cleanup_receipt_root": str(self.output.parent),
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _command(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["gh", "issue", "view"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"number": 8, "state": "CLOSED"}), "")
        if argv[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"number": 9, "state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z", "mergeCommit": {"oid": SHA_MERGE}, "headRefName": "ai/fix/8-test", "headRefOid": SHA_HEAD, "baseRefName": "main"}), "")
        if argv[-3:] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(argv, 0, "git@github.com:owner/repo.git\n", "")
        if "ls-remote" in argv:
            return subprocess.CompletedProcess(argv, 0, f"{SHA_MAIN}\trefs/heads/main\n{SHA_HEAD}\trefs/heads/ai/fix/8-test\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def test_writes_exact_terminal_receipt_and_closes_task_receipt(self) -> None:
        with mock.patch("repo_agent.steps.cleanup_reconcile.run_cmd", side_effect=self._command), mock.patch("repo_agent.steps.cleanup_reconcile.worktree_list", return_value=""), mock.patch("repo_agent.steps.cleanup_reconcile._local_branch_absent", return_value=True):
            result = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
        self.assertEqual(result["status"], "reconciled", result)
        payload = json.loads(self.output.read_text())
        self.assertEqual(payload["phase"], "CLEANUP_TERMINAL")
        self.assertEqual(payload["outcome"], "NO_TARGET_RECONCILED")
        self.assertTrue(payload["postconditions"]["remote_branch_retained"])
        self.assertFalse(payload["postconditions"]["remote_branch_deleted"])
        for name in ("worktree_absent", "local_branch_absent", "active_claim_absent", "task_process_absent", "task_lease_absent", "worker_lock_absent"):
            self.assertTrue(payload["postconditions"][name])
        task = json.loads(self.task_receipt.read_text())
        self.assertEqual(task["phase"], "CLEANUP_TERMINAL")
        self.assertEqual(task["cleanup_receipt"], str(self.output.resolve()))

    def test_rerun_accepts_matching_terminal_receipts(self) -> None:
        patches = (
            mock.patch("repo_agent.steps.cleanup_reconcile.run_cmd", side_effect=self._command),
            mock.patch("repo_agent.steps.cleanup_reconcile.worktree_list", return_value=""),
            mock.patch("repo_agent.steps.cleanup_reconcile._local_branch_absent", return_value=True),
        )
        with patches[0], patches[1], patches[2]:
            first = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
            second = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
        self.assertEqual(first["status"], "reconciled", first)
        self.assertEqual(second["status"], "reconciled", second)

    def test_receipt_publication_failure_reports_partial_mutation(self) -> None:
        with mock.patch("repo_agent.steps.cleanup_reconcile.run_cmd", side_effect=self._command), mock.patch("repo_agent.steps.cleanup_reconcile.worktree_list", return_value=""), mock.patch("repo_agent.steps.cleanup_reconcile._local_branch_absent", return_value=True), mock.patch("repo_agent.steps.cleanup_reconcile._atomic_replace_json", side_effect=OSError("disk")):
            result = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
        self.assertEqual(result["failure_class"], "reconcile_then_retry", result)
        self.assertTrue(result["mutated"], result)
        self.assertTrue(self.output.exists())

    def test_rejects_unauthorized_remote_retention_without_writing(self) -> None:
        self.data["remote_retention_authorized"] = False
        result = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
        self.assertEqual(result["reason"], "remote_retention_not_authorized")
        self.assertFalse(self.output.exists())

    def test_rejects_active_lease_without_writing(self) -> None:
        with sqlite3.connect(self.db) as connection:
            connection.execute("INSERT INTO processes VALUES (?,?,?,?,?,?,?,?)", ("r", "p", "running", "worker", "2099", json.dumps({"task_id": "task-8"}), "{}", "{}"))
        with mock.patch("repo_agent.steps.cleanup_reconcile.run_cmd", side_effect=self._command), mock.patch("repo_agent.steps.cleanup_reconcile.worktree_list", return_value=""), mock.patch("repo_agent.steps.cleanup_reconcile._local_branch_absent", return_value=True):
            result = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
        self.assertEqual(result["reason"], "cleanup_reconciliation_failed")
        self.assertIn("active Fala", result["error"])
        self.assertFalse(self.output.exists())

    def test_completed_process_with_stale_lease_metadata_does_not_block(self) -> None:
        with sqlite3.connect(self.db) as connection:
            connection.execute("INSERT INTO processes VALUES (?,?,?,?,?,?,?,?)", ("r", "p", "succeeded", "old-worker", "2020-01-01T00:00:00Z", json.dumps({"task_id": "task-8"}), "{}", "{}"))
        with mock.patch("repo_agent.steps.cleanup_reconcile.run_cmd", side_effect=self._command), mock.patch("repo_agent.steps.cleanup_reconcile.worktree_list", return_value=""), mock.patch("repo_agent.steps.cleanup_reconcile._local_branch_absent", return_value=True):
            result = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
        self.assertEqual(result["status"], "reconciled", result)

    def test_rejects_matching_alternate_claim_without_writing(self) -> None:
        claim = self.active / "claim-owner_repo-8.json"
        claim.write_text(json.dumps({"version": 1, "repo": "owner/repo", "issue": 8, "board": "board", "assignee": "owner", "claimedAt": "2026-01-01T00:00:00Z"}), encoding="utf-8")
        with mock.patch("repo_agent.steps.cleanup_reconcile.run_cmd", side_effect=self._command), mock.patch("repo_agent.steps.cleanup_reconcile.worktree_list", return_value=""), mock.patch("repo_agent.steps.cleanup_reconcile._local_branch_absent", return_value=True):
            result = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
        self.assertIn("active claim", result["error"])
        self.assertFalse(self.output.exists())

    def test_ineligible_task_does_not_create_publication_state(self) -> None:
        self.task_receipt.write_text(json.dumps({**json.loads(self.task_receipt.read_text()), "phase": "RUNNING", "outcome": "running"}), encoding="utf-8")
        output = self.root / "new-receipts" / "cleanup.json"
        self.data["receipt_path"] = str(output)
        self.config["cleanup_receipt_root"] = str(output.parent)
        lock = self.task_lock
        with mock.patch("repo_agent.steps.cleanup_reconcile.run_cmd", side_effect=self._command), mock.patch("repo_agent.steps.cleanup_reconcile.worktree_list", return_value=""), mock.patch("repo_agent.steps.cleanup_reconcile._local_branch_absent", return_value=True):
            result = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
        self.assertIn("not eligible", result["error"])
        self.assertFalse(output.parent.exists())
        self.assertTrue(lock.exists())

    def test_missing_task_lock_fails_closed_without_writing(self) -> None:
        self.task_lock.unlink()
        result = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
        self.assertIn("No such file", result["error"])
        self.assertFalse(self.output.exists())

    def test_unknown_correlated_process_status_fails_closed(self) -> None:
        with sqlite3.connect(self.db) as connection:
            connection.execute("INSERT INTO processes VALUES (?,?,?,?,?,?,?,?)", ("r", "p", "mystery", "", "", json.dumps({"task_id": "task-8"}), "{}", "{}"))
        with mock.patch("repo_agent.steps.cleanup_reconcile.run_cmd", side_effect=self._command), mock.patch("repo_agent.steps.cleanup_reconcile.worktree_list", return_value=""), mock.patch("repo_agent.steps.cleanup_reconcile._local_branch_absent", return_value=True):
            result = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
        self.assertIn("unknown Fala", result["error"])
        self.assertFalse(self.output.exists())

    def test_dangling_worktree_path_fails_closed(self) -> None:
        self.worktree.parent.mkdir(parents=True)
        self.worktree.symlink_to(self.root / "missing")
        with mock.patch("repo_agent.steps.cleanup_reconcile.run_cmd", side_effect=self._command), mock.patch("repo_agent.steps.cleanup_reconcile.worktree_list", return_value=""), mock.patch("repo_agent.steps.cleanup_reconcile._local_branch_absent", return_value=True):
            result = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
        self.assertIn("worktree path escapes configured root", result["error"])
        self.assertFalse(self.output.exists())

    def test_missing_clone_returns_context_failure(self) -> None:
        self.clone.rmdir()
        result = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
        self.assertEqual(result["reason"], "cleanup_context_missing")
        self.assertFalse(self.output.exists())

    def test_single_claim_file_ignores_sibling_json(self) -> None:
        claim_file = self.root / "active.json"
        self.config["claim_root"] = str(claim_file)
        self.data["claim_path"] = str(claim_file)
        (self.root / "unrelated.json").write_text("{}", encoding="utf-8")
        self.data["dry_run"] = True
        with mock.patch("repo_agent.steps.cleanup_reconcile.run_cmd", side_effect=self._command), mock.patch("repo_agent.steps.cleanup_reconcile.worktree_list", return_value=""), mock.patch("repo_agent.steps.cleanup_reconcile._local_branch_absent", return_value=True):
            result = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
        self.assertEqual(result["status"], "planned", result)
        self.assertFalse(self.output.exists())

    def test_missing_database_does_not_create_it(self) -> None:
        self.db.unlink()
        result = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
        self.assertFalse(result["ok"])
        self.assertFalse(self.db.exists())

    def test_clone_origin_must_match_repository(self) -> None:
        def command(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if argv[-3:] == ["remote", "get-url", "origin"]:
                return subprocess.CompletedProcess(argv, 0, "git@github.com:other/repo.git\n", "")
            return self._command(argv, **kwargs)
        with mock.patch("repo_agent.steps.cleanup_reconcile.run_cmd", side_effect=command), mock.patch("repo_agent.steps.cleanup_reconcile.worktree_list", return_value=""), mock.patch("repo_agent.steps.cleanup_reconcile._local_branch_absent", return_value=True):
            result = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
        self.assertIn("origin repository mismatch", result["error"])
        self.assertFalse(self.output.exists())

    def test_extended_sqlite_lock_is_retryable(self) -> None:
        error = sqlite3.OperationalError("locked")
        error.sqlite_errorcode = sqlite3.SQLITE_LOCKED | (1 << 8)
        with mock.patch("repo_agent.steps.cleanup_reconcile.run_cmd", side_effect=self._command), mock.patch("repo_agent.steps.cleanup_reconcile.worktree_list", return_value=""), mock.patch("repo_agent.steps.cleanup_reconcile._local_branch_absent", return_value=True), mock.patch("repo_agent.steps.cleanup_reconcile.sqlite3.connect", side_effect=error):
            result = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
        self.assertEqual(result["reason"], "database_lock_active")
        self.assertTrue(result["retry_safe"])

    def test_rejects_nested_active_task_identity(self) -> None:
        with sqlite3.connect(self.db) as connection:
            connection.execute("INSERT INTO processes VALUES (?,?,?,?,?,?,?,?)", ("r", "p", "running", "worker", "2099", json.dumps({"input": {"task_id": "task-8"}}), "{}", "{}"))
        with mock.patch("repo_agent.steps.cleanup_reconcile.run_cmd", side_effect=self._command), mock.patch("repo_agent.steps.cleanup_reconcile.worktree_list", return_value=""), mock.patch("repo_agent.steps.cleanup_reconcile._local_branch_absent", return_value=True):
            result = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
        self.assertIn("active Fala", result["error"])
        self.assertFalse(self.output.exists())

    def test_malformed_process_evidence_fails_closed(self) -> None:
        with sqlite3.connect(self.db) as connection:
            connection.execute("INSERT INTO processes VALUES (?,?,?,?,?,?,?,?)", ("r", "p", "done", "", "", "{", "{}", "{}"))
        with mock.patch("repo_agent.steps.cleanup_reconcile.run_cmd", side_effect=self._command), mock.patch("repo_agent.steps.cleanup_reconcile.worktree_list", return_value=""), mock.patch("repo_agent.steps.cleanup_reconcile._local_branch_absent", return_value=True):
            result = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
        self.assertIn("malformed Fala", result["error"])
        self.assertFalse(self.output.exists())

    def test_rejects_active_task_lock_without_writing(self) -> None:
        lock = self.task_lock
        with lock.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
        self.assertEqual(result["reason"], "task_lock_active")
        self.assertFalse(self.output.exists())

    def test_dry_run_validates_then_plans_without_writing(self) -> None:
        self.data["dry_run"] = True
        with mock.patch("repo_agent.steps.cleanup_reconcile.run_cmd", side_effect=self._command), mock.patch("repo_agent.steps.cleanup_reconcile.worktree_list", return_value=""), mock.patch("repo_agent.steps.cleanup_reconcile._local_branch_absent", return_value=True):
            result = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
        self.assertEqual(result["status"], "planned", result)
        self.assertIn("postconditions", result)
        self.assertFalse(self.output.exists())

    def test_rejects_noncanonical_branch_before_commands(self) -> None:
        self.data["branch"] = "../../outside"
        result = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
        self.assertEqual(result["reason"], "cleanup_identity_invalid")
        self.assertFalse(self.output.exists())

    def test_rejects_task_receipt_outside_trusted_root(self) -> None:
        outside = self.root / "outside.json"
        outside.write_text(self.task_receipt.read_text(), encoding="utf-8")
        os.chmod(outside, 0o600)
        Path(str(outside) + ".lock").touch()
        self.data["task_receipt_path"] = str(outside)
        result = reconcile_no_target_cleanup({"input": self.data, "config": self.config})
        self.assertEqual(result["reason"], "cleanup_context_mismatch")
        self.assertEqual(result["field"], "task_receipt_path")
        self.assertFalse(self.output.exists())

if __name__ == "__main__":
    unittest.main()
