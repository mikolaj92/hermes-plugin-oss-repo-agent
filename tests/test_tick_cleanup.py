from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lokay import tick_cleanup


class TickCleanupTests(unittest.TestCase):
    def test_reconcile_defaults_to_trusted_claim_and_task_receipt_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = SimpleNamespace(
                worktree_root=str(root / "worktrees"),
                active_issue=str(root / "active"),
                task_receipts=str(root / "task-receipts"),
                merge_receipts=str(root / "merge"),
            )
            repo = SimpleNamespace(repo="owner/repo", clone_path=str(root / "clone"))
            cfg = SimpleNamespace(paths=paths, repos=(repo,))
            captured: dict = {}

            host = SimpleNamespace(
                run_id="cleanup-reconcile",
                path_id="cleanup_reconcile",
                run_status="completed",
                ticks=1,
                processes=(),
                completed=(),
                failed=(),
            )

            async def run_path(**kwargs):
                captured["kwargs"] = kwargs
                return host

            argv = [
                "--reconcile-no-target", "--branch", "ai/fix/8-test", "--issue", "8",
                "--pr-number", "9", "--task-id", "task-8", "--task-receipt-path", str(root / "task-receipts" / "task.json"),
                "--merge-receipt-path", str(root / "merge" / "merge.json"), "--receipt-path", str(root / "merge" / "cleanup-outcomes" / "cleanup.json"),
                "--base-sha", "1" * 40, "--head-oid", "2" * 40, "--merge-oid", "3" * 40, "--origin-main-sha", "4" * 40,
                "--authorize-remote-retention", "--dry-run",
            ]
            with mock.patch.object(tick_cleanup, "load_config", return_value=cfg), mock.patch.object(tick_cleanup, "ensure_fala_paths", return_value=(root / "fala.sqlite", None)), mock.patch("lokay.flows.cleanup.run_package_path_async", new=run_path), mock.patch.object(tick_cleanup, "print_path_result", return_value=0):
                self.assertEqual(tick_cleanup.main(argv), 0)

            inputs = captured["kwargs"]["effector_inputs"]
            config = captured["kwargs"]["effector_configs"]
            expected = {
                "validate_reconcile_identity", "read_local_receipts", "read_claim_process_evidence",
                "read_github_terminal_state", "read_remote_provenance", "read_reconcile_worktree_state",
                "decide_no_target_reconciliation", "update_task_receipt", "publish_reconcile_receipt",
                "verify_no_target_reconciliation",
            }
            self.assertEqual(set(inputs), expected)
            self.assertEqual(set(config), expected)
            self.assertEqual(inputs["validate_reconcile_identity"]["claim_path"], str(root / "active"))
            paths.active_issue = str(root / "active.json")
            with mock.patch.object(tick_cleanup, "load_config", return_value=cfg), mock.patch.object(tick_cleanup, "ensure_fala_paths", return_value=(root / "fala.sqlite", None)), mock.patch("lokay.flows.cleanup.run_package_path_async", new=run_path), mock.patch.object(tick_cleanup, "print_path_result", return_value=0):
                self.assertEqual(tick_cleanup.main(argv), 0)
            inputs = captured["kwargs"]["effector_inputs"]
            config = captured["kwargs"]["effector_configs"]
            self.assertEqual(set(inputs), expected)
            self.assertEqual(set(config), expected)
            self.assertEqual(inputs["validate_reconcile_identity"]["claim_path"], str(root / "active.json"))
            self.assertEqual(config["validate_reconcile_identity"]["task_receipt_root"], str(root / "task-receipts"))


if __name__ == "__main__":
    unittest.main()
