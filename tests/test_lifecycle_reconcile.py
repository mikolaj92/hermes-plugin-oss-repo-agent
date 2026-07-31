from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lokay.steps import cleanup_reconcile
from lokay.adapters_cli import CommandError
from unittest import mock


class LifecycleReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.claim = root / "claim-owner_repo-10.json"
        self.claim.write_text(json.dumps({"version": 1, "repo": "owner/repo", "issue": 10, "board": "owner-repo", "assignee": "owner", "claimedAt": "2026-01-01T00:00:00Z"}), encoding="utf-8")
        self.claim.chmod(0o600)
        self.data = {
            "repo": "owner/repo", "issue": 10, "pr_number": 11,
            "branch": "ai/fix/10-recover", "head_oid": "abc123",
            "claim_path": str(self.claim), "task_receipt_path": str(root / "task.json"),
            "receipt_path": str(root / "receipt.json"), "worktree_path": str(root / "missing"),
            "db_path": str(root / "missing.sqlite"), "dry_run": False,
        }
        self.config = {"repo": "owner/repo", "claim_root": str(root)}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _conduction(self, *, state="OPEN", checks=None, linked=True, open_prs=None):
        pr = {
            "number": 11, "state": state, "headRefName": self.data["branch"],
            "headRefOid": self.data["head_oid"], "baseRefName": "main",
            "closingIssuesReferences": [{"number": 10}] if linked else [],
            "statusCheckRollup": checks if checks is not None else [{"state": "COMPLETED", "conclusion": "FAILURE"}],
        }
        gh = {"repo": "owner/repo", "issue": {"number": 10, "state": "OPEN"}, "pr": pr,
              "open_prs": open_prs if open_prs is not None else ([pr] if state == "OPEN" else []),
              "linked_issue_numbers": [10] if linked else [], "checks_state": cleanup_reconcile._lifecycle_check_state(pr)}
        return {"read_lifecycle_github_state": gh, "read_lifecycle_local_evidence": self._local()}

    def _local(self):
        present = self.claim.exists()
        return {"claim_paths": [str(self.claim)] if present else [], "claim_present": present,
                "task_receipt": None, "receipt": None, "worktree_present": False, "active_leases": []}

    def _absent_conduction(self, local=None):
        return {"read_lifecycle_github_state": {"ok": True, "status": "read", "repo": "owner/repo",
                "issue": "NOT_FOUND", "pr": "NOT_FOUND", "issue_missing": True, "pr_missing": True,
                "missing_lifecycle": True, "open_prs": [], "linked_issue_numbers": [], "checks_state": "pending"},
                "read_lifecycle_local_evidence": local if local is not None else self._local()}

    def _decide(self, conduction):
        return cleanup_reconcile.decide_lifecycle_transition({"input": self.data | {"conduction": conduction}, "config": self.config})

    def test_lifecycle_readers_propagate_no_selected_pr(self):
        conduction = {
            "triage_load_pr_fields": {"ok": True, "status": "noop", "reason": "no_open_prs", "mutated": False},
            "triage_decide_triage_action": {"ok": True, "status": "noop", "reason": "no_open_prs", "mutated": False},
        }
        request = {"input": {"conduction": conduction}, "config": self.config}
        with mock.patch.object(cleanup_reconcile, "run_cmd") as run_cmd:
            github = cleanup_reconcile.read_lifecycle_github_state(request)
            local = cleanup_reconcile.read_lifecycle_local_evidence(request)
        self.assertEqual((github["status"], github["reason"]), ("noop", "no_open_prs"))
        self.assertEqual((local["status"], local["reason"]), ("noop", "no_open_prs"))
        run_cmd.assert_not_called()

    def test_lifecycle_readers_recover_one_claimed_merged_receipt(self):
        root = Path(self.temp.name)
        receipts = root / "merges"
        receipts.mkdir()
        payload = {"phase": "MERGED", "repo": "owner/repo", "pr": 11, "headSha": "abc123",
            "verified_provenance": {"source": "github_pr_readback", "state": "MERGED", "repo": "owner/repo",
                "number": 11, "head_ref": "ai/fix/10-recover", "head_oid": "abc123"}}
        (receipts / "merged.json").write_text(json.dumps(payload), encoding="utf-8")
        (receipts / "merged.json").chmod(0o600)
        stale = receipts / "stale-issue.json"
        stale.write_text(json.dumps({"phase": "MERGED", "repo": "owner/repo", "pr": 99, "headSha": "def456",
            "verified_provenance": {"source": "github_pr_readback", "state": "MERGED", "repo": "owner/repo",
                "number": 99, "head_ref": "ai/fix/9-stale", "head_oid": "def456"}}), encoding="utf-8")
        stale.chmod(0o600)
        unrelated = receipts / "unrelated-malformed.json"
        unrelated.write_text(json.dumps({"phase": "MERGED", "repo": "owner/repo", "pr": "invalid",
            "verified_provenance": {"source": "github_pr_readback", "state": "MERGED", "repo": "owner/repo",
                "number": "invalid", "head_ref": "ai/fix/8-unrelated", "head_oid": ""}}), encoding="utf-8")
        unrelated.chmod(0o600)
        conduction = {
            "triage_load_pr_fields": {"ok": True, "status": "noop", "reason": "no_open_prs", "mutated": False},
            "triage_decide_triage_action": {"ok": True, "status": "noop", "reason": "no_open_prs", "mutated": False},
        }
        request = {"input": {"conduction": conduction, "merge_receipts": str(receipts), "claim_path": str(self.claim)}, "config": self.config}
        responses = [
            {"number": 10, "state": "CLOSED"},
            {"number": 11, "state": "MERGED", "headRefName": "ai/fix/10-recover", "headRefOid": "abc123",
                "closingIssuesReferences": [{"number": 10}], "statusCheckRollup": []},
            [],
        ]
        with mock.patch.object(cleanup_reconcile, "run_cmd", side_effect=[mock.Mock(stdout=json.dumps(value)) for value in responses]):
            github = cleanup_reconcile.read_lifecycle_github_state(request)
        self.assertEqual((github["status"], github["repo"], github["pr_number"]), ("read", "owner/repo", 11))
        result = self._decide({"read_lifecycle_github_state": github, "read_lifecycle_local_evidence": self._local()})
        self.assertEqual(result["outcome"], "finalize_merged")

    def test_lifecycle_durable_context_deduplicates_republished_identity(self):
        root = Path(self.temp.name)
        receipts = root / "merges"
        receipts.mkdir()
        payload = {"phase": "MERGED", "repo": "owner/repo", "pr": 11, "headSha": "abc123",
            "verified_provenance": {"source": "github_pr_readback", "state": "MERGED", "repo": "owner/repo",
                "number": 11, "head_ref": "ai/fix/10-recover", "head_oid": "abc123"}}
        for name in ("one.json", "two.json"):
            path = receipts / name
            path.write_text(json.dumps(payload), encoding="utf-8")
            path.chmod(0o600)
        (receipts / "stale.json").write_text("not-json", encoding="utf-8")
        conduction = {
            "triage_load_pr_fields": {"ok": True, "status": "noop", "reason": "no_open_prs"},
            "triage_decide_triage_action": {"ok": True, "status": "noop", "reason": "no_open_prs"},
        }
        result = cleanup_reconcile._resolve_lifecycle_context({"input": {
            "conduction": conduction, "merge_receipts": str(receipts), "claim_path": str(self.claim)}, "config": self.config})
        self.assertEqual((result["status"], result["repo"], result["issue"], result["pr_number"]),
            ("resolved", "owner/repo", 10, 11))
    def test_lifecycle_durable_context_wins_over_residual_open_pr(self):
        root = Path(self.temp.name)
        claim_dir = root / "active"
        claim_dir.mkdir()
        claim_path = claim_dir / "claim.json"
        claim_path.write_text(json.dumps({
            "version": 1,
            "repo": "owner/repo",
            "issue": 10,
            "board": "owner-repo",
            "assignee": "owner",
            "claimedAt": "2026-01-01T00:00:00Z",
        }), encoding="utf-8")
        claim_path.chmod(0o600)
        receipts = root / "merges"
        receipts.mkdir()
        payload = {
            "phase": "MERGED",
            "repo": "owner/repo",
            "pr": 11,
            "headSha": "abc123",
            "verified_provenance": {
                "source": "github_pr_readback",
                "state": "MERGED",
                "repo": "owner/repo",
                "number": 11,
                "head_ref": "ai/fix/10-recover",
                "head_oid": "abc123",
            },
        }
        (receipts / "merged.json").write_text(json.dumps(payload), encoding="utf-8")
        (receipts / "merged.json").chmod(0o600)
        residual = {
            "number": 99,
            "state": "OPEN",
            "headRefName": "ai/fix/10-residual",
            "headRefOid": "residual-head",
            "closingIssuesReferences": [{"number": 10}],
        }
        conduction = {
            "triage_load_pr_fields": {
                "ok": True,
                "status": "loaded",
                "repo": "owner/repo",
                "pr": residual,
                "mutated": False,
            },
            "triage_decide_triage_action": {
                "ok": True,
                "status": "decided",
                "action": "repair",
                "reason": "missing_test_evidence",
                "repo": "owner/repo",
                "pr": residual,
                "mutated": False,
            },
        }
        request = {
            "input": {
                "conduction": conduction,
                "claim_root": str(claim_dir),
                "active_issue_path": str(claim_dir),
                "merge_receipts": str(receipts),
            },
            "config": {},
        }
        result = cleanup_reconcile._resolve_lifecycle_context(request)
        self.assertEqual(
            (result["status"], result["repo"], result["issue"], result["pr_number"], result["branch"], result["head_oid"]),
            ("resolved", "owner/repo", 10, 11, "ai/fix/10-recover", "abc123"),
        )
        self.assertTrue(result.get("durable_merged"))
        later_payload = {
            "phase": "MERGED",
            "repo": "owner/repo",
            "pr": 99,
            "headSha": "residual-head",
            "mergedAt": "2026-01-02T00:00:00Z",
            "verified_provenance": {
                "source": "github_pr_readback",
                "state": "MERGED",
                "repo": "owner/repo",
                "number": 99,
                "head_ref": "ai/fix/10-residual",
                "head_oid": "residual-head",
                "merged_at": "2026-01-02T00:00:00Z",
            },
        }
        payload["mergedAt"] = "2026-01-01T00:00:00Z"
        payload["verified_provenance"]["merged_at"] = "2026-01-01T00:00:00Z"
        (receipts / "merged.json").write_text(json.dumps(payload), encoding="utf-8")
        (receipts / "later-residual.json").write_text(json.dumps(later_payload), encoding="utf-8")
        result = cleanup_reconcile._resolve_lifecycle_context(request)
        self.assertEqual((result["ok"], result["reason"], result["count"]), (False, "lifecycle_durable_context_ambiguous", 2))
        (receipts / "later-residual.json").unlink()

        responses = [
            {"number": 10, "state": "CLOSED"},
            {
                "number": 11,
                "state": "MERGED",
                "headRefName": "ai/fix/10-recover",
                "headRefOid": "abc123",
                "closingIssuesReferences": [{"number": 10}],
                "statusCheckRollup": [],
            },
            [],
        ]
        with mock.patch.object(
            cleanup_reconcile,
            "run_cmd",
            side_effect=[mock.Mock(stdout=json.dumps(value)) for value in responses],
        ):
            github = cleanup_reconcile.read_lifecycle_github_state(request)
            local = cleanup_reconcile.read_lifecycle_local_evidence(request)
        self.assertEqual((github["status"], github["pr_number"]), ("read", 11))
        self.assertEqual(str(github["pr"].get("headRefName") or ""), "ai/fix/10-recover")
        self.assertTrue(local["claim_present"])
        self.assertEqual([str(Path(path).resolve()) for path in local["claim_paths"]], [str(claim_path.resolve())])
        decision = cleanup_reconcile.decide_lifecycle_transition({
            "input": {
                "conduction": {
                    "read_lifecycle_github_state": github,
                    "read_lifecycle_local_evidence": local,
                }
            },
            "config": {},
        })
        self.assertEqual(decision["outcome"], "finalize_merged")
        self.assertEqual(decision["identity"]["pr_number"], 11)

    def test_lifecycle_local_evidence_reads_top_level_claim_root(self):
        root = Path(self.temp.name)
        claim_dir = root / "active-flat"
        claim_dir.mkdir()
        claim_path = claim_dir / "claim.json"
        claim_path.write_text(json.dumps({
            "version": 1,
            "repo": "owner/repo",
            "issue": 10,
            "board": "owner-repo",
            "assignee": "owner",
            "claimedAt": "2026-01-01T00:00:00Z",
        }), encoding="utf-8")
        claim_path.chmod(0o600)
        request = {
            "input": self.data | {
                "claim_root": str(claim_dir),
                "claim_path": None,
                "conduction": {
                    "triage_load_pr_fields": {
                        "ok": True,
                        "status": "loaded",
                        "repo": "owner/repo",
                        "pr": {
                            "number": 11,
                            "headRefName": self.data["branch"],
                            "headRefOid": self.data["head_oid"],
                            "closingIssuesReferences": [{"number": 10}],
                        },
                    },
                    "triage_decide_triage_action": {
                        "ok": True,
                        "status": "decided",
                        "action": "merge",
                        "repo": "owner/repo",
                        "pr": {
                            "number": 11,
                            "headRefName": self.data["branch"],
                            "headRefOid": self.data["head_oid"],
                            "closingIssuesReferences": [{"number": 10}],
                        },
                    },
                },
            },
            "config": {},
        }
        local = cleanup_reconcile.read_lifecycle_local_evidence(request)
        self.assertEqual(local["status"], "read")
        self.assertTrue(local["claim_present"])
        self.assertEqual([str(Path(path).resolve()) for path in local["claim_paths"]], [str(claim_path.resolve())])

    def test_lifecycle_context_ignores_one_noop_when_peer_has_identity(self):
        selected = {
            "number": 11,
            "headRefName": self.data["branch"],
            "headRefOid": self.data["head_oid"],
            "closingIssuesReferences": [{"number": 10}],
        }
        conduction = {
            "triage_load_pr_fields": {"ok": True, "status": "noop", "reason": "unrelated_branch", "mutated": False},
            "triage_decide_triage_action": {"ok": True, "status": "decided", "action": "merge", "repo": "owner/repo", "pr": selected},
        }
        result = cleanup_reconcile._resolve_lifecycle_context({"input": {"conduction": conduction}, "config": self.config})
        self.assertEqual((result["status"], result["repo"], result["issue"], result["pr_number"]), ("resolved", "owner/repo", 10, 11))

    def test_lifecycle_decision_propagates_inactive_readers(self):
        conduction = {
            "lifecycle_read_lifecycle_github_state": {"ok": True, "status": "noop", "reason": "no_open_prs"},
            "lifecycle_read_lifecycle_local_evidence": {"ok": True, "status": "noop", "reason": "no_open_prs"},
        }
        result = cleanup_reconcile.decide_lifecycle_transition({"input": {"conduction": conduction}, "config": self.config})
        self.assertEqual((result["status"], result["reason"]), ("noop", "no_open_prs"))

    def test_failed_open_pr_resumes_repair_without_label_mutation(self):
        result = self._decide(self._conduction())
        self.assertEqual(result["outcome"], "resume_repair")
        self.assertFalse(result["mutated"])

    def test_pending_checks_wait(self):
        result = self._decide(self._conduction(checks=[{"state": "IN_PROGRESS", "conclusion": ""}]))
        self.assertEqual(result["outcome"], "wait_pending_checks")
        self.assertFalse(result["mutated"])
    def test_green_checks_continue_to_merge(self):
        result = self._decide(self._conduction(checks=[{"state": "COMPLETED", "conclusion": "SUCCESS"}]))
        self.assertEqual(result["outcome"], "ready_for_merge")
        self.assertFalse(result["mutated"])
    def test_missing_evidence_resumes_repair_only_after_checks_pass(self):
        decision = {
            "ok": True,
            "status": "decided",
            "action": "repair",
            "reason": "missing_test_evidence",
        }
        passed = self._conduction(
            checks=[{"state": "COMPLETED", "conclusion": "SUCCESS"}]
        )
        passed["triage_decide_triage_action"] = decision
        result = self._decide(passed)
        self.assertEqual(result["outcome"], "resume_repair")
        self.assertEqual(result["repair_reason"], "missing_test_evidence")

        pending = self._conduction(
            checks=[{"state": "IN_PROGRESS", "conclusion": ""}]
        )
        pending["triage_decide_triage_action"] = decision
        self.assertEqual(self._decide(pending)["outcome"], "wait_pending_checks")

    def test_missing_evidence_resumes_repair_without_configured_checks(self):
        decision = {
            "ok": True,
            "status": "decided",
            "action": "repair",
            "reason": "missing_test_evidence",
        }
        no_checks = self._conduction(checks=[])
        no_checks["triage_decide_triage_action"] = decision
        request = {
            "input": self.data | {"require_checks": False, "conduction": no_checks},
            "config": self.config | {"require_test_evidence": True},
        }
        result = cleanup_reconcile.decide_lifecycle_transition(request)
        self.assertEqual(result["outcome"], "resume_repair")
        self.assertEqual(result["repair_reason"], "missing_test_evidence")

    def test_pending_checks_still_wait_when_checks_are_not_required(self):
        pending = self._conduction(checks=[{"state": "IN_PROGRESS", "conclusion": ""}])
        request = {
            "input": self.data | {"require_checks": False, "conduction": pending},
            "config": self.config | {"require_test_evidence": True},
        }
        self.assertEqual(cleanup_reconcile.decide_lifecycle_transition(request)["outcome"], "wait_pending_checks")



    def test_merged_and_closed_are_finalized(self):
        self.assertEqual(self._decide(self._conduction(state="MERGED", checks=[{"state": "COMPLETED", "conclusion": "SUCCESS"}]))["outcome"], "finalize_merged")
        self.assertEqual(self._decide(self._conduction(state="CLOSED", checks=[{"state": "COMPLETED", "conclusion": "FAILURE"}]))["outcome"], "finalize_closed")

    def test_identity_conflict_is_terminal_and_does_not_mutate(self):
        result = cleanup_reconcile.decide_lifecycle_transition({"input": self.data | {"head_oid": "wrong-head", "conduction": self._conduction()}, "config": self.config})
        self.assertEqual(result["reason"], "lifecycle_identity_conflict")
        self.assertFalse(result["mutated"])

    def test_graphql_issue_not_found_is_authoritative(self):
        marker = "Could not resolve to an issue with the number of 10."
        self.assertTrue(
            cleanup_reconcile._github_lifecycle_not_found(
                CommandError(["gh"], 1, "", marker), "issue"
            )
        )

    def test_graphql_pullrequest_not_found_is_authoritative(self):
        marker = "Could not resolve to a PullRequest with the number of 11."
        self.assertTrue(
            cleanup_reconcile._github_lifecycle_not_found(
                CommandError(["gh"], 1, "", marker), "pr"
            )
        )

    def test_github_reader_prefers_input_cli(self):
        commands: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            commands.append(list(cmd))
            if cmd[1:3] == ["issue", "view"]:
                payload = {"number": 10, "state": "OPEN", "labels": [], "assignees": []}
            elif cmd[1:3] == ["pr", "view"]:
                payload = {
                    "number": 11,
                    "state": "OPEN",
                    "headRefName": self.data["branch"],
                    "headRefOid": self.data["head_oid"],
                    "baseRefName": "main",
                    "closingIssuesReferences": [{"number": 10}],
                    "statusCheckRollup": [],
                }
            else:
                payload = []
            return mock.Mock(stdout=json.dumps(payload))

        request = {
            "input": self.data | {"gh_cli": "input-gh"},
            "config": self.config | {"gh_cli": "config-gh"},
        }
        with mock.patch("lokay.steps.cleanup_reconcile.run_cmd", side_effect=fake_run):
            result = cleanup_reconcile.read_lifecycle_github_state(request)
        self.assertEqual(result["status"], "read")
        self.assertTrue(commands)
        self.assertTrue(all(command[0] == "input-gh" for command in commands))

    def test_unrelated_missing_error_is_retryable_and_cannot_delete_claim(self):
        error = CommandError(["gh"], 1, "", "missing authentication token")
        with mock.patch("lokay.steps.cleanup_reconcile.run_cmd", side_effect=error):
            result = cleanup_reconcile.read_lifecycle_github_state(
                {"input": self.data, "config": self.config}
            )
        self.assertEqual(result["reason"], "lifecycle_github_state_read_failed")
        self.assertEqual(result["failure_class"], "retryable_read")
        self.assertTrue(result["retry_safe"])
        self.assertTrue(self.claim.exists())

    def test_missing_lifecycle_decides_release_then_fresh_absence_is_idempotent(self):
        conduction = self._absent_conduction()
        decision = self._decide(conduction)
        self.assertEqual(decision["outcome"], "release_orphan")
        conduction["decide_lifecycle_transition"] = decision
        released = cleanup_reconcile.release_orphan_claim({"input": self.data | {"conduction": conduction}, "config": self.config})
        self.assertEqual(released["status"], "released")
        self.assertFalse(self.claim.exists())
        fresh = self._absent_conduction(local=self._local())
        fresh_decision = self._decide(fresh)
        self.assertEqual(fresh_decision["outcome"], "already_absent")
        fresh["decide_lifecycle_transition"] = fresh_decision
        rerun = cleanup_reconcile.release_orphan_claim({"input": self.data | {"conduction": fresh}, "config": self.config})
        self.assertEqual(rerun["outcome"], "already_absent")

    def test_missing_lifecycle_with_receipt_conflict_fails_without_deletion(self):
        local = self._local()
        local["receipt"] = {"outcome": "resume_repair"}
        result = self._decide(self._absent_conduction(local=local))
        self.assertEqual(result["reason"], "lifecycle_state_conflict")
        self.assertTrue(self.claim.exists())

    def test_release_orphan_claim_with_absent_top_level_input_success(self):
        data = self.data.copy()
        data.pop("repo", None)
        data.pop("issue", None)
        conduction = self._absent_conduction()
        decision = self._decide(conduction)
        self.assertEqual(decision["repo"], "owner/repo")
        self.assertEqual(decision["issue"], 10)
        conduction["decide_lifecycle_transition"] = decision
        released = cleanup_reconcile.release_orphan_claim({"input": data | {"conduction": conduction}, "config": self.config})
        self.assertEqual(released["status"], "released")
        self.assertFalse(self.claim.exists())

    def test_release_orphan_claim_disagreement_decision_vs_local_fails(self):
        data = self.data.copy()
        data.pop("repo", None)
        data.pop("issue", None)
        conduction = self._absent_conduction()
        decision = self._decide(conduction)
        local = conduction["read_lifecycle_local_evidence"].copy()
        local["repo"] = "other/repo"
        conduction["read_lifecycle_local_evidence"] = local
        conduction["decide_lifecycle_transition"] = decision
        released = cleanup_reconcile.release_orphan_claim({"input": data | {"conduction": conduction}, "config": self.config})
        self.assertEqual(released["status"], "failed")
        self.assertEqual(released["reason"], "lifecycle_identity_conflict")
        self.assertEqual(released["field"], "repo")
        self.assertFalse(released["mutated"])
        self.assertTrue(self.claim.exists())

    def test_release_orphan_claim_disagreement_claim_vs_evidence_fails(self):
        data = self.data.copy()
        data.pop("repo", None)
        data.pop("issue", None)
        conduction = self._absent_conduction()
        decision = self._decide(conduction)
        self.claim.write_text(json.dumps({"repo": "owner/repo", "issue": 99}), encoding="utf-8")
        conduction["decide_lifecycle_transition"] = decision
        released = cleanup_reconcile.release_orphan_claim({"input": data | {"conduction": conduction}, "config": self.config})
        self.assertEqual(released["status"], "failed")
        self.assertEqual(released["reason"], "lifecycle_identity_conflict")
        self.assertEqual(released["field"], "claim")
        self.assertFalse(released["mutated"])
        self.assertTrue(self.claim.exists())

    def test_missing_lifecycle_reader_retains_numeric_identity(self):
        def cmd_side_effect(args, **kwargs):
            if "issue" in args and "view" in args:
                raise CommandError(args, 1, "", "Could not resolve to an issue with the number of 10.")
            if "pr" in args and "view" in args:
                raise CommandError(args, 1, "", "Could not resolve to a PullRequest with the number of 11.")
            return mock.MagicMock()
        with mock.patch("lokay.steps.cleanup_reconcile.run_cmd", side_effect=cmd_side_effect):
            result = cleanup_reconcile.read_lifecycle_github_state({"input": self.data, "config": self.config})
        self.assertEqual(result["status"], "read")
        self.assertTrue(result["missing_lifecycle"])
        self.assertEqual(result["issue"], "NOT_FOUND")
        self.assertEqual(result["pr"], "NOT_FOUND")
        self.assertEqual(result["requested_issue"], 10)
        self.assertEqual(result["requested_pr"], 11)
        self.assertEqual(result["issue_number"], 10)
        self.assertEqual(result["pr_number"], 11)

    def test_verify_non_orphan_release_is_skipped_without_path_check(self):
        configured_directory = str(Path(self.temp.name))
        result = cleanup_reconcile.verify_orphan_claim_release(
            {
                "input": {
                    "claim_path": configured_directory,
                    "conduction": {
                        "release_orphan_claim": {
                            "ok": True,
                            "status": "skipped",
                            "outcome": "not_orphan",
                            "mutated": False,
                        }
                    },
                },
                "config": self.config,
            }
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["outcome"], "not_orphan")
        self.assertFalse(result["mutated"])


if __name__ == "__main__":
    unittest.main()
