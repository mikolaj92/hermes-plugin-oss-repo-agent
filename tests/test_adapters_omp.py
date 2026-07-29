from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lokay import adapters_omp


class OmpAdapterTests(unittest.TestCase):
    @mock.patch("lokay.adapters_omp.run_cmd")
    def test_existing_mode_preserves_yolo_and_tail_contract(self, run_cmd) -> None:
        run_cmd.return_value = mock.Mock(returncode=0, stdout="x" * 2500, stderr="e" * 1200)
        out = adapters_omp.run_omp(prompt="p", cwd=".", command="omp", model="m", thinking="medium", timeout=10, dry_run=False)
        args = run_cmd.call_args.args[0]
        self.assertIn("yolo", args)
        self.assertNotIn("--no-tools", args)
        self.assertEqual(len(out["stdout_tail"]), 2000)
        self.assertNotIn("stdout", out)

    @mock.patch("lokay.adapters_omp.run_cmd")
    def test_classification_mode_returns_full_stdout_and_safe_args(self, run_cmd) -> None:
        stdout = '{"schema_version":1}' + " " * 2500
        run_cmd.return_value = mock.Mock(returncode=0, stdout=stdout, stderr="warning")
        with tempfile.TemporaryDirectory() as tmp:
            out = adapters_omp.run_omp(prompt="p", cwd=tmp, command="omp", model="m", thinking="medium", timeout=10, dry_run=False, classification=True)
            args = run_cmd.call_args.args[0]
            for flag in ("--no-tools", "--no-extensions", "--no-skills", "--no-rules", "--no-lsp", "--no-pty"):
                self.assertIn(flag, args)
            self.assertIn("always-ask", args)
            self.assertNotIn("yolo", args)
            self.assertEqual(out["stdout"], stdout)
            self.assertEqual(out["stdout_tail"], stdout[-2000:])
            self.assertEqual(Path(run_cmd.call_args.kwargs["cwd"]), Path(tmp).resolve())

    def test_classification_dry_run_exposes_safe_command(self) -> None:
        out = adapters_omp.run_omp(prompt="secret", cwd=".", command="omp", model="m", thinking="medium", timeout=10, dry_run=True, classification=True)
        self.assertEqual(out["status"], "planned")
        self.assertNotIn("secret", out["command"])
        self.assertIn("--no-tools", out["command"])


if __name__ == "__main__":
    unittest.main()
