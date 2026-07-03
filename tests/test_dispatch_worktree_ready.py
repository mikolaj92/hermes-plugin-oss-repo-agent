from __future__ import annotations

import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DISPATCHER = ROOT / "scripts" / "repo_issue_to_pr_dispatch.sh"
WORKTREE_BRANCH = "ai/fix/existing"
DISPATCHER_HELPERS = (
    "worktree_for_branch",
    "branch_exists",
    "ensure_existing_worktree_ready",
    "ensure_worktree_ready",
)
FUNCTION_DECLARATION = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\(\) \{$")


class DispatchWorktreeReadyTests(unittest.TestCase):
    def test_adopts_existing_clean_worktree_when_requested_task_path_is_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_path:
            # Given: a branch already checked out in an older clean worktree.
            root = Path(temporary_path)
            clone = root / "clone"
            old_worktree = root / "old clean worktree"
            new_worktree = root / "new-task-worktree"
            log_file = root / "dispatcher.log"
            harness = self._write_harness(root)
            self._make_repo_with_branch_worktree(clone, old_worktree)

            # When: the dispatcher prepares a missing per-task worktree for that branch.
            result = self._run_harness(harness, log_file, clone, new_worktree)

            # Then: it reuses the existing clean worktree instead of failing branch creation.
            log_text = self._log_text(log_file)
            self.assertEqual(
                0,
                result.returncode,
                result.stdout + result.stderr + log_text,
            )
            self.assertIn(f"READY_PATH={old_worktree.resolve()}", result.stdout)
            self.assertNotIn("worktree-create-failed", log_text)
            self.assertFalse(new_worktree.exists())

    def test_blocks_existing_dirty_worktree_without_destroying_dirty_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_path:
            # Given: the branch is checked out in an older dirty worktree.
            root = Path(temporary_path)
            clone = root / "clone"
            old_worktree = root / "old-dirty-worktree"
            new_worktree = root / "new-task-worktree"
            dirty_file = old_worktree / "dirty.txt"
            log_file = root / "dispatcher.log"
            harness = self._write_harness(root)
            self._make_repo_with_branch_worktree(clone, old_worktree)
            dirty_file.write_text("must survive\n")

            # When: the dispatcher prepares a missing per-task worktree for that branch.
            result = self._run_harness(harness, log_file, clone, new_worktree)

            # Then: it explicitly blocks on dirtiness and preserves user work.
            log_text = self._log_text(log_file)
            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("worktree-not-clean", log_text)
            self.assertEqual("must survive\n", dirty_file.read_text())
            self.assertFalse(new_worktree.exists())

    def test_creates_worktree_from_existing_local_branch_without_duplicate_branch_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_path:
            # Given: the fix branch exists locally but is not checked out anywhere.
            root = Path(temporary_path)
            clone = root / "clone"
            new_worktree = root / "new-task-worktree"
            log_file = root / "dispatcher.log"
            harness = self._write_harness(root)
            self._make_repo_with_branch(clone)

            # When: the dispatcher prepares a worktree for that existing branch.
            result = self._run_harness(harness, log_file, clone, new_worktree)

            # Then: it checks out the existing branch instead of trying to recreate it.
            log_text = self._log_text(log_file)
            self.assertEqual(
                0,
                result.returncode,
                result.stdout + result.stderr + log_text,
            )
            self.assertIn(f"READY_PATH={new_worktree}", result.stdout)
            self.assertNotIn("worktree-create-failed", log_text)
            inside = self._run_git(new_worktree, "rev-parse", "--is-inside-work-tree")
            self.assertEqual("true", inside.stdout.strip())
            current_branch = self._run_git(new_worktree, "branch", "--show-current")
            self.assertEqual(WORKTREE_BRANCH, current_branch.stdout.strip())

    def _make_repo_with_branch_worktree(self, clone: Path, worktree: Path) -> None:
        self._make_repo_with_branch(clone)
        self._run_git(clone, "worktree", "add", str(worktree), WORKTREE_BRANCH)

    def _make_repo_with_branch(self, clone: Path) -> None:
        root = clone.parent
        self._run_git(root, "init", str(clone))
        self._run_git(clone, "config", "user.email", "repo-agent@example.invalid")
        self._run_git(clone, "config", "user.name", "Repo Agent Test")
        (clone / "README.md").write_text("base\n")
        self._run_git(clone, "add", "README.md")
        self._run_git(clone, "commit", "-m", "base")
        self._run_git(clone, "branch", WORKTREE_BRANCH)

    def _run_git(
        self,
        working_directory: Path,
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        quoted_arguments = " ".join(shlex.quote(argument) for argument in arguments)
        command = f"GIT_MASTER=1 git {quoted_arguments}"
        return subprocess.run(
            ["bash", "-lc", command],
            cwd=working_directory,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def _run_harness(
        self,
        harness: Path,
        log_file: Path,
        clone: Path,
        worktree: Path,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "bash",
                str(harness),
                str(log_file),
                "ready",
                str(clone),
                str(worktree),
                WORKTREE_BRANCH,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def _write_harness(self, root: Path) -> Path:
        harness = root / "worktree_harness.sh"
        harness.write_text(
            "\n".join(
                (
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    "LOG_FILE=\"$1\"",
                    "shift",
                    "log() { printf '%s\\n' \"$1\" >>\"$LOG_FILE\"; }",
                    _dispatcher_function_block(),
                    "case \"$1\" in",
                    "  ready)",
                    "    shift",
                    "    ENSURE_WORKTREE_READY_PATH=\"\"",
                    "    if ensure_worktree_ready \"$1\" \"$2\" \"$3\"; then",
                    "      printf 'READY_PATH=%s\\n' \"$ENSURE_WORKTREE_READY_PATH\"",
                    "      exit 0",
                    "    fi",
                    "    exit 1",
                    "    ;;",
                    "  *)",
                    "    printf 'unknown command: %s\\n' \"$1\" >&2",
                    "    exit 2",
                    "    ;;",
                    "esac",
                    "",
                )
            )
        )
        harness.chmod(0o700)
        return harness

    def _log_text(self, log_file: Path) -> str:
        if log_file.exists():
            return log_file.read_text()
        return ""


def _dispatcher_function_block() -> str:
    source_lines = DISPATCHER.read_text().splitlines()
    blocks: list[str] = []
    for function_name in DISPATCHER_HELPERS:
        start = _find_function_start(source_lines, function_name)
        if start == -1:
            continue
        end = _find_function_end(source_lines, start)
        blocks.append("\n".join(source_lines[start:end]))
    if not blocks or not blocks[-1].startswith("ensure_worktree_ready()"):
        raise ValueError("dispatcher helper ensure_worktree_ready() was not found")
    return "\n\n".join(blocks)


def _find_function_start(source_lines: list[str], function_name: str) -> int:
    declaration = f"{function_name}() {{"
    for index, line in enumerate(source_lines):
        if line == declaration:
            return index
    return -1


def _find_function_end(source_lines: list[str], start: int) -> int:
    for index in range(start + 1, len(source_lines)):
        if FUNCTION_DECLARATION.match(source_lines[index]):
            return index
    return len(source_lines)


if __name__ == "__main__":
    unittest.main()
