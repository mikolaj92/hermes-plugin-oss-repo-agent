from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from repo_agent.steps import cleanup, issue_to_pr


def run_git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def request(data: dict, config: dict | None = None) -> dict:
    return {"input": data, "config": config or {}}


class TempGitSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.remote = root / "remote.git"
        self.seed = root / "seed"
        self.clone = root / "clone"
        self.worktrees = root / "worktrees"
        self.remote.mkdir()
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        self.seed.mkdir()
        run_git(self.seed, "init")
        run_git(self.seed, "config", "user.email", "test@example.invalid")
        run_git(self.seed, "config", "user.name", "Test")
        (self.seed / "README").write_text("one\n")
        run_git(self.seed, "add", "README")
        run_git(self.seed, "commit", "-m", "one")
        run_git(self.seed, "branch", "-M", "main")
        run_git(self.seed, "remote", "add", "origin", str(self.remote))
        run_git(self.seed, "push", "origin", "main")
        run_git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        subprocess.run(["git", "clone", str(self.remote), str(self.clone)], check=True, capture_output=True)
        run_git(self.clone, "config", "user.email", "test@example.invalid")
        run_git(self.clone, "config", "user.name", "Test")
        self.branch = "ai/fix/7-safe"
        self.identity = {"task_id": "task-7", "issue": 7, "receipt_path": str(root / "receipt.json"), "repo": "owner/repo"}
        self.common = {"clone_path": str(self.clone), "worktree_root": str(self.worktrees), "branch": self.branch, "base_branch": "main", "dry_run": False, **self.identity}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def prepare(self, **extra: object) -> dict:
        return issue_to_pr.prepare_worktree(request({**self.common, **extra}))

    def test_create_and_cleanup_owned_worktree(self) -> None:
        prepared = self.prepare()
        self.assertTrue(prepared["ok"], prepared)
        wt = Path(prepared["worktree_path"])
        self.assertEqual(prepared["head"], prepared["base_head"])
        guards = {"check_issue_closed": {"ok": True, "closed": True}, "check_no_open_pr": {"ok": True, "safe_to_cleanup": True}}
        removed = cleanup.remove_worktree(request({**self.common, "worktree_path": str(wt), "conduction": guards}))
        self.assertEqual(removed["status"], "removed", removed)
        deleted = cleanup.delete_local_fix_branch(request({**self.common, "conduction": {**guards, "remove_worktree": removed}}))
        self.assertEqual(deleted["status"], "deleted", deleted)
        self.assertFalse(wt.exists())
        self.assertFalse(run_git(self.clone, "branch", "--list", self.branch))

    def test_dirty_clone_fails_before_branch_mutation(self) -> None:
        (self.clone / "dirty.txt").write_text("dirty\n")
        result = self.prepare()
        self.assertEqual(result["reason"], "clone_dirty")
        self.assertFalse(run_git(self.clone, "branch", "--list", self.branch))

    def test_path_collision_fails_before_git_mutation(self) -> None:
        path = self.worktrees / self.branch
        path.parent.mkdir(parents=True)
        path.write_text("not a worktree\n")
        result = self.prepare()
        self.assertEqual(result["reason"], "worktree_path_collision")
        self.assertEqual(path.read_text(), "not a worktree\n")
        self.assertFalse(run_git(self.clone, "branch", "--list", self.branch))

    def test_foreign_worktree_fails_closed(self) -> None:
        path = self.worktrees / self.branch
        path.parent.mkdir(parents=True)
        run_git(self.clone, "worktree", "add", "-b", "foreign/branch", str(path), "origin/main")
        result = self.prepare()
        self.assertEqual(result["reason"], "worktree_provenance_mismatch")
        self.assertTrue(path.exists())
        self.assertEqual(run_git(path, "branch", "--show-current"), "foreign/branch")

    def test_stale_existing_branch_fails_closed(self) -> None:
        run_git(self.clone, "branch", self.branch, "origin/main")
        for key, value in (("task", "task-7"), ("issue", "7"), ("receipt", self.identity["receipt_path"]), ("repo", "owner/repo")):
            run_git(self.clone, "config", f"branch.{self.branch}.repo-agent-{key}", value)
        (self.seed / "README").write_text("two\n")
        run_git(self.seed, "add", "README")
        run_git(self.seed, "commit", "-m", "two")
        run_git(self.seed, "push", "origin", "main")
        result = self.prepare()
        self.assertEqual(result["reason"], "branch_stale")
        self.assertFalse((self.worktrees / self.branch).exists())

    def test_foreign_branch_is_not_deleted(self) -> None:
        run_git(self.clone, "branch", self.branch, "origin/main")
        guards = {"check_issue_closed": {"ok": True, "closed": True}, "check_no_open_pr": {"ok": True, "safe_to_cleanup": True}, "remove_worktree": {"ok": True, "status": "removed"}}
        result = cleanup.delete_local_fix_branch(request({**self.common, "conduction": guards}))
        self.assertEqual(result["reason"], "foreign_branch_ownership")
        self.assertTrue(run_git(self.clone, "branch", "--list", self.branch))


if __name__ == "__main__":
    unittest.main()
