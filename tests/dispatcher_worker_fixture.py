from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DISPATCHER = ROOT / "scripts" / "repo_issue_to_pr_dispatch.sh"
FUNCTION_DECLARATION = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\(\) \{$")
DISPATCHER_HELPERS = (
    "ensure_clean_clone",
    "worktree_for_branch",
    "branch_exists",
    "ensure_existing_worktree_ready",
    "ensure_worktree_ready",
    "board_lock_dir",
    "run_claude_for_fix_worker",
    "run_claude_for_fix",
)


class DispatcherWorkerFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.clone = root / "clone"
        self.fake_bin = root / "bin"
        self.log_file = root / "dispatcher.log"
        self.calls_file = root / "calls.log"
        self.worktree_root = root / "worktrees"
        self.harness = root / "worker_harness.sh"
        self.board = "board-one"
        self.branch = "ai/fix/background-test"
        self.repo = "owner/repo"
        self.issue = "123"
        self.title = "[fix-pr] owner/repo#123: background worker"

    def write_harness(self, *, unsafe_claude_enabled: bool = True) -> None:
        self.harness.write_text(
            "\n".join(
                (
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    "LOG_FILE=\"$1\"",
                    "CALLS_FILE=\"$2\"",
                    "WORKTREE_ROOT=\"$3\"",
                    "shift 3",
                    "MAX_CLAUDE_AGENTS=9",
                    f"ALLOW_UNSAFE_CLAUDE={1 if unsafe_claude_enabled else 0}",
                    "CLAUDE_TIMEOUT_SECONDS=30",
                    "MAX_TASK_ATTEMPTS=3",
                    "RETRY_BACKOFF_SECONDS=1",
                    "OPENCODE_DEFERRED_RC=10",
                    "log() { printf '%s %s\\n' \"$(date -u '+%Y-%m-%dT%H:%M:%SZ')\" \"$1\" >>\"$LOG_FILE\"; }",
                    _dispatcher_function_block(),
                    "active_claude_agents() { printf '0\\n'; }",
                    "open_pr_for_branch() {",
                    "  if [[ \"${FAKE_OPEN_PR:-0}\" == 1 ]]; then",
                    "    printf '17\\thttps://example.test/pr/17\\n'",
                    "    return 0",
                    "  fi",
                    "  return 1",
                    "}",
                    "complete_task() { printf 'COMPLETE\\t%s\\t%s\\t%s\\n' \"$1\" \"$2\" \"$3\" >>\"$CALLS_FILE\"; }",
                    "retry_failure_note() { printf 'repo-agent retry attempt=1/3 next_retry_after=2099-01-01T00:00:00Z'; }",
                    "run_claude_for_fix \"$@\"",
                    "",
                )
            )
        )
        self.harness.chmod(0o700)

    def write_fake_commands(self) -> None:
        self.fake_bin.mkdir()
        self._write_executable(
            "claude",
            (
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "worktree=''\n"
                "while [[ $# -gt 0 ]]; do\n"
                "  case \"$1\" in\n"
                "    --add-dir) shift; worktree=\"$1\" ;;\n"
                "  esac\n"
                "  shift || true\n"
                "done\n"
                "if [[ \"${FAKE_CLAUDE_TOUCH_DIRTY:-0}\" == 1 && -n \"$worktree\" ]]; then\n"
                "  printf 'dirty\\n' >\"$worktree/dirty-after-claude.txt\"\n"
                "fi\n"
                "sleep \"${FAKE_CLAUDE_SLEEP:-0}\"\n"
                "exit \"${FAKE_CLAUDE_RC:-0}\"\n"
            ),
        )
        self._write_executable(
            "gh",
            (
                "#!/usr/bin/env bash\n"
                "printf 'GH\\t%s\\n' \"$*\" >>\"${CALLS_FILE:?}\"\n"
                "exit 0\n"
            ),
        )
        self._write_executable(
            "hermes",
            (
                "#!/usr/bin/env bash\n"
                "printf 'HERMES\\t%s\\n' \"$*\" >>\"${CALLS_FILE:?}\"\n"
                "exit 0\n"
            ),
        )

    def make_repo(self) -> None:
        self._run_git(self.root, "init", str(self.clone))
        self._run_git(self.clone, "config", "user.email", "repo-agent@example.invalid")
        self._run_git(self.clone, "config", "user.name", "Repo Agent Test")
        (self.clone / "README.md").write_text("base\n")
        self._run_git(self.clone, "add", "README.md")
        self._run_git(self.clone, "commit", "-m", "base")

    def run_worker(
        self,
        *,
        fake_claude_sleep: str = "0",
        fake_claude_touch_dirty: str = "0",
        fake_open_pr: str,
        task_id: str,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "CALLS_FILE": str(self.calls_file),
                "FAKE_CLAUDE_SLEEP": fake_claude_sleep,
                "FAKE_CLAUDE_TOUCH_DIRTY": fake_claude_touch_dirty,
                "FAKE_OPEN_PR": fake_open_pr,
                "PATH": f"{self.fake_bin}{os.pathsep}{env['PATH']}",
            }
        )
        return subprocess.run(
            [
                "bash",
                str(self.harness),
                str(self.log_file),
                str(self.calls_file),
                str(self.worktree_root),
                self.board,
                str(self.clone),
                task_id,
                self.title,
                self.repo,
                self.issue,
                self.branch,
                "",
            ],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def wait_for_log(self, needle: str, timeout_seconds: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if needle in self.combined_log_text():
                return
            time.sleep(0.05)
        raise AssertionError(
            f"timed out waiting for {needle!r}; log={self.combined_log_text()}"
        )

    def worker_pid(self) -> str:
        pid_file = self.worktree_root / self.board / ".agent.lock" / "pid"
        if not pid_file.exists():
            return ""
        return pid_file.read_text().strip()

    def cleanup_worker(self) -> None:
        worker_pid = self.worker_pid()
        if worker_pid and pid_is_alive(worker_pid):
            subprocess.run(["kill", worker_pid], check=False)
        lock = self.worktree_root / self.board / ".agent.lock"
        deadline = time.monotonic() + 5.0
        while lock.exists() and time.monotonic() < deadline:
            time.sleep(0.05)

    def calls_text(self) -> str:
        if self.calls_file.exists():
            return self.calls_file.read_text()
        return ""

    def log_text(self) -> str:
        if self.log_file.exists():
            return self.log_file.read_text()
        return ""

    def combined_log_text(self) -> str:
        logs = [self.log_text()]
        for path in sorted(self.root.glob("claude-*.log")):
            logs.append(path.read_text())
        return "\n".join(logs)

    def _write_executable(self, name: str, content: str) -> None:
        path = self.fake_bin / name
        path.write_text(content)
        path.chmod(0o700)

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


def _dispatcher_function_block() -> str:
    source_lines = DISPATCHER.read_text().splitlines()
    blocks: list[str] = []
    for function_name in DISPATCHER_HELPERS:
        start = _find_function_start(source_lines, function_name)
        if start == -1:
            raise ValueError(f"dispatcher helper {function_name}() was not found")
        end = _find_function_end(source_lines, start)
        blocks.append("\n".join(source_lines[start:end]))
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
        if source_lines[index] == "processed=0":
            return index
    return len(source_lines)


def pid_is_alive(pid: str) -> bool:
    if not pid:
        return False
    return subprocess.run(["kill", "-0", pid], check=False).returncode == 0
