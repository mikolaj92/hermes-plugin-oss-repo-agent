from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Final


ROOT: Final = Path(__file__).resolve().parents[1]
DISPATCHER: Final = ROOT / "scripts" / "repo_issue_to_pr_dispatch.sh"


class DispatcherRuntimeTests(unittest.TestCase):
    def test_shell_syntax_and_help_are_local(self) -> None:
        syntax = subprocess.run(
            ["bash", "-n", str(DISPATCHER)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, syntax.returncode, syntax.stderr)

        help_result = subprocess.run(
            ["bash", str(DISPATCHER), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, help_result.returncode, help_result.stderr)
        self.assertIn("Usage:", help_result.stdout)

    def test_github_issue_dispatch_uses_exact_issue_branch_worktree_and_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_path:
            root = Path(temporary_path)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            clone = self._make_repo(root / "clone")
            calls = root / "calls.log"
            log = root / "dispatch.log"
            repos = root / "repos.txt"
            worktrees = root / "worktrees"
            lock = root / "dispatch.lock"
            repos.write_text(f"mikolaj92/Fala|fala|{clone}|100\n", encoding="utf-8")
            self._write_fake_gh(fake_bin / "gh")
            self._write_fake_hermes(fake_bin / "hermes")
            self._write_fake_omp(fake_bin / "omp")

            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
                    "CALLS_FILE": str(calls),
                    "HERMES_REPO_AGENT_REPOS_FILE": str(repos),
                    "HERMES_REPO_AGENT_SOURCE": "github",
                    "HERMES_ISSUE_TO_PR_DRY_RUN": "0",
                    "HERMES_ISSUE_TO_PR_RUN_OPENCODE": "1",
                    "HERMES_ISSUE_TO_PR_LOG": str(log),
                    "HERMES_ISSUE_TO_PR_LOCK_DIR": str(lock),
                    "HERMES_WORKTREE_ROOT": str(worktrees),
                    "HERMES_REPO_AGENT_RECEIPT_DIR": str(root / "receipts"),
                    "HERMES_OMP_TIMEOUT_SECONDS": "5",
                }
            )
            result = subprocess.run(
                ["bash", str(DISPATCHER), "--live", "--run-opencode", "--max", "1"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)

            omp_argv = self._wait_for_omp(calls)
            self._wait_for_log(root / "omp-gh-issue-123.log", "OMP_FINALIZED")
            self.assertGreaterEqual(len(omp_argv), 10, calls.read_text())
            self.assertEqual("--cwd", omp_argv[0])
            worktree = Path(omp_argv[1])
            self.assertTrue(worktree.is_dir(), omp_argv)
            branch = subprocess.run(
                ["git", "-C", str(worktree), "branch", "--show-current"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            self.assertTrue(branch.startswith("ai/fix/123-"), branch)
            self.assertEqual("--model", omp_argv[2])
            self.assertEqual("omniroute/omp/default", omp_argv[3])
            self.assertEqual("--thinking", omp_argv[4])
            self.assertEqual("medium", omp_argv[5])
            self.assertEqual("--approval-mode", omp_argv[6])
            self.assertEqual("yolo", omp_argv[7])
            self.assertEqual("-p", omp_argv[8])
            prompt = omp_argv[9]
            self.assertIn("mikolaj92/Fala#123", prompt)
            self.assertIn("open a PR", prompt)

    def _make_repo(self, path: Path) -> Path:
        path.mkdir(parents=True)
        self._run_git(path.parent, "init", str(path))
        self._run_git(path, "config", "user.email", "repo-agent@example.invalid")
        self._run_git(path, "config", "user.name", "Repo Agent Test")
        (path / "README.md").write_text("base\n", encoding="utf-8")
        self._run_git(path, "add", "README.md")
        self._run_git(path, "commit", "-m", "base")
        return path

    def _run_git(self, cwd: Path, *args: str) -> None:
        command = "GIT_MASTER=1 git " + " ".join(shlex.quote(arg) for arg in args)
        subprocess.run(["bash", "-lc", command], cwd=cwd, check=True, capture_output=True, text=True)

    def _write_fake_gh(self, path: Path) -> None:
        path.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
printf 'GH\\t%s\\n' "$*" >>"${CALLS_FILE:?}"
case " $* " in
  *' issue list '*) printf '123\\tFix Fala issue\\thttps://github.test/mikolaj92/Fala/issues/123\\t\\n' ;;
  *' pr list '*) : ;;
  *) : ;;
esac
""",
            encoding="utf-8",
        )
        path.chmod(0o700)
    def _write_fake_hermes(self, path: Path) -> None:
        path.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
exit 0
""",
            encoding="utf-8",
        )
        path.chmod(0o700)

    def _write_fake_omp(self, path: Path) -> None:
        path.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
printf 'OMP_ARGV' >>"${CALLS_FILE:?}"
for arg in "$@"; do printf '\\t%s' "$arg" >>"${CALLS_FILE:?}"; done
printf '\\n' >>"${CALLS_FILE:?}"
""",
            encoding="utf-8",
        )
        path.chmod(0o700)

    def _wait_for_omp(self, calls: Path) -> list[str]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if calls.exists():
                for line in calls.read_text(encoding="utf-8").splitlines():
                    if line.startswith("OMP_ARGV\t"):
                        return line.split("\t")[1:]
            time.sleep(0.05)
        self.fail(f"OMP was not invoked; calls={calls.read_text() if calls.exists() else ''}")
    def _wait_for_log(self, path: Path, marker: str) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.exists() and marker in path.read_text(encoding="utf-8"):
                return
            time.sleep(0.05)
        self.fail(f"Log marker {marker!r} was not written; log={path.read_text() if path.exists() else ''}")


if __name__ == "__main__":
    unittest.main()
