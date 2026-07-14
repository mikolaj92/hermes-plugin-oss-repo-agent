from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Final


ROOT: Final = Path(__file__).resolve().parents[1]
DISPATCHER: Final = ROOT / "scripts" / "repo_issue_to_pr_dispatch.sh"


class DispatcherWorkerContractTest(unittest.TestCase):
    def test_github_issue_row_preserves_exact_number_and_title_in_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_path:
            root = Path(temporary_path)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            clone = self._make_repo(root / "clone")
            repos = root / "repos.txt"
            log = root / "dispatch.log"
            lock = root / "dispatch.lock"
            calls = root / "calls.log"
            repos.write_text(f"mikolaj92/Fala|fala|{clone}|100\n", encoding="utf-8")
            self._write_fake_gh(fake_bin / "gh")

            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
                    "CALLS_FILE": str(calls),
                    "HERMES_REPO_AGENT_REPOS_FILE": str(repos),
                    "HERMES_REPO_AGENT_SOURCE": "github",
                    "HERMES_ISSUE_TO_PR_LOG": str(log),
                    "HERMES_ISSUE_TO_PR_LOCK_DIR": str(lock),
                }
            )
            result = subprocess.run(
                ["bash", str(DISPATCHER), "--dry-run", "--max", "1"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            output = log.read_text(encoding="utf-8")
            self.assertIn("repo=mikolaj92/Fala issue=123", output)
            self.assertIn("action=would-run-omp", output)
            self.assertIn("fix-exact-issue-title", output)
            self.assertNotIn("#1234", output)

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
        subprocess.run(
            ["bash", "-lc", command],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    def _write_fake_gh(self, path: Path) -> None:
        path.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
printf 'GH\\t%s\\n' "$*" >>"${CALLS_FILE:?}"
case " $* " in
  *' pr list '*) : ;;
  *' issue list '*) printf '123\\tFix exact issue title\\thttps://github.test/mikolaj92/Fala/issues/123\\t\\n' ;;
esac
""",
            encoding="utf-8",
        )
        path.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
