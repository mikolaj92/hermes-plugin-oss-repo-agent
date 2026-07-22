"""Focused durability tests for local receipt publication."""

from __future__ import annotations

from types import SimpleNamespace

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from repo_agent.steps import issue_to_pr, triage


def request(data: dict) -> dict:
    return {
        "input": data,
        "config": {},
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


if __name__ == "__main__":
    unittest.main()
