from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLEANUP = ROOT / "scripts" / "repo_agent_cleanup.sh"


class CleanupReceiptTests(unittest.TestCase):
    def test_matching_terminal_receipt_removes_clean_worktree_and_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clone = self._repo(root / "clone")
            worktree = root / "worktrees" / "board" / "task-123"
            branch = "ai/fix/123-terminal"
            self._git(clone, "worktree", "add", "-b", branch, str(worktree))
            receipt_dir = root / "receipts"
            receipt_dir.mkdir()
            self._receipt(receipt_dir / "terminal.json", clone, worktree, branch, task="task-123")
            repos = root / "repos.txt"
            repos.write_text(f"owner/repo|board|{clone}|1\n", encoding="utf-8")
            log = root / "cleanup.log"
            env = self._env(root, repos, receipt_dir, log)
            result = subprocess.run(["bash", str(CLEANUP), "--live"], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr + result.stdout)
            self.assertFalse(worktree.exists())
            branches = subprocess.run(["git", "-C", str(clone), "branch", "--list", branch], text=True, capture_output=True, check=True).stdout
            self.assertNotIn(branch, branches)
            self.assertIn("WORKTREE_REMOVED", log.read_text(encoding="utf-8"))
            outcome_path = receipt_dir / "cleanup-outcomes" / "owner_repo-123-ai_fix_123-terminal.json"
            outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
            self.assertEqual("CLEANUP_CONFIRMED", outcome["status"])
            self.assertEqual("task-123", outcome["task_id"])
            self.assertEqual(worktree.resolve(), Path(outcome["worktree_path"]))
            self.assertTrue(outcome["local_branch_deleted"])
            self.assertFalse(outcome["remote_branch_deleted"])
    def test_missing_target_writes_reconciled_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clone = self._repo(root / "clone")
            branch = "ai/fix/123-terminal"
            self._git(clone, "branch", branch)
            receipt_dir = root / "receipts"
            receipt_dir.mkdir()
            self._receipt(receipt_dir / "terminal.json", clone, root / "missing", branch, task="task-123")
            repos = root / "repos.txt"
            repos.write_text(f"owner/repo|board|{clone}|1\n", encoding="utf-8")
            result = subprocess.run(["bash", str(CLEANUP), "--live"], cwd=ROOT, env=self._env(root, repos, receipt_dir, root / "cleanup.log"), text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr + result.stdout)
            outcome_path = receipt_dir / "cleanup-outcomes" / "owner_repo-123-ai_fix_123-terminal.json"
            outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
            self.assertEqual("NO_TARGET_RECONCILED", outcome["status"])
            self.assertEqual("", outcome["worktree_path"])
            self.assertFalse(outcome["local_branch_deleted"])
            self.assertIn(branch, self._git_output(clone, "branch", "--format=%(refname:short)").splitlines())

    def test_board_worker_lock_preserves_matching_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clone = self._repo(root / "clone")
            worktree = root / "worktrees" / "board" / "task-123"
            branch = "ai/fix/123-board-lock"
            self._git(clone, "worktree", "add", "-b", branch, str(worktree))
            lock = root / "worktrees" / "board" / ".agent.lock"
            lock.mkdir(parents=True)
            (lock / "pid").write_text(str(os.getpid()) + "\n", encoding="utf-8")
            receipt_dir = root / "receipts"
            receipt_dir.mkdir()
            self._receipt(receipt_dir / "terminal.json", clone, worktree, branch, task="task-123")
            repos = root / "repos.txt"
            repos.write_text(f"owner/repo|board|{clone}|1\n", encoding="utf-8")
            log = root / "cleanup.log"
            result = subprocess.run(["bash", str(CLEANUP), "--live"], cwd=ROOT, env=self._env(root, repos, receipt_dir, log), text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr + result.stdout)
            self.assertTrue(worktree.exists())

    def test_canonical_triage_receipt_derives_worktree_from_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clone = self._repo(root / "clone")
            worktree = root / "worktrees" / "board" / "task-123"
            branch = "ai/fix/123-triage"
            self._git(clone, "worktree", "add", "-b", branch, str(worktree))
            receipt_dir = root / "receipts"
            receipt_dir.mkdir()
            base_sha = self._git_output(clone, "rev-parse", "main")
            (receipt_dir / "triage.json").write_text(json.dumps({
                "repo": "owner/repo", "issue": 123, "pr": 1, "branch": branch,
                "baseSha": base_sha, "preMergeBaseSha": base_sha,
                "headSha": base_sha, "mergeSha": base_sha,
                "originMainSha": base_sha, "baseRef": "main",
                "mergedAt": "2026-01-01T00:00:00Z",
                "phase": "ISSUE_CLOSED_CONFIRMED",
            }), encoding="utf-8")
            repos = root / "repos.txt"
            repos.write_text(f"owner/repo|board|{clone}|1\n", encoding="utf-8")
            log = root / "cleanup.log"
            result = subprocess.run(["bash", str(CLEANUP), "--live"], cwd=ROOT, env=self._env(root, repos, receipt_dir, log, branch=branch), text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr + result.stdout)
            self.assertFalse(worktree.exists())
    def test_forged_merge_provenance_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clone = self._repo(root / "clone")
            worktree = root / "worktrees" / "board" / "task-123"
            branch = "ai/fix/123-forged"
            self._git(clone, "worktree", "add", "-b", branch, str(worktree))
            receipt_dir = root / "receipts"; receipt_dir.mkdir()
            self._receipt(receipt_dir / "forged.json", clone, worktree, branch, task="task-123", merge_sha="d" * 40)
            repos = root / "repos.txt"; repos.write_text(f"owner/repo|board|{clone}|1\n", encoding="utf-8")
            log = root / "cleanup.log"
            result = subprocess.run(["bash", str(CLEANUP), "--live"], cwd=ROOT, env=self._env(root, repos, receipt_dir, log), text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr + result.stdout)
            self.assertTrue(worktree.exists())
            self.assertIn("merge-provenance-unverifiable", log.read_text(encoding="utf-8"))
    def test_merge_not_ancestor_of_origin_main_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clone = self._repo(root / "clone")
            worktree = root / "worktrees" / "board" / "task-123"
            branch = "ai/fix/123-rolled-back"
            self._git(clone, "worktree", "add", "-b", branch, str(worktree))
            self._git(clone, "switch", "--detach")
            (clone / "rollback.txt").write_text("not on origin main\n", encoding="utf-8")
            self._git(clone, "add", "rollback.txt")
            self._git(clone, "commit", "-m", "merge no longer on main")
            merge_sha = subprocess.check_output(["git", "-C", str(clone), "rev-parse", "HEAD"], text=True).strip()
            self._git(clone, "switch", "main")
            receipt_dir = root / "receipts"; receipt_dir.mkdir()
            self._receipt(receipt_dir / "rolled-back.json", clone, worktree, branch, task="task-123", merge_sha=merge_sha)
            repos = root / "repos.txt"; repos.write_text(f"owner/repo|board|{clone}|1\n", encoding="utf-8")
            log = root / "cleanup.log"
            result = subprocess.run(["bash", str(CLEANUP), "--live"], cwd=ROOT, env=self._env(root, repos, receipt_dir, log, branch=branch, merge_sha=merge_sha), text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr + result.stdout)
            self.assertTrue(worktree.exists())
            self.assertIn("merge-provenance-unverifiable", log.read_text(encoding="utf-8"))


    def test_open_issue_or_closure_pending_receipt_is_preserved_and_quarantined(self) -> None:
        for issue_state, phase in (("OPEN", "ISSUE_CLOSED_CONFIRMED"), ("CLOSED", "CLOSURE_PENDING")):
            with self.subTest(issue_state=issue_state, phase=phase), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                clone = self._repo(root / "clone")
                worktree = root / "worktrees" / "board" / "task-123"
                branch = "ai/fix/123-pending"
                self._git(clone, "worktree", "add", "-b", branch, str(worktree))
                receipt_dir = root / "receipts"
                receipt_dir.mkdir()
                self._receipt(receipt_dir / "pending.json", clone, worktree, branch, task="task-123", phase=phase)
                repos = root / "repos.txt"
                repos.write_text(f"owner/repo|board|{clone}|1\n", encoding="utf-8")
                env = self._env(root, repos, receipt_dir, root / "cleanup.log", issue_state=issue_state)
                result = subprocess.run(["bash", str(CLEANUP), "--live"], cwd=ROOT, env=env, text=True, capture_output=True)
                self.assertEqual(0, result.returncode, result.stderr + result.stdout)
                self.assertTrue(worktree.exists())
                self.assertTrue(list((receipt_dir / "quarantine").glob("*.json")))

    def test_malformed_missing_dirty_and_unrelated_receipts_are_preserved(self) -> None:
        cases = ("missing", "malformed", "unrelated", "dirty")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                clone = self._repo(root / "clone")
                worktree = root / "worktrees" / "board" / "task-123"
                branch = "ai/fix/123-safety"
                self._git(clone, "worktree", "add", "-b", branch, str(worktree))
                receipt_dir = root / "receipts"
                receipt_dir.mkdir()
                if case == "malformed":
                    (receipt_dir / "bad.json").write_text("{not-json", encoding="utf-8")
                elif case == "unrelated":
                    self._receipt(receipt_dir / "other.json", clone, worktree, "ai/fix/999-other", task="other")
                elif case == "dirty":
                    self._receipt(receipt_dir / "dirty.json", clone, worktree, branch, task="task-123")
                    (worktree / "dirty.txt").write_text("keep\n", encoding="utf-8")
                repos = root / "repos.txt"
                repos.write_text(f"owner/repo|board|{clone}|1\n", encoding="utf-8")
                env = self._env(root, repos, receipt_dir, root / "cleanup.log")
                result = subprocess.run(["bash", str(CLEANUP), "--live"], cwd=ROOT, env=env, text=True, capture_output=True)
                self.assertEqual(0, result.returncode, result.stderr + result.stdout)
                self.assertTrue(worktree.exists())

    def _env(self, root: Path, repos: Path, receipts: Path, log: Path, *, issue_state: str = "CLOSED", branch: str = "ai/fix/123-terminal", merge_sha: str | None = None) -> dict[str, str]:
        clone = Path(repos.read_text(encoding="utf-8").split("|")[2])
        base_sha = self._git_output(clone, "rev-parse", "main")
        head_sha = self._git_output(clone, "rev-parse", "HEAD")
        merge = merge_sha or base_sha
        pr = json.dumps({"number": 1, "state": "MERGED", "baseRefName": "main", "baseRefOid": base_sha, "headRefName": branch, "headRefOid": head_sha, "mergeCommit": {"oid": merge}, "closingIssuesReferences": [{"number": 123}]})
        gh = f'''() {{
  if [[ "$1" == "issue" && "$2" == "view" ]]; then printf '%s\\n' {shlex.quote(issue_state)}; return 0; fi
  if [[ "$1" == "pr" && "$2" == "list" ]]; then printf '0\\n'; return 0; fi
  if [[ "$1" == "pr" && "$2" == "view" ]]; then printf '%s\\n' {shlex.quote(pr)}; return 0; fi
  return 1
}}'''
        return os.environ | {
            "HERMES_REPO_AGENT_REPOS_FILE": str(repos),
            "HERMES_REPO_AGENT_TEST_FIXTURE": "1",
            "HERMES_REPO_CLEANUP_LOG": str(log),
            "HERMES_REPO_CLEANUP_LOCK_DIR": str(root / "lock"),
            "HERMES_WORKTREE_ROOT": str(root / "worktrees"),
            "HERMES_REPO_AGENT_CLEANUP_RECEIPT_DIR": str(receipts),
            "HERMES_REPO_CLEANUP_QUARANTINE_DIR": str(receipts / "quarantine"),
            "HERMES_REPO_AGENT_CLEANUP_OUTCOME_DIR": str(receipts / "cleanup-outcomes"),
            "BASH_FUNC_gh%%": gh,
        }

    def _receipt(self, path: Path, clone: Path, worktree: Path, branch: str, *, task: str, phase: str = "ISSUE_CLOSED_CONFIRMED", merge_sha: str | None = None) -> None:
        base_sha = self._git_output(clone, "rev-parse", "main")
        head_sha = self._git_output(clone, "rev-parse", "HEAD")
        merge = merge_sha or base_sha
        path.write_text(json.dumps({
            "phase": phase, "repo": "owner/repo", "issue": 123, "pr": 1,
            "branch": branch, "worktree": str(worktree), "task_id": task,
            "clone_path": str(clone), "preMergeBaseSha": base_sha,
            "headSha": head_sha, "mergeSha": merge, "originMainSha": base_sha,
            "baseRef": "main", "mergedAt": "2026-01-01T00:00:00Z",
        }), encoding="utf-8")

    def _repo(self, path: Path) -> Path:
        path.mkdir(parents=True)
        origin = path.parent / "origin.git"
        subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True, text=True)
        self._git(path.parent, "init", str(path))
        self._git(path, "config", "user.email", "test@example.invalid")
        self._git(path, "config", "user.name", "Cleanup Test")
        (path / "README").write_text("base\n", encoding="utf-8")
        self._git(path, "add", "README")
        self._git(path, "commit", "-m", "base")
        self._git(path, "branch", "-M", "main")
        self._git(path, "remote", "add", "origin", str(origin))
        self._git(path, "push", "-u", "origin", "main")
        self._git(path, "fetch", "origin", "main")
        return path
    def _git(self, cwd: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)

    def _git_output(self, cwd: Path, *args: str) -> str:
        return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()


if __name__ == "__main__":
    unittest.main()
