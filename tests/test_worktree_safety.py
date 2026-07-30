from __future__ import annotations

import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from lokay.steps import cleanup, issue_to_pr, orchestration


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

    def test_terminal_lifecycle_runs_composed_cleanup_through_branch_absence(self) -> None:
        prepared = self.prepare()
        self.assertTrue(prepared["ok"], prepared)
        wt = self.worktrees / self.branch
        head = run_git(self.clone, "rev-parse", self.branch)
        provenance = {"source": "github_pr_readback", "repo": self.identity["repo"], "number": 8, "head_ref": self.branch, "head_oid": head}
        lifecycle_identity = {"repo": self.identity["repo"], "issue": self.identity["issue"], "pr_number": 8, "branch": self.branch, "head_oid": head}
        aggregate = orchestration.aggregate_lane_results(request({"conduction": {
            "triage_verify_merge_receipt": {"status": "merge_receipt_verified", "ok": True, "verified_provenance": provenance},
            "triage_decide_triage_action": {"status": "verified", "ok": True, "board": "b", "clone_path": str(self.clone), "priority": 1, **lifecycle_identity},
            "lifecycle_decide_lifecycle_transition": {"status": "decided", "ok": True, "outcome": "finalize_merged", "identity": lifecycle_identity},
        }}))
        self.assertTrue(aggregate["cleanup_authorized"], aggregate)
        conduction = {"aggregate_lane_results": aggregate}
        base = {"worktree_root": str(self.worktrees), "dry_run": False}
        for atom in ("resolve_cleanup_branch_source", "parse_cleanup_issue_number", "read_branch_ownership", "derive_cleanup_paths", "validate_cleanup_identity"):
            result = getattr(cleanup, atom)(request({**base, "conduction": conduction}))
            self.assertTrue(result["ok"], result)
            conduction[atom] = result
        closed = mock.Mock(stdout='{"state":"CLOSED"}')
        no_prs = mock.Mock(stdout="[]")
        with mock.patch.object(cleanup, "run_cmd", side_effect=[closed, no_prs]):
            for atom in ("check_issue_closed", "check_no_open_pr_for_branch"):
                result = getattr(cleanup, atom)(request({**base, "conduction": conduction}))
                self.assertTrue(result["ok"], result)
                conduction[atom] = result
        for atom in ("verify_cleanup_guards", "read_worktree_ownership", "read_worktree_cleanliness", "remove_worktree", "verify_worktree_absent", "verify_branch_delete_guards", "read_local_branch_ownership", "delete_local_branch", "verify_local_branch_absent"):
            result = getattr(cleanup, atom)(request({**base, "conduction": conduction}))
            self.assertTrue(result["ok"], result)
            conduction[atom] = result
        self.assertEqual(conduction["verify_worktree_absent"]["status"], "verified")
        self.assertEqual(conduction["verify_local_branch_absent"]["status"], "verified")
        self.assertFalse(wt.exists())
        self.assertFalse(run_git(self.clone, "branch", "--list", self.branch))

    def test_composed_cleanup_preserves_branch_advanced_past_recorded_head(self) -> None:
        prepared = self.prepare()
        self.assertTrue(prepared["ok"], prepared)
        recorded = run_git(self.clone, "rev-parse", self.branch)
        worktree = self.worktrees / self.branch
        (worktree / "later.txt").write_text("later\n")
        run_git(worktree, "add", "later.txt")
        run_git(worktree, "commit", "-m", "later")
        actual = run_git(self.clone, "rev-parse", self.branch)
        identity = {"repo": self.identity["repo"], "issue": self.identity["issue"], "pr_number": 8, "branch": self.branch, "local_branch": self.branch, "clone_path": str(self.clone), "worktree_path": str(worktree), "task": self.identity["task_id"], "receipt": self.identity["receipt_path"], "local_oid": recorded, "remote_oid": actual, "target_branch": self.branch}
        conduction = {
            "aggregate_lane_results": {"ok": True, "status": "aggregated", "cleanup_authorized": True, "cleanup_identity": identity},
            "validate_cleanup_identity": {"ok": True, "status": "validated", "identity": identity},
            "verify_branch_delete_guards": {"ok": True, "status": "verified"},
        }
        result = cleanup.read_local_branch_ownership(request({"conduction": conduction}))
        self.assertEqual(result["reason"], "local_branch_head_mismatch")
        self.assertEqual(run_git(self.clone, "rev-parse", self.branch), actual)

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
        from lokay.adapters_git import branch_config_set
        run_git(self.clone, "branch", self.branch, "origin/main")
        for key, value in (("task", "task-7"), ("issue", "7"), ("receipt", self.identity["receipt_path"]), ("repo", "owner/repo")):
            branch_config_set(self.clone, self.branch, f"lokay-{key}", value)
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

    def test_owned_worktree_reconciles_new_run_receipt(self) -> None:
        first = self.prepare()
        self.assertEqual(first["status"], "verified", first)
        next_receipt = str(Path(self.tmp.name) / "next-run-receipt.json")
        second = self.prepare(receipt_path=next_receipt)
        self.assertEqual(second["status"], "verified", second)
        from lokay.adapters_git import branch_config_get
        self.assertEqual(branch_config_get(self.clone, self.branch, "lokay-receipt"), next_receipt)

    def test_reused_branch_forwards_worktree_inventory(self) -> None:
        self.assertEqual(self.prepare()["status"], "verified")
        retry = self.prepare(receipt_path=str(Path(self.tmp.name) / "retry-receipt.json"))
        self.assertEqual(retry["status"], "verified", retry)

    def test_owned_worktree_reuses_committed_head_after_omp(self) -> None:
        first = self.prepare()
        self.assertEqual(first["status"], "verified", first)
        worktree = self.worktrees / self.branch
        (worktree / "repair.txt").write_text("fixed\n")
        run_git(worktree, "add", "repair.txt")
        run_git(worktree, "commit", "-m", "repair")
        advanced_head = run_git(worktree, "rev-parse", "HEAD")
        retry = self.prepare(receipt_path=str(Path(self.tmp.name) / "retry-receipt.json"))
        self.assertEqual(retry["status"], "verified", retry)
        self.assertEqual(retry["head"], advanced_head)

    def test_reused_branch_overwrites_stale_input_with_observed_head(self) -> None:
        first = self.prepare()
        self.assertEqual(first["status"], "verified", first)
        worktree = self.worktrees / self.branch
        (worktree / "repair.txt").write_text("fixed\n")
        run_git(worktree, "add", "repair.txt")
        run_git(worktree, "commit", "-m", "repair")
        observed = run_git(worktree, "rev-parse", "HEAD")
        stale = "0" * 40
        values = {**self.common, "provenance": {"task": self.identity["task_id"], "issue": str(self.identity["issue"]), "receipt": self.common["receipt_path"], "repo": self.identity["repo"], "local-oid": stale}}
        prepared = prepare_chain(values)
        self.assertEqual(prepared["status"], "verified", prepared)
        from lokay.adapters_git import branch_config_get
        self.assertEqual(branch_config_get(self.clone, self.branch, "lokay-local-oid"), observed)

    def test_reused_branch_backfills_observed_local_oid(self) -> None:
        first = self.prepare()
        self.assertEqual(first["status"], "verified", first)
        from lokay.adapters_git import branch_config_get, branch_config_unset
        branch_config_unset(self.clone, self.branch, "lokay-local-oid")
        prepared = self.prepare()
        self.assertEqual(prepared["status"], "verified", prepared)
        self.assertEqual(branch_config_get(self.clone, self.branch, "lokay-local-oid"), run_git(self.clone, "rev-parse", self.branch))

    def test_empty_stored_receipt_is_not_adopted(self) -> None:
        self.assertEqual(self.prepare()["status"], "verified")
        from lokay.adapters_git import branch_config_set
        branch_config_set(self.clone, self.branch, "lokay-receipt", "")
        rejected = self.prepare(receipt_path=str(Path(self.tmp.name) / "retry-receipt.json"))
        self.assertEqual(rejected["reason"], "foreign_branch_ownership")


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

    def test_advanced_owned_worktree_preserves_original_base_for_omp(self) -> None:
        base = run_git(self.clone, "rev-parse", "main")
        prepared = self.prepare()
        self.assertEqual(prepared["status"], "verified", prepared)
        worktree = self.worktrees / self.branch
        (worktree / "fix.txt").write_text("fixed\n")
        run_git(worktree, "add", "fix.txt")
        run_git(worktree, "commit", "-m", "fix")
        head = run_git(worktree, "rev-parse", "HEAD")
        advanced = self.prepare()
        self.assertEqual(advanced["head"], head)
        self.assertEqual(advanced["base_head"], base)
        preconditions = issue_to_pr.read_omp_preconditions(request({**self.common, "conduction": {"dispatch_verify_worktree_head": advanced}}))
        with mock.patch("lokay.steps.issue_to_pr.run_omp") as run_omp:
            invoked = issue_to_pr.invoke_omp(request({**self.common, "conduction": {"dispatch_read_omp_preconditions": preconditions}}))
        run_omp.assert_not_called()
        self.assertEqual(invoked["status"], "reused")
        verified = issue_to_pr.verify_omp_postconditions(request({**self.common, "conduction": {"dispatch_invoke_omp": invoked, "dispatch_read_omp_preconditions": preconditions}}))
        self.assertEqual(verified["status"], "verified", verified)
        self.assertEqual(verified["head"], head)
        self.assertEqual(verified["base_head"], base)

    def test_advanced_owned_dirty_worktree_does_not_skip_omp(self) -> None:
        self.prepare()
        worktree = self.worktrees / self.branch
        (worktree / "fix.txt").write_text("fixed\n")
        run_git(worktree, "add", "fix.txt")
        run_git(worktree, "commit", "-m", "fix")
        advanced = self.prepare()
        (worktree / "leftover.txt").write_text("uncommitted\n")
        preconditions = issue_to_pr.read_omp_preconditions(request({**self.common, "conduction": {"dispatch_verify_worktree_head": advanced}}))
        with mock.patch("lokay.steps.issue_to_pr.run_omp") as run_omp:
            invoked = issue_to_pr.invoke_omp(request({**self.common, "conduction": {"dispatch_read_omp_preconditions": preconditions}}))
        run_omp.assert_not_called()
        self.assertEqual(invoked["reason"], "omp_worktree_dirty")
        self.assertFalse(invoked["mutated"])

    def test_diverged_owned_worktree_does_not_skip_omp(self) -> None:
        base = run_git(self.clone, "rev-parse", "main")
        self.prepare()
        worktree = self.worktrees / self.branch
        run_git(worktree, "checkout", "--orphan", "diverged")
        (worktree / "README").write_text("diverged\n")
        run_git(worktree, "add", "README")
        run_git(worktree, "commit", "-m", "diverged")
        diverged = run_git(worktree, "rev-parse", "HEAD")
        preconditions = {"status": "ready", "ok": True, "worktree_path": str(worktree), "branch": self.branch, "pre_head": diverged, "base_head": base, **self.identity}
        with mock.patch("lokay.steps.issue_to_pr.run_omp") as run_omp:
            invoked = issue_to_pr.invoke_omp(request({**self.common, "prompt": "fix", "conduction": {"dispatch_read_omp_preconditions": preconditions}}))
        run_omp.assert_not_called()
        self.assertEqual(invoked["reason"], "omp_branch_diverged")
        self.assertFalse(invoked["mutated"])

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
