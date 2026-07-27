"""Unit tests for mega-atomic effectors — drive real shipped handlers."""

from __future__ import annotations

from types import SimpleNamespace

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lokay.adapters_cli import CommandError
from lokay.catalog import EFFECTORS, domains, load_all
from lokay.steps import claim, cleanup, issue_direction, issue_to_pr, kanban_intake, poll, repair, triage
from lokay.steps.claim import _reserve_claim


def req(input_data=None, config=None):
    return {"input": input_data or {}, "config": config or {}}

def triage_req(action, input_data=None, config=None):
    data = dict(input_data or {})
    data["conduction"] = {
        "decide_triage_action": {
            "ok": True,
            "status": "decided",
            "action": action,
            "reason": "test fixture",
        }
    }
    return req(data, config)


class EnvelopeContractTests(unittest.TestCase):
    def test_terminal_upstream_preserves_exact_peer_payload(self) -> None:
        from lokay.envelope import terminal_upstream

        for status in ("failed", "cancelled", "timed_out"):
            with self.subTest(status=status):
                peer = {
                    "status": status,
                    "ok": status != "failed",
                    "mutated": status == "timed_out",
                    "reason": "peer detail",
                    "failure_class": "retryable_read",
                    "retry_safe": True,
                }
                out = terminal_upstream(
                    req({"conduction": {"auto_worker_read_peer": peer}}),
                    "dependent_operation",
                    "read_peer",
                )
                self.assertIsNotNone(out)
                assert out is not None
                self.assertEqual(out["status"], "failed")
                self.assertEqual(out["operation"], "dependent_operation")
                self.assertEqual(out["upstream_effector"], "auto_worker_read_peer")
                self.assertEqual(out["upstream"], peer)

    def test_terminal_upstream_ignores_success_noop_and_missing_peer(self) -> None:
        from lokay.envelope import terminal_upstream

        for peer in (
            None,
            {"status": "ok", "ok": True, "mutated": False},
            {"status": "noop", "ok": True, "mutated": False, "reason": "none"},
        ):
            conduction = {} if peer is None else {"read_peer": peer}
            with self.subTest(peer=peer):
                self.assertIsNone(
                    terminal_upstream(
                        req({"conduction": conduction}),
                        "dependent_operation",
                        "read_peer",
                    )
                )

    def test_terminal_upstream_checks_all_declared_peers(self) -> None:
        from lokay.envelope import terminal_upstream

        failed = {"status": "failed", "ok": False, "mutated": False, "reason": "late"}
        out = terminal_upstream(
            req(
                {
                    "conduction": {
                        "poll": {"status": "ok", "ok": True, "mutated": False},
                        "decide": failed,
                    }
                }
            ),
            "claim",
            "poll",
            "decide",
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out["upstream_effector"], "decide")
        self.assertEqual(out["upstream"], failed)

    def test_reserve_stops_on_prefixed_terminal_peer(self) -> None:
        peer = {
            "status": "cancelled",
            "ok": True,
            "mutated": False,
            "reason": "cancelled upstream",
        }
        out = claim.reserve_claim_file(
            req({"conduction": {"auto_worker_intake_select_issue_candidate": peer}, "dry_run": False})
        )
        self.assertEqual(out["reason"], "upstream_failed")
        self.assertEqual(out["operation"], "reserve_claim_file")
        self.assertEqual(out["upstream_effector"], "auto_worker_intake_select_issue_candidate")
        self.assertEqual(out["upstream"], peer)


class CatalogTests(unittest.TestCase):
    def test_catalog_spans_all_domains(self) -> None:
        d = domains()
        for needed in ("intake", "issue_to_pr", "triage", "repair", "cleanup"):
            self.assertIn(needed, d)
        self.assertGreaterEqual(len(EFFECTORS), 30)
        # every ref loads
        loaded = load_all()
        self.assertEqual(len(loaded), len(EFFECTORS))
        for e in EFFECTORS:
            self.assertTrue(callable(loaded[e.id]), e.id)
        # skeptic-required bricks for composition
        for eid in (
            "check_worktree_dirty",
            "list_controlled_worktrees",
            "push_branch",
            "issue_to_pr_add_issue_label",
            "complete_task",
            "fetch_clone_origin",
            "add_worktree",
            "aggregate_pr_label_results",
            "select_fix_pr",
            "load_pr_fields",
            "assign_pr",
            "close_linked_issue",
            "publish_merge_receipt",
            "block_task",
            "check_issue_closed",
            "delete_local_branch",
        ):
            self.assertIn(eid, loaded)


class IntakeAlignedTests(unittest.TestCase):
    def _selected(self, number=3):
        return {"repo": "o/r", "board": "b", "number": number, "title": "t", "url": "u", "labels": ["ai:ready"], "assignees": []}

    def test_poll_success_and_filter(self) -> None:
        issues = [
            {"number": 1, "title": "a", "url": "u", "labels": [{"name": "ai:ready"}], "assignees": []},
            {"number": 2, "title": "b", "url": "u", "labels": [{"name": "ai:ready"}, {"name": "ai:blocked"}], "assignees": []},
        ]
        with mock.patch("lokay.steps.poll.gh_json", return_value=issues):
            read = poll.read_open_issues(req({"repo": "o/r", "board": "b", "dry_run": True}, {"assignee": "mikolaj92"}))
        self.assertEqual(read["status"], "read")
        normalized = poll.normalize_issue_rows(req({"issues": read["issues"], "repo": "o/r", "board": "b", "conduction": {"read_open_issues": read}, "dry_run": True}))
        filtered = poll.filter_issue_eligibility(req({"conduction": {"normalize_issue_rows": normalized}, "dry_run": True}, {"assignee": "mikolaj92", "ready_label": "ai:ready"}))
        out = poll.select_issue_candidate(req({"conduction": {"filter_issue_eligibility": filtered}, "dry_run": True}))
        self.assertTrue(out["ok"])
        self.assertEqual(out["eligible_count"], 1)
        self.assertEqual(out["selected"]["number"], 1)

    def test_claim_dry_run_and_noop(self) -> None:
        noop = claim.reserve_claim_file(req({"conduction": {"select_issue_candidate": {"status": "noop", "selected": None}}, "dry_run": True}))
        self.assertEqual(noop["status"], "noop")
        planned = claim.reserve_claim_file(req({"dry_run": True, "conduction": {"select_issue_candidate": {"selected": self._selected()}}}, {"assignee": "mikolaj92"}))
        self.assertEqual(planned["status"], "planned")
        self.assertFalse(planned["mutated"])

    def test_claim_rejects_bool_and_string_issue_values(self) -> None:
        for value in (True, "3"):
            with self.subTest(value=value):
                out = claim.reserve_claim_file(req({"dry_run": True, "selected": {"repo": "o/r", "board": "b", "number": value}}))
                self.assertEqual(out["reason"], "invalid_selected_issue")

    def test_claim_rejects_malformed_unrelated_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "other.json"
            path.write_text("not json", encoding="utf-8")
            out = claim.reserve_claim_file(req({"dry_run": False, "selected": self._selected()}, {"active_issue_path": tmp}))
            self.assertEqual(out["reason"], "claim_malformed")

    def test_claim_rejects_invalid_existing_identity_fields(self) -> None:
        invalid_claims = [
            {"version": 2, "repo": "o/r", "issue": 1, "board": "b", "assignee": "a", "claimedAt": "now"},
            {"version": 1, "repo": "", "issue": 1, "board": "b", "assignee": "a", "claimedAt": "now"},
            {"version": 1, "repo": "o/r", "issue": True, "board": "b", "assignee": "a", "claimedAt": "now"},
            {"version": 1, "repo": "o/r", "issue": 1, "board": "b", "assignee": "", "claimedAt": "now"},
            {"version": 1, "repo": "o/r", "issue": 1, "board": "b", "assignee": "a", "claimedAt": ""},
        ]
        for payload in invalid_claims:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "claim.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                out = claim.reserve_claim_file(req({"dry_run": False, "selected": self._selected(1)}, {"assignee": "a", "active_issue_path": path}))
                self.assertEqual(out["reason"], "claim_malformed")

    def test_claim_capacity_and_same_identity_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _reserve_claim(Path(tmp) / "first.json", repo="o/r", issue=1, board="b", assignee="a")
            out = claim.reserve_claim_file(req({"dry_run": False, "selected": self._selected(2)}, {"assignee": "a", "active_issue_path": tmp, "max_active_issues": 1}))
            self.assertEqual(out["reason"], "claim_busy")
            reused = claim.reserve_claim_file(req({"dry_run": False, "selected": self._selected(1)}, {"assignee": "a", "active_issue_path": tmp, "max_active_issues": 1}))
            self.assertTrue(reused["ok"])
            self.assertTrue(reused["reused"])

    def test_claim_reservation_failures_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claim.json"
            _reserve_claim(path, repo="o/r", issue=1, board="b", assignee="a")
            _, error, reused = _reserve_claim(path, repo="o/r", issue=2, board="b", assignee="a")
            self.assertEqual(error, "claim_busy")
            self.assertFalse(reused)
            with mock.patch("lokay.steps.claim.os.fsync", side_effect=OSError("disk full")):
                reserved, fsync_error, reused = _reserve_claim(Path(tmp) / "second.json", repo="o/r", issue=2, board="b", assignee="a")
            self.assertTrue(fsync_error.startswith("claim_uncertain:"))
            self.assertFalse(reused)
            self.assertIsNotNone(reserved)

    def test_claim_state_read_and_mutation_failures_attribute_exact_atom(self) -> None:
        selected = self._selected(3)
        reserve = {"status": "claim_reserved", "ok": True, "selected": selected}
        with mock.patch("lokay.steps.claim.run_cmd", side_effect=CommandError(["gh"], 1, "", "denied")):
            read = claim.read_issue_claim_state(req({"conduction": {"reserve_claim_file": reserve}, "selected": selected}, {"repo": "o/r"}))
        self.assertEqual(read["reason"], "claim_read_failed")
        self.assertEqual(read["failure_class"], "retryable_read")
        state = {"status": "claim_state_read", "ok": True, "repo": "o/r", "number": 3, "assignees": [], "labels": []}
        with mock.patch("lokay.steps.claim.run_cmd", side_effect=CommandError(["gh"], 1, "", "denied")):
            failed = claim.assign_issue(req({"conduction": {"reserve_claim_file": reserve, "read_issue_claim_state": state}, "selected": selected, "dry_run": False}, {"assignee": "a"}))
        self.assertEqual(failed["reason"], "assign_issue_failed")
        self.assertEqual(failed["failure_class"], "reconcile_then_retry")
        self.assertTrue(failed["mutated"])

    def test_kanban_dry_run_chain(self) -> None:
        selected = self._selected(1)
        claim_result = {"status": "claimed", "ok": True, "selected": selected}
        with mock.patch("lokay.steps.kanban_intake.hermes_kanban_json", return_value=[]):
            read = kanban_intake.read_intake_tasks(req({"conduction": {"build_issue_claim_result": claim_result}, "selected": selected, "dry_run": True}))
        found = kanban_intake.find_intake_marker(req({"conduction": {"read_intake_tasks": read}, "dry_run": True}))
        out = kanban_intake.create_intake_task(req({"conduction": {"find_intake_marker": found}, "selected": selected, "dry_run": True}))
        self.assertEqual(out["status"], "planned")
        self.assertEqual(out["idempotency_key"], "github-issue:o/r:1")


class IssueToPrTests(unittest.TestCase):
    def test_parse_issue_ref_from_task(self) -> None:
        out = issue_to_pr.parse_issue_ref_from_task(
            req({"task": {"title": "[issue] acme/app#42: fix crash", "id": "t1"}}, {"branch_prefix": "ai/fix"})
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["repo"], "acme/app")
        self.assertEqual(out["issue"], 42)
        self.assertTrue(out["branch"].startswith("ai/fix/42-"))

    def test_dispatch_read_select_chain(self) -> None:
        rows = [{"id": "t1", "title": "[fix-pr] o/r#9", "status": "ready", "repo": "o/r"}]
        with mock.patch("lokay.steps.issue_to_pr.hermes_kanban_json", return_value=rows):
            read = issue_to_pr.read_dispatch_tasks(req({"board": "b"}, {}))
        self.assertEqual(read["status"], "read")
        selected = issue_to_pr.select_dispatch_task(req({"conduction": {"read_dispatch_tasks": read}}))
        self.assertEqual(selected["status"], "selected")
        self.assertEqual(selected["task_id"], "t1")

    def test_dispatch_selection_noop_and_failure_gate(self) -> None:
        noop = issue_to_pr.select_dispatch_task(
            req({"conduction": {"read_dispatch_tasks": {"status": "ok", "ok": True, "tasks": [{"id": "done", "status": "done"}]}}})
        )
        self.assertEqual(noop["status"], "noop")
        failed = {"status": "failed", "ok": False, "reason": "read_failed", "mutated": False}
        out = issue_to_pr.select_dispatch_task(req({"conduction": {"read_dispatch_tasks": failed}}))
        self.assertEqual(out["reason"], "upstream_failed")
        self.assertEqual(out["upstream"], failed)
        self.assertEqual(out["upstream_effector"], "read_dispatch_tasks")

    def test_fix_task_creation_dry_chain(self) -> None:
        found = {"status": "absent", "ok": True, "task": None, "marker": "fix-pr:o/r:9"}
        out = issue_to_pr.create_fix_task(
            req({"board": "b", "repo": "o/r", "issue": 9, "dry_run": True, "conduction": {"find_fix_task_marker": found}})
        )
        self.assertEqual(out["status"], "planned")
        self.assertEqual(out["idempotency_key"], "fix-pr:o/r:9")

    def test_completion_chain_dry_and_failed_predecessor(self) -> None:
        read = {"status": "read", "ok": True, "task_id": "t1", "task": {"id": "t1", "status": "ready"}}
        decision = issue_to_pr.decide_task_completion(req({"conduction": {"read_task_for_completion": read}}))
        self.assertTrue(decision["should_complete"])
        out = issue_to_pr.complete_task(req({"task_id": "t1", "board": "b", "dry_run": True, "conduction": {"decide_task_completion": decision}}))
        self.assertEqual(out["status"], "planned")
        failed = {"status": "failed", "ok": False, "reason": "decision_failed", "mutated": False}
        blocked = issue_to_pr.complete_task(req({"board": "b", "task_id": "t1", "conduction": {"decide_task_completion": failed}}))
        self.assertEqual(blocked["reason"], "upstream_failed")
        self.assertFalse(blocked["mutated"])

    def test_worktree_and_omp_dry_chain(self) -> None:
        pre = {"status": "ready", "ok": True, "clone_path": "/clone", "base_branch": "main"}
        fetched = issue_to_pr.fetch_clone_origin(req({"clone_path": "/clone", "dry_run": True, "conduction": {"read_clone_preconditions": pre}}))
        self.assertEqual(fetched["status"], "planned")
        omp_pre = {"status": "ready", "ok": True, "worktree_path": "/wt", "branch": "ai/fix/1", "pre_head": "abc"}
        omp = issue_to_pr.invoke_omp(req({"worktree_path": "/wt", "prompt": "fix", "dry_run": True, "conduction": {"read_omp_preconditions": omp_pre}}))
        self.assertEqual(omp["status"], "planned")

    def test_omp_postcondition_read_chain_propagates_noop(self) -> None:
        no_selection = {"status": "noop", "ok": True, "mutated": False, "reason": "no_selected_issue"}
        verified = issue_to_pr.verify_omp_postconditions(req({"conduction": {"dispatch_invoke_omp": no_selection}}))
        worktree = issue_to_pr.read_worktree_head(req({"conduction": {"dispatch_verify_omp_postconditions": verified}}))
        base = issue_to_pr.read_base_head(req({"conduction": {"dispatch_read_worktree_head": worktree}}))
        self.assertEqual([verified["status"], worktree["status"], base["status"]], ["noop", "noop", "noop"])
        self.assertEqual(base["reason"], "no_selected_issue")
        failed = {"status": "failed", "ok": False, "mutated": False, "reason": "omp_diff_path_escape"}
        terminal = issue_to_pr.read_worktree_head(req({"conduction": {"dispatch_verify_omp_postconditions": failed}}))
        self.assertEqual(terminal["reason"], "upstream_failed")
        self.assertEqual(terminal["upstream_effector"], "dispatch_verify_omp_postconditions")
        self.assertFalse(terminal["retry_safe"])

    def test_push_pr_and_receipt_dry_chains(self) -> None:
        push_read = {"status": "read", "ok": True, "worktree_path": "/wt", "branch": "ai/fix/1", "local_oid": "abc"}
        pushed = issue_to_pr.push_branch(req({"worktree_path": "/wt", "branch": "ai/fix/1", "dry_run": True, "conduction": {"read_push_head": push_read}}))
        self.assertEqual(pushed["status"], "planned")
        pr_decision = {"status": "create", "ok": True, "should_create": True}
        pr = issue_to_pr.create_pull_request(req({"repo": "o/r", "branch": "ai/fix/1", "dry_run": True, "conduction": {"decide_existing_pr": pr_decision}}))
        self.assertEqual(pr["status"], "planned")
        built = issue_to_pr.build_dispatch_receipt(req({"payload": {"phase": "CLAIMED"}}))
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "receipt.json")
            published = issue_to_pr.publish_dispatch_receipt(req({"receipt_path": path, "dry_run": False, "conduction": {"build_dispatch_receipt": built}}))
            verified = issue_to_pr.verify_dispatch_receipt(req({"receipt_path": path, "dry_run": False, "conduction": {"publish_dispatch_receipt": published, "build_dispatch_receipt": built}}))
            self.assertEqual(published["status"], "published")
            self.assertEqual(verified["status"], "verified")


class RepairTests(unittest.TestCase):
    def test_build_repair_prompt_uses_exact_linked_issue_and_lane_context(self) -> None:
        out = repair.build_repair_prompt(req({
            "issue": 10, "failures": ["ci"], "reason": "checks_failed",
            "conduction": {"load_pr_fields": {
                "status": "loaded", "repo": "owner/repo", "number": 11,
                "board": "board", "clone_path": "/clone", "priority": 2,
                "pr": {"number": 11, "title": "fix", "headRefName": "ai/fix/11", "closingIssuesReferences": [{"number": 10}]},
            }},
        }))
        self.assertTrue(out["ok"])
        self.assertEqual(out["issue"], 10)
        self.assertEqual(out["pr_number"], 11)
        for value in ("owner/repo", "#10", "ai/fix/11", "/clone", "board", "priority 2"):
            self.assertIn(value, out["prompt"])

    def test_build_repair_prompt_rejects_invalid_linked_issue_identity(self) -> None:
        base = {"number": 11, "title": "fix", "headRefName": "ai/fix/11"}
        for refs, explicit, reason in (([], None, "expected_exactly_one_closing_issue"), ([{"number": 10}, {"number": 12}], None, "expected_exactly_one_closing_issue"), ([{"number": 10}], 12, "explicit_issue_mismatch")):
            with self.subTest(reason=reason):
                pr = {**base, "closingIssuesReferences": refs}
                data = {"pr": pr, "conduction": {"load_pr_fields": {"status": "loaded", "pr": pr}}}
                if explicit is not None:
                    data["issue"] = explicit
                out = repair.build_repair_prompt(req(data))
                self.assertFalse(out["ok"])
                self.assertEqual(out["reason"], reason)
                self.assertFalse(out.get("mutated"))

    def test_invalid_repair_identity_blocks_worktree_mutation(self) -> None:
        failed = {"status": "failed", "ok": False, "reason": "expected_exactly_one_closing_issue", "mutated": False}
        out = repair.read_repair_context(req({"conduction": {"build_repair_prompt": failed}}))
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["reason"], "upstream_failed")
        self.assertFalse(out.get("mutated"))

    def test_build_repair_prompt_requires_linked_issue_in_standalone_input(self) -> None:
        out = repair.build_repair_prompt(req({"pr": {"number": 11, "title": "fix"}, "failures": ["ci"]}))
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "missing_closing_issue_references")

    def test_review_task_dry_chain(self) -> None:
        found = {"status": "absent", "ok": True, "task": None, "marker": "fix-pr-review:o/r:2"}
        out = repair.create_review_task(req({"board": "b", "repo": "o/r", "number": 2, "reason": "conflict", "dry_run": True, "conduction": {"find_review_marker": found}}))
        self.assertEqual(out["status"], "planned")
        self.assertEqual(out["idempotency_key"], "fix-pr-review:o/r:2")

    def test_block_task_dry_chain_and_failure_gate(self) -> None:
        read = {"status": "read", "ok": True, "task_id": "t1", "task": {"id": "t1", "status": "ready"}}
        decision = repair.decide_task_block(req({"conduction": {"read_task_for_block": read}}))
        self.assertTrue(decision["should_block"])
        out = repair.block_task(req({"board": "b", "task_id": "t1", "dry_run": True, "conduction": {"decide_task_block": decision}}))
        self.assertEqual(out["status"], "planned")
        failed = {"status": "failed", "ok": False, "reason": "read_failed", "mutated": False}
        blocked = repair.block_task(req({"board": "b", "task_id": "t1", "conduction": {"decide_task_block": failed}}))
        self.assertEqual(blocked["reason"], "upstream_failed")
        self.assertFalse(blocked["mutated"])


    def test_repair_ownership_allows_empty_dispatch_task(self) -> None:
        context = {
            "repo": "owner/repo", "issue": 10, "pr_number": 11,
            "branch": "ai/fix/10-test", "clone_path": "/clone",
            "worktree_root": "/worktrees", "task_id": "", "receipt": "/receipt",
        }
        base_conduction = {
            "read_repair_context": {"ok": True, "status": "read", **context},
            "read_repair_remote_head": {"ok": True, "status": "read", "remote_oid": "a" * 40},
            "read_repair_worktree_inventory": {"ok": True, "status": "read", "worktree": None},
        }
        fresh = repair.decide_repair_worktree_ownership(req({
            **context,
            "conduction": {**base_conduction, "read_repair_branch_provenance": {"ok": True, "status": "read", "exists": False, "provenance": {}}},
        }))
        self.assertTrue(fresh["ok"])
        self.assertEqual(fresh["expected"]["task"], "")

        matching = {"task": "", "issue": "10", "repo": "owner/repo", "pr": "11", "receipt": repair._repair_context(req(context))["receipt"], "remote_oid": "a" * 40, "target_branch": context["branch"]}
        reuse = repair.decide_repair_worktree_ownership(req({
            **context,
            "conduction": {**base_conduction, "read_repair_branch_provenance": {"ok": True, "status": "read", "exists": True, "provenance": matching}},
        }))
        self.assertTrue(reuse["ok"])

        conflicting = repair.decide_repair_worktree_ownership(req({
            **context,
            "conduction": {**base_conduction, "read_repair_branch_provenance": {"ok": True, "status": "read", "exists": True, "provenance": {**matching, "task": "foreign-task"}}},
        }))
        self.assertFalse(conflicting["ok"])
        self.assertEqual(conflicting["reason"], "foreign_repair_branch_ownership")

        for missing in ("repo", "issue", "pr_number"):
            broken = dict(context)
            broken[missing] = ""
            conduction = dict(base_conduction)
            conduction["read_repair_context"] = {"ok": True, "status": "read", **broken}
            conduction["read_repair_branch_provenance"] = {"ok": True, "status": "read", "exists": False, "provenance": {}}
            out = repair.decide_repair_worktree_ownership(req({**broken, "conduction": conduction}))
            self.assertFalse(out["ok"], missing)
    def test_repair_ownership_receipt_is_stable_and_collision_safe(self) -> None:
        base = {"repo": "owner/repo", "pr_number": 11, "branch": "ai/fix/10", "repair_state_root": "/state", "run_id": "run-one"}
        first = repair._repair_context(req(base))["receipt"]
        second = repair._repair_context(req({**base, "run_id": "run-two"}))["receipt"]
        other_pr = repair._repair_context(req({**base, "pr_number": 12}))["receipt"]
        other_repo = repair._repair_context(req({**base, "repo": "other/repo"}))["receipt"]
        other_branch = repair._repair_context(req({**base, "branch": "ai/fix/11"}))["receipt"]
        self.assertEqual(first, second)
        self.assertEqual(len({first, other_pr, other_repo, other_branch}), 4)
        self.assertTrue(first.startswith("/state/repair-ownership/owner__repo/11/"))
    @mock.patch("lokay.steps.repair.branch_config_unset")
    @mock.patch("lokay.steps.repair.branch_config_set")
    def test_pushed_head_refreshes_stable_ownership_for_next_tick(self, set_config, unset_config) -> None:
        data = {"repo": "owner/repo", "issue": 10, "pr_number": 11, "branch": "ai/fix/10", "clone_path": "/clone", "worktree_root": "/worktrees", "repair_state_root": "/state", "task_id": "", "live": True, "enabled": True, "dry_run": False}
        verified = {"ok": True, "status": "verified", "remote_oid": "b" * 40}
        authorized = {"ok": True, "status": "authorized", "authorize": True}
        updated = repair.update_repair_branch_provenance(req({**data, "conduction": {"decide_repair_attempt": authorized, "verify_repair_push_oid": verified, "verify_repair_receipt": {"ok": True, "status": "verified", "receipt_path": "/state/attempt-b.json"}}}))
        self.assertTrue(updated["ok"])
        self.assertEqual(updated["provenance"]["remote_oid"], "b" * 40)
        unset_config.assert_called_once_with("/clone", repair._repair_context(req(data))["local_branch"], "lokay-task")
        self.assertEqual(updated["provenance"]["receipt"], repair._repair_context(req({**data, "run_id": "next"}))["receipt"])
        self.assertEqual(updated["provenance"]["repair_receipt"], "/state/attempt-b.json")

        repair_context = repair._repair_context(req(data))
        inventory = {"ok": True, "status": "read", "worktrees": [{"path": repair_context["worktree_path"], "branch": repair_context["local_branch"], "head": "b" * 40}]}
        reuse = repair.decide_repair_worktree_ownership(req({**data, "run_id": "next", "conduction": {
            "read_repair_context": {"ok": True, "status": "read", **data},
            "read_repair_remote_head": {"ok": True, "status": "read", "remote_oid": "b" * 40},
            "read_repair_worktree_inventory": inventory,
            "read_repair_branch_provenance": {"ok": True, "status": "read", "exists": True, "provenance": updated["provenance"]},
        }}))
        self.assertTrue(reuse["ok"])
        self.assertTrue(reuse["reuse"])
    @mock.patch("lokay.steps.repair.branch_config_get")
    def test_provenance_readback_normalizes_only_missing_task(self, get_config) -> None:
        provenance = {"task": "", "issue": "10", "repo": "owner/repo", "pr": "11", "receipt": "/state/ownership.json", "remote_oid": "b" * 40, "target_branch": "ai/fix/10"}
        missing = CommandError(["git"], 1, "", "missing")
        get_config.side_effect = [missing, "10", "owner/repo", "11", "/state/ownership.json", "b" * 40, "ai/fix/10"]
        checked = repair.verify_updated_repair_branch_provenance(req({"repo": "owner/repo", "issue": 10, "pr_number": 11, "branch": "ai/fix/10", "clone_path": "/clone", "worktree_root": "/worktrees", "repair_state_root": "/state", "dry_run": False, "conduction": {"update_repair_branch_provenance": {"ok": True, "status": "updated", "provenance": provenance}}}))
        self.assertTrue(checked["ok"])
        self.assertEqual(checked["provenance"], provenance)

    @mock.patch("lokay.steps.repair.branch_config_get")
    def test_provenance_readback_fails_when_required_key_is_missing(self, get_config) -> None:
        provenance = {"task": "", "issue": "10", "repo": "owner/repo", "pr": "11", "receipt": "/state/ownership.json", "remote_oid": "b" * 40, "target_branch": "ai/fix/10"}
        get_config.side_effect = [CommandError(["git"], 1, "", "missing"), CommandError(["git"], 1, "", "missing")]
        checked = repair.verify_updated_repair_branch_provenance(req({"repo": "owner/repo", "issue": 10, "pr_number": 11, "branch": "ai/fix/10", "clone_path": "/clone", "worktree_root": "/worktrees", "repair_state_root": "/state", "dry_run": False, "conduction": {"update_repair_branch_provenance": {"ok": True, "status": "updated", "provenance": provenance}}}))
        self.assertFalse(checked["ok"])
        self.assertEqual(checked["reason"], "repair_provenance_readback_failed")

    def test_repair_provenance_dry_run_retains_expected_values(self) -> None:
        data = {"repo": "owner/repo", "issue": 10, "pr_number": 11, "branch": "ai/fix/10", "clone_path": "/clone", "repair_state_root": "/state", "task_id": "", "dry_run": True}
        authorized = {"ok": True, "status": "authorized", "authorize": True}
        verified = {"ok": True, "status": "verified", "remote_oid": "b" * 40}
        updated = repair.update_repair_branch_provenance(req({**data, "conduction": {"decide_repair_attempt": authorized, "verify_repair_push_oid": verified, "verify_repair_receipt": {"ok": True, "status": "verified", "receipt_path": "/state/attempt-b.json"}}}))
        self.assertEqual(updated["status"], "planned")
        self.assertEqual(updated["provenance"]["task"], "")
        checked = repair.verify_updated_repair_branch_provenance(req({**data, "conduction": {"update_repair_branch_provenance": updated}}))
        self.assertEqual(checked["status"], "planned")

    @mock.patch("lokay.steps.repair.git_push_branch")
    def test_repair_push_targets_selected_remote_branch_without_force(self, push) -> None:
        push.return_value = "ok"
        data = {"repo": "owner/repo", "issue": 10, "pr_number": 11, "branch": "ai/fix/10", "clone_path": "/clone", "worktree_root": "/worktrees", "repair_state_root": "/state", "remote": "upstream", "live": True, "enabled": True, "dry_run": False}
        decision = {"ok": True, "status": "push", "should_push": True, "local_oid": "b" * 40}
        authorized = {"ok": True, "status": "authorized", "authorize": True}
        result = repair.push_repair_branch(req({**data, "conduction": {"decide_repair_attempt": authorized, "decide_repair_push": decision}}))
        self.assertTrue(result["ok"])
        context = repair._repair_context(req(data))
        push.assert_called_once_with(context["worktree_path"], "ai/fix/10", remote="upstream", set_upstream=False)

    def test_foreign_selected_branch_does_not_conflict_with_owned_local_ref(self) -> None:
        data = {"repo": "owner/repo", "issue": 10, "pr_number": 11, "branch": "ai/fix/10", "clone_path": "/clone", "worktree_root": "/worktrees", "repair_state_root": "/state", "task_id": ""}
        context = repair._repair_context(req(data))
        inventory = {"ok": True, "status": "read", "worktrees": [{"path": "/legacy/ai/fix/10", "branch": "ai/fix/10", "head": "b" * 40}]}
        result = repair.decide_repair_worktree_ownership(req({**data, "conduction": {
            "read_repair_context": {"ok": True, "status": "read", **context},
            "read_repair_remote_head": {"ok": True, "status": "read", "remote_oid": "b" * 40},
            "read_repair_worktree_inventory": inventory,
            "read_repair_branch_provenance": {"ok": True, "status": "read", "exists": False, "provenance": {}},
        }}))
        self.assertTrue(result["ok"])
        self.assertFalse(result["reuse"])
        self.assertEqual(result["local_branch"], context["local_branch"])
        self.assertNotEqual(result["local_branch"], result["branch"])

    def test_repair_attempt_receipts_retain_each_head_transition(self) -> None:
        data = {"repo": "owner/repo", "issue": 10, "pr_number": 11, "branch": "ai/fix/10", "repair_state_root": "/state"}
        context = repair._repair_context(req(data))
        def payload(before: str, after: str, run_id: str) -> dict:
            return {"before_oid": before, "after_oid": after, "candidate": "candidate", "run": {"run_id": run_id, "omp_process_id": f"omp-{run_id}", "receipt_process_id": "verify-repair"}, "provenance": {"repo": context["repo"], "pr_number": 11}}
        first = repair._repair_attempt_receipt(req(data), payload("a" * 40, "b" * 40, "run-b"))
        second = repair._repair_attempt_receipt(req(data), payload("b" * 40, "c" * 40, "run-c"))
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("/state/repair-receipts/owner__repo/11/"))
        self.assertTrue(second.startswith("/state/repair-receipts/owner__repo/11/"))
        self.assertNotEqual(first, context["receipt"])




    def test_decide_repair_attempt_disabled_or_dry_run(self) -> None:
        from lokay.steps.repair import decide_repair_attempt
        # disabled
        r_dis = req({"enabled": False, "live": True, "dry_run": False})
        out = decide_repair_attempt(r_dis)
        self.assertEqual(out["decision"], "wait")
        self.assertFalse(out["authorize"])
        # not live
        r_nl = req({"enabled": True, "live": False, "dry_run": False})
        out = decide_repair_attempt(r_nl)
        self.assertEqual(out["decision"], "wait")
        self.assertFalse(out["authorize"])
        # dry run
        r_dry = req({"enabled": True, "live": True, "dry_run": True})
        out = decide_repair_attempt(r_dry)
        self.assertEqual(out["decision"], "wait")
        self.assertFalse(out["authorize"])

    def test_decide_repair_attempt_missing_provenance_conflict(self) -> None:
        from lokay.steps.repair import decide_repair_attempt
        # missing repo
        r = req({"enabled": True, "live": True, "dry_run": False, "number": 1, "verified_head": "abc", "candidate": "c1", "run_id": "r1"})
        out = decide_repair_attempt(r)
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["decision"], "terminal_conflict")
        self.assertEqual(out["conflict"], "missing_repair_provenance")

    def test_decide_repair_attempt_valid_invoke(self) -> None:
        from lokay.steps.repair import decide_repair_attempt
        r = req({
            "enabled": True, "live": True, "dry_run": False,
            "repo": "o/r", "number": 1, "verified_head": "abc", "candidate": "c1", "run_id": "r1",
            "checks": [{"name": "ci", "conclusion": "FAILURE"}]
        })
        out = decide_repair_attempt(r)
        self.assertEqual(out["status"], "invoke")
        self.assertEqual(out["decision"], "invoke")
        self.assertTrue(out["authorize"])
        self.assertEqual(out["failures"], [{"identity": "ci", "conclusion": "FAILURE"}])


    def test_decide_repair_attempt_repeated_head_blocks_changed_checks(self) -> None:
        from lokay.steps.repair import decide_repair_attempt

        state = {
            "repo": "o/r", "pr_number": 1, "verified_head": "abc",
            "candidate": "c1", "run_id": "r1", "status": "invoked",
            "attempted": True, "checks": [{"identity": "ci", "conclusion": "FAILURE"}],
        }
        out = decide_repair_attempt(req({
            "enabled": True, "live": True, "dry_run": False,
            "repo": "o/r", "number": 1, "verified_head": "abc", "candidate": "c1", "run_id": "r1",
            "checks": [{"name": "ci", "conclusion": "ERROR"}], "attempt_state": state,
        }))
        self.assertEqual(out["status"], "already_repaired")
        self.assertFalse(out["authorize"])

    def test_decide_repair_attempt_malformed_executor_flags_are_terminal(self) -> None:
        from lokay.steps.repair import decide_repair_attempt

        out = decide_repair_attempt(req({"enabled": "yes", "live": True, "dry_run": False}))
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["decision"], "terminal_conflict")
        self.assertEqual(out["conflict"], "executor_enabled_must_be_boolean")
    def test_decide_repair_attempt_pending_checks(self) -> None:
        from lokay.steps.repair import decide_repair_attempt
        r = req({
            "enabled": True, "live": True, "dry_run": False,
            "repo": "o/r", "number": 1, "verified_head": "abc", "candidate": "c1", "run_id": "r1",
            "checks": [{"name": "ci", "conclusion": "PENDING"}]
        })
        out = decide_repair_attempt(r)
        self.assertEqual(out["status"], "pending")
        self.assertEqual(out["decision"], "wait")
        self.assertFalse(out["authorize"])
        self.assertEqual(out["reason"], "checks_pending")

    def test_decide_repair_attempt_already_repaired(self) -> None:
        from lokay.steps.repair import decide_repair_attempt
        r = req({
            "enabled": True, "live": True, "dry_run": False,
            "repo": "o/r", "number": 1, "verified_head": "abc", "candidate": "c1", "run_id": "r1",
            "checks": [{"name": "ci", "conclusion": "FAILURE"}],
            "attempt_state": {
                "repo": "o/r", "pr_number": 1, "verified_head": "abc", "candidate": "c1", "run_id": "r1",
                "status": "repaired", "checks": [{"identity": "ci", "conclusion": "FAILURE"}]
            }
        })
        out = decide_repair_attempt(r)
        self.assertEqual(out["status"], "already_repaired")
        self.assertEqual(out["decision"], "already_repaired")
        self.assertFalse(out["authorize"])

    def test_decide_repair_attempt_identity_mismatch(self) -> None:
        from lokay.steps.repair import decide_repair_attempt
        r = req({
            "enabled": True, "live": True, "dry_run": False,
            "repo": "o/r", "number": 1, "verified_head": "abc", "candidate": "c1", "run_id": "r1",
            "checks": [{"name": "ci", "conclusion": "FAILURE"}],
            "attempt_state": {
                "repo": "o/r", "pr_number": 1, "verified_head": "different_head", "candidate": "c1", "run_id": "r1",
                "status": "repaired", "checks": [{"identity": "ci", "conclusion": "FAILURE"}]
            }
        })
        out = decide_repair_attempt(r)
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["decision"], "terminal_conflict")
        self.assertEqual(out["conflict"], "verified_head_mismatch")

    def test_repair_reservation_restart_blocks_changed_checks_and_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = {"enabled": True, "live": True, "dry_run": False, "repo": "o/r", "number": 1, "verified_head": "head-a", "candidate": "candidate-a", "run_id": "run-a", "repair_state_root": tmp, "checks": [{"name": "ci", "conclusion": "FAILURE"}]}
            first = repair.decide_repair_attempt(req(base))
            self.assertTrue(first["authorize"])
            reserved = repair.reserve_repair_attempt(req(dict(base, conduction={"decide_repair_attempt": first})))
            self.assertEqual(reserved["status"], "reserved")
            restart = dict(base, candidate="candidate-b", run_id="run-b", checks=[{"name": "ci", "conclusion": "ERROR"}])
            read = repair.read_repair_attempt_state(req(restart))
            self.assertEqual(read["status"], "found")
            second = repair.decide_repair_attempt(req(dict(restart, conduction={"read_repair_attempt_state": read})))
            self.assertEqual(second["status"], "already_repaired")
            self.assertFalse(second["authorize"])

    def test_repair_reservation_new_head_authorizes_and_malformed_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = {"enabled": True, "live": True, "dry_run": False, "repo": "o/r", "number": 1, "verified_head": "head-a", "candidate": "candidate-a", "run_id": "run-a", "repair_state_root": tmp, "checks": [{"name": "ci", "conclusion": "FAILURE"}]}
            first = repair.decide_repair_attempt(req(base))
            self.assertTrue(first["authorize"])
            new_head = dict(base, verified_head="head-b", candidate="candidate-b", run_id="run-b")
            read = repair.read_repair_attempt_state(req(new_head))
            second = repair.decide_repair_attempt(req(dict(new_head, conduction={"read_repair_attempt_state": read})))
            self.assertTrue(second["authorize"])
            path = repair._repair_reservation_path(req(base), {"repo": "o/r", "pr_number": 1, "verified_head": "head-a"})
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not-json", encoding="utf-8")
            malformed = repair.read_repair_attempt_state(req(base))
            self.assertEqual((malformed["reason"], malformed["failure_class"]), ("repair_attempt_state_malformed", "terminal"))

    def test_repair_invoke_requires_verified_reservation_after_crash_boundary(self) -> None:
        with mock.patch("lokay.steps.repair.run_omp") as run:
            out = repair.invoke_repair_omp(req({"dry_run": False, "conduction": {"decide_repair_attempt": {"ok": True, "status": "invoke", "authorize": True}, "read_repair_omp_preconditions": {"status": "ready", "ok": True, "worktree_path": "/tmp", "pre_head": "head"}}}))
            self.assertEqual(out["reason"], "repair_attempt_reservation_required")
            run.assert_not_called()



class TriageTests(unittest.TestCase):
    def test_verify_merge_receipt_propagates_publisher_noop(self) -> None:
        published = {"status": "noop", "ok": True, "mutated": False, "reason": "not_selected", "action": "skip"}
        out = triage.verify_merge_receipt(req({"receipt_path": "/missing/receipt.json", "dry_run": False, "conduction": {"triage_publish_merge_receipt": published}}))
        self.assertEqual(out["status"], "noop")
        self.assertEqual(out["reason"], "not_selected")
        self.assertEqual(out["operation"], "verify_merge_receipt")
        self.assertFalse(out["mutated"])

    def test_evaluate_checks_pass_and_fail(self) -> None:
        good = triage.evaluate_checks(req({"pr": {"statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS", "state": "SUCCESS"}]}}))
        self.assertTrue(good["pass_"])
        bad = triage.evaluate_checks(req({"pr": {"statusCheckRollup": [{"name": "ci", "conclusion": "FAILURE", "state": "FAILURE"}]}}))
        self.assertFalse(bad["pass_"])
        self.assertEqual(bad["status"], "checks_failed")

    def test_evaluate_test_evidence(self) -> None:
        miss = triage.evaluate_test_evidence(req({"pr": {"body": "no plan"}, "require_test_evidence": True}))
        self.assertFalse(miss["pass_"])
        hit = triage.evaluate_test_evidence(req({"pr": {"body": "Test plan: ran pytest"}, "require_test_evidence": True}))
        self.assertTrue(hit["pass_"])

    def test_decide_triage_action_routes(self) -> None:
        base = {"pr": {"state": "OPEN", "mergeable": "MERGEABLE", "reviewDecision": "APPROVED", "labels": [], "author": {"login": "o"}}, "checks_pass": True, "evidence_pass": True, "automerge": True}
        self.assertEqual(triage.decide_triage_action(req(base))["action"], "merge")
        self.assertEqual(triage.decide_triage_action(req({**base, "checks_pass": False}))["action"], "repair")
        self.assertEqual(triage.decide_triage_action(req({**base, "evidence_pass": False}))["action"], "comment_block")

    def test_canonical_comment_chain_dry_run(self) -> None:
        decision = {"status": "decided", "ok": True, "action": "comment_block"}
        read = {"status": "comments_read", "ok": True, "comments": [], "repo": "o/r", "number": 5}
        request = req({"repo": "o/r", "number": 5, "body": "blocked", "dry_run": True, "conduction": {"decide_triage_action": decision, "read_pr_comments": read}})
        selected = triage.decide_pr_comment(request)
        self.assertEqual(selected["status"], "comment_selected")
        posted = triage.post_pr_comment(req({**request["input"], "conduction": {"decide_triage_action": decision, "read_pr_comments": read, "decide_pr_comment": selected}}))
        self.assertEqual(posted["status"], "planned")

    def test_canonical_merge_chain_and_branch_gate(self) -> None:
        decision = {"status": "decided", "ok": True, "action": "merge"}
        pre = {"status": "merge_preconditions_read", "ok": True, "repo": "o/r", "number": 5, "head_oid": "abc"}
        request = req({"repo": "o/r", "number": 5, "head_oid": "abc", "dry_run": True, "conduction": {"decide_triage_action": decision, "read_merge_preconditions": pre}})
        merged = triage.merge_pr(request)
        self.assertEqual(merged["status"], "planned")
        blocked = triage.merge_pr(req({"repo": "o/r", "number": 5, "head_oid": "abc", "conduction": {"decide_triage_action": {"status": "decided", "ok": True, "action": "comment_block"}, "read_merge_preconditions": pre}}))
        self.assertEqual(blocked["reason"], "not_selected")

    def test_terminal_decision_attribution_is_exact(self) -> None:
        peer = {"status": "failed", "ok": False, "reason": "invalid_pr", "failure_class": "terminal"}
        out = triage.merge_pr(req({"repo": "o/r", "number": 3, "head_oid": "abc", "conduction": {"decide_triage_action": peer}}))
        self.assertEqual(out["reason"], "upstream_failed")
        self.assertEqual(out["upstream_effector"], "decide_triage_action")
        self.assertEqual(out["upstream"], peer)




class CleanupTests(unittest.TestCase):
    def test_resolve_and_parse_cleanup_branch(self) -> None:
        resolved = cleanup.resolve_cleanup_branch_source(req({"branch": "ai/fix/17-fix-login"}))
        self.assertEqual(resolved["status"], "resolved")
        parsed = cleanup.parse_cleanup_issue_number(req({"branch": "ai/fix/17-fix-login", "conduction": {"resolve_cleanup_branch_source": resolved}}))
        self.assertEqual(parsed["issue"], 17)

    def test_parse_cleanup_branch_rejects_unparseable(self) -> None:
        resolved = cleanup.resolve_cleanup_branch_source(req({"branch": "feature/foo"}))
        out = cleanup.parse_cleanup_issue_number(req({"conduction": {"resolve_cleanup_branch_source": resolved}}))
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "unparseable_branch")

    def test_verify_cleanup_guards_requires_canonical_evidence(self) -> None:
        missing = cleanup.verify_cleanup_guards(req({"conduction": {}}))
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["reason"], "cleanup_guard_failed")
        out = cleanup.verify_cleanup_guards(req({"conduction": {
            "check_issue_closed": {"ok": True, "status": "checked", "closed": True, "issue": 3},
            "check_no_open_pr_for_branch": {"ok": True, "status": "checked", "safe_to_cleanup": True, "open_count": 0},
        }}))
        self.assertEqual(out["status"], "verified")

    def test_remove_worktree_consumes_reads_and_dry_runs(self) -> None:
        conduction = {
            "verify_cleanup_guards": {"ok": True, "status": "verified"},
            "read_worktree_ownership": {"ok": True, "status": "read", "clone_path": "/c", "worktree_path": "/wt", "branch": "ai/fix/3-x"},
            "read_worktree_cleanliness": {"ok": True, "status": "checked", "clean": True, "dirty": False},
        }
        out = cleanup.remove_worktree(req({"clone_path": "/c", "worktree_path": "/wt", "dry_run": True, "conduction": conduction}))
        self.assertEqual(out["status"], "planned")

    def test_remove_worktree_fails_closed_on_dirty_read(self) -> None:
        out = cleanup.remove_worktree(req({"clone_path": "/c", "worktree_path": "/wt", "conduction": {
            "verify_cleanup_guards": {"ok": True, "status": "verified"},
            "read_worktree_ownership": {"ok": True, "status": "read", "clone_path": "/c", "worktree_path": "/wt", "branch": "ai/fix/3-x"},
            "read_worktree_cleanliness": {"ok": True, "status": "checked", "clean": False, "dirty": True},
        }}))
        self.assertEqual(out["reason"], "worktree_dirty")

    def test_verify_branch_delete_guards_requires_absence_read(self) -> None:
        out = cleanup.verify_branch_delete_guards(req({"conduction": {
            "verify_cleanup_guards": {"ok": True},
            "verify_worktree_absent": {"ok": True, "status": "verified", "absent": True},
        }}))
        self.assertEqual(out["status"], "verified")

    def test_delete_local_branch_dry_run_requires_canonical_reads(self) -> None:
        conduction = {
            "verify_branch_delete_guards": {"ok": True, "status": "verified"},
            "read_local_branch_ownership": {"ok": True, "status": "read", "owned": True, "exists": True},
        }
        out = cleanup.delete_local_branch(req({"clone_path": "/c", "branch": "ai/fix/3-x", "dry_run": True, "conduction": conduction}))
        self.assertEqual(out["status"], "planned")

    def test_collect_cleanup_receipt_evidence_uses_canonical_names(self) -> None:
        names = ("parse_cleanup_issue_number", "check_issue_closed", "check_no_open_pr_for_branch", "remove_worktree", "delete_local_branch", "release_claim_file")
        out = cleanup.collect_cleanup_receipt_evidence(req({"conduction": {name: {"ok": True, "status": "checked"} for name in names}}))
        self.assertEqual(out["status"], "collected")
        self.assertEqual(set(out["evidence"]), set(names))

    def test_create_maintenance_dry_requires_read_and_find(self) -> None:
        out = cleanup.create_maintenance_task(req({"board": "b", "worktree_path": "/wt", "reason": "dirty", "dry_run": True, "conduction": {
            "read_maintenance_tasks": {"ok": True, "status": "read", "tasks": []},
            "find_maintenance_marker": {"ok": True, "status": "found", "found": False, "marker": "maintenance:/wt:pr:none"},
        }}))
        self.assertEqual(out["status"], "planned")

    def test_check_issue_closed_paths(self) -> None:
        with mock.patch("lokay.steps.cleanup.run_cmd", return_value=SimpleNamespace(stdout=json.dumps({"state": "CLOSED"}), stderr="", returncode=0)):
            out = cleanup.check_issue_closed(req({"repo": "o/r", "issue": 3}))
        self.assertTrue(out["closed"])

    def test_check_no_open_pr_paths(self) -> None:
        with mock.patch("lokay.steps.cleanup.run_cmd", return_value=SimpleNamespace(stdout="[]", stderr="", returncode=0)):
            out = cleanup.check_no_open_pr_for_branch(req({"repo": "o/r", "branch": "ai/fix/3-x"}))
        self.assertTrue(out["safe_to_cleanup"])


class AdapterFailurePathTests(unittest.TestCase):
    def test_dispatch_read_failure_attribution(self) -> None:
        with mock.patch(
            "lokay.steps.issue_to_pr.hermes_kanban_json",
            side_effect=CommandError(["hermes"], 1, "", "boom"),
        ):
            read = issue_to_pr.read_dispatch_tasks(req({"board": "b"}))
        self.assertFalse(read["ok"])
        self.assertEqual(read["reason"], "kanban_list_failed")
        self.assertEqual(read["operation"], "read_dispatch_tasks")
        self.assertEqual(read["failure_class"], "retryable_read")
        self.assertTrue(read["retry_safe"])

        selected = issue_to_pr.select_dispatch_task(
            req({"conduction": {"read_dispatch_tasks": read}})
        )
        self.assertFalse(selected["ok"])
        self.assertEqual(selected["reason"], "upstream_failed")
        self.assertEqual(selected["operation"], "select_dispatch_task")
        self.assertEqual(selected["upstream_effector"], "read_dispatch_tasks")
        self.assertEqual(selected["upstream"], read)


if __name__ == "__main__":
    unittest.main()
