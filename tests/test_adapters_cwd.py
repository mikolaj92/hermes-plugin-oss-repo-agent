from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from repo_agent.adapters_cli import run_cmd
from repo_agent.adapters_omp import run_omp


class AdapterCwdTests(unittest.TestCase):
    def test_run_cmd_forwards_cwd_to_subprocess(self) -> None:
        completed = subprocess.CompletedProcess(["tool"], 0, "out", "")
        with mock.patch("repo_agent.adapters_cli.subprocess.run", return_value=completed) as run:
            result = run_cmd(["tool"], cwd=Path("/tmp/worktree"), timeout=7.5)

        self.assertIs(result, completed)
        self.assertEqual(run.call_args.kwargs["cwd"], Path("/tmp/worktree"))
        self.assertEqual(run.call_args.kwargs["timeout"], 7.5)

    def test_run_omp_resolves_and_forwards_cwd(self) -> None:
        completed = subprocess.CompletedProcess(["omp"], 0, "done", "")
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            with mock.patch("repo_agent.adapters_omp.run_cmd", return_value=completed) as run:
                result = run_omp(
                    prompt="fix",
                    cwd=worktree,
                    command="omp",
                    model="test-model",
                    thinking="medium",
                    timeout=12.0,
                    dry_run=False,
                )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(run.call_args.kwargs["cwd"], worktree.resolve())
        self.assertEqual(run.call_args.kwargs["timeout"], 12.0)
        self.assertEqual(
            run.call_args.args[0],
            [
                "omp",
                "--cwd",
                str(worktree.resolve()),
                "--model",
                "test-model",
                "--thinking",
                "medium",
                "--approval-mode",
                "yolo",
                "--no-session",
                "-p",
                "fix",
            ],
        )

    def test_run_omp_dry_run_does_not_invoke_subprocess(self) -> None:
        with mock.patch("repo_agent.adapters_omp.run_cmd") as run:
            result = run_omp(
                prompt="fix",
                cwd="relative/worktree",
                command="omp",
                model="test-model",
                thinking="medium",
                timeout=12.0,
                dry_run=True,
            )

        run.assert_not_called()
        self.assertEqual(result["status"], "planned")
        self.assertIn(str(Path("relative/worktree").resolve()), result["command"])



if __name__ == "__main__":
    unittest.main()
