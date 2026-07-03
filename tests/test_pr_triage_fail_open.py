from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Final


ROOT: Final = Path(__file__).resolve().parents[1]
TRIAGE: Final = ROOT / "scripts" / "repo_pr_triage.sh"


class PrTriageFailOpenTests(unittest.TestCase):
    def test_continues_to_later_repo_when_pr_list_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_path:
            # Given: one repo whose PR list fails before a healthy later repo.
            root = Path(temporary_path)
            calls_file = root / "calls.log"
            log_file = root / "triage.log"
            repos_file = root / "repos.txt"
            lock_dir = root / "triage.lock"
            repos_file.write_text(
                "\n".join(
                    (
                        "owner/bad|board-bad|/tmp/bad|10",
                        "owner/good|board-good|/tmp/good|10",
                        "",
                    )
                )
            )

            # When: the triage script runs in dry-run mode against fake gh.
            result = self._run_triage(
                calls_file=calls_file,
                log_file=log_file,
                repos_file=repos_file,
                lock_dir=lock_dir,
            )

            # Then: it logs the failing repo, still lists the later repo, and
            # reaches the final summary instead of aborting under set -e.
            combined_output = result.stdout + result.stderr + self._read_text(log_file)
            calls = self._read_text(calls_file)
            self.assertEqual(1, result.returncode, combined_output + calls)
            self.assertIn("PR_LIST_FAILED repo=owner/bad", combined_output)
            self.assertIn("GH\tpr list --repo owner/good", calls)
            self.assertIn("NO_OPEN_PRS repo=owner/good", combined_output)
            self.assertIn("DONE mode=dry-run", combined_output)
            self.assertIn("failures=1", combined_output)

    def test_logs_merge_failure_and_reaches_done_when_live_merge_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_path:
            # Given: one clean, mergeable repo-agent PR whose live merge fails.
            root = Path(temporary_path)
            calls_file = root / "calls.log"
            log_file = root / "triage.log"
            repos_file = root / "repos.txt"
            lock_dir = root / "triage.lock"
            repos_file.write_text("owner/good|board-good|/tmp/good|10\n")
            pr_list_json = json.dumps(
                [
                    {
                        "number": 7,
                        "title": "[fix-pr] merge me",
                        "url": "https://example.invalid/pr/7",
                        "headRefName": "ai/fix/merge",
                        "baseRefName": "main",
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "reviewDecision": "APPROVED",
                        "labels": [
                            {"name": "ai:generated"},
                            {"name": "ai:pr-opened"},
                        ],
                        "author": {"login": "owner"},
                    }
                ]
            )

            # When: live triage reaches the merge command through fake gh.
            result = self._run_triage(
                calls_file=calls_file,
                log_file=log_file,
                repos_file=repos_file,
                lock_dir=lock_dir,
                live=True,
                pr_list_json=pr_list_json,
            )

            # Then: the merge failure is recorded and the final summary is reached.
            combined_output = result.stdout + result.stderr + self._read_text(log_file)
            calls = self._read_text(calls_file)
            self.assertEqual(1, result.returncode, combined_output + calls)
            self.assertIn(
                "DECISION repo=owner/good pr=7 decision=merge reason=own-pr-clean",
                combined_output,
            )
            self.assertIn("GH\tpr merge 7 --repo owner/good --merge", calls)
            self.assertIn("MERGE_FAILED repo=owner/good pr=7", combined_output)
            self.assertIn("DONE mode=live", combined_output)
            self.assertIn("failures=1", combined_output)

    def test_skips_merge_when_live_label_repair_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_path:
            # Given: a clean owner-authored ai/fix PR missing required AI labels.
            root = Path(temporary_path)
            calls_file = root / "calls.log"
            log_file = root / "triage.log"
            repos_file = root / "repos.txt"
            lock_dir = root / "triage.lock"
            repos_file.write_text("owner/good|board-good|/tmp/good|10\n")
            pr_list_json = json.dumps(
                [
                    {
                        "number": 7,
                        "title": "[fix-pr] repair labels first",
                        "url": "https://example.invalid/pr/7",
                        "headRefName": "ai/fix/repair-labels",
                        "baseRefName": "main",
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "reviewDecision": "APPROVED",
                        "labels": [],
                        "author": {"login": "owner"},
                    }
                ]
            )

            # When: live label reconciliation fails through fake gh.
            result = self._run_triage(
                calls_file=calls_file,
                log_file=log_file,
                repos_file=repos_file,
                lock_dir=lock_dir,
                live=True,
                pr_list_json=pr_list_json,
                label_repair_fails=True,
            )

            # Then: the PR is skipped for this run and never merged unlabelled.
            combined_output = result.stdout + result.stderr + self._read_text(log_file)
            calls = self._read_text(calls_file)
            self.assertEqual(1, result.returncode, combined_output + calls)
            self.assertIn("LABEL_REPAIR_FAILED repo=owner/good pr=7", combined_output)
            self.assertIn(
                "DECISION repo=owner/good pr=7 decision=skip reason=label-repair-failed",
                combined_output,
            )
            self.assertIn("DONE mode=live", combined_output)
            self.assertIn("failures=1", combined_output)
            self.assertIn(
                "GH\tpr edit 7 --repo owner/good --add-label ai:generated --add-label ai:pr-opened",
                calls,
            )
            self.assertNotIn("GH\tpr merge 7 --repo owner/good --merge", calls)
            self.assertNotIn("decision=merge reason=own-pr-clean", combined_output)

    def _run_triage(
        self,
        *,
        calls_file: Path,
        log_file: Path,
        repos_file: Path,
        lock_dir: Path,
        live: bool = False,
        pr_list_json: str = "",
        label_repair_fails: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "BASH_FUNC_gh%%": _fake_gh_function(),
                "CALLS_FILE": str(calls_file),
                "HERMES_PR_TRIAGE_LOCK_DIR": str(lock_dir),
                "HERMES_PR_TRIAGE_LOG": str(log_file),
                "HERMES_REPO_AGENT_REPOS_FILE": str(repos_file),
            }
        )
        mode_argument = "--live" if live else "--dry-run"
        if pr_list_json:
            env["GH_PR_LIST_JSON"] = pr_list_json
        if label_repair_fails:
            env["GH_PR_EDIT_FAIL"] = "1"
        if live:
            env.update(
                {
                    "HERMES_PR_ALLOW_NO_CHECKS": "1",
                    "HERMES_PR_AUTOMERGE": "1",
                    "HERMES_PR_REQUIRE_APPROVED": "1",
                    "HERMES_PR_REQUIRE_TEST_EVIDENCE": "0",
                }
            )
        return subprocess.run(
            ["bash", str(TRIAGE), mode_argument],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def _read_text(self, path: Path) -> str:
        if path.exists():
            return path.read_text()
        return ""


def _fake_gh_function() -> str:
    return r'''() {
  printf 'GH\t%s\n' "$*" >>"${CALLS_FILE:?}"
  if [[ "${1:-}" == "pr" && "${2:-}" == "list" ]]; then
    local repo=""
    local args=("$@")
    local index=0
    while [[ $index -lt ${#args[@]} ]]; do
      if [[ "${args[$index]}" == "--repo" ]]; then
        index=$((index + 1))
        repo="${args[$index]:-}"
      fi
      index=$((index + 1))
    done
    if [[ "$repo" == "owner/bad" ]]; then
      printf 'fake pr list failure\n' >&2
      return 42
    fi
    if [[ "$repo" == "owner/good" && -n "${GH_PR_LIST_JSON:-}" ]]; then
      printf '%s\n' "$GH_PR_LIST_JSON"
      return 0
    fi
    printf '[]\n'
    return 0
  fi
  if [[ "${1:-}" == "pr" && "${2:-}" == "checks" ]]; then
    printf '[]\n'
    return 0
  fi
    if [[ "${1:-}" == "pr" && "${2:-}" == "edit" && "${3:-}" == "7" && "${GH_PR_EDIT_FAIL:-}" == "1" && " $* " == *" --add-label "* ]]; then
      printf 'fake label repair failure\n' >&2
      return 42
    fi
  if [[ "${1:-}" == "pr" && "${2:-}" == "merge" && "${3:-}" == "7" ]]; then
    printf 'fake merge failure\n' >&2
    return 42
  fi
  return 0
}'''


if __name__ == "__main__":
    unittest.main()
