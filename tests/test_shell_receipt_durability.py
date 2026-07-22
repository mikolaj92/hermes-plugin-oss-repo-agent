from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
DISPATCH: Final = ROOT / "scripts" / "repo_issue_to_pr_dispatch.sh"
TRIAGE: Final = ROOT / "scripts" / "repo_pr_triage.sh"
INTAKE: Final = ROOT / "scripts" / "repo_issue_intake.sh"


def function_block(path: Path, names: list[str]) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks: list[str] = []
    for name in names:
        start = lines.index(f"{name}() {{")
        end = len(lines)
        for index in range(start + 1, len(lines)):
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\(\) \{$", lines[index]):
                end = index
                break
        blocks.append("\n".join(lines[start:end]))
    return "\n\n".join(blocks)


class ShellReceiptDurabilityTests(unittest.TestCase):
    def run_shell(self, script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", "-c", script], cwd=ROOT, text=True, capture_output=True)

    def test_dispatch_claim_and_update_are_durable_and_clean_temps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = f"""
set -eu
RECEIPT_DIR={root!s}
{function_block(DISPATCH, ['slugify', 'receipt_file', 'receipt_write_claim', 'receipt_update'])}
receipt_write_claim task-1 owner/repo 7 ai/fix/7 /clone base
receipt_update task-1 phase finished
python3 - "$RECEIPT_DIR/task-1.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as stream:
    data = json.load(stream)
assert data["task_id"] == "task-1" and data["phase"] == "finished"
PY
! find "$RECEIPT_DIR" -name '.receipt.*' -print -quit | grep -q .
"""
            result = self.run_shell(script)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_dispatch_conflicting_claim_fails_without_clobbering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "task-1.json"
            original = {"version": 1, "task_id": "task-1", "repo": "other/repo", "phase": "claimed"}
            path.write_text(json.dumps(original), encoding="utf-8")
            script = f"""
set -eu
RECEIPT_DIR={root!s}
{function_block(DISPATCH, ['slugify', 'receipt_file', 'receipt_write_claim'])}
if receipt_write_claim task-1 owner/repo 7 ai/fix/7 /clone base; then exit 1; fi
exit 0
"""
            result = self.run_shell(script)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)
    def test_temp_creation_failure_propagates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = f"""
set -eu
RECEIPT_DIR={directory}
{function_block(DISPATCH, ['slugify', 'receipt_file', 'receipt_write_claim'])}
mktemp() {{ return 1; }}
if receipt_write_claim task-1 owner/repo 7 ai/fix/7 /clone base; then exit 1; fi
"""
            result = self.run_shell(script)
            self.assertEqual(result.returncode, 0, result.stderr)

            self.assertEqual(list(Path(directory).glob(".receipt.*")), [])

    def test_intake_claim_is_atomic_and_rejects_conflicting_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / "active"
            script = f"""
set -eu
ACTIVE_ISSUE_DIR={active!s}
ACTIVE_CLAIM_PATH="$ACTIVE_ISSUE_DIR/claim.json"
CLAIM_ASSIGNEE=agent
log() {{ :; }}
{function_block(INTAKE, ['validate_active_claim', 'ensure_active_claim'])}
ensure_active_claim owner/repo 9 board
"""
            result = self.run_shell(script)
            self.assertEqual(result.returncode, 0, result.stderr)
            claim = active / "claim.json"
            payload = json.loads(claim.read_text(encoding="utf-8"))
            self.assertEqual(payload["issue"], 9)
            self.assertEqual(payload["assignee"], "agent")
            claim.write_text(json.dumps({"version": 1, "repo": "other/repo", "issue": 8, "board": "board", "assignee": "agent", "claimedAt": "2024-01-01T00:00:00Z"}), encoding="utf-8")
            conflict = script + "\nif ensure_active_claim owner/repo 9 board; then exit 1; fi\n"
            result = self.run_shell(conflict)
            self.assertNotEqual(result.returncode, 0)


    def test_intake_claim_rejects_matching_incomplete_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / "active"
            active.mkdir()
            claim = active / "claim.json"
            claim.write_text(json.dumps({"version": 1, "repo": "owner/repo", "issue": 9, "board": "board"}), encoding="utf-8")
            script = f"""
set -eu
ACTIVE_ISSUE_DIR={active!s}
ACTIVE_CLAIM_PATH=\"$ACTIVE_ISSUE_DIR/claim.json\"
CLAIM_ASSIGNEE=agent
log() {{ :; }}
{{function_block(INTAKE, ['validate_active_claim', 'ensure_active_claim'])}}
if ensure_active_claim owner/repo 9 board; then exit 1; fi
"""
            result = self.run_shell(script)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(claim.read_text(encoding="utf-8"))["repo"], "owner/repo")

    def test_triage_merge_and_closed_receipts_are_durable_and_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = f"""
set -eu
MERGE_RECEIPT_DIR={root!s}
{function_block(TRIAGE, ['write_merge_receipt', 'mark_receipt_closed'])}
write_merge_receipt owner/repo 4 9 base head merge now main origin ai/fix/9
mark_receipt_closed "$MERGE_RECEIPT_PATH"
python3 - "$MERGE_RECEIPT_PATH" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as stream:
    assert json.load(stream)["phase"] == "ISSUE_CLOSED_CONFIRMED"
PY
! find "$MERGE_RECEIPT_DIR" -name '.receipt.*' -print -quit | grep -q .
"""
            result = self.run_shell(script)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_release_active_claim_fails_closed_on_malformed_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / "active"
            active.mkdir()
            claim = active / "claim.json"
            claim.write_text("not-json", encoding="utf-8")
            script = f"""
set -eu
HERMES_REPO_AGENT_ACTIVE_ISSUE_DIR={active!s}
{function_block(TRIAGE, ['release_active_claim'])}
release_active_claim owner/repo 9
"""
            result = self.run_shell(script)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(claim.exists())

    def test_shell_syntax(self) -> None:
        for script in (DISPATCH, TRIAGE, INTAKE):
            result = subprocess.run(["bash", "-n", str(script)], cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, f"{script}: {result.stderr}")


if __name__ == "__main__":
    unittest.main()
