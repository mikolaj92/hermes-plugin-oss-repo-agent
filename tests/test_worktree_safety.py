from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from lokay.steps import cleanup, issue_to_pr


WORKTREE_ATOMS = ("read_clone_preconditions", "fetch_clone_origin", "read_base_ref", "read_worktree_inventory", "read_branch_provenance", "create_local_branch", "write_branch_provenance", "add_worktree", "verify_worktree_head")
BRANCH_ATOMS = ("verify_branch_delete_guards", "read_local_branch_ownership", "delete_local_branch", "verify_local_branch_absent")


def prepare_chain(data: dict) -> dict:
    conduction = dict(data.get("conduction") or {})
    source = next((blob for name, blob in conduction.items() if isinstance(blob, dict) and ("parse_issue_ref" in name or "build_repair_prompt" in name)), {})
    branch = str(data.get("branch") or source.get("branch") or "")
    root = Path(str(data.get("worktree_root") or ".")).resolve()
    values_base = {**data, "branch": branch, "worktree_path": str(root / branch)}
    for atom in WORKTREE_ATOMS:
        values = {**values_base, "conduction": conduction}
        if atom == "write_branch_provenance":
            values["provenance"] = {"task": data.get("task_id") or source.get("task_id"), "issue": str(data.get("issue") or source.get("issue") or ""), "receipt": data.get("receipt_path") or source.get("receipt_id"), "repo": data.get("repo") or source.get("repo")}
        result = getattr(issue_to_pr, atom)(request(values))
        conduction[atom] = result
        if result.get("ok") is False:
            return result
    return conduction[WORKTREE_ATOMS[-1]]


def delete_chain(data: dict) -> dict:
    conduction = dict(data.get("conduction") or {})
    for atom in ("verify_cleanup_guards", *BRANCH_ATOMS):
        result = getattr(cleanup, atom)(request({**data, "conduction": conduction}))
        conduction[atom] = result
        if result.get("ok") is False:
            return result
    return conduction["verify_local_branch_absent"]


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
        data = {**self.common, **extra}
        conduction = data.pop("conduction", None)
        if conduction is None:
            conduction = {
                "dispatch_parse_issue_ref": {
                    "task_id": self.identity["task_id"],
                    "issue": self.identity["issue"],
                    "repo": self.identity["repo"],
                    "branch": self.branch,
                },
                "dispatch_write_dispatch_receipt": {
                    "receipt_path": self.identity["receipt_path"],
                },
            }
        data["conduction"] = conduction
        return prepare_chain(data)

    def test_create_and_cleanup_owned_worktree(self) -> None:
        prepared = self.prepare()
        self.assertTrue(prepared["ok"], prepared)
        wt = self.worktrees / self.branch
        self.assertEqual(prepared["status"], "verified", prepared)
        self.assertEqual(prepared["head"], run_git(self.clone, "rev-parse", "origin/main"))
        guards = {
            "check_issue_closed": {"ok": True, "closed": True},
            "check_no_open_pr_for_branch": {"ok": True, "safe_to_cleanup": True, "open_count": 0},
        }
        removed = cleanup.remove_worktree(request({**self.common, "worktree_path": str(wt), "conduction": {**guards, "verify_cleanup_guards": {"ok": True, "status": "verified"}, "read_worktree_ownership": {"ok": True, "status": "read", "clone_path": str(self.clone), "worktree_path": str(wt), "branch": self.branch}, "read_worktree_cleanliness": {"ok": True, "status": "checked", "clean": True, "dirty": False}}}))
        self.assertEqual(removed["status"], "removed", removed)
        deleted = delete_chain({**self.common, "worktree_path": str(wt), "conduction": {**guards, "remove_worktree": removed, "verify_worktree_absent": {"ok": True, "status": "verified", "absent": True}, "read_local_branch_ownership": {"ok": True, "status": "read", "exists": True, "owned": True}, "delete_local_branch": {"ok": True, "status": "deleted"}}})
        self.assertEqual(deleted["status"], "verified", deleted)
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
        self.assertEqual(result["reason"], "worktree_path_collision")
        self.assertTrue(path.exists())
        self.assertEqual(run_git(path, "branch", "--show-current"), "foreign/branch")

    def test_stale_existing_branch_fails_closed(self) -> None:
        run_git(self.clone, "branch", self.branch, "origin/main")
        for key, value in (("task", "task-7"), ("issue", "7"), ("receipt", self.identity["receipt_path"]), ("repo", "owner/repo")):
            run_git(self.clone, "config", f"branch.{self.branch}.lokay-{key}", value)
        (self.seed / "README").write_text("two\n")
        run_git(self.seed, "add", "README")
        run_git(self.seed, "commit", "-m", "two")
        run_git(self.seed, "push", "origin", "main")
        result = self.prepare()
        self.assertEqual(result["reason"], "branch_create_failed")
        self.assertFalse((self.worktrees / self.branch).exists())

    def test_foreign_branch_is_not_deleted(self) -> None:
        run_git(self.clone, "branch", self.branch, "origin/main")
        guards = {"check_issue_closed": {"ok": True, "closed": True}, "check_no_open_pr_for_branch": {"ok": True, "safe_to_cleanup": True, "open_count": 0}, "verify_cleanup_guards": {"ok": True, "status": "verified"}, "remove_worktree": {"ok": True, "status": "removed"}, "verify_worktree_absent": {"ok": True, "status": "verified", "absent": True}}
        result = delete_chain({**self.common, "conduction": guards})
        self.assertEqual(result["reason"], "foreign_branch_ownership")
        self.assertTrue(run_git(self.clone, "branch", "--list", self.branch))


    def test_triage_repair_reuses_only_exact_conduction_provenance(self) -> None:
        repair_conduction = {
            "build_repair_prompt": {
                "task_id": "review-task-7", "issue": "7", "receipt_id": str(Path(self.tmp.name) / "repair-receipt.json"), "repo": "owner/repo", "branch": self.branch,
            },
        }
        path_input = {"clone_path": str(self.clone), "worktree_root": str(self.worktrees), "base_branch": "main", "dry_run": False, "conduction": repair_conduction}
        first = prepare_chain(path_input)
        self.assertEqual(first["status"], "verified", first)
        second = prepare_chain(path_input)
        self.assertEqual(second["status"], "verified", second)

    def test_repair_provenance_missing_or_mismatched_fails_closed(self) -> None:
        base = {"clone_path": str(self.clone), "worktree_root": str(self.worktrees), "base_branch": "main", "dry_run": False, "conduction": {"build_repair_prompt": {"issue": "7", "receipt_id": str(Path(self.tmp.name) / "repair-receipt.json"), "repo": "owner/repo", "branch": self.branch}}}
        missing = prepare_chain(base)
        self.assertEqual(missing["operation"], "read_branch_provenance")
        owned = dict(base)
        owned["conduction"] = {"build_repair_prompt": {"task_id": "review-task-7", "issue": "7", "receipt_id": str(Path(self.tmp.name) / "repair-receipt.json"), "repo": "owner/repo", "branch": self.branch}}
        self.assertEqual(prepare_chain(owned)["status"], "verified")
        mismatched = dict(owned)
        mismatched["conduction"] = {"build_repair_prompt": {"task_id": "another-task", "issue": "7", "receipt_id": str(Path(self.tmp.name) / "repair-receipt.json"), "repo": "owner/repo", "branch": self.branch}}
        foreign = prepare_chain(mismatched)
        self.assertEqual(foreign["reason"], "foreign_branch_ownership")

    def test_live_shaped_repair_input_has_complete_provenance(self) -> None:
        request_data = {
            "clone_path": str(self.clone),
            "worktree_root": str(self.worktrees),
            "base_branch": "main",
            "repo": "owner/repo",
            "issue": "7",
            "receipt_path": str(Path(self.tmp.name) / "repair-receipt.json"),
            "dry_run": False,
            "conduction": {
                "build_repair_prompt": {
                    "task_id": "review-task-7",
                    "repo": "owner/repo",
                    "issue": "7",
                    "branch": self.branch,
                },
                "decide_triage_action": {"action": "repair", "status": "decided"},
            },
        }
        result = prepare_chain(request_data)
        self.assertEqual(result["status"], "verified", result)
        conflict = dict(request_data)
        conflict["conduction"] = {
            **request_data["conduction"],
            "repair_build_repair_prompt": {"task_id": "other-task"},
        }
        rejected = prepare_chain(conflict)
        self.assertEqual(rejected["reason"], "conflicting_worktree_provenance")
        self.assertFalse(rejected["mutated"])
if __name__ == "__main__":
    unittest.main()
