from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from tests.dispatcher_worker_fixture import DispatcherWorkerFixture, pid_is_alive


class DispatcherWorkerBackgroundTest(unittest.TestCase):
    def test_run_opencode_returns_before_fake_claude_exits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_path:
            # Given: a clean clone and a fake Claude process that runs long enough
            # to make synchronous dispatch visible.
            fixture = DispatcherWorkerFixture(Path(temporary_path))
            fixture.write_harness()
            fixture.write_fake_commands()
            fixture.make_repo()

            # When: the dispatcher starts Claude for a fix task.
            started = time.monotonic()
            result = fixture.run_worker(
                fake_claude_sleep="2",
                fake_open_pr="0",
                task_id="task-background",
            )
            elapsed = time.monotonic() - started

            # Then: run_claude_for_fix has returned while the worker is still
            # alive under the per-board lock, proving the global script lock can
            # be released promptly.
            self.addCleanup(fixture.cleanup_worker)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertLess(
                elapsed,
                1.0,
                f"dispatcher waited {elapsed:.2f}s for fake Claude; output={result.stdout}{result.stderr}",
            )
            worker_pid = fixture.worker_pid()
            self.assertTrue(worker_pid, "worker pid was not written under board lock")
            self.assertTrue(
                pid_is_alive(worker_pid),
                f"worker pid {worker_pid} is not alive; log={fixture.combined_log_text()}",
            )

    def test_dirty_worktree_after_worker_blocks_instead_of_finalizing_existing_pr(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_path:
            # Given: fake Claude exits successfully but leaves the fix worktree dirty,
            # while an open PR already exists for the branch.
            fixture = DispatcherWorkerFixture(Path(temporary_path))
            fixture.write_harness()
            fixture.write_fake_commands()
            fixture.make_repo()

            # When: the worker finalizes the task.
            result = fixture.run_worker(
                fake_claude_touch_dirty="1",
                fake_open_pr="1",
                task_id="task-dirty",
            )
            self.addCleanup(fixture.cleanup_worker)
            fixture.wait_for_log("CLAUDE_FINALIZED")

            # Then: dirty worker output blocks the task for inspection instead of
            # completing it just because a PR exists.
            calls = fixture.calls_text()
            log_text = fixture.combined_log_text()
            self.assertEqual(0, result.returncode, result.stdout + result.stderr + log_text)
            self.assertNotIn("COMPLETE\t", calls)
            self.assertIn(" block ", f" {calls} ")
            self.assertIn("worktree-dirty-after-claude", calls + log_text)

    def test_run_opencode_defers_without_unsafe_claude_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_path:
            fixture = DispatcherWorkerFixture(Path(temporary_path))
            fixture.write_harness(unsafe_claude_enabled=False)
            fixture.write_fake_commands()
            fixture.make_repo()

            result = fixture.run_worker(
                fake_open_pr="0",
                task_id="fixture-unsafe-disabled",
            )

            calls = fixture.calls_text()
            log_text = fixture.combined_log_text()
            self.assertEqual(10, result.returncode, result.stdout + result.stderr + log_text)
            self.assertIn("CLAUDE_SKIPPED", log_text)
            self.assertIn("unsafe-claude-disabled", log_text)
            self.assertNotIn(" block ", f" {calls} ")
            self.assertIn("HERMES_ALLOW_UNSAFE_CLAUDE=1", calls + log_text)
            self.assertEqual("", fixture.worker_pid())
            self.assertFalse(fixture.worktree_root.exists())
            self.assertNotIn("CLAUDE_START", log_text)

if __name__ == "__main__":
    unittest.main()
