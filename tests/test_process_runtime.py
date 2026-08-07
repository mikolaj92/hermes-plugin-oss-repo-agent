"""Focused contracts for the SQLite process runtime foundation."""

from __future__ import annotations

import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from lokay.process_runtime import (
    CursorError,
    ExternalInputRecord,
    FenceError,
    LeaseError,
    ProcessDisabledError,
    ProcessRuntime,
    ProcessRuntimeError,
    ReceiptConflictError,
    initialize_schema,
    subject_key,
)


class ProcessRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.generation_path = self.root / "generation"
        self.rt = ProcessRuntime.open(
            self.root / "state",
            dry_run=False,
            generation_path=self.generation_path,
            owner="test-owner",
        )
        self.generation = self.rt.write_generation("gen-1")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_schema_created_durably(self) -> None:
        db = self.root / "state" / "process-state.sqlite3"
        self.assertTrue(db.is_file())
        mode = db.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        initialize_schema(db)
        self.assertTrue(db.is_file())

    def test_receipt_idempotent_and_conflict(self) -> None:
        payload = {"decision": "ready", "issue": 7}
        first = self.rt.publish_receipt(
            process_id="issue_triage",
            receipt_kind="issue_decision",
            subject={"repo": "owner/repo", "number": 7},
            payload=payload,
            generation=self.generation,
            candidate_id="c" * 64,
            config_sha256="a" * 64,
            operation="publish_issue_decision",
        )
        self.assertEqual(first.status, "written")
        self.assertTrue(first.path.is_file())
        self.assertEqual(first.path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(stat.S_ISREG(first.path.stat().st_mode))
        self.assertEqual(first.path.stat().st_nlink, 1)

        second = self.rt.publish_receipt(
            process_id="issue_triage",
            receipt_kind="issue_decision",
            subject={"repo": "owner/repo", "number": 7},
            payload=payload,
            generation=self.generation,
            candidate_id="c" * 64,
            config_sha256="a" * 64,
            operation="publish_issue_decision",
        )
        self.assertEqual(second.status, "exists")
        self.assertEqual(second.digest, first.digest)
        self.assertEqual(second.path, first.path)
        original = first.path.read_bytes()

        with self.assertRaises(ReceiptConflictError):
            self.rt.publish_receipt(
                process_id="issue_triage",
                receipt_kind="issue_decision",
                subject={"repo": "owner/repo", "number": 7},
                payload={"decision": "close", "issue": 7},
                generation=self.generation,
                candidate_id="c" * 64,
                config_sha256="a" * 64,
                operation="publish_issue_decision",
            )
        self.assertEqual(first.path.read_bytes(), original)

        readback = self.rt.read_receipt(first.path)
        self.assertEqual(readback["content_digest"], first.digest)
        self.assertEqual(readback["payload"], payload)
        self.assertEqual(readback["verified_readback_state"], "verified")

    def test_receipt_republish_preserves_original_created_at(self) -> None:
        first = self.rt.publish_receipt(
            process_id="issue_triage",
            receipt_kind="issue_decision",
            subject={"repo": "owner/repo", "number": 7},
            payload={"decision": "ready", "issue": 7},
            generation=self.generation,
            candidate_id="c" * 64,
            config_sha256="a" * 64,
            operation="publish_issue_decision",
        )
        subject_text = subject_key({"repo": "owner/repo", "number": 7})
        connection = sqlite3.connect(str(self.rt.paths.db_path))
        before = connection.execute(
            "SELECT created_at FROM receipts WHERE process_id=? AND receipt_kind=? AND subject=?",
            ("issue_triage", "issue_decision", subject_text),
        ).fetchone()[0]
        connection.close()
        with mock.patch("lokay.process_runtime._utc_now", return_value="2099-01-01T00:00:00Z"):
            second = self.rt.publish_receipt(
                process_id="issue_triage",
                receipt_kind="issue_decision",
                subject={"repo": "owner/repo", "number": 7},
                payload={"decision": "ready", "issue": 7},
                generation=self.generation,
                candidate_id="c" * 64,
                config_sha256="a" * 64,
                operation="publish_issue_decision",
            )
        connection = sqlite3.connect(str(self.rt.paths.db_path))
        after = connection.execute(
            "SELECT created_at FROM receipts WHERE process_id=? AND receipt_kind=? AND subject=?",
            ("issue_triage", "issue_decision", subject_text),
        ).fetchone()[0]
        connection.close()
        self.assertEqual(first.status, "written")
        self.assertEqual(second.status, "exists")
        self.assertEqual(second.digest, first.digest)
        self.assertEqual(after, before)

    def test_cursor_advances_only_after_verified_receipt(self) -> None:
        with self.assertRaises(CursorError):
            self.rt.advance_cursor(
                process_id="repo_issue_poll",
                cursor_key="poll_repo",
                value="owner/repo",
                receipt_digest="0" * 64,
            )

        receipt = self.rt.publish_receipt(
            process_id="repo_issue_poll",
            receipt_kind="repo_poll",
            subject={"repo": "owner/repo"},
            payload={"heartbeat": True, "repo": "owner/repo"},
            generation=self.generation,
        )
        cursor = self.rt.advance_cursor(
            process_id="repo_issue_poll",
            cursor_key="poll_repo",
            value="owner/repo",
            receipt_digest=receipt.digest,
            receipt_path=receipt.path,
        )
        self.assertEqual(cursor.value, "owner/repo")
        self.assertEqual(cursor.receipt_digest, receipt.digest)
        loaded = self.rt.read_cursor("repo_issue_poll", "poll_repo")
        assert loaded is not None
        self.assertEqual(loaded.value, "owner/repo")

    def test_stale_lease_recovery_and_live_owner_rejection(self) -> None:
        with self.rt.process_lease(
            process_id="issue_ready",
            subject="owner/repo#1",
            lease_seconds=30,
            stale_owner_after_seconds=60,
            generation=self.generation,
        ) as lease:
            self.assertEqual(lease.owner, "test-owner")
            self.assertFalse(lease.reclaimed)
            visible = self.rt.read_lease(process_id="issue_ready", subject="owner/repo#1")
            assert visible is not None
            self.assertEqual(visible.owner, "test-owner")
            lease_dir = self.root / "state" / "leases" / "issue_ready"
            self.assertTrue(any(lease_dir.glob("*.json")))

        connection = sqlite3.connect(str(self.rt.paths.db_path))
        connection.execute(
            """
            INSERT INTO leases(
                process_id, subject, owner, owner_pid, acquired_at,
                expires_at, stale_after, generation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "issue_ready",
                "owner/repo#2",
                "dead-owner",
                2_000_000_001,
                "2020-01-01T00:00:00Z",
                100.0,
                200.0,
                self.generation,
            ),
        )
        connection.commit()
        connection.close()

        live = ProcessRuntime.open(
            self.root / "state",
            dry_run=False,
            generation_path=self.generation_path,
            owner="live-owner",
        )
        with mock.patch("lokay.process_runtime.time.time", return_value=1_000.0):
            with mock.patch("lokay.process_runtime._pid_alive", return_value=False):
                with live.process_lease(
                    process_id="issue_ready",
                    subject="owner/repo#2",
                    lease_seconds=30,
                    stale_owner_after_seconds=60,
                    generation=self.generation,
                ) as reclaimed:
                    self.assertTrue(reclaimed.reclaimed)
                    self.assertEqual(reclaimed.owner, "live-owner")
                    health = live.read_health("issue_ready")
                    assert health is not None
                    self.assertEqual(health.status, "stale_reclaimed")

        holder = ProcessRuntime.open(
            self.root / "state",
            dry_run=False,
            generation_path=self.generation_path,
            owner="holder",
        )
        contender = ProcessRuntime.open(
            self.root / "state",
            dry_run=False,
            generation_path=self.generation_path,
            owner="contender",
        )
        with holder.process_lease(
            process_id="cleanup",
            subject="owner/repo#9",
            lease_seconds=30,
            stale_owner_after_seconds=60,
            generation=self.generation,
        ):
            with self.assertRaises(LeaseError):
                with contender.process_lease(
                    process_id="cleanup",
                    subject="owner/repo#9",
                    lease_seconds=30,
                    stale_owner_after_seconds=60,
                    generation=self.generation,
                ):
                    pass

    def test_generation_fence_rejects_before_callback(self) -> None:
        called = {"n": 0}

        def boom(_lease: object) -> str:
            called["n"] += 1
            return "mutated"

        with self.assertRaises(FenceError):
            self.rt.run_fenced(
                process_id="pr_merge",
                enabled=True,
                generation="wrong-gen",
                subject="owner/repo#3",
                lease_seconds=30,
                stale_owner_after_seconds=60,
                lock_scope="merge/repo/number/head",
                callback=boom,
            )
        self.assertEqual(called["n"], 0)

        result = self.rt.run_fenced(
            process_id="pr_merge",
            enabled=True,
            generation=self.generation,
            subject="owner/repo#3",
            lease_seconds=30,
            stale_owner_after_seconds=60,
            lock_scope="merge/repo/number/head",
            callback=boom,
        )
        self.assertEqual(result, "mutated")
        self.assertEqual(called["n"], 1)

    def test_disabled_and_dry_run_non_mutation(self) -> None:
        called = {"n": 0}

        def boom(_lease: object) -> str:
            called["n"] += 1
            return "nope"

        with self.assertRaises(ProcessDisabledError):
            self.rt.run_fenced(
                process_id="issue_close",
                enabled=False,
                generation=self.generation,
                callback=boom,
            )
        self.assertEqual(called["n"], 0)
        health = self.rt.read_health("issue_close")
        assert health is not None
        self.assertEqual(health.status, "disabled")

        dry = ProcessRuntime.open(
            self.root / "state-dry",
            dry_run=True,
            generation_path=self.root / "generation-dry",
        )
        gen = dry.write_generation("dry-gen")
        planned = dry.publish_receipt(
            process_id="repo_issue_poll",
            receipt_kind="issue_snapshot",
            subject={"repo": "owner/repo"},
            payload={"issues": []},
            generation=gen,
        )
        self.assertEqual(planned.status, "planned")
        self.assertFalse(planned.path.exists())

        dry_called = {"n": 0}

        def mark(_lease: object) -> str:
            dry_called["n"] += 1
            return "planned-run"

        out = dry.run_fenced(
            process_id="repo_issue_poll",
            enabled=True,
            generation=gen,
            callback=mark,
        )
        self.assertEqual(out, "planned-run")
        self.assertEqual(dry_called["n"], 1)
        self.assertIsNone(dry.read_lease(process_id="repo_issue_poll"))
        self.assertIsNone(dry.read_health("repo_issue_poll"))
        connection = sqlite3.connect(str(dry.paths.db_path))
        count = connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        connection.close()
        self.assertEqual(count, 0)

    def test_external_input_dry_run_is_non_mutation(self) -> None:
        dry = ProcessRuntime.open(
            self.root / "state-external-dry",
            dry_run=True,
            generation_path=self.root / "generation-external-dry",
        )
        gen = dry.write_generation("dry-external")
        planned = dry.publish_external_input(
            process_id="cleanup_reconcile",
            input_kind="unresolved_cleanup_evidence",
            subject={"repo": "owner/repo", "number": 7},
            payload={
                "repo": "owner/repo",
                "issue": 7,
                "pr_number": 107,
                "task_id": "task-7",
                "branch": "ai/fix/7",
                "clone_path": "/tmp/7/clone",
                "worktree_path": "/tmp/7/worktree",
                "task_receipt_path": "/tmp/7/task.json",
                "claim_path": "/tmp/7/claim.json",
                "merge_receipt_path": "/tmp/7/merge.json",
                "receipt_path": "/tmp/7/cleanup.json",
                "db_path": "/tmp/7/fala.sqlite",
                "base_sha": "b" * 40,
                "head_oid": "c" * 40,
                "merge_oid": "d" * 40,
                "origin_main_sha": "e" * 40,
            },
            generation=gen,
            candidate_id="c" * 64,
            config_sha256="a" * 64,
        )
        self.assertIsInstance(planned, ExternalInputRecord)
        self.assertEqual(planned.status, "planned")
        self.assertFalse(planned.path.exists())
        self.assertEqual(planned.payload["verified_readback_state"], "not_applicable")
        connection = sqlite3.connect(str(dry.paths.db_path))
        count = connection.execute("SELECT COUNT(*) FROM external_inputs").fetchone()[0]
        connection.close()
        self.assertEqual(count, 0)
    def test_external_input_republish_preserves_original_created_at(self) -> None:
        subject = {"repo": "owner/repo", "number": 7}
        payload = {
            "repo": "owner/repo",
            "issue": 7,
            "pr_number": 107,
            "task_id": "task-7",
            "branch": "ai/fix/7",
            "clone_path": "/tmp/7/clone",
            "worktree_path": "/tmp/7/worktree",
            "task_receipt_path": "/tmp/7/task.json",
            "claim_path": "/tmp/7/claim.json",
            "merge_receipt_path": "/tmp/7/merge.json",
            "receipt_path": "/tmp/7/cleanup.json",
            "db_path": "/tmp/7/fala.sqlite",
            "base_sha": "b" * 40,
            "head_oid": "c" * 40,
            "merge_oid": "d" * 40,
            "origin_main_sha": "e" * 40,
            "remote_retention_authorized": True,
        }
        first = self.rt.publish_external_input(
            process_id="cleanup_reconcile",
            input_kind="unresolved_cleanup_evidence",
            subject=subject,
            payload=payload,
            generation=self.generation,
            candidate_id="c" * 64,
            config_sha256="a" * 64,
        )
        before = self.rt.list_external_inputs(
            process_id="cleanup_reconcile", input_kind="unresolved_cleanup_evidence"
        )[0]
        with mock.patch("lokay.process_runtime._utc_now", return_value="2099-01-01T00:00:00Z"):
            second = self.rt.publish_external_input(
                process_id="cleanup_reconcile",
                input_kind="unresolved_cleanup_evidence",
                subject=subject,
                payload=payload,
                generation=self.generation,
                candidate_id="c" * 64,
                config_sha256="a" * 64,
            )
        after = self.rt.list_external_inputs(
            process_id="cleanup_reconcile", input_kind="unresolved_cleanup_evidence"
        )[0]
        self.assertEqual(second.status, "exists")
        self.assertEqual(second.digest, first.digest)
        self.assertEqual(after["created_at"], before["created_at"])

    def test_retry_backoff_is_bounded(self) -> None:
        first = self.rt.decide_retry(
            process_id="issue_triage",
            subject="owner/repo#1",
            failure_class="retryable_read",
            attempt=1,
            max_attempts=5,
            backoff_seconds=(30, 60, 120, 300, 600),
            now=1_000.0,
        )
        self.assertTrue(first.should_retry)
        self.assertEqual(first.backoff_seconds, 30)
        self.assertEqual(first.next_attempt_at, 1_030.0)

        last = self.rt.decide_retry(
            process_id="issue_triage",
            subject="owner/repo#1",
            failure_class="retryable_read",
            attempt=5,
            max_attempts=5,
            backoff_seconds=(30, 60, 120, 300, 600),
            now=1_000.0,
        )
        self.assertFalse(last.should_retry)
        self.assertTrue(last.exhausted)
        self.assertEqual(last.backoff_seconds, 0)

        terminal = self.rt.decide_retry(
            process_id="issue_triage",
            subject="owner/repo#1",
            failure_class="terminal",
            attempt=1,
            max_attempts=5,
            now=1_000.0,
        )
        self.assertFalse(terminal.should_retry)
        self.assertTrue(terminal.exhausted)

    def test_subject_lock_is_os_visible(self) -> None:
        with self.rt.subject_lock(
            lock_scope="issue/repo/number", subject="owner/repo#8"
        ) as path:
            self.assertTrue(path.exists())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(stat.S_ISREG(path.stat().st_mode))


    def test_lease_heartbeat_renews_during_blocking_callback(self) -> None:
        renewed = threading.Event()
        real_renew = ProcessRuntime.renew_lease

        def wrap(runtime_self: ProcessRuntime, **kwargs: object) -> object:
            record = real_renew(runtime_self, **kwargs)  # type: ignore[arg-type]
            renewed.set()
            return record

        def callback(lease: object) -> str:
            self.assertTrue(renewed.wait(5.0), "expected concurrent lease renewal")
            visible = self.rt.read_lease(process_id="pr_merge", subject="owner/repo#3")
            assert visible is not None
            self.assertEqual(visible.owner, "test-owner")
            self.assertEqual(visible.generation, self.generation)
            self.assertGreaterEqual(visible.expires_at, getattr(lease, "expires_at"))
            return "done"

        with mock.patch.object(ProcessRuntime, "renew_lease", wrap):
            result = self.rt.run_fenced(
                process_id="pr_merge",
                enabled=True,
                generation=self.generation,
                subject="owner/repo#3",
                lease_seconds=30,
                stale_owner_after_seconds=60,
                lease_renew_seconds=1,
                lock_scope="merge/repo/number/head",
                callback=callback,
            )
        self.assertEqual(result, "done")
        self.assertTrue(renewed.is_set())
        # Heartbeat must be joined before lease release leaves no holder.
        self.assertIsNone(self.rt.read_lease(process_id="pr_merge", subject="owner/repo#3"))

    def test_lease_heartbeat_renewal_failure_is_fail_closed(self) -> None:
        entered = threading.Event()

        def fail_renew(runtime_self: ProcessRuntime, **kwargs: object) -> object:
            raise LeaseError("renewal denied")

        def callback(_lease: object) -> str:
            entered.set()
            time.sleep(1.5)
            return "should-not-return"

        with mock.patch.object(ProcessRuntime, "renew_lease", fail_renew):
            with self.assertRaises(LeaseError) as ctx:
                self.rt.run_fenced(
                    process_id="pr_merge",
                    enabled=True,
                    generation=self.generation,
                    subject="owner/repo#4",
                    lease_seconds=30,
                    stale_owner_after_seconds=60,
                    lease_renew_seconds=1,
                    lock_scope="merge/repo/number/head",
                    callback=callback,
                )
        self.assertIn("renewal denied", str(ctx.exception))
        self.assertTrue(entered.is_set())
        self.assertIsNone(self.rt.read_lease(process_id="pr_merge", subject="owner/repo#4"))

if __name__ == "__main__":
    unittest.main()
