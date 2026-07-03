from __future__ import annotations

import os
import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Final


ROOT: Final = Path(__file__).resolve().parents[1]
DISPATCHER: Final = ROOT / "scripts" / "repo_issue_to_pr_dispatch.sh"


class DispatcherFailOpenTests(unittest.TestCase):
    def test_continues_to_later_board_when_one_board_list_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_path:
            # Given: two clean registered repos where the first board cannot be listed.
            root = Path(temporary_path)
            bad_clone = root / "bad-clone"
            good_clone = root / "good-clone"
            self._make_repo(bad_clone)
            self._make_repo(good_clone)
            calls_file = root / "calls.log"
            log_file = root / "dispatcher.log"
            repos_file = root / "repos.txt"
            board_good_json = root / "board-good.json"
            worktree_root = root / "worktrees"
            lock_dir = root / "dispatch.lock"
            repos_file.write_text(
                "\n".join(
                    (
                        f"owner/bad|board-bad|{bad_clone}|10",
                        f"owner/good|board-good|{good_clone}|10",
                        "",
                    )
                )
            )
            board_good_json.write_text(
                """
[
  {
    "id": "task-good",
    "title": "[fix-pr] owner/good#123: healthy lower board",
    "status": "ready",
    "branch": "ai/fix/good",
    "body": "bug"
  }
]
""".strip()
            )

            # When: the dispatcher runs in dry-run mode against fake CLIs.
            result = self._run_dispatcher(
                calls_file=calls_file,
                log_file=log_file,
                repos_file=repos_file,
                board_good_json=board_good_json,
                worktree_root=worktree_root,
                lock_dir=lock_dir,
            )

            # Then: it records the bad board failure, still evaluates board-good,
            # and reaches the final summary instead of aborting under set -e.
            combined_output = result.stdout + result.stderr + self._read_text(log_file)
            calls = self._read_text(calls_file)
            self.assertEqual(1, result.returncode, combined_output + calls)
            self.assertIn("KANBAN_LIST_FAILED board=board-bad", combined_output)
            self.assertIn("HERMES\tkanban --board board-good list", calls)
            self.assertIn(
                "DECISION board=board-good task=task-good action=run-claude",
                combined_output,
            )
            self.assertIn("DONE mode=dry-run", combined_output)
            self.assertIn("failures=1", combined_output)

    def test_considers_lower_ready_task_when_top_task_is_manual_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_path:
            # Given: a high-scoring manual block sits above a healthy ready task.
            root = Path(temporary_path)
            good_clone = root / "good-clone"
            self._make_repo(good_clone)
            calls_file = root / "calls.log"
            log_file = root / "dispatcher.log"
            repos_file = root / "repos.txt"
            board_good_json = root / "board-good.json"
            worktree_root = root / "worktrees"
            lock_dir = root / "dispatch.lock"
            repos_file.write_text(f"owner/good|board-good|{good_clone}|10\n")
            board_good_json.write_text(
                json.dumps(
                    [
                        {
                            "id": "task-manual",
                            "title": (
                                "[fix-pr] owner/good#999: urgent critical "
                                "security bug manual"
                            ),
                            "status": "blocked",
                            "branch": "ai/fix/manual",
                            "body": "critical security bug",
                        },
                        {
                            "id": "task-healthy",
                            "title": "[fix-pr] owner/good#123: healthy lower work",
                            "status": "ready",
                            "branch": "ai/fix/healthy",
                            "body": "bug",
                        },
                    ]
                )
            )

            # When: the dispatcher can start only one worker for the board.
            result = self._run_dispatcher(
                calls_file=calls_file,
                log_file=log_file,
                repos_file=repos_file,
                board_good_json=board_good_json,
                worktree_root=worktree_root,
                lock_dir=lock_dir,
            )

            # Then: the manual skip is preserved and the lower ready task still runs.
            combined_output = result.stdout + result.stderr + self._read_text(log_file)
            calls = self._read_text(calls_file)
            self.assertEqual(0, result.returncode, combined_output + calls)
            self.assertIn(
                "DECISION board=board-good task=task-manual action=skip "
                "reason=manual-blocked-fix-task",
                combined_output,
            )
            self.assertIn(
                "DECISION board=board-good task=task-healthy action=run-claude",
                combined_output,
            )
            self.assertIn("DONE mode=dry-run", combined_output)

    def _run_dispatcher(
        self,
        *,
        calls_file: Path,
        log_file: Path,
        repos_file: Path,
        board_good_json: Path,
        worktree_root: Path,
        lock_dir: Path,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "BASH_FUNC_gh%%": _fake_gh_function(),
                "BASH_FUNC_hermes%%": _fake_hermes_function(),
                "BOARD_GOOD_JSON": str(board_good_json),
                "CALLS_FILE": str(calls_file),
                "HERMES_ISSUE_TO_PR_LOCK_DIR": str(lock_dir),
                "HERMES_ISSUE_TO_PR_LOG": str(log_file),
                "HERMES_REPO_AGENT_SOURCE": "kanban",
                "HERMES_REPO_AGENT_REPOS_FILE": str(repos_file),
                "HERMES_WORKTREE_ROOT": str(worktree_root),
            }
        )
        return subprocess.run(
            ["bash", str(DISPATCHER), "--dry-run", "--max", "1"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def _make_repo(self, clone: Path) -> None:
        root = clone.parent
        self._run_git(root, "init", str(clone))
        self._run_git(clone, "config", "user.email", "repo-agent@example.invalid")
        self._run_git(clone, "config", "user.name", "Repo Agent Test")
        (clone / "README.md").write_text("base\n")
        self._run_git(clone, "add", "README.md")
        self._run_git(clone, "commit", "-m", "base")

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

    def _read_text(self, path: Path) -> str:
        if path.exists():
            return path.read_text()
        return ""


def _fake_hermes_function() -> str:
    return r'''() {
  local original_args="$*"
  printf 'HERMES\t%s\n' "$*" >>"${CALLS_FILE:?}"
  local board=""
  local command=""
  if [[ "${1:-}" == "kanban" ]]; then
    shift
  fi
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --board)
        shift
        board="${1:-}"
        ;;
      list|show|block|complete|create|comment|reassign|unblock)
        command="$1"
        ;;
    esac
    shift || true
  done
  if [[ "$command" == "list" && "$board" == "board-bad" ]]; then
    printf 'fake board-bad list failure\n' >&2
    return 42
  fi
  if [[ "$command" == "list" && "$board" == "board-good" ]]; then
    cat "${BOARD_GOOD_JSON:?}"
    return 0
  fi
  if [[ "$command" == "show" && "$original_args" == *"task-manual"* ]]; then
    printf 'worktree-dirty-after-claude\n'
    return 0
  fi
  return 0
}'''


def _fake_gh_function() -> str:
    return r'''() {
  printf 'GH\t%s\n' "$*" >>"${CALLS_FILE:?}"
  if [[ "${1:-}" == "issue" && "${2:-}" == "view" ]]; then
    printf 'OPEN\n'
    return 0
  fi
  if [[ "${1:-}" == "pr" && "${2:-}" == "view" ]]; then
    printf 'OPEN\n'
    return 0
  fi
  if [[ "${1:-}" == "pr" && "${2:-}" == "list" ]]; then
    printf '[]\n'
    return 0
  fi
  return 0
}'''


if __name__ == "__main__":
    unittest.main()
