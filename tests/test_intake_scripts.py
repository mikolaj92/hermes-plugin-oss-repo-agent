from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Final


ROOT: Final = Path(__file__).resolve().parents[1]
INTAKE: Final = ROOT / "scripts" / "repo_issue_intake.sh"


class IntakeOwnershipTests(unittest.TestCase):
    def test_foreign_assignee_is_skipped_without_edit_or_create_and_unassigned_is_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_path:
            root = Path(temporary_path)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            clone = root / "clone"
            self._make_repo(clone)
            repos = root / "repos.txt"
            repos.write_text(f"owner/repo|board|{clone}|1\n", encoding="utf-8")
            calls = root / "calls.log"
            log = root / "intake.log"
            self._write_gh(fake_bin / "gh", calls)
            self._write_hermes(fake_bin / "hermes", calls)
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
                    "CALLS_FILE": str(calls),
                    "HERMES_REPO_AGENT_REPOS_FILE": str(repos),
                    "HERMES_REPO_AGENT_TEST_FIXTURE": "1",
                    "HERMES_REPO_AGENT_SOURCE": "kanban",
                    "HERMES_REPO_AGENT_ASSIGNEE": "agent",
                    "HERMES_INTAKE_LOG": str(log),
                    "HERMES_INTAKE_LOCK_DIR": str(root / "lock"),
                    "HERMES_REPO_AGENT_ACTIVE_ISSUE_DIR": str(root / "active"),
                }
            )
            result = subprocess.run(
                ["bash", str(INTAKE), "--live", "--limit", "2"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            calls_text = calls.read_text(encoding="utf-8")
            self.assertNotIn("issue edit 3388", calls_text)
            self.assertIn("issue edit 42 --repo owner/repo --add-assignee agent", calls_text)
            self.assertNotIn("create [issue] owner/repo#3388", calls_text)
            self.assertIn("create [issue] owner/repo#42", calls_text)
            log_text = log.read_text(encoding="utf-8")
            self.assertIn("ISSUE_SKIPPED_NOT_READY repo=owner/repo issue=3388", log_text)

    def test_replaced_claim_blocks_assignment_and_kanban_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_path:
            root = Path(temporary_path)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            clone = root / "clone"
            self._make_repo(clone)
            repos = root / "repos.txt"
            repos.write_text(f"owner/repo|board|{clone}|1\n", encoding="utf-8")
            calls = root / "calls.log"
            log = root / "intake.log"
            active = root / "active"
            self._write_gh(fake_bin / "gh", calls)
            self._write_hermes(fake_bin / "hermes", calls)
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
                    "CALLS_FILE": str(calls),
                    "HERMES_REPO_AGENT_REPOS_FILE": str(repos),
                    "HERMES_REPO_AGENT_TEST_FIXTURE": "1",
                    "HERMES_REPO_AGENT_SOURCE": "kanban",
                    "HERMES_REPO_AGENT_ASSIGNEE": "agent",
                    "HERMES_INTAKE_LOG": str(log),
                    "HERMES_INTAKE_LOCK_DIR": str(root / "lock"),
                    "HERMES_REPO_AGENT_ACTIVE_ISSUE_DIR": str(active),
                    "HERMES_INTAKE_CLAIM_GUARD_HOOK": (
                        "rm -rf \"$HERMES_REPO_AGENT_ACTIVE_ISSUE_DIR\"; "
                        "mkdir -p \"$HERMES_REPO_AGENT_ACTIVE_ISSUE_DIR\"; "
                        "printf '%s\\n' '{\"version\":1,\"repo\":\"other/repo\",\"issue\":99,\"board\":\"other-board\",\"claimedAt\":\"2024-01-01T00:00:00Z\"}' "
                        ">\"$HERMES_REPO_AGENT_ACTIVE_ISSUE_DIR/claim.json\""
                    ),
                }
            )
            result = subprocess.run(
                ["bash", str(INTAKE), "--live", "--limit", "1"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(1, result.returncode, result.stdout + result.stderr)
            calls_text = calls.read_text(encoding="utf-8")
            self.assertNotIn("issue edit 42", calls_text)
            self.assertNotIn("create [issue] owner/repo#42", calls_text)
            self.assertIn("claim_mismatch", log.read_text(encoding="utf-8"))
            self.assertIn('"repo":"other/repo"', (active / "claim.json").read_text(encoding="utf-8"))

    def test_shell_syntax(self) -> None:
        result = subprocess.run(["bash", "-n", str(INTAKE)], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def _make_repo(self, path: Path) -> None:
        subprocess.run(["git", "init", str(path)], cwd=path.parent, check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
        (path / "README").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(path), "add", "README"], check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-m", "base"], check=True, capture_output=True, text=True)

    def _write_gh(self, path: Path, calls: Path) -> None:
        path.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
printf 'GH\\t%s\\n' "$*" >>"${CALLS_FILE:?}"
case " $* " in
  *' label list '*) printf '%s\n' 'ai:ready' ;;
  *issue*) printf '%s\n' $'3388\tTemida foreign\thttps://example/3388\t-\tother-user\tfalse' $'42\tUnassigned issue\thttps://example/42\tai:ready\t-\ttrue' ;;
esac
""",
            encoding="utf-8",
        )
        path.chmod(0o700)

    def _write_hermes(self, path: Path, calls: Path) -> None:
        path.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
printf 'HERMES\\t%s\\n' "$*" >>"${CALLS_FILE:?}"
case " $* " in
  *' kanban '*' list '*) printf '%s\\n' '[]' ;;
  *' kanban '*' create '*) : ;;
esac
""",
            encoding="utf-8",
        )
        path.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
