"""Focused durability tests for local receipt publication."""

from __future__ import annotations

from types import SimpleNamespace

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from repo_agent.steps import cleanup, issue_to_pr, triage


def request(data: dict) -> dict:
    return {
        "input": data,
        "config": {},
        "conduction": {"decide_triage_action": {"ok": True, "status": "decided", "action": "merge", "reason": "ready"}},
    }


PROVENANCE = {
    "source": "github_pr_readback",
    "state": "MERGED",
    "repo": "owner/repo",
    "number": 7,
    "head_oid": "head-7",
    "head_ref": "ai/fix/7",
    "merge_oid": "merge-7",
    "merged_at": "2026-01-01T00:00:00Z",
}
MERGE_VIEW = {
    "state": "MERGED",
    "mergedAt": PROVENANCE["merged_at"],
    "headRefOid": PROVENANCE["head_oid"],
    "headRefName": PROVENANCE["head_ref"],
    "mergeCommit": {"oid": PROVENANCE["merge_oid"]},
}


class ReceiptDurabilityTests(unittest.TestCase):
    def _dispatch(self, path: Path, payload: dict) -> dict:
        return issue_to_pr.write_dispatch_receipt(
            request({"receipt_path": str(path), "payload": payload, "dry_run": False})
        )

    def _merge(self, path: Path, payload: dict) -> dict:
        with mock.patch(
            "repo_agent.steps.triage.run_cmd",
            return_value=SimpleNamespace(stdout=json.dumps(MERGE_VIEW), stderr="", returncode=0),
        ):
            return triage.write_merge_receipt(
                request(
                    {
                        "receipt_path": str(path),
                        "payload": payload,
                        "verified_provenance": PROVENANCE,
                        "dry_run": False,
                    }
                )
            )

    def test_directory_fsync_failure_fails_closed_and_cleans_temp_for_both_writers(self) -> None:
        writers = (
            ("dispatch", self._dispatch, {"phase": "DISPATCHED", "issue": 1}, "repo_agent.steps.issue_to_pr.os.fsync"),
            ("merge", self._merge, {"repo": "owner/repo", "pr": 7, "phase": "MERGED"}, "repo_agent.steps.triage.os.fsync"),
        )
        for name, writer, payload, fsync_path in writers:
            with self.subTest(writer=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "receipt.json"
                with mock.patch(fsync_path, side_effect=[None, OSError("directory fsync failed")]):
                    result = writer(path, payload)
                self.assertFalse(result["ok"])
                self.assertEqual(result["reason"], "receipt_write_failed")
                self.assertFalse(path.exists())
                self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_readback_mismatch_and_json_corruption_fail_closed_and_clean_temp(self) -> None:
        writers = (
            ("dispatch", self._dispatch, issue_to_pr, {"phase": "DISPATCHED", "issue": 1}),
            ("merge", self._merge, triage, {"repo": "owner/repo", "pr": 7, "phase": "MERGED"}),
        )
        for name, writer, module, payload in writers:
            for case, readback in (("mismatch", "{}"), ("corruption", "not-json")):
                with self.subTest(writer=name, case=case), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "receipt.json"
                    with mock.patch(f"{module.__name__}.Path.read_text", return_value=readback):
                        result = writer(path, payload)
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["reason"], "receipt_write_failed")
                    self.assertFalse(path.exists())
                    self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_existing_identical_receipt_is_idempotent_and_conflict_does_not_clobber(self) -> None:
        writers = (
            ("dispatch", self._dispatch, {"phase": "DISPATCHED", "issue": 1}),
            ("merge", self._merge, {"repo": "owner/repo", "pr": 7, "phase": "MERGED"}),
        )
        for name, writer, payload in writers:
            with self.subTest(writer=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "receipt.json"
                first = writer(path, payload)
                self.assertEqual(first["status"], "written")
                original = path.read_bytes()
                same = writer(path, payload)
                self.assertEqual(same["status"], "exists")
                self.assertFalse(same["mutated"])
                different = writer(path, dict(payload, phase="CONFLICT"))
                self.assertFalse(different["ok"])
                self.assertEqual(different["reason"], "receipt_conflict")
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_dry_run_remains_planned_without_creating_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dispatch_path = Path(tmp) / "dispatch.json"
            dispatch = issue_to_pr.write_dispatch_receipt(
                request({"receipt_path": str(dispatch_path), "payload": {"phase": "DISPATCHED"}, "dry_run": True})
            )
            self.assertEqual(dispatch["status"], "planned")
            self.assertFalse(dispatch_path.exists())

            merge_path = Path(tmp) / "merge.json"
            merge = triage.write_merge_receipt(
                request({"receipt_path": str(merge_path), "payload": {"phase": "MERGED"}, "dry_run": True})
            )
            self.assertEqual(merge["status"], "planned")
            self.assertFalse(merge_path.exists())

    def test_supplied_identifiers_survive_receipt_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dispatch_path = Path(tmp) / "dispatch.json"
            out = issue_to_pr.write_dispatch_receipt(request({
                "receipt_path": str(dispatch_path),
                "payload": {"phase": "DISPATCHED", "run_id": "payload-run", "path_id": "payload-path", "process_id": "payload-process", "candidate": "payload-candidate"},
                "run_id": "input-run", "path_id": "input-path", "process_id": "input-process", "candidate": "input-candidate", "dry_run": False,
            }))
            self.assertEqual(out["status"], "written")
            payload = json.loads(dispatch_path.read_text())
            self.assertEqual(payload["run_id"], "input-run")
            self.assertEqual(payload["path_id"], "input-path")
            self.assertEqual(payload["process_id"], "input-process")
            self.assertEqual(payload["candidate"], "input-candidate")
    def _cleanup(self, path: Path, *, process_id: str = "cleanup-process", **overrides: object) -> dict:
        identity = {
            "task_id": "task-7",
            "repo": "owner/repo",
            "issue": 7,
            "receipt_id": str(path),
            "branch": "ai/fix/7-cleanup",
            "clone_path": "/tmp/clone-owner-repo",
            "worktree_path": "/tmp/worktree-owner-repo",
        }
        evidence = {
            "parse_issue_from_branch": {"ok": True, "status": "parsed", "issue": 7, "branch": identity["branch"], "task_id": identity["task_id"], "repo": identity["repo"]},
            "check_issue_closed": {"ok": True, "status": "checked", "closed": True, "repo": identity["repo"], "issue": 7},
            "check_no_open_pr": {"ok": True, "status": "checked", "safe_to_cleanup": True, "open_count": 0, "branch": identity["branch"]},
            "remove_worktree": {"ok": True, "status": "already_absent", "mutated": False, "clone_path": identity["clone_path"], "worktree_path": identity["worktree_path"], "branch": identity["branch"]},
            "delete_local_fix_branch": {"ok": True, "status": "already_absent", "mutated": False, "clone_path": identity["clone_path"], "branch": identity["branch"]},
            "release_active_issue_claim": {"ok": True, "status": "already_absent", "mutated": False, "repo": identity["repo"], "issue": 7},
        }
        identity.update(overrides)
        return cleanup.write_cleanup_receipt({
            "input": {
                **identity,
                "receipt_path": str(path),
                "dry_run": False,
                "conduction": evidence,
            },
            "config": {},
            "run_id": "cleanup-run",
            "path_id": "cleanup",
            "process_id": process_id,
        })

    def test_generic_ok_blobs_and_missing_identity_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cleanup.json"
            generic = {name: {"ok": True, "status": "noop", "mutated": False} for name in ("parse_issue_from_branch", "check_issue_closed", "check_no_open_pr", "remove_worktree", "delete_local_fix_branch", "release_active_issue_claim")}
            result = cleanup.write_cleanup_receipt({"input": {"receipt_path": str(path), "dry_run": False, "conduction": generic}, "config": {}})
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "cleanup_identity_missing")
            missing = self._cleanup(path, task_id="")
            self.assertFalse(missing["ok"])
            self.assertEqual(missing["reason"], "cleanup_identity_missing")

    def test_symlink_and_hardlink_receipts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.json"
            target.write_text("{}", encoding="utf-8")
            symlink = Path(tmp) / "symlink.json"
            symlink.symlink_to(target)
            self.assertEqual(self._cleanup(symlink)["reason"], "receipt_conflict")
            hardlink = Path(tmp) / "hardlink.json"
            target.unlink()
            first = self._cleanup(target)
            self.assertEqual(first["status"], "written")
            os.link(target, hardlink)
            self.assertEqual(self._cleanup(target)["reason"], "receipt_conflict")

    def test_cleanup_receipt_is_durable_and_preserves_request_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cleanup.json"
            result = self._cleanup(path)
            self.assertEqual(result["status"], "written")
            payload = json.loads(path.read_text())
            self.assertEqual(payload["run_id"], "cleanup-run")
            self.assertEqual(payload["path_id"], "cleanup")
            self.assertEqual(payload["process_id"], "cleanup-process")
            same = self._cleanup(path)
            self.assertEqual(same["status"], "exists")
            self.assertFalse(same["mutated"])
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_cleanup_receipt_directory_fsync_failure_unpublishes_and_allows_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cleanup.json"
            with mock.patch("repo_agent.steps.cleanup.os.fsync", side_effect=[None, OSError("directory fsync failed"), None]):
                result = self._cleanup(path)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "receipt_write_failed")
            self.assertFalse(path.exists())
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])

            retry = self._cleanup(path)
            self.assertEqual(retry["status"], "written")
            self.assertTrue(path.is_file())
    def test_cleanup_receipt_failed_rollback_is_never_accepted_without_fsync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cleanup.json"
            real_unlink = os.unlink

            def fail_published_unlink(target: Path | str) -> None:
                if Path(target) == path:
                    raise OSError("rollback unlink failed")
                real_unlink(target)

            with (
                mock.patch("repo_agent.steps.cleanup.os.fsync", side_effect=[None, OSError("directory fsync failed"), OSError("rollback fsync failed")]),
                mock.patch("repo_agent.steps.cleanup.os.unlink", side_effect=fail_published_unlink),
            ):
                result = self._cleanup(path)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "receipt_write_failed")
            self.assertTrue(path.is_file())

            with mock.patch("repo_agent.steps.cleanup.os.fsync", side_effect=OSError("durability still unconfirmed")):
                retry = self._cleanup(path)
            self.assertFalse(retry["ok"])
            self.assertEqual(retry["reason"], "receipt_durability_unconfirmed")

    def test_cleanup_receipt_fsync_before_publication_fails_closed_and_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cleanup.json"
            with mock.patch("repo_agent.steps.cleanup.os.fsync", side_effect=OSError("fsync failed")):
                result = self._cleanup(path)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "receipt_write_failed")
            self.assertFalse(path.exists())
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_cleanup_receipt_publish_race_does_not_clobber(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cleanup.json"
            competitor_path = Path(tmp) / "competitor.json"
            competitor = self._cleanup(competitor_path, process_id="competitor")
            self.assertEqual(competitor["status"], "written")
            competitor_payload = competitor_path.read_text()

            def publish_competitor(_source: Path, destination: Path) -> None:
                destination.write_text(competitor_payload)
                raise FileExistsError

            with mock.patch("repo_agent.steps.cleanup.os.link", side_effect=publish_competitor):
                result = self._cleanup(path, process_id="loser")
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "receipt_conflict")
            self.assertEqual(json.loads(path.read_text())["process_id"], "competitor")
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
