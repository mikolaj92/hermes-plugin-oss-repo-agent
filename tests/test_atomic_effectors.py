"""Unit tests for mega-atomic effectors — drive real shipped handlers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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

def invoke_req(input_data=None, config=None, *, process_id):
    request = req(input_data, config)
    request["process_id"] = process_id
    return request

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
            self.assertTrue(out["ok"])
            self.assertEqual(out["status"], "noop")
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

    def test_reused_assignee_still_applies_label_and_claims(self) -> None:
        selected = self._selected(3590)
        reserve = {
            "status": "claim_reserved",
            "ok": True,
            "mutated": False,
            "reused": True,
            "selected": selected,
            "claim_path": "/tmp/claim.json",
            "claim": {"repo": "o/r", "issue": 3590, "board": "b", "assignee": "mikolaj92"},
        }
        state = {
            "status": "claim_state_read",
            "ok": True,
            "repo": "o/r",
            "number": 3590,
            "assignees": ["mikolaj92"],
            "labels": ["ai:ready"],
            "selected": selected,
        }
        assign = claim.assign_issue(
            req(
                {
                    "dry_run": False,
                    "selected": selected,
                    "conduction": {
                        "reserve_claim_file": reserve,
                        "read_issue_claim_state": state,
                    },
                },
                {"assignee": "mikolaj92"},
            )
        )
        self.assertEqual(assign["status"], "issue_assigned")
        self.assertTrue(assign.get("reused"))
        self.assertFalse(assign["mutated"])
        with mock.patch("lokay.steps.claim.run_cmd") as run_cmd:
            run_cmd.side_effect = [
                mock.Mock(stdout=json.dumps([]), returncode=0),  # list labels
                mock.Mock(stdout="", returncode=0),  # create label
                mock.Mock(stdout=json.dumps([{"name": "ai:in-progress"}]), returncode=0),  # re-list
                mock.Mock(stdout="", returncode=0),  # issue edit
                mock.Mock(
                    stdout=json.dumps(
                        {
                            "assignees": [{"login": "mikolaj92"}],
                            "labels": [{"name": "ai:ready"}, {"name": "ai:in-progress"}],
                        }
                    ),
                    returncode=0,
                ),  # verify view
            ]
            labeled = claim.add_issue_label(
                req(
                    {
                        "dry_run": False,
                        "selected": selected,
                        "label": "ai:in-progress",
                        "conduction": {
                            "reserve_claim_file": reserve,
                            "read_issue_claim_state": state,
                            "assign_issue": assign,
                        },
                    }
                )
            )
            self.assertEqual(labeled["status"], "issue_label_added")
            self.assertTrue(labeled["mutated"])
            verified = claim.verify_issue_claim(
                req(
                    {
                        "dry_run": False,
                        "selected": selected,
                        "assignee": "mikolaj92",
                        "label": "ai:in-progress",
                        "conduction": {
                            "reserve_claim_file": reserve,
                            "read_issue_claim_state": state,
                            "assign_issue": assign,
                            "intake_add_issue_label": labeled,
                        },
                    }
                )
            )
        self.assertEqual(run_cmd.call_count, 5)
        self.assertEqual(run_cmd.call_args_list[0].args[0][:3], ["gh", "label", "list"])
        self.assertIn("--add-label", run_cmd.call_args_list[3].args[0])
        self.assertEqual(run_cmd.call_args_list[4].args[0][:3], ["gh", "issue", "view"])
        self.assertEqual(verified["status"], "claim_verified")
        self.assertTrue(verified["verified"])
        self.assertIn("ai:in-progress", verified["labels"])
        built = claim.build_issue_claim_result(
            req(
                {
                    "dry_run": False,
                    "selected": selected,
                    "conduction": {
                        "reserve_claim_file": reserve,
                        "verify_issue_claim": verified,
                    },
                }
            )
        )
        self.assertEqual(built["status"], "claimed")
        self.assertTrue(built["ok"])
        with mock.patch("lokay.steps.kanban_intake.hermes_kanban_json", return_value=[]):
            tasks = kanban_intake.read_intake_tasks(
                req(
                    {
                        "dry_run": True,
                        "selected": selected,
                        "conduction": {"build_issue_claim_result": built},
                    }
                )
            )
        self.assertEqual(tasks["status"], "intake_tasks_read")

    def test_claim_result_fails_when_reservation_failed_and_verification_missing(self) -> None:
        reserve = {
            "status": "failed", "ok": False, "reason": "claim_busy",
            "failure_class": "terminal", "mutated": False,
        }
        out = claim.build_issue_claim_result(req({"dry_run": False, "conduction": {"reserve_claim_file": reserve}}))
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["reason"], "claim_failed")
        self.assertEqual(out["reserve_reason"], "claim_busy")
        self.assertEqual(out["reserve"], reserve)

    def test_missing_required_label_fails_claim_verification(self) -> None:
        selected = self._selected(3590)
        reserve = {"status": "claim_reserved", "ok": True, "selected": selected, "claim_path": "/tmp/claim.json"}
        state = {
            "status": "claim_state_read",
            "ok": True,
            "repo": "o/r",
            "number": 3590,
            "assignees": ["mikolaj92"],
            "labels": ["ai:ready"],
            "selected": selected,
        }
        assign = {
            "status": "issue_assigned",
            "ok": True,
            "reused": True,
            "mutated": False,
            "assignee": "mikolaj92",
            "repo": "o/r",
            "number": 3590,
        }
        out = claim.verify_issue_claim(
            req(
                {
                    "dry_run": False,
                    "selected": selected,
                    "assignee": "mikolaj92",
                    "conduction": {
                        "reserve_claim_file": reserve,
                        "read_issue_claim_state": state,
                        "assign_issue": assign,
                        "intake_add_issue_label": {
                            "status": "failed",
                            "ok": False,
                            "reason": "add_issue_label_failed",
                            "mutated": True,
                        },
                    },
                }
            )
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "upstream_failed")

    def test_add_issue_label_provisions_missing_label(self) -> None:
        selected = self._selected(3590)
        state = {
            "status": "claim_state_read",
            "ok": True,
            "repo": "o/r",
            "number": 3590,
            "assignees": ["mikolaj92"],
            "labels": ["ai:ready"],
            "selected": selected,
        }
        assign = {
            "status": "issue_assigned",
            "ok": True,
            "reused": True,
            "mutated": False,
            "assignee": "mikolaj92",
        }
        with mock.patch("lokay.steps.claim.run_cmd") as run_cmd:
            run_cmd.side_effect = [
                mock.Mock(stdout=json.dumps([]), returncode=0),  # list labels
                mock.Mock(stdout="", returncode=0),  # create label
                mock.Mock(stdout=json.dumps([{"name": "ai:in-progress"}]), returncode=0),  # re-list
                mock.Mock(stdout="", returncode=0),  # issue edit
            ]
            out = claim.add_issue_label(
                req(
                    {
                        "dry_run": False,
                        "selected": selected,
                        "label": "ai:in-progress",
                        "conduction": {
                            "reserve_claim_file": {"status": "claim_reserved", "ok": True, "selected": selected},
                            "read_issue_claim_state": state,
                            "assign_issue": assign,
                        },
                    }
                )
            )
        self.assertEqual(out["status"], "issue_label_added")
        self.assertTrue(out["mutated"])
        self.assertTrue(out.get("provisioned"))
        self.assertEqual(run_cmd.call_count, 4)
        self.assertEqual(run_cmd.call_args_list[0].args[0][:3], ["gh", "label", "list"])
        self.assertEqual(run_cmd.call_args_list[1].args[0][:3], ["gh", "label", "create"])
        self.assertEqual(run_cmd.call_args_list[3].args[0][:3], ["gh", "issue", "edit"])

    def test_dispatch_board_resolves_from_intake_selected(self) -> None:
        board = issue_to_pr._atomic_board(
            req(
                {
                    "conduction": {
                        "intake_build_issue_claim_result": {
                            "status": "claimed",
                            "ok": True,
                            "selected": {"repo": "o/r", "number": 3590, "board": "mikolaj92-temida"},
                        }
                    }
                }
            )
        )
        self.assertEqual(board, "mikolaj92-temida")
        board2 = issue_to_pr._atomic_board(
            req(
                {
                    "conduction": {
                        "intake_reconcile_intake_task": {
                            "status": "intake_reconciled",
                            "ok": True,
                            "board": "mikolaj92-temida",
                            "task_id": "t1",
                        }
                    }
                }
            )
        )
        self.assertEqual(board2, "mikolaj92-temida")

    def test_dispatch_reads_board_despite_claim_busy(self) -> None:
        task = {
            "id": "t_14722de2",
            "title": "[issue] mikolaj92/Temida#3590: retry",
            "status": "ready",
            "assignee": "lokay-intake",
        }
        with tempfile.TemporaryDirectory() as tmp:
            claim_path = Path(tmp) / "claim.json"
            claim_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "repo": "mikolaj92/Temida",
                        "issue": 3590,
                        "board": "mikolaj92-temida",
                        "assignee": "mikolaj92",
                        "claimedAt": "2026-07-29T12:18:17.734195Z",
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch("lokay.steps.issue_to_pr.hermes_kanban_json", return_value=[task]) as listed:
                read = issue_to_pr.read_dispatch_tasks(
                    req(
                        {
                            "dry_run": False,
                            "active_issue_path": tmp,
                            "repos": [
                                {"repo": "mikolaj92/lokay", "board": "mikolaj92-lokay"},
                                {"repo": "mikolaj92/Temida", "board": "mikolaj92-temida"},
                            ],
                            "conduction": {
                                "intake_kanban": {
                                    "status": "failed",
                                    "ok": False,
                                    "reason": "claim_busy",
                                    "failure_class": "terminal",
                                },
                                "intake_reserve_claim_file": {
                                    "status": "failed",
                                    "ok": False,
                                    "reason": "claim_busy",
                                    "failure_class": "terminal",
                                    "selected": {
                                        "repo": "mikolaj92/lokay",
                                        "number": 99,
                                        "board": "mikolaj92-lokay",
                                    },
                                },
                                "intake_build_issue_claim_result": {
                                    "status": "failed",
                                    "ok": False,
                                    "reason": "claim_busy",
                                },
                                "intake_reconcile_intake_task": {
                                    "status": "failed",
                                    "ok": False,
                                    "reason": "upstream_failed",
                                },
                            },
                        }
                    )
                )
                selected = issue_to_pr.select_dispatch_task(
                    req(
                        {
                            "dry_run": False,
                            "conduction": {"read_dispatch_tasks": read},
                        }
                    )
                )
        self.assertEqual(read["status"], "read")
        self.assertEqual(read["board"], "mikolaj92-temida")
        self.assertNotEqual(read["board"], "mikolaj92-lokay")
        self.assertEqual(selected["status"], "selected")
        self.assertEqual(selected["task_id"], "t_14722de2")
        listed.assert_called()
        self.assertEqual(listed.call_args.args[0][1], "mikolaj92-temida")

    def test_kanban_dry_run_chain(self) -> None:
        selected = self._selected(1)
        claim_result = {"status": "claimed", "ok": True, "selected": selected}
        with mock.patch("lokay.steps.kanban_intake.hermes_kanban_json", return_value=[]):
            read = kanban_intake.read_intake_tasks(req({"conduction": {"build_issue_claim_result": claim_result}, "selected": selected, "dry_run": True}))
        found = kanban_intake.find_intake_marker(req({"conduction": {"read_intake_tasks": read}, "dry_run": True}))
        out = kanban_intake.create_intake_task(req({"conduction": {"find_intake_marker": found}, "selected": selected, "dry_run": True}))
        self.assertEqual(out["status"], "planned")
        self.assertEqual(out["idempotency_key"], "github-issue:o/r:1")

    def test_existing_intake_task_continues_to_dispatch_board(self) -> None:
        selected = self._selected(3590)
        found = {
            "status": "intake_marker_found",
            "ok": True,
            "found": True,
            "task_id": "t_14722de2",
            "task": {"id": "t_14722de2", "title": "[issue] o/r#3590", "status": "ready"},
            "marker": "github-issue:o/r:3590",
            "board": "mikolaj92-temida",
            "selected": selected,
            "already_completed": False,
        }
        created = kanban_intake.create_intake_task(
            req(
                {
                    "dry_run": False,
                    "selected": selected,
                    "conduction": {"find_intake_marker": found},
                }
            )
        )
        self.assertEqual(created["status"], "intake_task_exists")
        self.assertFalse(created["mutated"])
        reconciled = kanban_intake.reconcile_intake_task(
            req(
                {
                    "dry_run": False,
                    "conduction": {
                        "create_intake_task": created,
                        "find_intake_marker": found,
                    },
                }
            )
        )
        self.assertEqual(reconciled["status"], "intake_reconciled")
        self.assertTrue(reconciled["verified"])
        self.assertNotEqual(reconciled.get("status"), "noop")
        with mock.patch("lokay.steps.issue_to_pr.hermes_kanban_json", return_value=[found["task"]]) as listed:
            read = issue_to_pr.read_dispatch_tasks(
                req(
                    {
                        "dry_run": False,
                        "conduction": {
                            "intake_reconcile_intake_task": reconciled,
                            "intake_create_intake_task": created,
                            "intake_build_issue_claim_result": {
                                "status": "claimed",
                                "ok": True,
                                "selected": selected,
                            },
                        },
                    }
                )
            )
        self.assertEqual(read["status"], "read")
        self.assertEqual(read["board"], "mikolaj92-temida")
        listed.assert_called()

    def test_completed_intake_task_does_not_continue(self) -> None:
        selected = self._selected(3590)
        found = {
            "status": "intake_marker_found",
            "ok": True,
            "found": True,
            "task_id": "t_done",
            "task": {"id": "t_done", "title": "[issue] o/r#3590", "status": "done"},
            "marker": "github-issue:o/r:3590",
            "board": "mikolaj92-temida",
            "selected": selected,
            "already_completed": True,
        }
        created = kanban_intake.create_intake_task(
            req({"dry_run": False, "selected": selected, "conduction": {"find_intake_marker": found}})
        )
        self.assertEqual(created["status"], "intake_task_exists")
        self.assertTrue(created.get("already_completed"))
        reconciled = kanban_intake.reconcile_intake_task(
            req(
                {
                    "dry_run": False,
                    "conduction": {
                        "create_intake_task": created,
                        "find_intake_marker": found,
                    },
                }
            )
        )
        self.assertEqual(reconciled["status"], "noop")
        self.assertEqual(reconciled["reason"], "intake_task_already_completed")


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

    def test_held_claim_selects_matching_task_not_newer_ready_task(self) -> None:
        unrelated = {"id": "newer", "title": "[issue] mikolaj92/Temida#3650: unrelated", "status": "ready"}
        held_task = {
            "id": "t_14722de2",
            "title": "[issue] mikolaj92/Temida#3590: retry response",
            "body": "Repository: mikolaj92/Temida\nIssue: #3590\nIdempotency-Key: github-issue:mikolaj92/Temida:3590\n",
            "status": "ready",
        }
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "claim.json").write_text(json.dumps({
                "version": 1, "repo": "mikolaj92/Temida", "issue": 3590,
                "board": "mikolaj92-temida", "assignee": "mikolaj92",
                "claimedAt": "2026-07-29T12:18:17.734195Z",
            }))
            out = issue_to_pr.select_dispatch_task(req({
                "active_issue_path": tmp,
                "conduction": {"read_dispatch_tasks": {"status": "read", "ok": True, "tasks": [unrelated, held_task]}},
            }))
        self.assertEqual(out["task_id"], "t_14722de2")

    def test_held_claim_without_ready_task_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "claim.json").write_text(json.dumps({
                "version": 1, "repo": "o/r", "issue": 9, "board": "b",
                "assignee": "mikolaj92", "claimedAt": "2026-07-29T12:18:17Z",
            }))
            out = issue_to_pr.select_dispatch_task(req({
                "active_issue_path": tmp,
                "conduction": {"read_dispatch_tasks": {"status": "read", "ok": True, "tasks": [{"id": "x", "title": "[issue] o/r#10", "status": "ready"}]}},
            }))
        self.assertEqual(out["status"], "noop")
        self.assertEqual(out["reason"], "held_claim_task_unavailable")

    def test_held_claim_with_merged_receipt_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            claim_root = Path(tmp) / "claim"
            receipt_root = Path(tmp) / "merge"
            claim_root.mkdir()
            receipt_root.mkdir()
            (claim_root / "claim.json").write_text(json.dumps({
                "version": 1,
                "repo": "o/r",
                "issue": 9,
                "board": "b",
                "assignee": "mikolaj92",
                "claimedAt": "2026-07-29T12:18:17Z",
            }))
            (receipt_root / "merge.json").write_text(json.dumps({
                "phase": "MERGED",
                "repo": "o/r",
                "pr": 42,
                "headSha": "abc123",
                "mergeSha": "def456",
                "mergedAt": "2026-08-01T09:14:30Z",
                "verified_provenance": {
                    "source": "github_pr_readback",
                    "state": "MERGED",
                    "repo": "o/r",
                    "number": 42,
                    "head_ref": "ai/fix/9-issue-o-r-9",
                    "head_oid": "abc123",
                    "merge_oid": "def456",
                    "merged_at": "2026-08-01T09:14:30Z",
                },
            }))
            ready_fix = {
                "id": "fix-1",
                "title": "[fix-pr] o/r#9: leftover",
                "body": "Repository: o/r\nIssue: #9\nIdempotency-Key: fix-pr:o/r:9\n",
                "status": "ready",
            }
            out = issue_to_pr.select_dispatch_task(req({
                "active_issue_path": str(claim_root),
                "merge_receipts": str(receipt_root),
                "conduction": {
                    "read_dispatch_tasks": {
                        "status": "read",
                        "ok": True,
                        "tasks": [ready_fix],
                    }
                },
            }))
        self.assertEqual(out["status"], "noop")
        self.assertEqual(out["reason"], "held_claim_task_unavailable")
        self.assertTrue(out["already_merged"])
        self.assertEqual(out["task_id"], "fix-1")
        self.assertEqual(out["task"], ready_fix)
        self.assertEqual(out["repo"], "o/r")
        self.assertEqual(out["issue"], 9)

    def test_decide_held_issue_already_merged_emits_identity(self) -> None:
        read = {
            "status": "read",
            "ok": True,
            "mutated": False,
            "repo": "o/r",
            "issue": 9,
            "board": "b",
            "task_id": "t1",
            "prs": [
                {
                    "number": 42,
                    "url": "https://example.test/pr/42",
                    "baseRefName": "main",
                    "headRefName": "3791-emp-suggested-edit",
                    "headRefOid": "abc123",
                    "mergedAt": "2026-08-02T14:27:26Z",
                    "mergeCommit": {"oid": "def456"},
                    "closingIssuesReferences": [{"number": 9}],
                }
            ],
        }
        out = issue_to_pr.decide_held_issue_already_merged(
            req({"conduction": {"read_merged_closing_prs": read}})
        )
        self.assertEqual(out["status"], "noop")
        self.assertTrue(out["already_merged"])
        self.assertEqual(out["reason"], "held_claim_task_unavailable")
        self.assertEqual(out["repo"], "o/r")
        self.assertEqual(out["issue"], 9)
        self.assertEqual(out["pr_number"], 42)
        self.assertEqual(out["branch"], "3791-emp-suggested-edit")
        self.assertEqual(out["head_oid"], "abc123")
        self.assertTrue(issue_to_pr._already_merged_held_issue(out))

    def test_already_merged_completion_bypasses_decide_noop(self) -> None:
        decide = {
            "status": "noop", "ok": True, "mutated": False,
            "reason": "held_claim_task_unavailable", "already_merged": True,
            "operation": "decide_held_issue_already_merged", "repo": "o/r", "issue": 9,
            "pr_number": 42, "number": 42, "branch": "3791-emp-suggested-edit",
            "head_oid": "abc123", "board": "b", "task_id": "t1",
        }
        select = {"status": "selected", "ok": True, "mutated": False, "task_id": "t1", "board": "b", "repo": "o/r", "issue": 9}
        task = {"id": "t1", "status": "ready", "title": "[issue] o/r#9: bug"}
        read = {"status": "read", "ok": True, "mutated": False, "operation": "read_merged_closing_prs", "repo": "o/r", "issue": 9, "task_id": "t1", "prs": [{"number": 42, "headRefName": "3791-emp-suggested-edit", "headRefOid": "abc123", "closingIssuesReferences": [{"number": 9}]}]}
        with tempfile.TemporaryDirectory() as tmp:
            claim_root, receipt_root = Path(tmp) / "claim", Path(tmp) / "merge"
            claim_root.mkdir(); receipt_root.mkdir()
            (claim_root / "claim.json").write_text(json.dumps({"version": 1, "repo": "o/r", "issue": 9, "board": "b", "assignee": "mikolaj92", "claimedAt": "2026-07-29T12:18:17Z"}))
            (receipt_root / "merge.json").write_text(json.dumps({"phase": "MERGED", "repo": "o/r", "issue": 9, "pr": 42, "headSha": "abc123", "verified_provenance": {"source": "github_pr_readback", "state": "MERGED", "repo": "o/r", "number": 42, "head_ref": "3791-emp-suggested-edit", "head_oid": "abc123"}}))
            request = {"board": "b", "task_id": "t1", "active_issue_path": str(claim_root), "merge_receipts": str(receipt_root), "conduction": {"select_dispatch_task": select, "decide_held_issue_already_merged": decide, "read_merged_closing_prs": read}}
            with mock.patch("lokay.steps.issue_to_pr.hermes_kanban_json", return_value=[task]):
                read_task = issue_to_pr.read_task_for_completion(req(request))
            self.assertEqual(read_task["status"], "read")
            with mock.patch("lokay.steps.issue_to_pr.run_cmd") as run:
                run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
                completed = issue_to_pr.complete_task(req({**request, "result": "already merged", "dry_run": False, "conduction": {**request["conduction"], "read_task_for_completion": read_task, "decide_task_completion": {"status": "should_complete", "ok": True, "should_complete": True, "task_id": "t1"}}}))
            self.assertEqual(completed["status"], "completed")
            run.assert_called_once()
            forged = {**decide, "head_oid": "forged"}
            blocked = issue_to_pr.read_task_for_completion(req({
                **request,
                "conduction": {**request["conduction"], "decide_held_issue_already_merged": forged},
            }))
            self.assertEqual(blocked["status"], "noop")
            self.assertEqual(blocked["reason"], "held_claim_task_unavailable")

    def test_tagged_selector_noop_bypasses_completion_gate(self) -> None:
        selector = {
            "status": "noop",
            "ok": True,
            "mutated": False,
            "reason": "held_claim_task_unavailable",
            "already_merged": True,
            "operation": "select_dispatch_task",
            "repo": "o/r",
            "issue": 9,
            "task_id": "t1",
            "task": {"id": "t1", "status": "ready", "title": "[fix-pr] o/r#9: leftover"},
        }
        task = {"id": "t1", "status": "ready", "title": "[fix-pr] o/r#9: leftover"}
        with tempfile.TemporaryDirectory() as tmp:
            claim_root = Path(tmp) / "claim"
            receipt_root = Path(tmp) / "merge"
            claim_root.mkdir()
            receipt_root.mkdir()
            (claim_root / "claim.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "repo": "o/r",
                        "issue": 9,
                        "board": "b",
                        "assignee": "lokay",
                        "claimedAt": "2026-07-30T00:00:00Z",
                    }
                )
            )
            (receipt_root / "merge.json").write_text(
                json.dumps(
                    {
                        "phase": "MERGED",
                        "repo": "o/r",
                        "pr": 42,
                        "headSha": "abc123",
                        "verified_provenance": {
                            "source": "github_pr_readback",
                            "state": "MERGED",
                            "repo": "o/r",
                            "number": 42,
                            "head_ref": "ai/fix/9-leftover",
                            "head_oid": "abc123",
                        },
                    }
                )
            )
            request = {
                "board": "b",
                "task_id": "t1",
                "active_issue_path": str(claim_root),
                "merge_receipts": str(receipt_root),
            }
            with mock.patch("lokay.steps.issue_to_pr.hermes_kanban_json", return_value=[task]):
                read = issue_to_pr.read_task_for_completion(
                    req({**request, "conduction": {"select_dispatch_task": selector}})
                )
            self.assertEqual(read["status"], "read")
            self.assertEqual(read["task_id"], "t1")
            with mock.patch("lokay.steps.issue_to_pr.run_cmd") as run:
                run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
                completed = issue_to_pr.complete_task(
                    req(
                        {
                            **request,
                            "dry_run": False,
                            "conduction": {
                                "select_dispatch_task": selector,
                                "read_task_for_completion": read,
                                "decide_task_completion": {
                                    "status": "should_complete",
                                    "ok": True,
                                    "should_complete": True,
                                },
                            },
                        }
                    )
                )
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["task_id"], "t1")
            run.assert_called_once()

    def test_invalid_claim_cannot_authorize_selector_noop(self) -> None:
        selector = {
            "status": "noop",
            "ok": True,
            "mutated": False,
            "reason": "held_claim_task_unavailable",
            "already_merged": True,
            "operation": "select_dispatch_task",
            "repo": "o/r",
            "issue": 9,
            "task_id": "t1",
            "task": {"id": "t1", "status": "ready"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            claim_root, receipt_root = Path(tmp) / "claim", Path(tmp) / "merge"
            claim_root.mkdir()
            receipt_root.mkdir()
            (claim_root / "claim.json").write_text(json.dumps({"repo": "o/r", "issue": 9}))
            (receipt_root / "merge.json").write_text(json.dumps({
                "phase": "MERGED", "repo": "o/r", "pr": 42, "headSha": "abc123",
                "verified_provenance": {
                    "source": "github_pr_readback", "state": "MERGED", "repo": "o/r",
                    "number": 42, "head_ref": "ai/fix/9-leftover", "head_oid": "abc123",
                },
            }))
            out = issue_to_pr.read_task_for_completion(req({
                "board": "b", "task_id": "t1", "active_issue_path": str(claim_root),
                "merge_receipts": str(receipt_root), "conduction": {"select_dispatch_task": selector},
            }))
        self.assertEqual(out["status"], "noop")
        self.assertEqual(out["reason"], "held_claim_task_unavailable")

    def test_malformed_already_merged_envelopes_do_not_bypass(self) -> None:
        base = {
            "status": "noop", "ok": True, "mutated": False,
            "reason": "held_claim_task_unavailable", "already_merged": True,
            "operation": "decide_held_issue_already_merged", "repo": "o/r", "issue": 9,
            "pr_number": 42, "branch": "feature", "head_oid": "abc123",
        }
        for key, value in (("ok", False), ("reason", "not_authorized")):
            with self.subTest(key=key):
                decide = {**base, key: value}
                out = issue_to_pr.read_task_for_completion(req({
                    "board": "b", "task_id": "t1",
                    "conduction": {"decide_held_issue_already_merged": decide},
                }))
                if key == "ok":
                    self.assertEqual(out["status"], "failed")
                    self.assertEqual(out["reason"], "upstream_failed")
                else:
                    self.assertEqual(out["status"], "noop")
                    self.assertEqual(out["reason"], "not_authorized")
                self.assertFalse(issue_to_pr._already_merged_held_issue(decide))
    def test_already_merged_completion_keeps_other_upstream_noop(self) -> None:
        decide = {
            "status": "noop",
            "ok": True,
            "mutated": False,
            "reason": "held_claim_task_unavailable",
            "already_merged": True,
            "operation": "decide_held_issue_already_merged",
            "repo": "o/r",
            "issue": 9,
            "pr_number": 42,
            "branch": "3791-emp-suggested-edit",
            "head_oid": "abc123",
        }
        select_noop = {
            "status": "noop",
            "ok": True,
            "mutated": False,
            "reason": "no_ready_task",
        }
        out = issue_to_pr.read_task_for_completion(
            req(
                {
                    "board": "b",
                    "conduction": {
                        "select_dispatch_task": select_noop,
                        "decide_held_issue_already_merged": decide,
                    },
                }
            )
        )
        self.assertEqual(out["status"], "noop")
        self.assertEqual(out["reason"], "no_ready_task")

        blocked = issue_to_pr.complete_task(
            req(
                {
                    "board": "b",
                    "conduction": {
                        "select_dispatch_task": select_noop,
                        "decide_held_issue_already_merged": decide,
                    },
                }
            )
        )
        self.assertEqual(blocked["status"], "noop")
        self.assertEqual(blocked["reason"], "no_ready_task")

    def test_next_dispatch_selects_fix_task_after_handoff(self) -> None:
        issue_task = {
            "id": "issue-1",
            "title": "[issue] o/r#9: bug",
            "body": "Repository: o/r\nIssue: #9\nIdempotency-Key: github-issue:o/r:9\n",
            "status": "ready",
        }
        first = issue_to_pr.select_dispatch_task(req({
            "conduction": {"read_dispatch_tasks": {"status": "read", "ok": True, "tasks": [issue_task]}},
        }))
        self.assertEqual(first["task_id"], "issue-1")
        fix_task = {
            "id": "fix-1",
            "title": "[fix-pr] o/r#9: Fix o/r#9",
            "body": "Repository: o/r\nIssue: #9\nIdempotency-Key: fix-pr:o/r:9\n",
            "status": "ready",
        }
        second = issue_to_pr.select_dispatch_task(req({
            "conduction": {"read_dispatch_tasks": {"status": "read", "ok": True, "tasks": [issue_task, fix_task]}},
        }))
        self.assertEqual(second["task_id"], "fix-1")
        requested = issue_to_pr.select_dispatch_task(req({
            "task_id": "issue-1",
            "conduction": {"read_dispatch_tasks": {"status": "read", "ok": True, "tasks": [issue_task, fix_task]}},
        }))
        self.assertEqual(requested["status"], "noop")
        self.assertEqual(requested["reason"], "fix_task_handoff")
        self.assertEqual(requested["task_id"], "issue-1")

    def test_fix_marker_does_not_prefix_match_another_issue(self) -> None:
        issue_9 = {"id": "issue-9", "title": "[issue] o/r#9: bug", "status": "ready"}
        fix_90 = {
            "id": "fix-90", "title": "[fix-pr] o/r#90: Fix o/r#90", "status": "ready",
            "body": "Idempotency-Key: fix-pr:o/r:90\n",
        }
        selected = issue_to_pr.select_dispatch_task(req({
            "conduction": {"read_dispatch_tasks": {"status": "read", "ok": True, "tasks": [issue_9, fix_90]}},
        }))
        self.assertEqual(selected["task_id"], "issue-9")

    def test_dispatch_selection_excludes_fix_pr_review(self) -> None:
        review = {
            "id": "review-1",
            "title": "[fix-pr-review] o/r#9: repair checks",
            "body": "Repository: o/r\nIssue: #9\nIdempotency-Key: fix-pr-review:o/r:9\n",
            "status": "ready",
        }
        only_review = issue_to_pr.select_dispatch_task(req({
            "conduction": {"read_dispatch_tasks": {"status": "read", "ok": True, "tasks": [review]}},
        }))
        self.assertEqual(only_review["status"], "noop")
        self.assertEqual(only_review["reason"], "no_ready_task")

        fix = {
            "id": "fix-1",
            "title": "[fix-pr] o/r#9: Fix o/r#9",
            "body": "Repository: o/r\nIssue: #9\nIdempotency-Key: fix-pr:o/r:9\n",
            "status": "ready",
        }
        selected = issue_to_pr.select_dispatch_task(req({
            "conduction": {"read_dispatch_tasks": {"status": "read", "ok": True, "tasks": [review, fix]}},
        }))
        self.assertEqual(selected["status"], "selected")
        self.assertEqual(selected["task_id"], "fix-1")

        issue_task = {
            "id": "issue-1",
            "title": "[issue] o/r#9: bug",
            "body": "Repository: o/r\nIssue: #9\nIdempotency-Key: github-issue:o/r:9\n",
            "status": "ready",
        }
        # Review markers must not supersede the issue task for dispatch handoff.
        still_issue = issue_to_pr.select_dispatch_task(req({
            "conduction": {"read_dispatch_tasks": {"status": "read", "ok": True, "tasks": [issue_task, review]}},
        }))
        self.assertEqual(still_issue["task_id"], "issue-1")

    def test_non_fix_task_cannot_claim_fix_marker(self) -> None:
        impostor = {
            "id": "issue-1", "title": "[issue] o/r#9: bug", "status": "ready",
            "body": "Idempotency-Key: fix-pr:o/r:9\n",
        }
        found = issue_to_pr.find_fix_task_marker(req({
            "conduction": {"read_fix_tasks": {
                "status": "read", "ok": True, "repo": "o/r", "issue": 9,
                "board": "b", "tasks": [impostor],
            }},
        }))
        self.assertEqual(found["status"], "absent")
        self.assertIsNone(found.get("task_id"))

    def test_absent_fix_marker_does_not_inherit_dispatch_task_id(self) -> None:
        found = issue_to_pr.find_fix_task_marker(req({
            "repos": [{"repo": "mikolaj92/Temida", "board": "mikolaj92-temida", "clone_path": "/clones/Temida"}],
            "conduction": {
                "read_fix_tasks": {
                    "status": "read",
                    "ok": True,
                    "repo": "mikolaj92/Temida",
                    "issue": 3590,
                    "board": "mikolaj92-temida",
                    "branch": "ai/fix/3590-x",
                    "task_id": "t_14722de2",
                    "clone_path": "/clones/Temida",
                    "tasks": [],
                }
            },
        }))
        self.assertEqual(found["status"], "absent")
        self.assertEqual(found["marker"], "fix-pr:mikolaj92/Temida:3590")
        self.assertIsNone(found.get("task_id"))
        self.assertEqual(found.get("source_task_id"), "t_14722de2")
        created = issue_to_pr.create_fix_task(req({
            "repos": [{"repo": "mikolaj92/Temida", "board": "mikolaj92-temida", "clone_path": "/clones/Temida"}],
            "dry_run": True,
            "conduction": {"find_fix_task_marker": found},
        }))
        self.assertEqual(created["status"], "planned")
        self.assertNotEqual(created.get("status"), "fix_task_exists")

    def test_issue_task_reconciliation_stops_before_pr_work(self) -> None:
        fix_task = {
            "id": "fix-1",
            "title": "[fix-pr] o/r#9: Fix o/r#9",
            "body": "Idempotency-Key: fix-pr:o/r:9",
            "status": "ready",
        }
        with mock.patch("lokay.steps.issue_to_pr.hermes_kanban_json", return_value=[fix_task]):
            out = issue_to_pr.reconcile_fix_task(req({
                "conduction": {
                    "select_dispatch_task": {
                        "status": "selected", "ok": True, "task_id": "issue-1",
                        "task": {"id": "issue-1", "title": "[issue] o/r#9: bug"},
                    },
                    "create_fix_task": {
                        "status": "created", "ok": True, "board": "b",
                        "repo": "o/r", "issue": 9, "idempotency_key": "fix-pr:o/r:9",
                    },
                }
            }))
        self.assertEqual(out["status"], "noop")
        self.assertEqual(out["reason"], "fix_task_handoff")
        self.assertEqual(out["source_task_id"], "issue-1")
        self.assertEqual(out["task_id"], "fix-1")
        downstream = issue_to_pr.read_clone_preconditions(req({
            "conduction": {"reconcile_fix_task": out},
        }))
        self.assertEqual(downstream["status"], "noop")
        self.assertEqual(downstream["reason"], "fix_task_handoff")

    def test_fix_task_reconciliation_continues_to_pr_work(self) -> None:
        fix_task = {
            "id": "fix-1",
            "title": "[fix-pr] o/r#9: Fix o/r#9",
            "body": "Idempotency-Key: fix-pr:o/r:9",
            "status": "ready",
        }
        with mock.patch("lokay.steps.issue_to_pr.hermes_kanban_json", return_value=[fix_task]):
            out = issue_to_pr.reconcile_fix_task(req({
                "conduction": {
                    "select_dispatch_task": {
                        "status": "selected", "ok": True, "task_id": "fix-1", "task": fix_task,
                    },
                    "create_fix_task": {
                        "status": "fix_task_exists", "ok": True, "board": "b",
                        "repo": "o/r", "issue": 9, "task_id": "fix-1",
                        "task": fix_task, "idempotency_key": "fix-pr:o/r:9",
                    },
                }
            }))
        self.assertEqual(out["status"], "reconciled")
        self.assertEqual(out["task_id"], "fix-1")

    def test_branch_chain_resolves_from_inventory_identity(self) -> None:
        repos = [{"repo": "mikolaj92/Temida", "board": "mikolaj92-temida", "clone_path": "/clones/Temida"}]
        inventory = {
            "status": "read",
            "ok": True,
            "clone_path": "/clones/Temida",
            "repo": "mikolaj92/Temida",
            "issue": 3590,
            "board": "mikolaj92-temida",
            "branch": "ai/fix/3590-issue-mikolaj92-temida-3590-temida_llm",
            "task_id": "t_14722de2",
            "worktrees": [{"branch": "main", "head": "abc", "path": "/clones/Temida"}],
        }
        with mock.patch("lokay.steps.issue_to_pr.branch_exists", return_value=False):
            proven = issue_to_pr.read_branch_provenance(req({
                "repos": repos,
                "worktree_root": "/tmp/worktrees",
                "receipt_path": "/tmp/receipt.json",
                "conduction": {"read_worktree_inventory": inventory},
            }))
        self.assertEqual(proven["status"], "read")
        self.assertEqual(proven["branch"], "ai/fix/3590-issue-mikolaj92-temida-3590-temida_llm")
        self.assertEqual(proven["clone_path"], "/clones/Temida")
        self.assertEqual(proven["task_id"], "t_14722de2")
        self.assertEqual(proven["receipt"], "/tmp/receipt.json")
        self.assertTrue(str(proven.get("worktree_path") or "").endswith("ai/fix/3590-issue-mikolaj92-temida-3590-temida_llm"))
        with mock.patch("lokay.steps.issue_to_pr.branch_exists", return_value=False), mock.patch("lokay.steps.issue_to_pr.git") as git_mock:
            created = issue_to_pr.create_local_branch(req({
                "repos": repos,
                "worktree_root": "/tmp/worktrees",
                "receipt_path": "/tmp/receipt.json",
                "dry_run": True,
                "conduction": {
                    "read_branch_provenance": proven,
                    "read_base_ref": {"status": "read", "ok": True, "clone_path": "/clones/Temida", "base_head": "baseoid", "branch": proven["branch"], "repo": "mikolaj92/Temida", "issue": 3590, "task_id": "t_14722de2"},
                },
            }))
        self.assertEqual(created["status"], "planned")
        self.assertEqual(created["branch"], proven["branch"])
        self.assertEqual(Path(created["worktree_path"]).resolve(), Path(proven["worktree_path"]).resolve())
        git_mock.assert_not_called()

    def test_fix_chain_resolves_identity_and_clone_from_configured_repos(self) -> None:
        repos = [{"repo": "mikolaj92/Temida", "board": "mikolaj92-temida", "clone_path": "/clones/Temida"}]
        parsed = {"status": "parsed", "ok": True, "repo": "mikolaj92/Temida", "issue": 3590, "board": "mikolaj92-temida", "clone_path": "/clones/Temida"}
        read = {"status": "read", "ok": True, "repo": "mikolaj92/Temida", "issue": 3590, "board": "mikolaj92-temida", "clone_path": "/clones/Temida", "tasks": []}
        found = issue_to_pr.find_fix_task_marker(req({"repos": repos, "conduction": {"read_fix_tasks": read}}))
        self.assertEqual(found["marker"], "fix-pr:mikolaj92/Temida:3590")
        created = issue_to_pr.create_fix_task(req({"repos": repos, "dry_run": True, "conduction": {"find_fix_task_marker": found}}))
        self.assertEqual(created["repo"], "mikolaj92/Temida")
        self.assertEqual(created["issue"], 3590)
        self.assertEqual(created["clone_path"], "/clones/Temida")
        reconciled = {"status": "reconciled", "ok": True, "repo": created["repo"], "issue": created["issue"], "board": created["board"], "clone_path": created["clone_path"]}
        with mock.patch("lokay.steps.issue_to_pr.status_porcelain", return_value=""), mock.patch("lokay.steps.issue_to_pr.remote_url", return_value="git@github.com:mikolaj92/Temida.git"), mock.patch("pathlib.Path.exists", return_value=True):
            pre = issue_to_pr.read_clone_preconditions(req({"repos": repos, "conduction": {"reconcile_fix_task": reconciled}}))
        self.assertEqual(pre["status"], "ready")
        self.assertEqual(pre["clone_path"], "/clones/Temida")

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

    def test_invoke_omp_timeout_returns_failed_result(self) -> None:
        pre = {
            "status": "ready",
            "ok": True,
            "worktree_path": "/wt",
            "branch": "ai/fix/1",
            "pre_head": "abc",
            "base_head": "abc",
            "task_id": "t1",
        }
        with mock.patch("lokay.steps.issue_to_pr._omp_diff_paths", return_value=[]), mock.patch(
            "lokay.steps.issue_to_pr.run_omp",
            side_effect=subprocess.TimeoutExpired(cmd=["omp"], timeout=7200),
        ):
            out = issue_to_pr.invoke_omp(
                req(
                    {
                        "worktree_path": "/wt",
                        "prompt": "fix",
                        "timeout_seconds": 7200,
                        "dry_run": False,
                        "conduction": {"read_omp_preconditions": pre},
                    }
                )
            )
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["reason"], "omp_failed")
        self.assertTrue(out["timed_out"])
        self.assertTrue(out["mutated"])
        self.assertFalse(out["ok"])

    def test_complete_task_blocks_on_failed_invoke_omp(self) -> None:
        decision = {"status": "should_complete", "ok": True, "should_complete": True}
        failed_invoke = {
            "status": "failed",
            "ok": False,
            "reason": "omp_failed",
            "timed_out": True,
            "mutated": True,
        }
        blocked = issue_to_pr.complete_task(
            req(
                {
                    "board": "b",
                    "task_id": "t1",
                    "conduction": {
                        "decide_task_completion": decision,
                        "read_task_for_completion": {
                            "status": "read",
                            "ok": True,
                            "task_id": "t1",
                            "task": {"id": "t1", "status": "ready"},
                        },
                        "select_dispatch_task": {"status": "selected", "ok": True, "task_id": "t1"},
                        "invoke_omp": failed_invoke,
                    },
                }
            )
        )
        self.assertEqual(blocked["reason"], "upstream_failed")
        self.assertFalse(blocked["mutated"])
        self.assertEqual(blocked["upstream_effector"], "invoke_omp")

    def test_worktree_and_omp_dry_chain(self) -> None:
        pre = {"status": "ready", "ok": True, "clone_path": "/clone", "base_branch": "main"}
        fetched = issue_to_pr.fetch_clone_origin(req({"clone_path": "/clone", "dry_run": True, "conduction": {"read_clone_preconditions": pre}}))
        self.assertEqual(fetched["status"], "planned")
        omp_pre = {"status": "ready", "ok": True, "worktree_path": "/wt", "branch": "ai/fix/1", "pre_head": "abc"}
        omp = issue_to_pr.invoke_omp(req({"worktree_path": "/wt", "prompt": "fix", "dry_run": True, "conduction": {"read_omp_preconditions": omp_pre}}))
        self.assertEqual(omp["status"], "planned")
        self.assertFalse(omp["mutated"])

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
        pr_decision = {"status": "create", "ok": True, "should_create": True, "issue": 1}
        pr = issue_to_pr.create_pull_request(req({"repo": "o/r", "branch": "ai/fix/1", "dry_run": True, "conduction": {"decide_existing_pr": pr_decision}}))
        self.assertEqual(pr["status"], "planned")
        created = {"status": "created", "ok": True, "repo": "o/r", "issue": 1, "branch": "ai/fix/1", "base": "main"}
        with mock.patch("lokay.steps.issue_to_pr.run_cmd", return_value=SimpleNamespace(stdout=json.dumps([{"number": 2, "url": "https://example.test/pr/2", "baseRefName": "main", "headRefName": "ai/fix/1"}]))):
            reconciled = issue_to_pr.reconcile_pull_request(req({"conduction": {"dispatch_create_pull_request": created}}))
        self.assertEqual(reconciled["status"], "reconciled")
        self.assertEqual(reconciled["repo"], "o/r")
        self.assertEqual(reconciled["branch"], "ai/fix/1")
        self.assertEqual(reconciled["prs"][0]["number"], 2)
        with mock.patch("lokay.steps.issue_to_pr.run_cmd", side_effect=CommandError(["gh"], 1, "", "offline")):
            failed_reconcile = issue_to_pr.reconcile_pull_request(req({"conduction": {"dispatch_create_pull_request": created}}))
        self.assertEqual(failed_reconcile["status"], "failed")
        self.assertEqual(failed_reconcile["reason"], "pr_list_failed")
        self.assertTrue(failed_reconcile["retry_safe"])
        built = issue_to_pr.build_dispatch_receipt(req({"payload": {"phase": "CLAIMED"}}))
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "receipt.json")
            published = issue_to_pr.publish_dispatch_receipt(req({"receipt_path": path, "dry_run": False, "conduction": {"build_dispatch_receipt": built}}))
            verified = issue_to_pr.verify_dispatch_receipt(req({"receipt_path": path, "dry_run": False, "conduction": {"publish_dispatch_receipt": published, "build_dispatch_receipt": built}}))
            self.assertEqual(published["status"], "published")
            self.assertEqual(verified["status"], "verified")

    @mock.patch("lokay.steps.issue_to_pr.branch_config_get")
    @mock.patch("lokay.steps.issue_to_pr.branch_config_set")
    def test_pushed_head_refreshes_local_oid_for_cleanup(self, set_config, get_config) -> None:
        tip = "71d4027717c759b256ee2dc131d6d4f286aa1ea4"
        verified = {
            "ok": True,
            "status": "verified",
            "local_oid": tip,
            "remote_oid": tip,
            "repo": "owner/repo",
            "issue": 3722,
            "board": "owner-repo",
            "task_id": "t_test",
            "branch": "ai/fix/3722",
        }
        written = {
            "ok": True,
            "status": "written",
            "clone_path": "/clone",
            "branch": "ai/fix/3722",
        }
        updated = issue_to_pr.update_branch_local_oid(
            req(
                {
                    "clone_path": "/clone",
                    "branch": "ai/fix/3722",
                    "dry_run": False,
                    "conduction": {
                        "verify_push_oid": verified,
                        "write_branch_provenance": written,
                    },
                }
            )
        )
        self.assertTrue(updated["ok"])
        self.assertEqual(updated["status"], "updated")
        self.assertEqual(updated["local_oid"], tip)
        set_config.assert_called_once_with("/clone", "ai/fix/3722", "lokay-local-oid", tip)

        get_config.return_value = tip
        checked = issue_to_pr.verify_updated_branch_local_oid(
            req(
                {
                    "dry_run": False,
                    "conduction": {"update_branch_local_oid": updated},
                }
            )
        )
        self.assertTrue(checked["ok"])
        self.assertEqual(checked["status"], "verified")
        self.assertEqual(checked["local_oid"], tip)
        get_config.assert_called_once_with("/clone", "ai/fix/3722", "lokay-local-oid")

        planned = issue_to_pr.update_branch_local_oid(
            req(
                {
                    "clone_path": "/clone",
                    "branch": "ai/fix/3722",
                    "dry_run": True,
                    "conduction": {"verify_push_oid": verified},
                }
            )
        )
        self.assertEqual(planned["status"], "planned")
        set_config.assert_called_once()

    @mock.patch("lokay.steps.issue_to_pr.run_cmd")
    def test_read_open_pr_for_branch_uses_updated_local_oid_peer(self, run_cmd) -> None:
        """Package conduction only wires verify_updated_branch_local_oid after push re-auth."""
        run_cmd.return_value = SimpleNamespace(stdout="[]")
        verified = {
            "ok": True,
            "status": "verified",
            "operation": "verify_updated_branch_local_oid",
            "clone_path": "/clone",
            "branch": "ai/fix/3723-fix-pr-mikolaj92-temida-3723-fix-mikola",
            "local_oid": "f4c7412a6ba1b0334ea8d89dd5665f3ec0ad056a",
            "repo": "mikolaj92/Temida",
            "issue": 3723,
            "board": "mikolaj92-temida",
            "task_id": "t_41437db9",
        }
        # Live dispatch path: only this peer is conducted (not verify_push_oid).
        result = issue_to_pr.read_open_pr_for_branch(
            req(
                {
                    "dry_run": False,
                    "base_branch": "main",
                    "conduction": {"verify_updated_branch_local_oid": verified},
                }
            )
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "read")
        self.assertEqual(result["repo"], "mikolaj92/Temida")
        self.assertEqual(result["branch"], "ai/fix/3723-fix-pr-mikolaj92-temida-3723-fix-mikola")
        self.assertEqual(result["issue"], 3723)
        self.assertEqual(result["prs"], [])
        run_cmd.assert_called_once()
        args = run_cmd.call_args.args[0]
        self.assertEqual(args[1:3], ["pr", "list"])
        self.assertEqual(args[args.index("--repo") + 1], "mikolaj92/Temida")
        self.assertEqual(args[args.index("--head") + 1], "ai/fix/3723-fix-pr-mikolaj92-temida-3723-fix-mikola")

    def test_create_pull_request_serializes_recheck_create_and_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = {"created": False, "creates": 0, "branch": ""}
            gate = __import__("threading").Barrier(2)

            def gh(args, **kwargs):
                row = {"number": 2, "url": "https://example.test/pr/2", "baseRefName": "main", "headRefName": state["branch"], "closingIssuesReferences": [{"number": 1}]}
                if args[1:3] == ["pr", "list"]:
                    if "closingIssuesReferences" in args[args.index("--json") + 1]:
                        return SimpleNamespace(stdout=json.dumps([row] if state["created"] else []))
                    branch_row = {key: value for key, value in row.items() if key != "closingIssuesReferences"}
                    requested_branch = args[args.index("--head") + 1]
                    return SimpleNamespace(stdout=json.dumps([branch_row] if state["created"] and state["branch"] == requested_branch else []))
                if args[1:3] == ["pr", "create"]:
                    state["creates"] += 1
                    state["branch"] = args[args.index("--head") + 1]
                    state["created"] = True
                    return SimpleNamespace(stdout="created")
                raise AssertionError(args)

            original_lock_path = issue_to_pr._pr_creation_lock_path
            def synchronized_lock_path(request, repo, issue):
                gate.wait(timeout=2)
                return original_lock_path(request, repo, issue)
            requests = [
                req({"repo": "o/r", "branch": branch, "issue": 1, "dry_run": False, "conduction": {"decide_existing_pr": {"ok": True, "status": "create", "should_create": True}}}, {"task_receipts": tmp})
                for branch in ("ai/fix/1-intake", "ai/fix/1-canonical")
            ]
            with mock.patch("lokay.steps.issue_to_pr.run_cmd", side_effect=gh), mock.patch("lokay.steps.issue_to_pr._pr_creation_lock_path", side_effect=synchronized_lock_path):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(issue_to_pr.create_pull_request, requests))

        self.assertEqual(state["creates"], 1)
        self.assertEqual({result["status"] for result in results}, {"created", "already_open"})
        self.assertTrue(all(result.get("number") == 2 for result in results))

    def test_create_pull_request_lock_root_reads_live_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            decision = {
                "ok": True,
                "status": "create",
                "should_create": True,
                "repo": "o/r",
                "issue": 1,
                "board": "b",
                "task_id": "t1",
                "branch": "ai/fix/1",
                "base": "main",
            }
            request = {
                "input": {
                    "repo": "o/r",
                    "branch": "ai/fix/1",
                    "issue": 1,
                    "dry_run": False,
                    "task_receipts": tmp,
                    "conduction": {"dispatch_decide_existing_pr": decision},
                },
                # Live Fala package config is handler-only.
                "config": {"handler": "lokay.steps.issue_to_pr.create_pull_request"},
            }
            created = {
                "number": 9,
                "url": "https://example.test/pr/9",
                "baseRefName": "main",
                "headRefName": "ai/fix/1",
                "closingIssuesReferences": [{"number": 1}],
            }

            def gh(args, **kwargs):
                if args[1:3] == ["pr", "list"]:
                    if "closingIssuesReferences" in args[args.index("--json") + 1]:
                        return SimpleNamespace(stdout=json.dumps([created] if state["created"] else []))
                    return SimpleNamespace(stdout=json.dumps([created] if state["created"] else []))
                if args[1:3] == ["pr", "create"]:
                    state["created"] = True
                    return SimpleNamespace(stdout="https://example.test/pr/9")
                raise AssertionError(args)

            state = {"created": False}
            with mock.patch("lokay.steps.issue_to_pr.run_cmd", side_effect=gh):
                result = issue_to_pr.create_pull_request(request)
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "created")
            self.assertEqual(result["number"], 9)
            self.assertTrue((Path(tmp) / "pr-creation-locks").is_dir())

    def test_create_pull_request_falls_back_to_branch_recon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            decision = {
                "ok": True,
                "status": "create",
                "should_create": True,
                "repo": "o/r",
                "issue": 1,
                "board": "b",
                "task_id": "t1",
                "branch": "ai/fix/1",
                "base": "main",
            }
            branch_row = {
                "number": 9,
                "url": "https://example.test/pr/9",
                "baseRefName": "main",
                "headRefName": "ai/fix/1",
            }

            def gh(args, **kwargs):
                if args[1:3] == ["pr", "list"]:
                    if "closingIssuesReferences" in args[args.index("--json") + 1]:
                        # Issue link lag: create succeeded, closing refs not visible yet.
                        return SimpleNamespace(stdout=json.dumps([]))
                    requested = args[args.index("--head") + 1]
                    return SimpleNamespace(
                        stdout=json.dumps([branch_row] if state["created"] and requested == "ai/fix/1" else [])
                    )
                if args[1:3] == ["pr", "create"]:
                    state["created"] = True
                    return SimpleNamespace(stdout="created")
                raise AssertionError(args)

            state = {"created": False}
            with mock.patch("lokay.steps.issue_to_pr.run_cmd", side_effect=gh):
                result = issue_to_pr.create_pull_request(
                    req(
                        {
                            "repo": "o/r",
                            "branch": "ai/fix/1",
                            "issue": 1,
                            "dry_run": False,
                            "conduction": {"decide_existing_pr": decision},
                        },
                        {"task_receipts": tmp},
                    )
                )
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "created")
            self.assertEqual(result["number"], 9)

    def test_create_pull_request_already_open_uses_existing_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            decision = {
                "ok": True,
                "status": "create",
                "should_create": True,
                "repo": "o/r",
                "issue": 1,
                "board": "b",
                "task_id": "fix-1",
                "branch": "ai/fix/1-fix-pr-o-r-1",
                "base": "main",
            }
            existing = {
                "number": 77,
                "url": "https://example.test/pr/77",
                "baseRefName": "main",
                "headRefName": "ai/fix/1-issue-o-r-1",
                "closingIssuesReferences": [{"number": 1}],
            }

            def gh(args, **kwargs):
                if args[1:3] == ["pr", "list"]:
                    if "closingIssuesReferences" in args[args.index("--json") + 1]:
                        return SimpleNamespace(stdout=json.dumps([existing]))
                    return SimpleNamespace(stdout=json.dumps([]))
                raise AssertionError(args)

            with mock.patch("lokay.steps.issue_to_pr.run_cmd", side_effect=gh):
                created = issue_to_pr.create_pull_request(
                    req(
                        {
                            "repo": "o/r",
                            "branch": "ai/fix/1-fix-pr-o-r-1",
                            "issue": 1,
                            "dry_run": False,
                            "conduction": {"decide_existing_pr": decision},
                        },
                        {"task_receipts": tmp},
                    )
                )
            self.assertTrue(created["ok"])
            self.assertEqual(created["status"], "already_open")
            self.assertEqual(created["number"], 77)
            self.assertEqual(created["branch"], "ai/fix/1-issue-o-r-1")
            self.assertEqual(created["base"], "main")
            self.assertFalse(created["mutated"])

            # Reconcile must not re-query by the fix-pr branch.
            with mock.patch("lokay.steps.issue_to_pr.run_cmd", side_effect=AssertionError("gh should not run")):
                reconciled = issue_to_pr.reconcile_pull_request(
                    req(
                        {
                            "repo": "o/r",
                            "branch": "ai/fix/1-fix-pr-o-r-1",
                            "conduction": {"create_pull_request": created},
                        }
                    )
                )
            self.assertTrue(reconciled["ok"])
            self.assertEqual(reconciled["status"], "reconciled")
            self.assertEqual(reconciled["number"], 77)
            self.assertEqual(reconciled["branch"], "ai/fix/1-issue-o-r-1")

            task_row = {"id": "fix-1", "title": "[fix-pr] o/r#1", "status": "ready"}
            with mock.patch("lokay.steps.issue_to_pr.hermes_kanban_json", return_value=[task_row]):
                read = issue_to_pr.read_task_for_completion(
                    req(
                        {
                            "board": "b",
                            "task_id": "fix-1",
                            "conduction": {
                                "select_dispatch_task": {
                                    "status": "selected",
                                    "ok": True,
                                    "task_id": "fix-1",
                                    "task": task_row,
                                },
                                "create_pull_request": created,
                                "reconcile_pull_request": reconciled,
                            },
                        }
                    )
                )
            self.assertEqual(read["status"], "read")
            self.assertEqual(read["task_id"], "fix-1")

            decision = issue_to_pr.decide_task_completion(
                req({"conduction": {"read_task_for_completion": read}})
            )
            self.assertTrue(decision["should_complete"])
            completed = issue_to_pr.complete_task(
                req(
                    {
                        "board": "b",
                        "task_id": "fix-1",
                        "dry_run": True,
                        "conduction": {
                            "decide_task_completion": decision,
                            "read_task_for_completion": read,
                            "select_dispatch_task": {
                                "status": "selected",
                                "ok": True,
                                "task_id": "fix-1",
                            },
                            "create_pull_request": created,
                            "reconcile_pull_request": reconciled,
                        },
                    }
                )
            )
            self.assertEqual(completed["status"], "planned")
            self.assertNotEqual(completed.get("reason"), "issue_pr_already_open")


    def test_live_shaped_labels_and_receipt_preserve_identity(self) -> None:
        reconciled = {"status": "reconciled", "ok": True, "repo": "o/r", "number": 2, "issue": 1, "board": "b", "task_id": "t1"}
        normalized = issue_to_pr.normalize_pr_labels(req({"conduction": {"dispatch_reconcile_pull_request": reconciled}}))
        with mock.patch("lokay.steps.issue_to_pr.run_cmd"):
            pr_labeled = issue_to_pr.add_pr_label(req({"dry_run": False, "conduction": {"dispatch_normalize_pr_labels": normalized}}))
        pr_results = issue_to_pr.aggregate_pr_label_results(req({"conduction": {"dispatch_add_pr_label": pr_labeled}}))
        with mock.patch("lokay.steps.issue_to_pr.run_cmd"):
            issue_labeled = issue_to_pr.add_issue_label(req({"dry_run": False, "conduction": {"dispatch_aggregate_pr_label_results": pr_results}}))
        issue_results = issue_to_pr.aggregate_issue_label_results(req({"conduction": {"dispatch_add_issue_label": issue_labeled}}))
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "dispatch.json")
            built = issue_to_pr.build_dispatch_receipt(req({"dry_run": False, "receipt_path": path, "conduction": {"dispatch_aggregate_issue_label_results": issue_results}}))
            published = issue_to_pr.publish_dispatch_receipt(req({"dry_run": False, "conduction": {"dispatch_build_dispatch_receipt": built}}))
            verified = issue_to_pr.verify_dispatch_receipt(req({"dry_run": False, "conduction": {"dispatch_publish_dispatch_receipt": published, "dispatch_build_dispatch_receipt": built}}))
        self.assertEqual([normalized["status"], pr_labeled["status"], pr_results["status"], issue_labeled["status"], issue_results["status"], published["status"], verified["status"]], ["normalized", "added", "labeled", "added", "labeled", "published", "verified"])
        self.assertEqual(built["payload"], {"repo": "o/r", "issue": 1, "board": "b", "task_id": "t1"})

    def test_live_shaped_completion_reconciles_board_and_task(self) -> None:
        selected = {
            "status": "selected",
            "ok": True,
            "board": "mikolaj92-temida",
            "task_id": "t_54ac7a8b",
            "task": {"id": "t_54ac7a8b", "status": "ready"}
        }
        verified_receipt = {
            "status": "verified",
            "ok": True,
            "receipt_path": "/path/receipt.json"
        }
        with mock.patch("lokay.steps.issue_to_pr.hermes_kanban_json") as list_cmd:
            list_cmd.return_value = [{"id": "t_54ac7a8b", "status": "ready"}]
            res = issue_to_pr.read_task_for_completion(req({
                "conduction": {
                    "dispatch_select_dispatch_task": selected,
                    "dispatch_verify_dispatch_receipt": verified_receipt
                }
            }))
        self.assertEqual(res["status"], "read")
        self.assertEqual(res["board"], "mikolaj92-temida")
        self.assertEqual(res["task_id"], "t_54ac7a8b")
        list_cmd.assert_called_once_with(["--board", "mikolaj92-temida", "list", "--json", "--sort", "created-desc"])

    def test_live_shaped_verify_task_completed_success(self) -> None:
        completed = {
            "status": "completed",
            "ok": True,
            "board": "mikolaj92-temida",
            "task_id": "t_54ac7a8b",
        }
        with mock.patch("lokay.steps.issue_to_pr.hermes_kanban_json") as list_cmd:
            list_cmd.return_value = [{"id": "t_54ac7a8b", "status": "done"}]
            res = issue_to_pr.verify_task_completed(req({
                "conduction": {
                    "dispatch_complete_task": completed
                }
            }))
        self.assertEqual(res["status"], "verified")
        self.assertEqual(res["task_id"], "t_54ac7a8b")
        list_cmd.assert_called_once_with(["--board", "mikolaj92-temida", "list", "--json", "--sort", "created-desc"])

    def test_live_shaped_verify_task_completed_failure(self) -> None:
        completed = {
            "status": "completed",
            "ok": True,
            "board": "mikolaj92-temida",
            "task_id": "t_54ac7a8b",
        }
        with mock.patch("lokay.steps.issue_to_pr.hermes_kanban_json") as list_cmd:
            list_cmd.return_value = [{"id": "t_54ac7a8b", "status": "ready"}]
            res = issue_to_pr.verify_task_completed(req({
                "conduction": {
                    "dispatch_complete_task": completed
                }
            }))
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["reason"], "task_not_completed")
        self.assertEqual(res["task_id"], "t_54ac7a8b")

    def test_reconcile_pull_request_edges(self) -> None:
        created = {"status": "created", "ok": True, "repo": "o/r", "issue": 1, "branch": "ai/fix/1", "base": "main"}

        # Edge 1: No PRs found
        with mock.patch("lokay.steps.issue_to_pr.run_cmd", return_value=SimpleNamespace(stdout=json.dumps([]))):
            res = issue_to_pr.reconcile_pull_request(req({"conduction": {"dispatch_create_pull_request": created}}))
        self.assertEqual(res["reason"], "no_matching_pr")
        self.assertFalse(res["ok"])

        # Edge 2: Multiple PRs found
        multiple_prs = [
            {"number": 2, "url": "https://example.test/pr/2", "baseRefName": "main", "headRefName": "ai/fix/1"},
            {"number": 3, "url": "https://example.test/pr/3", "baseRefName": "main", "headRefName": "ai/fix/1"},
        ]
        with mock.patch("lokay.steps.issue_to_pr.run_cmd", return_value=SimpleNamespace(stdout=json.dumps(multiple_prs))):
            res = issue_to_pr.reconcile_pull_request(req({"conduction": {"dispatch_create_pull_request": created}}))
        self.assertEqual(res["reason"], "ambiguous_matching_prs")
        self.assertFalse(res["ok"])

        # Edge 3: Invalid/non-positive PR number
        bad_pr = [{"number": 0, "url": "https://example.test/pr/0", "baseRefName": "main", "headRefName": "ai/fix/1"}]
        with mock.patch("lokay.steps.issue_to_pr.run_cmd", return_value=SimpleNamespace(stdout=json.dumps(bad_pr))):
            res = issue_to_pr.reconcile_pull_request(req({"conduction": {"dispatch_create_pull_request": created}}))
        self.assertEqual(res["reason"], "invalid_pr_number")
        self.assertFalse(res["ok"])

class RepairTests(unittest.TestCase):
    def test_build_repair_prompt_uses_exact_linked_issue_and_lane_context(self) -> None:
        out = repair.build_repair_prompt(req({
            "issue": 10, "failures": ["ci"], "reason": "checks_failed",
            "worktree_root": "/wt-root",
            "conduction": {"load_pr_fields": {
                "status": "loaded", "repo": "owner/repo", "number": 11,
                "board": "board", "clone_path": "/clone", "priority": 2,
                "pr": {"number": 11, "title": "fix", "headRefName": "ai/fix/11", "closingIssuesReferences": [{"number": 10}]},
            }},
        }))
        self.assertTrue(out["ok"])
        self.assertEqual(out["issue"], 10)
        self.assertEqual(out["pr_number"], 11)
        local_branch = repair._repair_local_branch("owner/repo", "11", "ai/fix/11")
        worktree_path = f"/wt-root/{local_branch}"
        for value in ("owner/repo", "#10", "ai/fix/11", "/clone", "board", "priority 2", local_branch, worktree_path):
            self.assertIn(value, out["prompt"])
        self.assertIn("Commit the changes locally so HEAD advances.", out["prompt"])
        self.assertIn("Do not push, force-push, or merge.", out["prompt"])
        self.assertIn("Stay in this cwd", out["prompt"])
        self.assertIn("Do not cd into Clone", out["prompt"])
        self.assertEqual(out.get("worktree_path"), worktree_path)
        self.assertEqual(out.get("local_branch"), local_branch)

    def test_build_repair_prompt_requires_nonempty_evidence_change(self) -> None:
        out = repair.build_repair_prompt(req({
            "issue": 10, "failures": [], "reason": "missing_test_evidence",
            "worktree_root": "/wt-root",
            "conduction": {"load_pr_fields": {
                "status": "loaded", "repo": "owner/repo", "number": 11,
                "board": "board", "clone_path": "/clone", "priority": 2,
                "pr": {
                    "number": 11, "title": "fix", "headRefName": "ai/fix/11",
                    "closingIssuesReferences": [{"number": 10}],
                },
            }},
        }))
        self.assertTrue(out["ok"])
        self.assertEqual(out["reason"], "missing_test_evidence")
        self.assertIn("missing required test evidence", out["prompt"])
        self.assertIn("PR body, non-lokay comments, and commit messages", out["prompt"])
        self.assertIn("real non-empty tree change", out["prompt"])
        self.assertIn("Evidence:", out["prompt"])
        self.assertIn("empty commits", out["prompt"])
        self.assertIn("Do not push, force-push, or merge.", out["prompt"])
        local_branch = repair._repair_local_branch("owner/repo", "11", "ai/fix/11")
        self.assertIn(f"/wt-root/{local_branch}", out["prompt"])
        self.assertIn("Stay in this cwd", out["prompt"])

    def test_build_repair_prompt_soft_omits_worktree_without_root(self) -> None:
        out = repair.build_repair_prompt(req({
            "issue": 10, "failures": ["ci"], "reason": "checks_failed",
            "conduction": {"load_pr_fields": {
                "status": "loaded", "repo": "owner/repo", "number": 11,
                "board": "board", "clone_path": "/clone", "priority": 2,
                "pr": {"number": 11, "title": "fix", "headRefName": "ai/fix/11", "closingIssuesReferences": [{"number": 10}]},
            }},
        }))
        self.assertTrue(out["ok"])
        local_branch = repair._repair_local_branch("owner/repo", "11", "ai/fix/11")
        self.assertNotIn("Work only inside Worktree", out["prompt"])
        self.assertIn("Stay in the provided cwd worktree", out["prompt"])
        self.assertIn(f"Local branch: {local_branch}", out["prompt"])
        self.assertIn("Worktree: n/a", out["prompt"])
        self.assertNotIn("worktree_path", out)
        self.assertEqual(out.get("local_branch"), local_branch)

    def test_build_repair_prompt_reads_cfg_worktree_root(self) -> None:
        out = repair.build_repair_prompt(req(
            {
                "issue": 10, "failures": ["ci"], "reason": "checks_failed",
                "conduction": {"load_pr_fields": {
                    "status": "loaded", "repo": "owner/repo", "number": 11,
                    "board": "board", "clone_path": "/clone", "priority": 2,
                    "pr": {"number": 11, "title": "fix", "headRefName": "ai/fix/11", "closingIssuesReferences": [{"number": 10}]},
                }},
            },
            config={"paths": {"worktree_root": "/cfg-wt"}},
        ))
        self.assertTrue(out["ok"])
        local_branch = repair._repair_local_branch("owner/repo", "11", "ai/fix/11")
        self.assertEqual(out.get("worktree_path"), f"/cfg-wt/{local_branch}")
        self.assertIn("Work only inside Worktree", out["prompt"])

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

    def test_repair_noop_gates_context_base_and_tail(self) -> None:
        decision = {"status": "decided", "ok": True, "action": "comment_block", "reason": "missing_test_evidence"}
        request = req({"conduction": {"triage_decide_triage_action": decision}})
        with mock.patch("lokay.steps.repair.run_cmd") as run:
            for result in (
                repair.read_repair_context(request),
                repair.read_repair_base_head(request),
                repair.read_repair_remote_head(request),
            ):
                self.assertEqual(result["status"], "noop")
                self.assertEqual(result["reason"], "not_selected")
                self.assertFalse(result["mutated"])
        run.assert_not_called()

    def test_repair_tail_rejects_failed_conducted_evidence(self) -> None:
        failed = {"status": "failed", "ok": False, "reason": "broken", "mutated": False}
        authorized = {"status": "authorized", "ok": True, "authorize": True}
        base = {"conduction": {"decide_repair_attempt": authorized}}

        prompt = repair.build_repair_prompt(req({"pr": {"closingIssuesReferences": [{"number": 10}]}, "conduction": {"evaluate_checks": failed}}))
        self.assertEqual(prompt["reason"], "upstream_failed")

        cases = (
            (repair.reserve_repair_attempt, "read_repair_attempt_baseline"),
            (repair.verify_repair_attempt_reservation, "verify_repair_attempt_recovery"),
            (repair.invoke_repair_omp, "build_repair_prompt"),
            (repair.update_repair_branch_provenance, "verify_repair_push_oid"),
            (repair.build_repair_receipt, "verify_repair_omp_postconditions"),
            (repair.verify_repair_receipt, "build_repair_receipt"),
        )
        with mock.patch("lokay.steps.repair.run_omp") as run_omp, mock.patch("lokay.steps.repair.branch_config_set") as config_set:
            for handler, peer in cases:
                with self.subTest(handler=handler.__name__, peer=peer):
                    out = handler(req({**base, "conduction": {**base["conduction"], peer: failed}}))
                    self.assertEqual(out["status"], "failed")
                    self.assertEqual(out["reason"], "upstream_failed")
                    self.assertFalse(out["mutated"])
        run_omp.assert_not_called()
        config_set.assert_not_called()

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


    def test_fetch_repair_remote_head_targets_exact_branch_without_force_or_reset(self) -> None:
        context = {"repo": "owner/repo", "issue": "10", "pr_number": "11", "branch": "feature/x", "clone_path": "/clone", "worktree_root": "/worktrees", "remote": "upstream"}
        remote_oid = "a" * 40
        with mock.patch("lokay.steps.repair.git") as git_call, mock.patch("lokay.steps.repair.rev_parse") as rev_parse_call:
            out = repair.fetch_repair_remote_head(req({**context, "dry_run": False, "conduction": {
                "read_repair_remote_head": {"ok": True, "status": "read", "remote_oid": remote_oid},
            }}))
        self.assertTrue(out["ok"])
        self.assertTrue(out["mutated"])
        rev_parse_call.assert_not_called()
        command = git_call.call_args.args[0]
        self.assertEqual(command[:3], ["fetch", "--no-tags", "upstream"])
        self.assertEqual(command[3], "refs/heads/feature/x:" + out["acquired_ref"])
        self.assertNotIn("--force", command)
        self.assertNotIn("reset", command)
    def test_fast_forward_rejects_boundary_branch_head_and_dirty_drift(self) -> None:
        context = {"repo": "owner/repo", "issue": 10, "pr_number": 11, "branch": "feature/x", "clone_path": "/clone", "worktree_root": "/worktrees"}
        expected = "a" * 40
        remote = "b" * 40
        cases = (("branch", "foreign-branch", expected, ""), ("head", "lokay/repair/x", "c" * 40, ""), ("dirty", "lokay/repair/x", expected, " M file"))
        for label, branch, head, porcelain in cases:
            decision = {"ok": True, "status": "authorized", "should_fast_forward": True, "authorized_branch": "lokay/repair/x", "authorized_local_oid": expected, "remote_oid": remote}
            with self.subTest(label=label), mock.patch("lokay.steps.repair.git", side_effect=[branch, porcelain] if label == "branch" else [branch, porcelain]) as git_call, mock.patch("lokay.steps.repair.rev_parse", return_value=head):
                out = repair.fast_forward_repair_worktree(req({**context, "dry_run": False, "conduction": {"decide_repair_worktree_fast_forward_execution": decision}}))
            self.assertFalse(out["ok"])
            self.assertFalse(out.get("mutated"))
            self.assertFalse(any(call.args and call.args[0][:1] == ["merge"] for call in git_call.call_args_list))

    def test_fast_forward_requires_exact_post_merge_head(self) -> None:
        context = {"repo": "owner/repo", "issue": 10, "pr_number": 11, "branch": "feature/x", "clone_path": "/clone", "worktree_root": "/worktrees"}
        expected = "a" * 40
        remote = "b" * 40
        decision = {"ok": True, "status": "authorized", "should_fast_forward": True, "authorized_branch": "lokay/repair/x", "authorized_local_oid": expected, "remote_oid": remote}
        with mock.patch("lokay.steps.repair.claim_directory_lock") as lock, mock.patch("lokay.steps.repair.git", side_effect=["lokay/repair/x", "", ""]), mock.patch("lokay.steps.repair.rev_parse", side_effect=[expected, "c" * 40]):
            lock.return_value.__enter__.return_value = None
            out = repair.fast_forward_repair_worktree(req({**context, "dry_run": False, "conduction": {"decide_repair_worktree_fast_forward_execution": decision}}))
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "repair_fast_forward_readback_mismatch")
        self.assertTrue(out["mutated"])
    def test_fast_forward_stops_after_legacy_refresh(self) -> None:
        refreshed = {"status": "refreshed", "ok": True, "refresh_kind": "legacy_base_synchronization"}
        authorized = {"ok": True, "status": "authorized", "should_fast_forward": True, "authorized_branch": "lokay/repair/x", "authorized_local_oid": "a" * 40, "remote_oid": "b" * 40}
        request = req({"conduction": {"verify_legacy_repair_pr_head": refreshed, "decide_repair_worktree_fast_forward_execution": authorized}})
        with mock.patch("lokay.steps.repair.git") as git_call:
            out = repair.fast_forward_repair_worktree(request)
        self.assertEqual(out["status"], "noop")
        self.assertEqual(out["reason"], "legacy_base_refreshed")
        self.assertFalse(out["mutated"])
        git_call.assert_not_called()


    def test_ownership_propagates_legacy_refresh_failure(self) -> None:
        failed = {"ok": False, "status": "failed", "reason": "legacy_refresh_failed", "mutated": False}
        out = repair.decide_repair_worktree_ownership(req({"conduction": {"verify_legacy_repair_pr_head": failed}}))
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "upstream_failed")

    def test_fetch_repair_remote_head_failure_is_mutation_unknown(self) -> None:
        context = {"repo": "owner/repo", "issue": "10", "pr_number": "11", "branch": "feature/x", "clone_path": "/clone", "worktree_root": "/worktrees"}
        with mock.patch("lokay.steps.repair.git", side_effect=CommandError(["git", "fetch"], 1, "", "fetch failed")):
            out = repair.fetch_repair_remote_head(req({**context, "dry_run": False, "conduction": {
                "read_repair_remote_head": {"ok": True, "status": "read", "remote_oid": "a" * 40},
            }}))
        self.assertFalse(out["ok"])
        self.assertEqual(out["failure_class"], "reconcile_then_retry")
        self.assertFalse(out["retry_safe"])
        self.assertTrue(out["mutated"])
        self.assertTrue(out["mutation_unknown"])

    def test_verify_fetched_repair_remote_head_fails_closed_on_oid_mismatch(self) -> None:
        context = {"repo": "owner/repo", "issue": "10", "pr_number": "11", "branch": "feature/x", "clone_path": "/clone", "worktree_root": "/worktrees"}
        fetched = {"ok": True, "status": "fetched", "remote_oid": "a" * 40, "acquired_ref": "refs/lokay/acquired"}
        with mock.patch("lokay.steps.repair.rev_parse", return_value="b" * 40) as rev_parse_call:
            out = repair.verify_fetched_repair_remote_head(req({**context, "dry_run": False, "conduction": {"fetch_repair_remote_head": fetched}}))
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "repair_remote_head_verification_mismatch")
        self.assertFalse(out["retry_safe"])
        rev_parse_call.assert_called_once_with("/clone", "refs/lokay/acquired")

    def test_verify_fetched_repair_remote_head_exposes_verified_oid(self) -> None:
        context = {"repo": "owner/repo", "issue": "10", "pr_number": "11", "branch": "feature/x", "clone_path": "/clone", "worktree_root": "/worktrees"}
        oid = "a" * 40
        with mock.patch("lokay.steps.repair.rev_parse", return_value=oid):
            out = repair.verify_fetched_repair_remote_head(req({**context, "dry_run": False, "conduction": {"fetch_repair_remote_head": {"ok": True, "status": "fetched", "remote_oid": oid, "acquired_ref": "refs/lokay/acquired"}}}))
        self.assertTrue(out["ok"])
        self.assertTrue(out["verified"])
        self.assertEqual(out["acquired_oid"], oid)
    def test_dry_run_remote_head_chain_stays_planned(self) -> None:
        context = {
            "repo": "owner/repo",
            "issue": "10",
            "pr_number": "11",
            "branch": "feature/x",
            "clone_path": "/clone",
            "worktree_root": "/worktrees",
            "dry_run": True,
        }
        fetched = repair.fetch_repair_remote_head(req({
            **context,
            "conduction": {
                "read_repair_remote_head": {
                    "ok": True,
                    "status": "read",
                    "remote_oid": "a" * 40,
                }
            },
        }))
        verified = repair.verify_fetched_repair_remote_head(req({
            **context,
            "conduction": {"fetch_repair_remote_head": fetched},
        }))
        ancestry = repair.read_repair_remote_ancestry(req({
            **context,
            "conduction": {"verify_fetched_repair_remote_head": verified},
        }))
        self.assertEqual(fetched["status"], "planned")
        self.assertEqual(verified["status"], "planned")
        self.assertEqual(ancestry["status"], "planned")
        self.assertEqual(ancestry["remote_oid"], "a" * 40)
        self.assertNotIn("acquired_oid", ancestry)

    def test_repair_lifecycle_gate_routes_authoritative_outcomes(self) -> None:
        operation = "read_repair_attempt_state"
        for outcome, expected in (
            ("resume_repair", None),
            ("ready_for_merge", "noop"),
            ("wait_pending_checks", "noop"),
            ("finalize_merged", "noop"),
            ("finalize_closed", "noop"),
        ):
            with self.subTest(outcome=outcome):
                result = repair._repair_lifecycle_gate(req({
                    "conduction": {
                        "lifecycle_decide_lifecycle_transition": {
                            "ok": True,
                            "status": "decided",
                            "action": outcome,
                        }
                    }
                }), operation, "lifecycle_decide_lifecycle_transition")
                if expected is None:
                    self.assertIsNone(result)
                else:
                    self.assertEqual(result["status"], expected)
                    self.assertEqual(result["reason"], outcome)

        inactive = repair._repair_lifecycle_gate(req({
            "conduction": {
                "lifecycle_decide_lifecycle_transition": {
                    "ok": True,
                    "status": "noop",
                    "reason": "no_open_prs",
                }
            }
        }), operation, "lifecycle_decide_lifecycle_transition")
        self.assertEqual((inactive["status"], inactive["reason"]), ("noop", "no_open_prs"))

        malformed = repair._repair_lifecycle_gate(req({
            "conduction": {
                "lifecycle_decide_lifecycle_transition": {
                    "ok": True,
                    "status": "decided",
                    "action": "unknown",
                }
            }
        }), operation, "lifecycle_decide_lifecycle_transition")
        self.assertFalse(malformed["ok"])
        self.assertEqual(malformed["reason"], "invalid_repair_lifecycle")

    def test_review_task_dry_run_reconciliation_stays_planned(self) -> None:
        created = {
            "ok": True,
            "status": "planned",
            "board": "board-r",
            "idempotency_key": "fix-pr-review:o/r:9",
        }
        out = repair.reconcile_review_task(req({
            "dry_run": True,
            "conduction": {
                "decide_triage_action": {
                    "ok": True,
                    "status": "decided",
                    "action": "repair",
                },
                "create_review_task": created,
            },
        }))
        self.assertEqual(out["status"], "planned")
        self.assertEqual(out["board"], "board-r")
        self.assertEqual(out["idempotency_key"], "fix-pr-review:o/r:9")
    @mock.patch("lokay.steps.issue_to_pr.hermes_kanban_json")
    def test_review_task_reconciliation_uses_created_identity(self, kanban: mock.Mock) -> None:
        marker = "fix-pr-review:o/r:9"
        kanban.return_value = [{"id": "task-1", "title": "[fix-pr-review] o/r#9", "body": f"Idempotency-Key: {marker}"}]
        out = repair.reconcile_review_task(req({
            "conduction": {
                "decide_triage_action": {
                    "ok": True,
                    "status": "decided",
                    "action": "repair",
                },
                "create_review_task": {
                    "ok": True,
                    "status": "created",
                    "board": "board-r",
                    "marker": marker,
                },
            },
        }))
        self.assertEqual(out["status"], "reconciled")
        self.assertEqual(out["board"], "board-r")
        self.assertEqual(out["marker"], marker)


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
    def test_interrupted_repair_branch_requires_exact_creation_evidence(self) -> None:
        context = {
            "repo": "owner/repo", "issue": "10", "pr_number": "11", "branch": "ai/fix/10",
            "clone_path": "/clone", "worktree_root": "/worktrees", "repair_state_root": "/state",
            "candidate": "c" * 64, "run_id": "current-run", "db_path": "/journal.sqlite",
        }
        resolved = repair._repair_context(req(context))
        remote_oid = "a" * 40
        base = {
            "read_repair_context": {"ok": True, "status": "read", **resolved},
            "read_repair_remote_head": {"ok": True, "status": "read", "remote_oid": remote_oid},
            "read_repair_worktree_inventory": {"ok": True, "status": "read", "worktrees": []},
            "read_repair_branch_provenance": {"ok": True, "status": "read", "exists": True, "branch_head": remote_oid, "provenance": {}},
        }
        denied = repair.decide_repair_worktree_ownership(req({**context, "conduction": {**base, "read_repair_creation_evidence": {"ok": True, "status": "absent", "verified": False}}}))
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["reason"], "foreign_repair_branch_ownership")
        verified = {"ok": True, "status": "verified", "verified": True, "remote_oid": remote_oid}
        recovered = repair.decide_repair_worktree_ownership(req({**context, "conduction": {**base, "read_repair_creation_evidence": verified}}))
        self.assertTrue(recovered["ok"])
        self.assertTrue(recovered["recover_branch"])
        stale = dict(base["read_repair_branch_provenance"], branch_head="b" * 40)
        rejected = repair.decide_repair_worktree_ownership(req({**context, "conduction": {**base, "read_repair_branch_provenance": stale, "read_repair_creation_evidence": verified}}))
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["reason"], "foreign_repair_branch_ownership")

    def test_recovery_precedes_reuse_and_repairs_provenance(self) -> None:
        context = {
            "repo": "owner/repo", "issue": "10", "pr_number": "11", "branch": "ai/fix/10",
            "clone_path": "/clone", "worktree_root": "/worktrees", "repair_state_root": "/state",
            "dry_run": False,
        }
        resolved = repair._repair_context(req(context))
        remote_oid = "a" * 40
        decision = {
            "ok": True, "status": "recover", "recover_branch": True, "reuse": True,
            "remote_oid": remote_oid, **resolved,
        }
        created = repair.create_repair_branch(req({**context, "conduction": {"decide_repair_worktree_ownership": decision}}))
        self.assertEqual(created["status"], "recovered")
        with mock.patch("lokay.steps.repair.branch_config_set") as set_config:
            written = repair.write_repair_branch_provenance(req({**context, "conduction": {
                "create_repair_branch": created,
                "decide_repair_worktree_ownership": decision,
            }}))
        self.assertEqual(written["status"], "written")
        self.assertTrue(set_config.called)
        with mock.patch("lokay.steps.repair.worktree_add") as add_worktree:
            added = repair.add_repair_worktree(req({**context, "conduction": {
                "create_repair_branch": created,
                "write_repair_branch_provenance": written,
                "decide_repair_worktree_ownership": decision,
            }}))
        self.assertEqual(added["status"], "reused")
        add_worktree.assert_not_called()


    def test_repair_setup_context_survives_atomic_conduction(self) -> None:
        context = {
            "repo": "owner/repo",
            "issue": "10",
            "pr_number": "11",
            "branch": "ai/fix/10",
            "clone_path": "/clone",
            "worktree_root": "/worktrees",
            "repair_state_root": "/state",
        }
        created = repair._repair_context(req(context))
        downstream = repair._repair_context(req({
            "worktree_root": "/worktrees",
            "repair_state_root": "/state",
            "conduction": {"create_repair_branch": {"ok": True, "status": "created", **created}},
        }))
        self.assertEqual(downstream, created)
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

    def test_decide_repair_push_reads_before_oid_from_postconditions(self) -> None:
        data = {
            "repo": "owner/repo",
            "issue": 10,
            "pr_number": 11,
            "branch": "ai/fix/10",
            "clone_path": "/clone",
            "worktree_root": "/worktrees",
            "repair_state_root": "/state",
            "remote": "upstream",
            "live": True,
            "enabled": True,
            "dry_run": False,
        }
        before = "a" * 40
        after = "b" * 40
        authorized = {"ok": True, "status": "invoke", "authorize": True, "decision": "invoke"}
        result = repair.decide_repair_push(req({
            **data,
            "conduction": {
                "decide_repair_attempt": authorized,
                "read_repair_worktree_head": {
                    "ok": True,
                    "status": "read",
                    "local_oid": after,
                    "after_oid": after,
                },
                "verify_repair_omp_postconditions": {
                    "ok": True,
                    "status": "verified",
                    "before_oid": before,
                    "after_oid": after,
                },
            },
        }))
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "push")
        self.assertTrue(result["should_push"])
        self.assertEqual(result["before_oid"], before)
        self.assertEqual(result["local_oid"], after)

        missing = repair.decide_repair_push(req({
            **data,
            "conduction": {
                "decide_repair_attempt": authorized,
                "read_repair_worktree_head": {
                    "ok": True,
                    "status": "read",
                    "local_oid": after,
                    "after_oid": after,
                },
                # typo name must not be required; without verify/remote before_oid this fails closed
                "read_repair_omp_postconditions": {
                    "ok": True,
                    "status": "verified",
                    "before_oid": before,
                    "after_oid": after,
                },
            },
        }))
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["reason"], "missing_repair_push_oids")

    def test_verify_repair_omp_postconditions_emits_worktree_path(self) -> None:
        data = {
            "repo": "owner/repo",
            "issue": 10,
            "pr_number": 11,
            "branch": "ai/fix/10",
            "clone_path": "/clone",
            "worktree_root": "/worktrees",
            "repair_state_root": "/state",
            "live": True,
            "enabled": True,
            "dry_run": False,
        }
        context = repair._repair_context(req(data))
        before = "a" * 40
        after = "b" * 40
        preconditions = {
            "ok": True,
            "status": "ready",
            "worktree_path": context["worktree_path"],
            "pre_head": before,
            "repo": context["repo"],
            "issue": context["issue"],
            "pr_number": context["pr_number"],
            "branch": context["branch"],
            "local_branch": context["local_branch"],
            "clone_path": context["clone_path"],
            "worktree_root": context["worktree_root"],
            "remote": "origin",
        }
        with mock.patch("lokay.steps.repair.git", side_effect=[context["worktree_path"], "changed.py"]), mock.patch(
            "lokay.steps.repair.rev_parse",
            return_value=after,
        ):
            out = repair.verify_repair_omp_postconditions(req({
                **data,
                "conduction": {
                    "decide_repair_attempt": {"ok": True, "status": "invoke", "authorize": True},
                    "invoke_repair_omp": {"ok": True, "status": "invoked", "mutated": True, "omp": {"status": "completed"}},
                    "read_repair_omp_preconditions": preconditions,
                },
            }))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "verified")
        self.assertEqual(out["worktree_path"], context["worktree_path"])
        self.assertEqual(out["before_oid"], before)
        self.assertEqual(out["after_oid"], after)
        self.assertEqual(out["branch"], context["branch"])

    def test_read_repair_worktree_head_uses_postconditions_path(self) -> None:
        path = "/worktrees/lokay/repair/deadbeef"
        after = "c" * 40
        with mock.patch("lokay.steps.repair.rev_parse", return_value=after) as rev:
            out = repair.read_repair_worktree_head(req({
                "live": True,
                "enabled": True,
                "dry_run": False,
                "worktree_root": "/worktrees",
                "conduction": {
                    "decide_repair_attempt": {"ok": True, "status": "invoke", "authorize": True},
                    "verify_repair_omp_postconditions": {
                        "ok": True,
                        "status": "verified",
                        "before_oid": "a" * 40,
                        "after_oid": after,
                        "worktree_path": path,
                    },
                },
            }))
        rev.assert_called_once_with(path)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["local_oid"], after)
        self.assertEqual(out["worktree_path"], path)

    def test_push_repair_branch_uses_decide_target_fields(self) -> None:
        path = "/worktrees/lokay/repair/deadbeef"
        branch = "ai/fix/10"
        with mock.patch("lokay.steps.repair.git_push_branch", return_value="pushed") as push:
            out = repair.push_repair_branch(req({
                "live": True,
                "enabled": True,
                "dry_run": False,
                "worktree_root": "/worktrees",
                "conduction": {
                    "decide_repair_attempt": {"ok": True, "status": "invoke", "authorize": True},
                    "decide_repair_push": {
                        "ok": True,
                        "status": "push",
                        "should_push": True,
                        "before_oid": "a" * 40,
                        "local_oid": "b" * 40,
                        "worktree_path": path,
                        "branch": branch,
                        "remote": "origin",
                    },
                },
            }))
        push.assert_called_once_with(path, branch, remote="origin", set_upstream=False)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "pushed")
        self.assertEqual(out["worktree_path"], path)
        self.assertEqual(out["branch"], branch)

    def test_read_repair_pushed_ref_uses_push_target_fields(self) -> None:
        path = "/worktrees/lokay/repair/deadbeef"
        branch = "ai/fix/10"
        remote_oid = "d" * 40
        with mock.patch(
            "lokay.steps.repair.git",
            return_value=f"{remote_oid}\trefs/heads/{branch}",
        ) as git_cmd:
            out = repair.read_repair_pushed_ref(req({
                "live": True,
                "enabled": True,
                "dry_run": False,
                "worktree_root": "/worktrees",
                "conduction": {
                    "decide_repair_attempt": {"ok": True, "status": "invoke", "authorize": True},
                    "push_repair_branch": {
                        "ok": True,
                        "status": "pushed",
                        "worktree_path": path,
                        "branch": branch,
                        "remote": "origin",
                        "repo": "owner/repo",
                        "pr_number": "11",
                    },
                },
            }))
        git_cmd.assert_called_once_with(["ls-remote", "origin", f"refs/heads/{branch}"], cwd=path)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["remote_oid"], remote_oid)
        self.assertEqual(out["worktree_path"], path)
        self.assertEqual(out["branch"], branch)

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
        same_head = repair._repair_attempt_receipt(req(data), payload("a" * 40, "d" * 40, "run-d"))
        self.assertNotEqual(first, second)
        self.assertEqual(first, same_head)
        self.assertTrue(first.startswith("/state/repair-receipts/owner__repo/11/"))
        self.assertTrue(second.startswith("/state/repair-receipts/owner__repo/11/"))
        self.assertNotEqual(first, context["receipt"])

    def test_same_head_repair_receipt_conflict_is_no_clobber(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = {
                "repo": "owner/repo",
                "issue": 10,
                "pr_number": 11,
                "branch": "ai/fix/10",
                "repair_state_root": str(root),
                "dry_run": False,
            }
            context = repair._repair_context(req(data))
            first_payload = {
                "phase": "REPAIR_COMPLETED",
                "before_oid": "a" * 40,
                "after_oid": "b" * 40,
                "candidate": "candidate-a",
                "run": {"run_id": "run-a", "status": "completed", "omp_process_id": "omp-a", "receipt_process_id": "verify-a"},
                "provenance": {
                    "repo": context["repo"],
                    "pr_number": 11,
                    "branch": context["branch"],
                    "local_branch": context["local_branch"],
                    "worktree_path": context["worktree_path"],
                    "task_id": context["task_id"],
                    "issue": context["issue"],
                    "receipt": context["receipt"],
                },
            }
            second_payload = dict(first_payload)
            second_payload["after_oid"] = "c" * 40
            second_payload["run"] = {
                "run_id": "run-b",
                "status": "completed",
                "omp_process_id": "omp-b",
                "receipt_process_id": "verify-b",
            }
            path = repair._repair_attempt_receipt(req(data), first_payload)
            self.assertEqual(path, repair._repair_attempt_receipt(req(data), second_payload))
            first = repair.publish_repair_receipt(req({
                **data,
                "conduction": {
                    "build_repair_receipt": {
                        "ok": True,
                        "status": "built",
                        "payload": first_payload,
                        "receipt_path": path,
                    },
                    "decide_repair_attempt": {"ok": True, "status": "invoke", "authorize": True},
                },
            }))
            self.assertEqual(first["status"], "written")
            original = Path(path).read_text(encoding="utf-8")
            conflict = repair.publish_repair_receipt(req({
                **data,
                "conduction": {
                    "build_repair_receipt": {
                        "ok": True,
                        "status": "built",
                        "payload": second_payload,
                        "receipt_path": path,
                    },
                    "decide_repair_attempt": {"ok": True, "status": "invoke", "authorize": True},
                },
            }))
            self.assertEqual(conflict["status"], "failed")
            self.assertEqual(conflict["reason"], "receipt_conflict")
            self.assertEqual(Path(path).read_text(encoding="utf-8"), original)
    def test_malformed_completed_receipt_blocks_second_omp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity = {
                "repo": "o/r",
                "pr_number": 1,
                "verified_head": "head-a",
                "candidate": "candidate-a",
                "run_id": "run-a",
            }
            path = repair._repair_completed_receipt_path(req({"repair_state_root": str(root)}), identity)
            assert path is not None
            path.parent.mkdir(parents=True)
            path.write_text("not-json", encoding="utf-8")
            completed = repair.read_repair_completed_receipt(req({
                "repo": "o/r",
                "number": 1,
                "verified_head": "head-a",
                "candidate": "candidate-a",
                "run_id": "run-a",
                "repair_state_root": str(root),
            }))
            self.assertEqual(completed["status"], "failed")
            self.assertEqual(completed["reason"], "repair_completed_receipt_malformed")
            decision = repair.decide_repair_attempt(req({
                "enabled": True,
                "live": True,
                "dry_run": False,
                "repo": "o/r",
                "number": 1,
                "verified_head": "head-a",
                "candidate": "candidate-a",
                "run_id": "run-a",
                "checks": [{"name": "ci", "conclusion": "FAILURE"}],
                "conduction": {"read_repair_completed_receipt": completed},
            }))
            self.assertNotEqual(decision.get("status"), "invoke")
            self.assertFalse(decision.get("authorize", False))




    def test_read_repair_omp_preconditions_success_preserves_identity(self) -> None:
        from lokay.steps import repair

        request = req({
            "repo": "mikolaj92/lokay",
            "issue": 10,
            "number": 11,
            "branch": "ai/fix/10-issue",
            "clone_path": "/tmp/clone",
            "worktree_root": "/tmp/worktrees",
            "conduction": {
                "decide_repair_attempt": {"ok": True, "status": "invoke", "authorize": True},
                "verify_repair_worktree": {"ok": True, "status": "verified", "head": "head-a"},
                "verify_repair_worktree_head": {"ok": True, "status": "verified"},
            },
        })
        context = repair._repair_context(request)
        with mock.patch("lokay.steps.repair.git", side_effect=[context["worktree_path"], context["local_branch"]]), mock.patch("lokay.steps.repair.rev_parse", return_value="head-a"):
            out = repair.read_repair_omp_preconditions(request)
        self.assertEqual(out["status"], "ready")
        self.assertEqual(out["branch"], "ai/fix/10-issue")
        self.assertEqual(out["worktree_path"], context["worktree_path"])
        self.assertEqual(out["remote_oid"], "head-a")

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
    def test_decide_repair_attempt_invokes_for_missing_test_evidence(self) -> None:
        from lokay.steps.repair import decide_repair_attempt

        out = decide_repair_attempt(req({
            "enabled": True,
            "live": True,
            "dry_run": False,
            "repo": "o/r",
            "number": 1,
            "verified_head": "abc",
            "candidate": "c1",
            "run_id": "r1",
            "checks": [{"name": "ci", "conclusion": "SUCCESS"}],
            "conduction": {
                "triage_decide_triage_action": {
                    "ok": True,
                    "status": "decided",
                    "action": "repair",
                    "reason": "missing_test_evidence",
                },
            },
        }))
        self.assertEqual(out["status"], "invoke")
        self.assertTrue(out["authorize"])
        self.assertEqual(out["reason"], "missing_test_evidence")
        self.assertEqual(out["failures"], [])

    def test_decide_repair_attempt_invokes_for_merge_conflict(self) -> None:
        from lokay.steps.repair import decide_repair_attempt

        out = decide_repair_attempt(req({
            "enabled": True,
            "live": True,
            "dry_run": False,
            "repo": "o/r",
            "number": 1,
            "verified_head": "abc",
            "candidate": "c1",
            "run_id": "r1",
            "checks": [{"name": "ci", "conclusion": "SUCCESS"}],
            "conduction": {
                "triage_decide_triage_action": {
                    "ok": True,
                    "status": "decided",
                    "action": "repair",
                    "reason": "merge_conflict",
                },
            },
        }))
        self.assertEqual(out["status"], "invoke")
        self.assertTrue(out["authorize"])
        self.assertEqual(out["reason"], "merge_conflict")
        self.assertEqual(out["failures"], [])

    def test_decide_repair_attempt_allows_missing_checks_only_when_disabled(self) -> None:
        from lokay.steps.repair import decide_repair_attempt

        data = {
            "enabled": True,
            "live": True,
            "dry_run": False,
            "repo": "o/r",
            "number": 1,
            "verified_head": "abc",
            "candidate": "c1",
            "run_id": "r1",
            "conduction": {
                "triage_decide_triage_action": {
                    "ok": True,
                    "status": "decided",
                    "action": "repair",
                    "reason": "missing_test_evidence",
                },
            },
        }
        allowed = decide_repair_attempt(req(data | {"require_checks": False}))
        self.assertEqual(allowed["status"], "invoke")
        self.assertTrue(allowed["authorize"])
        self.assertEqual(allowed["checks"], [])

        required = decide_repair_attempt(req(data | {"require_checks": True}))
        self.assertEqual(required["status"], "failed")
        self.assertEqual(required["conflict"], "missing_check_evidence")

    def test_decide_repair_attempt_keeps_nonempty_pending_checks_pending(self) -> None:
        from lokay.steps.repair import decide_repair_attempt

        out = decide_repair_attempt(req({
            "enabled": True,
            "live": True,
            "dry_run": False,
            "require_checks": False,
            "repo": "o/r",
            "number": 1,
            "verified_head": "abc",
            "candidate": "c1",
            "run_id": "r1",
            "checks": [{"name": "ci", "conclusion": "IN_PROGRESS"}],
        }))
        self.assertEqual(out["status"], "pending")
        self.assertFalse(out["authorize"])



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

    def test_verified_push_restart_resumes_pending_without_second_omp_or_push(self) -> None:
        """Controlled SQLite restart: one OMP and one push across two runs."""
        import sqlite3

        from lokay.flows.runtime import read_journal_processes

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "state.sqlite"
            schema = """
CREATE TABLE processes (
    run_id TEXT NOT NULL,
    id TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    output_json TEXT NOT NULL,
    error_json TEXT NOT NULL
);
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    metadata TEXT NOT NULL
);
"""
            with sqlite3.connect(db) as connection:
                connection.executescript(schema)
                connection.execute("INSERT INTO runs VALUES (?, ?)", ("run-a", json.dumps({"mode": "live"})))
            context = {
                "repo": "o/r",
                "issue": "1",
                "pr_number": "1",
                "branch": "ai/fix/1",
                "clone_path": str(root / "clone"),
                "worktree_root": str(root / "worktrees"),
                "repair_state_root": str(root / "receipts"),
                "candidate": "candidate-a",
                "run_id": "run-a",
                "db_path": str(db),
                "enabled": True,
                "live": True,
                "dry_run": False,
            }

            def persist(run_id: str, step: str, output: dict[str, object], *, attempt: int = 1) -> None:
                with sqlite3.connect(db) as connection:
                    connection.execute(
                        "INSERT INTO processes VALUES (?,?,?,?,?,?,?)",
                        (
                            run_id,
                            f"{run_id}:auto_worker:{step}",
                            "succeeded",
                            attempt,
                            1,
                            json.dumps(output, sort_keys=True),
                            "{}",
                        ),
                    )

            first_decision = repair.decide_repair_attempt(req({
                **context,
                "number": 1,
                "verified_head": "a" * 40,
                "checks": [{"name": "ci", "conclusion": "FAILURE"}],
            }))
            self.assertEqual(first_decision["status"], "invoke")
            persist("run-a", "triage_decide_repair_attempt", first_decision)
            ctx = repair._repair_context(req({**context, "number": 1, "verified_head": "a" * 40}))
            reserved = repair.reserve_repair_attempt(req({
                **context,
                "number": 1,
                "verified_head": "a" * 40,
                "conduction": {
                    "decide_repair_attempt": first_decision,
                    "read_repair_context": {"ok": True, "status": "read", **ctx},
                    "read_repair_attempt_baseline": {
                        "ok": True,
                        "status": "read",
                        "baseline_verified": True,
                        "repo": ctx["repo"],
                        "pr_number": ctx["pr_number"],
                        "pre_head": "a" * 40,
                        "pre_status": "",
                        "branch": ctx["branch"],
                        "local_branch": ctx["local_branch"],
                        "worktree_path": ctx["worktree_path"],
                    },
                },
            }))
            self.assertEqual(reserved["status"], "reserved")
            persist("run-a", "triage_reserve_repair_attempt", reserved)
            verified_reservation = repair.verify_repair_attempt_reservation(req({
                **context,
                "number": 1,
                "verified_head": "a" * 40,
                "conduction": {"reserve_repair_attempt": reserved, "read_repair_context": {"ok": True, "status": "read", **ctx}},
            }))
            self.assertTrue(verified_reservation["verified"])
            persist("run-a", "triage_verify_repair_attempt_reservation", verified_reservation)
            with (
                mock.patch("lokay.steps.repair.rev_parse", return_value="a" * 40),
                mock.patch("lokay.steps.repair.git", return_value=""),
                mock.patch("lokay.steps.repair.run_omp", return_value={"status": "completed", "stdout": "ok"}) as run_omp,
            ):
                invoked = repair.invoke_repair_omp(invoke_req({
                    **context,
                    "prompt": "fix checks",
                    "worktree_path": str(root / "worktrees" / "wt"),
                    "conduction": {
                        "decide_repair_attempt": first_decision,
                        "verify_repair_attempt_reservation": verified_reservation,
                        "read_repair_omp_preconditions": {
                            "ok": True,
                            "status": "ready",
                            "worktree_path": str(root / "worktrees" / "wt"),
                            "pre_head": "a" * 40,
                        },
                    },
                }, process_id="run-a:auto_worker:triage_invoke_repair_omp"))
            self.assertEqual(invoked["status"], "invoked")
            run_omp.assert_called_once()
            persist("run-a", "triage_invoke_repair_omp", invoked)
            with mock.patch("lokay.steps.repair.git_push_branch", return_value="pushed") as push:
                pushed = repair.push_repair_branch(req({
                    **context,
                    "conduction": {
                        "decide_repair_attempt": first_decision,
                        "decide_repair_push": {
                            "ok": True,
                            "status": "push",
                            "should_push": True,
                            "before_oid": "a" * 40,
                            "local_oid": "b" * 40,
                            "branch": "ai/fix/1",
                        },
                    },
                }))
            self.assertEqual(pushed["status"], "pushed")
            push.assert_called_once()
            persist("run-a", "triage_push_repair_branch", pushed)
            persist(
                "run-a",
                "triage_verify_repair_push_oid",
                {"ok": True, "status": "verified", "local_oid": "b" * 40, "remote_oid": "b" * 40},
            )
            receipt_payload = {
                "phase": "REPAIR_COMPLETED",
                "before_oid": "a" * 40,
                "after_oid": "b" * 40,
                "candidate": "candidate-a",
                "checks": [{"identity": "ci", "conclusion": "FAILURE"}],
                "run": {
                    "run_id": "run-a",
                    "status": "completed",
                    "omp_process_id": "run-a:auto_worker:triage_invoke_repair_omp",
                    "receipt_process_id": "run-a:auto_worker:triage_verify_repair_receipt",
                },
                "provenance": {
                    "repo": "o/r",
                    "pr_number": 1,
                    "branch": "ai/fix/1",
                    "local_branch": repair._repair_context(req(context))["local_branch"],
                    "worktree_path": repair._repair_context(req(context))["worktree_path"],
                    "task_id": "",
                    "issue": "1",
                    "receipt": repair._repair_context(req(context))["receipt"],
                },
            }
            receipt_path = repair._repair_attempt_receipt(req(context), receipt_payload)
            published = repair.publish_repair_receipt(req({
                **context,
                "conduction": {
                    "build_repair_receipt": {
                        "ok": True,
                        "status": "built",
                        "payload": receipt_payload,
                        "receipt_path": receipt_path,
                    },
                    "decide_repair_attempt": first_decision,
                },
            }))
            self.assertEqual(published["status"], "written")
            persist("run-a", "triage_publish_repair_receipt", published)

            first_run = read_journal_processes(db, "run-a")
            omp_rows = [row for row in first_run if row.step_id == "triage_invoke_repair_omp"]
            push_rows = [row for row in first_run if row.step_id == "triage_push_repair_branch"]
            self.assertEqual(len(omp_rows), 1)
            self.assertEqual(len(push_rows), 1)
            self.assertEqual(omp_rows[0].status, "succeeded")
            self.assertEqual(push_rows[0].status, "succeeded")

            completed = repair.read_repair_completed_receipt(req({
                "repo": "o/r",
                "number": 1,
                "verified_head": "a" * 40,
                "candidate": "candidate-b",
                "run_id": "run-b",
                "repair_state_root": context["repair_state_root"],
            }))
            self.assertEqual(completed["status"], "found")
            restart = repair.decide_repair_attempt(req({
                "enabled": True,
                "live": True,
                "dry_run": False,
                "repo": "o/r",
                "number": 1,
                "verified_head": "a" * 40,
                "candidate": "candidate-b",
                "run_id": "run-b",
                "checks": [{"name": "ci", "conclusion": "PENDING"}],
                "conduction": {"read_repair_completed_receipt": completed},
            }))
            self.assertEqual(restart["status"], "already_repaired")
            self.assertFalse(restart["authorize"])
            with mock.patch("lokay.steps.repair.run_omp") as run_omp_restart, mock.patch("lokay.steps.repair.git_push_branch") as push_restart:
                blocked_omp = repair.invoke_repair_omp(req({
                    "prompt": "fix checks",
                    "worktree_path": str(root / "worktrees" / "wt"),
                    "dry_run": False,
                    "conduction": {
                        "decide_repair_attempt": restart,
                        "verify_repair_attempt_reservation": {"ok": True, "verified": True},
                        "read_repair_omp_preconditions": {
                            "ok": True,
                            "status": "ready",
                            "worktree_path": str(root / "worktrees" / "wt"),
                            "pre_head": "a" * 40,
                        },
                    },
                }))
                blocked_push = repair.push_repair_branch(req({
                    "dry_run": False,
                    "conduction": {
                        "decide_repair_attempt": restart,
                        "decide_repair_push": {
                            "ok": True,
                            "status": "push",
                            "should_push": True,
                            "before_oid": "a" * 40,
                            "local_oid": "b" * 40,
                        },
                    },
                }))
            self.assertEqual(blocked_omp["status"], "noop")
            self.assertEqual(blocked_push["status"], "noop")
            run_omp_restart.assert_not_called()
            push_restart.assert_not_called()
            persist("run-b", "triage_decide_repair_attempt", restart)
            persist("run-b", "triage_invoke_repair_omp", blocked_omp)
            persist("run-b", "triage_push_repair_branch", blocked_push)

            second_run = read_journal_processes(db, "run-b")
            self.assertEqual([row.step_id for row in second_run if row.step_id == "triage_invoke_repair_omp" and row.output.get("status") == "invoked"], [])
            self.assertEqual([row.step_id for row in second_run if row.step_id == "triage_push_repair_branch" and row.output.get("status") == "pushed"], [])
            all_rows = read_journal_processes(db, "run-a") + second_run
            self.assertEqual(len([row for row in all_rows if row.step_id == "triage_invoke_repair_omp" and row.output.get("status") == "invoked"]), 1)
            self.assertEqual(len([row for row in all_rows if row.step_id == "triage_push_repair_branch" and row.output.get("status") == "pushed"]), 1)


    def test_reserve_repair_attempt_uses_conducted_baseline_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            branch = "ai/fix/10"
            worktree_root = str(Path(tmp) / "worktrees")
            local_branch = repair._repair_local_branch("o/r", "11", branch)
            worktree_path = str(Path(worktree_root) / local_branch)
            request = req({
                "enabled": True,
                "live": True,
                "dry_run": False,
                "candidate": "a" * 64,
                "run_id": "run-a",
                "repair_state_root": tmp,
                "conduction": {
                    "decide_repair_attempt": {
                        "ok": True,
                        "status": "invoke",
                        "authorize": True,
                        "repo": "o/r",
                        "pr_number": 11,
                        "verified_head": "head-a",
                        "candidate": "a" * 64,
                        "run_id": "run-a",
                        "checks": [{"identity": "ci", "conclusion": "FAILURE"}],
                    },
                    "read_repair_context": {
                        "ok": True,
                        "status": "read",
                        "repo": "o/r",
                        "pr_number": "11",
                        "branch": branch,
                        "local_branch": local_branch,
                        "worktree_root": worktree_root,
                        "worktree_path": worktree_path,
                    },
                    "read_repair_attempt_baseline": {
                        "ok": True,
                        "status": "read",
                        "baseline_verified": True,
                        "repo": "o/r",
                        "pr_number": "11",
                        "pre_head": "head-a",
                        "pre_status": "",
                        "branch": branch,
                        "local_branch": local_branch,
                        "worktree_path": worktree_path,
                    },
                    "verify_repair_attempt_recovery": {"ok": True, "status": "inactive"},
                    "verify_repair_recovery_continuation": {"ok": True, "status": "inactive"},
                },
            })
            reserved = repair.reserve_repair_attempt(request)
            self.assertEqual(reserved["status"], "reserved")
            self.assertEqual(reserved["reservation"]["repo_branch"], branch)
            self.assertEqual(reserved["reservation"]["local_branch"], local_branch)
            self.assertEqual(reserved["reservation"]["worktree_path"], worktree_path)


    def test_repair_reservation_restart_blocks_changed_checks_and_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = {
                "enabled": True, "live": True, "dry_run": False, "repo": "o/r", "number": 1, "verified_head": "head-a",
                "candidate": "candidate-a", "run_id": "run-a", "repair_state_root": tmp, "checks": [{"name": "ci", "conclusion": "FAILURE"}],
                "branch": "ai/fix/1",
                "worktree_root": str(Path(tmp) / "worktrees"),
            }
            first = repair.decide_repair_attempt(req(base))
            self.assertTrue(first["authorize"])
            ctx = repair._repair_context(req(base))
            reserved = repair.reserve_repair_attempt(req(dict(base, conduction={
                "decide_repair_attempt": first,
                "read_repair_context": {"ok": True, "status": "read", **ctx},
                "read_repair_attempt_baseline": {
                    "ok": True,
                    "status": "read",
                    "baseline_verified": True,
                    "repo": ctx["repo"],
                    "pr_number": ctx["pr_number"],
                    "pre_head": "head-a",
                    "pre_status": "",
                    "branch": ctx["branch"],
                    "local_branch": ctx["local_branch"],
                    "worktree_path": ctx["worktree_path"],
                }
            })))
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

    def test_repair_invoke_timeout_records_observed_mutation_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reservation = Path(tmp) / "reservation.json"
            reservation.write_text("{}\n", encoding="utf-8")
            process_id = "run:auto_worker:triage_invoke_repair_omp"
            request = invoke_req({
                "dry_run": False,
                "prompt": "fix checks",
                "worktree_path": "/tmp/worktree",
                "conduction": {
                    "decide_repair_attempt": {"ok": True, "status": "invoke", "authorize": True},
                    "read_repair_omp_preconditions": {
                        "ok": True,
                        "status": "ready",
                        "worktree_path": "/tmp/worktree",
                        "pre_head": "head-a",
                    },
                    "verify_repair_attempt_reservation": {
                        "ok": True,
                        "status": "verified",
                        "verified": True,
                        "reservation_path": str(reservation),
                    },
                },
            }, process_id=process_id)
            timeout = subprocess.TimeoutExpired(["omp"], 30)
            with (
                mock.patch("lokay.steps.repair.rev_parse", side_effect=["head-a", "head-a"]),
                mock.patch("lokay.steps.repair.git", side_effect=["", " M changed.py"]),
                mock.patch("lokay.steps.repair.run_omp", side_effect=timeout),
            ):
                out = repair.invoke_repair_omp(request)
            self.assertEqual(out["reason"], "repair_omp_failed")
            self.assertTrue(out["mutated"])
            terminal = json.loads(repair._repair_invoke_terminal_evidence_path(reservation, process_id).read_text(encoding="utf-8"))
            self.assertEqual(terminal["status"], "timed_out")
            self.assertEqual(terminal["post_head"], "head-a")
            self.assertEqual(terminal["post_status"], " M changed.py")

    def test_repair_invoke_rejects_timeout_beyond_fala_boundary(self) -> None:
        request = req({
            "dry_run": False,
            "prompt": "fix checks",
            "worktree_path": "/tmp/worktree",
            "timeout_seconds": 7201,
            "conduction": {
                "decide_repair_attempt": {"ok": True, "status": "invoke", "authorize": True},
                "read_repair_omp_preconditions": {
                    "ok": True,
                    "status": "ready",
                    "worktree_path": "/tmp/worktree",
                    "pre_head": "head-a",
                },
                "verify_repair_attempt_reservation": {"ok": True, "status": "verified", "verified": True},
            },
        })
        with (
            mock.patch("lokay.steps.repair.rev_parse", return_value="head-a"),
            mock.patch("lokay.steps.repair.git", return_value=""),
            mock.patch("lokay.steps.repair.run_omp") as run,
        ):
            out = repair.invoke_repair_omp(request)
        self.assertEqual(out["reason"], "invalid_repair_omp_timeout")
        self.assertFalse(out["mutated"])
        run.assert_not_called()

    def test_invoke_evidence_requires_started_and_terminal_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reservation = Path(tmp) / "reservation.json"
            process_id = "run:auto_worker:triage_invoke_repair_omp"
            started_path = repair._repair_invoke_evidence_path(reservation, process_id)
            terminal_path = repair._repair_invoke_terminal_evidence_path(reservation, process_id)
            started_path.parent.mkdir(parents=True, exist_ok=True)
            started = {"kind": "repair_invoke_evidence", "process_id": process_id, "status": "started", "pre_head": "head", "pre_status": "", "mutated": None}
            started_path.write_text(json.dumps(started) + "\n", encoding="utf-8")
            self.assertEqual(repair._read_repair_invoke_evidence(reservation, process_id)["status"], "unknown")
            terminal = {**started, "status": "failed", "post_head": "head", "post_status": "", "mutated": False, "error": "failed"}
            terminal_path.write_text(json.dumps(terminal) + "\n", encoding="utf-8")
            self.assertEqual(repair._read_repair_invoke_evidence(reservation, process_id)["status"], "failed")
            terminal_path.unlink()
            terminal["process_id"] = "other-process"
            terminal_path.write_text(json.dumps(terminal) + "\n", encoding="utf-8")
            malformed = repair._read_repair_invoke_evidence(reservation, process_id)
            self.assertEqual(malformed["reason"], "repair_invoke_evidence_malformed")

    def test_invoke_evidence_orphan_terminal_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reservation = Path(tmp) / "reservation.json"
            process_id = "run:auto_worker:triage_invoke_repair_omp"
            terminal_path = repair._repair_invoke_terminal_evidence_path(reservation, process_id)
            terminal_path.parent.mkdir(parents=True, exist_ok=True)
            terminal_path.write_text(json.dumps({"kind": "repair_invoke_evidence", "process_id": process_id, "status": "failed", "pre_head": "head", "pre_status": "", "post_head": "head", "post_status": "", "mutated": False, "error": "failed"}) + "\n", encoding="utf-8")
            self.assertEqual(repair._read_repair_invoke_evidence(reservation, process_id)["reason"], "repair_invoke_evidence_malformed")

    def test_invoke_repair_omp_prepublication_failure_does_not_run_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, reservation = Path(tmp), Path(tmp) / "reservation.json"
            reservation.write_text("{}\n")
            request = invoke_req({"repo": "o/r", "number": 1, "verified_head": "head", "worktree_path": str(root / "wt"), "prompt": "fix", "conduction": {"verify_repair_attempt_reservation": {"ok": True, "status": "verified", "verified": True, "reservation_path": str(reservation)}, "read_repair_omp_preconditions": {"ok": True, "status": "ready", "worktree_path": str(root / "wt"), "pre_head": "head"}, "decide_repair_attempt": {"ok": True, "status": "invoke", "authorize": True}}}, process_id="run:auto_worker:triage_invoke_repair_omp")
            with mock.patch("lokay.steps.repair.rev_parse", return_value="head"), mock.patch("lokay.steps.repair.git", return_value=""), mock.patch("lokay.steps.repair._write_invoke_evidence", side_effect=OSError("publish")), mock.patch("lokay.steps.repair.run_omp") as run:
                out = repair.invoke_repair_omp(request)
            self.assertEqual(out["reason"], "repair_invoke_evidence_write_failed")
            self.assertFalse(out["mutated"])
            run.assert_not_called()

    def test_invoke_repair_omp_terminal_publication_failure_reports_mutated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, reservation = Path(tmp), Path(tmp) / "reservation.json"
            reservation.write_text("{}\n")
            process_id = "run:auto_worker:triage_invoke_repair_omp"
            request = invoke_req({"repo": "o/r", "number": 1, "verified_head": "head", "worktree_path": str(root / "wt"), "prompt": "fix", "conduction": {"verify_repair_attempt_reservation": {"ok": True, "status": "verified", "verified": True, "reservation_path": str(reservation)}, "read_repair_omp_preconditions": {"ok": True, "status": "ready", "worktree_path": str(root / "wt"), "pre_head": "head"}, "decide_repair_attempt": {"ok": True, "status": "invoke", "authorize": True}}}, process_id=process_id)
            calls = {"count": 0}
            def write(path, payload, exclusive=False):
                calls["count"] += 1
                if not exclusive:
                    raise OSError("terminal publish")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            with mock.patch("lokay.steps.repair.rev_parse", return_value="head"), mock.patch("lokay.steps.repair.git", return_value=""), mock.patch("lokay.steps.repair._write_invoke_evidence", side_effect=write), mock.patch("lokay.steps.repair.run_omp", return_value={"status": "completed"}):
                out = repair.invoke_repair_omp(request)
            self.assertEqual(out["reason"], "repair_invoke_evidence_write_failed")
            self.assertTrue(out["mutated"])

    def test_invoke_repair_omp_requires_invocation_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, reservation = Path(tmp), Path(tmp) / "reservation.json"
            reservation.write_text("{}\n")
            request = req({"repo": "o/r", "number": 1, "verified_head": "head", "worktree_path": str(root / "wt"), "prompt": "fix", "conduction": {"verify_repair_attempt_reservation": {"ok": True, "status": "verified", "verified": True, "reservation_path": str(reservation)}, "read_repair_omp_preconditions": {"ok": True, "status": "ready", "worktree_path": str(root / "wt"), "pre_head": "head"}, "decide_repair_attempt": {"ok": True, "status": "invoke", "authorize": True}}})
            with mock.patch("lokay.steps.repair.rev_parse", return_value="head"), mock.patch("lokay.steps.repair.git", return_value=""), mock.patch("lokay.steps.repair.run_omp") as run:
                out = repair.invoke_repair_omp(request)
            self.assertEqual(out["reason"], "repair_invoke_evidence_identity_required")
            self.assertFalse(out["mutated"])
            run.assert_not_called()

    def test_repair_attempt_recovery_inactive_without_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = {
                "repo": "o/r",
                "number": 1,
                "verified_head": "head-a",
                "candidate": "old-candidate",
                "run_id": "old-run",
                "repair_state_root": str(root),
            }
            identity = {
                "repo": "o/r",
                "pr_number": 1,
                "verified_head": "head-a",
                "candidate": "old-candidate",
                "run_id": "old-run",
            }
            reservation = {
                **identity,
                "status": "reserved",
                "attempted": True,
                "kind": "repair_attempt_reservation",
                "checks": [],
            }
            path = repair._repair_reservation_path(req(base), identity)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(reservation, sort_keys=True) + "\n", encoding="utf-8")
            state = repair.read_repair_attempt_state(req({
                "repo": "o/r", "number": 1, "verified_head": "head-a",
                "candidate": "new-candidate", "run_id": "new-run", "repair_state_root": str(root),
            }))
            self.assertEqual(state["status"], "found")
            evidence = repair.read_repair_attempt_recovery_evidence(req({
                "repo": "o/r", "number": 1, "verified_head": "head-a",
                "candidate": "new-candidate", "run_id": "new-run", "repair_state_root": str(root),
                "conduction": {"read_repair_attempt_state": state},
            }))
            self.assertEqual(evidence["status"], "inactive")
            claim = repair.claim_repair_attempt_recovery(req({
                "conduction": {"read_repair_attempt_recovery_evidence": evidence},
            }))
            self.assertEqual(claim["status"], "inactive")
            verified = repair.verify_repair_attempt_recovery(req({
                "conduction": {"claim_repair_attempt_recovery": claim},
            }))
            self.assertEqual(verified["status"], "inactive")
            decision = repair.decide_repair_attempt(req({
                "enabled": True, "live": True, "dry_run": False,
                "repo": "o/r", "number": 1, "verified_head": "head-a",
                "candidate": "new-candidate", "run_id": "new-run",
                "checks": [{"name": "ci", "conclusion": "FAILURE"}],
                "conduction": {
                    "read_repair_attempt_state": state,
                    "verify_repair_attempt_recovery": verified,
                },
            }))
            self.assertEqual(decision["status"], "already_repaired")
            self.assertFalse(decision["authorize"])

    def test_repair_attempt_recovery_rejects_invoke_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity = {
                "repo": "o/r",
                "pr_number": 1,
                "verified_head": "head-a",
                "candidate": "old-candidate",
                "run_id": "old-run",
            }

            reservation = {**identity, "status": "reserved", "attempted": True, "kind": "repair_attempt_reservation", "checks": []}
            path = root / "repair-attempts" / "o__r" / "1" / "reservation.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(reservation, sort_keys=True) + "\n", encoding="utf-8")
            state = {
                "ok": True,
                "status": "found",
                "attempt_state": reservation,
                "reservation_path": str(path),
            }
            evidence = repair.read_repair_attempt_recovery_evidence(req({
                "repo": "o/r", "number": 1, "verified_head": "head-a",
                "candidate": "new-candidate", "run_id": "new-run",
                "db_path": str(root / "state.sqlite"),
                "attempt_recovery": {
                    "run_id": "old-run",
                    "process_id": "old-run:auto_worker:triage_invoke_repair_omp",
                    "candidate": "old-candidate",
                    "path_id": "auto_worker",
                    "effector_id": "triage_invoke_repair_omp",
                    "repo": "o/r",
                    "pr_number": 1,
                    "verified_head": "head-a",
                },
                "conduction": {"read_repair_attempt_state": state},
            }))
            self.assertEqual(evidence["status"], "failed")
            self.assertEqual(evidence["reason"], "repair_attempt_recovery_mismatch")

    def test_repair_attempt_recovery_claim_exists_without_reading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claim_path = root / "claim.json"
            claim = {
                "kind": "repair_attempt_recovery_claim",
                "repo": "o/r",
                "pr_number": 1,
                "verified_head": "head-a",
                "reservation_run_id": "old-run",
                "reservation_candidate": "old-candidate",
                "evidence_process_id": "old-run:auto_worker:triage_verify_repair_attempt_reservation",
                "recovery_run_id": "new-run",
                "recovery_candidate": "new-candidate",
            }
            claim_path.write_text(json.dumps({"different": True}, sort_keys=True) + "\n", encoding="utf-8")
            claimed = repair.claim_repair_attempt_recovery(req({
                "dry_run": False,
                "conduction": {
                    "read_repair_attempt_recovery_evidence": {
                        "ok": True,
                        "status": "validated",
                        "recovery_claim": claim,
                        "recovery_claim_path": str(claim_path),
                    }
                },
            }))
            self.assertEqual(claimed["status"], "exists")
            self.assertFalse(claimed["mutated"])
            self.assertEqual(claimed["recovery_claim"], claim)
            verified = repair.verify_repair_attempt_recovery(req({
                "dry_run": False,
                "conduction": {"claim_repair_attempt_recovery": claimed},
            }))
            self.assertEqual(verified["status"], "failed")
            self.assertEqual(verified["reason"], "repair_attempt_recovery_claim_mismatch")

    def test_repair_attempt_recovery_accepts_failed_mutation_false_terminal_evidence(self) -> None:
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "state" / "state.sqlite"
            db.parent.mkdir(parents=True)
            candidate = "old-candidate"
            expected_cwd = db.resolve().parent.parent / "deployment" / "versions" / candidate / "source" / "project"
            expected_cwd.mkdir(parents=True)
            reservation = root / "reservation.json"
            reservation.write_text("{}\n")
            state = {"repo": "o/r", "pr_number": 1, "verified_head": "head-a", "candidate": candidate, "run_id": "old-run", "pre_head": "head-a", "pre_status": ""}
            process_id = "old-run:auto_worker:triage_verify_repair_attempt_reservation"
            invoke_id = "old-run:auto_worker:triage_invoke_repair_omp"
            started = {"kind": "repair_invoke_evidence", "process_id": invoke_id, "status": "started", "pre_head": "head-a", "pre_status": "", "mutated": None}
            terminal = {**started, "status": "failed", "post_head": "head-a", "post_status": "", "mutated": False, "error": "failed"}
            repair._repair_invoke_evidence_path(reservation, invoke_id).write_text(json.dumps(started) + "\n", encoding="utf-8")
            repair._repair_invoke_terminal_evidence_path(reservation, invoke_id).write_text(json.dumps(terminal) + "\n", encoding="utf-8")
            process_input = {"candidate": candidate, "candidate_id": candidate, "conduction": {"triage_reserve_repair_attempt": {"ok": True, "mutated": True, "reservation_path": str(reservation), "reservation": state}}}
            with sqlite3.connect(db) as connection:
                connection.execute("CREATE TABLE processes (run_id TEXT, id TEXT, status TEXT, input_json TEXT, output_json TEXT, error_json TEXT, metadata TEXT)")
                connection.execute("INSERT INTO processes VALUES (?,?,?,?,?,?,?)", ("old-run", process_id, "failed", json.dumps(process_input), "{}", json.dumps({"code": "adapter_failed"}), json.dumps({"__adapter_binding": {"cwd": str(expected_cwd)}})))
            out = repair.read_repair_attempt_recovery_evidence(req({
                "run_id": "new-run", "candidate": "new-candidate", "repo": "o/r", "number": 1, "verified_head": "head-a", "db_path": str(db),
                "attempt_recovery": {"run_id": "old-run", "process_id": process_id, "candidate": candidate, "path_id": "auto_worker", "effector_id": "triage_verify_repair_attempt_reservation", "repo": "o/r", "pr_number": 1, "verified_head": "head-a"},
                "conduction": {"read_repair_attempt_state": {"ok": True, "status": "found", "attempt_state": state, "reservation_path": str(reservation)}, "read_repair_attempt_reconciliation": {"ok": True, "status": "unchanged", "authorize_reinvoke": True, "snapshot": {"pre_head": "head-a", "pre_status": ""}}}
            }))
            self.assertEqual(out["status"], "validated", out)
            self.assertFalse(out["mutated"])
            self.assertEqual(out["recovery_claim"]["evidence_process_id"], process_id)

    def test_repair_attempt_recovery_accepts_failed_verifier_without_invoke_sidecar(self) -> None:
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "state" / "state.sqlite"
            db.parent.mkdir(parents=True)
            candidate = "old-candidate"
            expected_cwd = db.resolve().parent.parent / "deployment" / "versions" / candidate / "source" / "project"
            expected_cwd.mkdir(parents=True)
            reservation = root / "reservation.json"
            reservation.write_text("{}\n")
            state = {"repo": "o/r", "pr_number": 1, "verified_head": "head-a", "candidate": candidate, "run_id": "old-run", "pre_head": "head-a", "pre_status": ""}
            process_id = "old-run:auto_worker:triage_verify_repair_attempt_reservation"
            process_input = {"candidate": candidate, "candidate_id": candidate, "conduction": {"triage_reserve_repair_attempt": {"ok": True, "mutated": True, "reservation_path": str(reservation), "reservation": state}}}
            metadata = {"effector_id": "triage_verify_repair_attempt_reservation", "__correlation_conduction": ["triage_reserve_repair_attempt", "triage_verify_repair_attempt_recovery", "triage_verify_repair_recovery_continuation"], "__adapter_binding": {"cwd": str(expected_cwd)}}
            with sqlite3.connect(db) as connection:
                connection.execute("CREATE TABLE processes (run_id TEXT, id TEXT, status TEXT, input_json TEXT, output_json TEXT, error_json TEXT, metadata TEXT)")
                connection.execute("INSERT INTO processes VALUES (?,?,?,?,?,?,?)", ("old-run", process_id, "failed", json.dumps(process_input), "{}", json.dumps({"code": "adapter_failed"}), json.dumps(metadata)))
            out = repair.read_repair_attempt_recovery_evidence(req({
                "path_id": "auto_worker", "run_id": "new-run", "candidate": "new-candidate", "repo": "o/r", "number": 1, "verified_head": "head-a", "db_path": str(db),
                "attempt_recovery": {"run_id": "stale-run", "process_id": "stale", "candidate": "stale-candidate", "path_id": "auto_worker", "effector_id": "triage_verify_repair_attempt_reservation", "repo": "o/r", "pr_number": 1, "verified_head": "head-a"},
                "conduction": {"read_repair_attempt_state": {"ok": True, "status": "found", "attempt_state": state, "reservation_path": str(reservation)}, "read_repair_attempt_reconciliation": {"ok": True, "status": "unchanged", "authorize_reinvoke": True, "snapshot": {"pre_head": "head-a", "pre_status": ""}}}
            }))
            self.assertEqual(out["status"], "validated", out)
            self.assertEqual(out["recovery_claim"]["evidence_process_id"], process_id)

    def test_repair_attempt_recovery_accepts_semantic_ok_false_no_mutation(self) -> None:
        """Exit-0 semantic failures land as succeeded + values.ok=false, not adapter_failed."""
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "state" / "state.sqlite"
            db.parent.mkdir(parents=True)
            candidate = "old-candidate"
            expected_cwd = db.resolve().parent.parent / "deployment" / "versions" / candidate / "source" / "project"
            expected_cwd.mkdir(parents=True)
            reservation = root / "reservation.json"
            reservation.write_text("{}\n")
            state = {
                "repo": "o/r",
                "pr_number": 1,
                "verified_head": "head-a",
                "candidate": candidate,
                "run_id": "old-run",
                "pre_head": "head-a",
                "pre_status": "",
            }
            process_id = "old-run:auto_worker:triage_verify_repair_attempt_reservation"
            process_input = {
                "candidate": candidate,
                "candidate_id": candidate,
                "conduction": {
                    "triage_reserve_repair_attempt": {
                        "ok": True,
                        "mutated": True,
                        "reservation_path": str(reservation),
                        "reservation": state,
                    }
                },
            }
            metadata = {
                "effector_id": "triage_verify_repair_attempt_reservation",
                "__correlation_conduction": [
                    "triage_reserve_repair_attempt",
                    "triage_verify_repair_attempt_recovery",
                    "triage_verify_repair_recovery_continuation",
                ],
                "__adapter_binding": {"cwd": str(expected_cwd)},
            }
            semantic_output = {
                "adapter": {"returncode": 0, "stdout": "", "stderr": "lokay effector reported failure"},
                "values": {
                    "ok": False,
                    "status": "failed",
                    "mutated": False,
                    "reason": "repair_attempt_reservation_mismatch",
                    "failure_class": "terminal",
                    "retry_safe": False,
                },
            }
            with sqlite3.connect(db) as connection:
                connection.execute(
                    "CREATE TABLE processes (run_id TEXT, id TEXT, status TEXT, input_json TEXT, output_json TEXT, error_json TEXT, metadata TEXT)"
                )
                connection.execute(
                    "INSERT INTO processes VALUES (?,?,?,?,?,?,?)",
                    (
                        "old-run",
                        process_id,
                        "succeeded",
                        json.dumps(process_input),
                        json.dumps(semantic_output),
                        "{}",
                        json.dumps(metadata),
                    ),
                )
            out = repair.read_repair_attempt_recovery_evidence(
                req(
                    {
                        "path_id": "auto_worker",
                        "run_id": "new-run",
                        "candidate": "new-candidate",
                        "repo": "o/r",
                        "number": 1,
                        "verified_head": "head-a",
                        "db_path": str(db),
                        "attempt_recovery": {
                            "run_id": "stale-run",
                            "process_id": "stale",
                            "candidate": "stale-candidate",
                            "path_id": "auto_worker",
                            "effector_id": "triage_verify_repair_attempt_reservation",
                            "repo": "o/r",
                            "pr_number": 1,
                            "verified_head": "head-a",
                        },
                        "conduction": {
                            "read_repair_attempt_state": {
                                "ok": True,
                                "status": "found",
                                "attempt_state": state,
                                "reservation_path": str(reservation),
                            },
                            "read_repair_attempt_reconciliation": {
                                "ok": True,
                                "status": "unchanged",
                                "authorize_reinvoke": True,
                                "snapshot": {"pre_head": "head-a", "pre_status": ""},
                            },
                        },
                    }
                )
            )
            self.assertEqual(out["status"], "validated", out)
            self.assertFalse(out["mutated"])
            self.assertEqual(out["recovery_claim"]["evidence_process_id"], process_id)

    def test_repair_attempt_recovery_authorizes_composite_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity = {
                "repo": "o/r",
                "pr_number": 1,
                "verified_head": "head-a",
                "candidate": "old-candidate",
                "run_id": "old-run",
            }
            reservation = {
                **identity,
                "status": "reserved",
                "attempted": True,
                "kind": "repair_attempt_reservation",
                "checks": [{"identity": "ci", "conclusion": "FAILURE"}],
                "pre_head": "head-a",
                "pre_status": "",
                "repo_branch": "branch-a",
                "local_branch": "local-branch-a",
                "worktree_path": "worktree-path-a",
            }
            path = root / "repair-attempts" / "o__r" / "1" / "reservation.json"
            path.parent.mkdir(parents=True)
            original = json.dumps(reservation, sort_keys=True) + "\n"
            path.write_text(original, encoding="utf-8")
            claim = {
                "kind": "repair_attempt_recovery_claim",
                "repo": "o/r",
                "pr_number": 1,
                "verified_head": "head-a",
                "reservation_run_id": "old-run",
                "reservation_candidate": "old-candidate",
                "evidence_process_id": "old-run:auto_worker:triage_verify_repair_attempt_reservation",
                "recovery_run_id": "new-run",
                "recovery_candidate": "new-candidate",
            }
            claim_path = repair._repair_recovery_claim_path(path, claim["evidence_process_id"])
            claim_path.write_text(json.dumps(claim, sort_keys=True) + "\n", encoding="utf-8")
            state = {
                "ok": True,
                "status": "found",
                "attempt_state": reservation,
                "reservation_path": str(path),
            }
            verified = {
                "ok": True,
                "status": "verified",
                "recovery_verified": True,
                "recovery_claim": claim,
                "recovery_claim_path": str(claim_path),
            }
            decision = repair.decide_repair_attempt(req({
                "enabled": True, "live": True, "dry_run": False,
                "repo": "o/r", "number": 1, "verified_head": "head-a",
                "candidate": "new-candidate", "run_id": "new-run",
                "checks": [{"name": "ci", "conclusion": "FAILURE"}],
                "conduction": {
                    "read_repair_attempt_state": state,
                    "verify_repair_attempt_recovery": verified,
                    "verify_repair_recovery_continuation": {
                        "ok": True,
                        "status": "original",
                        "continuation_verified": True,
                    },
                },
            }))
            self.assertEqual(decision["status"], "invoke")
            self.assertTrue(decision["authorize"])
            self.assertEqual(decision["reason"], "verified_failed_attempt_recovery")
            reserved = repair.reserve_repair_attempt(req({
                "enabled": True, "live": True, "dry_run": False,
                "repo": "o/r", "number": 1, "verified_head": "head-a",
                "candidate": "new-candidate", "run_id": "new-run",
                "repair_state_root": str(root),
                "conduction": {
                    "decide_repair_attempt": decision,
                    "verify_repair_attempt_recovery": verified,
                },
            }))
            self.assertEqual(reserved["status"], "recovered")
            self.assertFalse(reserved["mutated"])
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            verified_reservation = repair.verify_repair_attempt_reservation(req({
                "repo": "o/r", "number": 1, "verified_head": "head-a",
                "candidate": "new-candidate", "run_id": "new-run",
                "conduction": {
                    "reserve_repair_attempt": reserved,
                    "verify_repair_attempt_recovery": verified,
                    "verify_repair_recovery_continuation": {
                        "ok": True,
                        "status": "original",
                        "continuation_verified": True,
                    },
                },
            }))
            self.assertEqual(verified_reservation["status"], "verified")
            self.assertTrue(verified_reservation["recovered"])
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_verify_recovered_reservation_without_top_level_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity = {
                "repo": "o/r",
                "pr_number": 1,
                "verified_head": "head-a",
                "candidate": "old-candidate",
                "run_id": "old-run",
            }
            reservation = {
                **identity,
                "status": "reserved",
                "attempted": True,
                "kind": "repair_attempt_reservation",
                "checks": [{"identity": "ci", "conclusion": "FAILURE"}],
                "pre_head": "head-a",
                "pre_status": "",
                "repo_branch": "branch-a",
                "local_branch": "local-branch-a",
                "worktree_path": "worktree-path-a",
            }
            path = root / "reservation.json"
            path.write_text(json.dumps(reservation, sort_keys=True) + "\n", encoding="utf-8")
            claim = {
                "kind": "repair_attempt_recovery_claim",
                "repo": "o/r",
                "pr_number": 1,
                "verified_head": "head-a",
                "reservation_run_id": "old-run",
                "reservation_candidate": "old-candidate",
                "evidence_process_id": "old-run:auto_worker:triage_verify_repair_attempt_reservation",
                "recovery_run_id": "new-run",
                "recovery_candidate": "new-candidate",
            }
            reserved = {
                "ok": True,
                "status": "recovered",
                "mutated": False,
                "reservation_path": str(path),
                "recovery_claim": claim,
            }
            verified = {
                "ok": True,
                "status": "verified",
                "recovery_verified": True,
                "recovery_claim": claim,
            }
            out = repair.verify_repair_attempt_reservation(req({
                "repo": "o/r", "number": 1, "verified_head": "head-a",
                "candidate": "new-candidate", "run_id": "new-run",
                "branch": "branch-a", "local_branch": "local-branch-a", "worktree_path": "worktree-path-a",
                "conduction": {
                    "reserve_repair_attempt": reserved,
                    "read_repair_context": {"ok": True, "status": "read", "repo": "o/r", "pr_number": 1, "branch": "branch-a", "local_branch": "local-branch-a", "worktree_path": "worktree-path-a"},
                    "verify_repair_attempt_recovery": verified,
                    "verify_repair_recovery_continuation": {
                        "ok": True,
                        "status": "original",
                        "continuation_verified": True,
                    },
                },
            }))
            self.assertEqual(out["status"], "verified")
            self.assertTrue(out["recovered"])
            self.assertTrue(out["verified"])

    def test_repair_attempt_recovery_claim_paths_differ_by_evidence_process(self) -> None:
        path = Path("/tmp/reservation.json")
        first = repair._repair_recovery_claim_path(path, "old-run:auto_worker:triage_verify_repair_attempt_reservation")
        second = repair._repair_recovery_claim_path(path, "later-run:auto_worker:triage_verify_repair_attempt_reservation")
        self.assertNotEqual(first, second)
        self.assertTrue(first.name.startswith("reservation.recovery."))
        self.assertTrue(second.name.startswith("reservation.recovery."))



    def test_recovery_continuation_authorizes_next_link_and_rejects_second_contender(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reservation_path = root / "reservation.json"
            head = "a" * 40
            reconciliation = {"pre_head": head, "pre_status": "", "actual_head": head, "actual_status": ""}
            claim = {
                "kind": "repair_attempt_recovery_claim",
                "repo": "o/r",
                "pr_number": 1,
                "verified_head": head,
                "reservation_run_id": "old-run",
                "reservation_candidate": "old-candidate",
                "evidence_process_id": "old-run:auto_worker:triage_verify_repair_attempt_reservation",
                "recovery_run_id": "run-b",
                "recovery_candidate": "candidate-b",
                "reconciliation": reconciliation,
            }
            claim_path = root / "claim.json"
            claim_path.write_text(json.dumps(claim, sort_keys=True) + "\n", encoding="utf-8")
            reservation_path.write_text("{}\n", encoding="utf-8")
            db = root / "state.sqlite"
            invoke_process_id = "run-b:auto_worker:triage_invoke_repair_omp"
            started = {"kind": "repair_invoke_evidence", "process_id": invoke_process_id, "status": "started", "pre_head": head, "pre_status": "", "mutated": None}
            terminal = {**started, "status": "failed", "post_head": head, "post_status": "", "mutated": False, "error": "failed"}
            repair._repair_invoke_evidence_path(reservation_path, invoke_process_id).write_text(json.dumps(started) + "\n", encoding="utf-8")
            repair._repair_invoke_terminal_evidence_path(reservation_path, invoke_process_id).write_text(json.dumps(terminal) + "\n", encoding="utf-8")
            with sqlite3.connect(db) as connection:
                connection.execute(
                    "CREATE TABLE processes (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, status TEXT NOT NULL, output_json TEXT NOT NULL, error_json TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO processes VALUES (?,?,?,?,?)",
                    (
                        "run-b:auto_worker:triage_invoke_repair_omp",
                        "run-b",
                        "failed",
                        "{}",
                        json.dumps({"code": "adapter_failed", "message": "pre-omp crash", "mutated": False}, sort_keys=True),
                    ),
                )
                connection.execute(
                    "INSERT INTO processes VALUES (?,?,?,?,?)",
                    (
                        "run-b:auto_worker:triage_push_repair_branch",
                        "run-b",
                        "succeeded",
                        json.dumps({"values": {"ok": True, "status": "noop", "mutated": False}}, sort_keys=True),
                        "{}",
                    ),
                )
            verified = {
                "ok": True,
                "status": "verified",
                "recovery_verified": True,
                "recovery_claim": claim,
                "recovery_claim_path": str(claim_path),
                "reservation_path": str(reservation_path),
            }
            state = {"ok": True, "status": "found", "reservation_path": str(reservation_path)}
            evidence = repair.read_repair_recovery_continuation_evidence(req({
                "run_id": "run-c",
                "candidate": "candidate-c",
                "db_path": str(db),
                "path_id": "auto_worker",
                "conduction": {
                    "verify_repair_attempt_recovery": verified,
                    "read_repair_attempt_state": state,
                },
            }))
            self.assertEqual(evidence["status"], "validated", evidence)
            claimed = repair.claim_repair_recovery_continuation(req({
                "dry_run": False,
                "conduction": {"read_repair_recovery_continuation_evidence": evidence},
            }))
            self.assertEqual(claimed["status"], "claimed")
            contender = dict(evidence["continuation"])
            contender["continuation_run_id"] = "run-d"
            contender["continuation_candidate"] = "candidate-d"
            second = repair.claim_repair_recovery_continuation(req({
                "dry_run": False,
                "conduction": {
                    "read_repair_recovery_continuation_evidence": {
                        "ok": True,
                        "status": "validated",
                        "continuation": contender,
                        "continuation_path": evidence["continuation_path"],
                        "reservation_path": str(reservation_path),
                    }
                },
            }))
            self.assertEqual(second["status"], "failed")
            self.assertEqual(second["reason"], "repair_recovery_continuation_conflict")
            verified_link = repair.verify_repair_recovery_continuation(req({
                "dry_run": False,
                "conduction": {
                    "claim_repair_recovery_continuation": claimed,
                    "verify_repair_attempt_recovery": verified,
                    "read_repair_attempt_state": state,
                },
            }))
            self.assertEqual(verified_link["status"], "verified")
            decision = repair.decide_repair_attempt(req({
                "enabled": True, "live": True, "dry_run": False,
                "repo": "o/r", "number": 1, "verified_head": head,
                "candidate": "candidate-c", "run_id": "run-c",
                "checks": [{"name": "ci", "conclusion": "FAILURE"}],
                "conduction": {
                    "read_repair_attempt_state": {
                        "ok": True,
                        "status": "found",
                        "attempt_state": {
                            "repo": "o/r",
                            "pr_number": 1,
                            "verified_head": head,
                            "candidate": "old-candidate",
                            "run_id": "old-run",
                            "status": "reserved",
                            "attempted": True,
                        },
                        "reservation_path": str(reservation_path),
                    },
                    "verify_repair_attempt_recovery": verified,
                    "verify_repair_recovery_continuation": verified_link,
                },
            }))
            self.assertEqual(decision["status"], "invoke")
            self.assertTrue(decision["authorize"])

    def test_recovery_continuation_blocks_without_explicit_mutation_evidence(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reservation_path = root / "reservation.json"
            head = "a" * 40
            claim = {
                "kind": "repair_attempt_recovery_claim",
                "repo": "o/r",
                "pr_number": 1,
                "verified_head": head,
                "reservation_run_id": "old-run",
                "reservation_candidate": "old-candidate",
                "evidence_process_id": "old-run:auto_worker:triage_verify_repair_attempt_reservation",
                "recovery_run_id": "run-b",
                "recovery_candidate": "candidate-b",
            }
            claim_path = root / "claim.json"
            claim_path.write_text(json.dumps(claim, sort_keys=True) + "\n", encoding="utf-8")
            reservation_path.write_text("{}\n", encoding="utf-8")
            db = root / "state.sqlite"
            with sqlite3.connect(db) as connection:
                connection.execute(
                    "CREATE TABLE processes (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, status TEXT NOT NULL, output_json TEXT NOT NULL, error_json TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO processes VALUES (?,?,?,?,?)",
                    (
                        "run-b:auto_worker:triage_invoke_repair_omp",
                        "run-b",
                        "failed",
                        "{}",
                        json.dumps({"code": "adapter_failed"}, sort_keys=True),
                    ),
                )
                connection.execute(
                    "INSERT INTO processes VALUES (?,?,?,?,?)",
                    (
                        "run-b:auto_worker:triage_push_repair_branch",
                        "run-b",
                        "succeeded",
                        json.dumps({"values": {"ok": True, "status": "noop", "mutated": False}}, sort_keys=True),
                        "{}",
                    ),
                )
            evidence = repair.read_repair_recovery_continuation_evidence(req({
                "run_id": "run-c",
                "candidate": "candidate-c",
                "db_path": str(db),
                "path_id": "auto_worker",
                "conduction": {
                    "verify_repair_attempt_recovery": {
                        "ok": True,
                        "status": "verified",
                        "recovery_verified": True,
                        "recovery_claim": claim,
                        "recovery_claim_path": str(claim_path),
                        "reservation_path": str(reservation_path),
                    },
                    "read_repair_attempt_state": {"ok": True, "status": "found", "reservation_path": str(reservation_path)},
                },
            }))
            self.assertEqual(evidence["status"], "failed")
            self.assertEqual(evidence["reason"], "repair_recovery_continuation_mutation_unknown")

    def test_recovery_continuation_accepts_semantic_ok_false_invoke(self) -> None:
        """Continuation must accept succeeded+values.ok=false invoke rows after exit-0."""
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reservation_path = root / "reservation.json"
            head = "a" * 40
            reconciliation = {
                "pre_head": head,
                "pre_status": "",
                "actual_head": head,
                "actual_status": "",
            }
            claim = {
                "kind": "repair_attempt_recovery_claim",
                "repo": "o/r",
                "pr_number": 1,
                "verified_head": head,
                "reservation_run_id": "old-run",
                "reservation_candidate": "old-candidate",
                "evidence_process_id": "old-run:auto_worker:triage_verify_repair_attempt_reservation",
                "recovery_run_id": "run-b",
                "recovery_candidate": "candidate-b",
                "reconciliation": reconciliation,
            }
            claim_path = root / "claim.json"
            claim_path.write_text(json.dumps(claim, sort_keys=True) + "\n", encoding="utf-8")
            reservation_path.write_text("{}\n", encoding="utf-8")
            db = root / "state.sqlite"
            invoke_process_id = "run-b:auto_worker:triage_invoke_repair_omp"
            started = {
                "kind": "repair_invoke_evidence",
                "process_id": invoke_process_id,
                "status": "started",
                "pre_head": head,
                "pre_status": "",
                "mutated": None,
            }
            terminal = {
                **started,
                "status": "failed",
                "post_head": head,
                "post_status": "",
                "mutated": False,
                "error": "failed",
            }
            repair._repair_invoke_evidence_path(reservation_path, invoke_process_id).write_text(
                json.dumps(started) + "\n", encoding="utf-8"
            )
            repair._repair_invoke_terminal_evidence_path(reservation_path, invoke_process_id).write_text(
                json.dumps(terminal) + "\n", encoding="utf-8"
            )
            semantic_invoke = {
                "adapter": {"returncode": 0, "stdout": "", "stderr": "lokay effector reported failure"},
                "values": {
                    "ok": False,
                    "status": "failed",
                    "mutated": False,
                    "reason": "repair_omp_precondition_failed",
                    "failure_class": "terminal",
                    "retry_safe": False,
                },
            }
            with sqlite3.connect(db) as connection:
                connection.execute(
                    "CREATE TABLE processes (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, status TEXT NOT NULL, output_json TEXT NOT NULL, error_json TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO processes VALUES (?,?,?,?,?)",
                    (
                        "run-b:auto_worker:triage_invoke_repair_omp",
                        "run-b",
                        "succeeded",
                        json.dumps(semantic_invoke, sort_keys=True),
                        "{}",
                    ),
                )
                connection.execute(
                    "INSERT INTO processes VALUES (?,?,?,?,?)",
                    (
                        "run-b:auto_worker:triage_push_repair_branch",
                        "run-b",
                        "succeeded",
                        json.dumps({"values": {"ok": True, "status": "noop", "mutated": False}}, sort_keys=True),
                        "{}",
                    ),
                )
            evidence = repair.read_repair_recovery_continuation_evidence(
                req(
                    {
                        "run_id": "run-c",
                        "candidate": "candidate-c",
                        "db_path": str(db),
                        "path_id": "auto_worker",
                        "conduction": {
                            "verify_repair_attempt_recovery": {
                                "ok": True,
                                "status": "verified",
                                "recovery_verified": True,
                                "recovery_claim": claim,
                                "recovery_claim_path": str(claim_path),
                                "reservation_path": str(reservation_path),
                            },
                            "read_repair_attempt_state": {
                                "ok": True,
                                "status": "found",
                                "reservation_path": str(reservation_path),
                            },
                        },
                    }
                )
            )
            self.assertEqual(evidence["status"], "validated", evidence)

    def test_read_repair_attempt_baseline_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = req({
                "repo": "o/r", "pr_number": 1, "branch": "ai/fix/1",
                "worktree_root": str(root / "worktrees"),
                "conduction": {
                    "decide_repair_attempt": {"ok": True, "status": "invoke", "authorize": True},
                    "verify_repair_worktree": {"ok": True, "status": "verified", "head": "head-a"}
                }
            })
            with (
                mock.patch("lokay.steps.repair.rev_parse", return_value="head-a"),
                mock.patch("lokay.steps.repair.git", return_value=""),
            ):
                out = repair.read_repair_attempt_baseline(request)
            self.assertEqual(out["status"], "read")
            self.assertEqual(out["pre_head"], "head-a")
            self.assertEqual(out["pre_status"], "")
            self.assertTrue(out["baseline_verified"])

    def test_dry_run_worktree_verification_stays_planned(self) -> None:
        request = req({
            "dry_run": True,
            "repo": "o/r",
            "pr_number": 1,
            "branch": "ai/fix/1",
            "conduction": {
                "add_repair_worktree": {"ok": True, "status": "planned"},
            },
        })
        out = repair.verify_repair_worktree(request)
        self.assertEqual(out["status"], "planned")
        self.assertEqual(out["operation"], "verify_repair_worktree")

        baseline = repair.read_repair_attempt_baseline(req({
            "dry_run": True,
            "conduction": {"verify_repair_worktree": out},
        }))
        self.assertEqual(baseline["status"], "noop")
        self.assertEqual(baseline["reason"], "dry_run")
    def test_read_repair_attempt_baseline_dirty_or_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = req({
                "repo": "o/r", "pr_number": 1, "branch": "ai/fix/1",
                "worktree_root": str(root / "worktrees"),
                "conduction": {
                    "decide_repair_attempt": {"ok": True, "status": "invoke", "authorize": True},
                    "verify_repair_worktree": {"ok": True, "status": "verified", "head": "head-a"}
                }
            })
            # Scenario 1: head mismatch
            with (
                mock.patch("lokay.steps.repair.rev_parse", return_value="head-b"),
                mock.patch("lokay.steps.repair.git", return_value=""),
            ):
                out = repair.read_repair_attempt_baseline(request)
            self.assertEqual(out["status"], "failed")
            self.assertEqual(out["reason"], "repair_attempt_baseline_mismatch")
            # Scenario 2: dirty status
            with (
                mock.patch("lokay.steps.repair.rev_parse", return_value="head-a"),
                mock.patch("lokay.steps.repair.git", return_value="M file.py"),
            ):
                out = repair.read_repair_attempt_baseline(request)
            self.assertEqual(out["status"], "failed")
            self.assertEqual(out["reason"], "repair_attempt_baseline_mismatch")

    def test_repair_reconciliation_propagates_nonselected_state(self) -> None:
        out = repair.read_repair_attempt_reconciliation(req({
            "conduction": {
                "read_repair_attempt_state": {
                    "ok": True,
                    "status": "noop",
                    "reason": "not_selected",
                    "mutated": False,
                }
            }
        }))
        self.assertEqual(out["status"], "noop")
        self.assertEqual(out["reason"], "not_selected")
        self.assertTrue(out["ok"])

    def test_read_repair_attempt_reconciliation_accepts_stale_provenance_oid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_req = req({
                "repo": "o/r", "pr_number": 1, "branch": "ai/fix/1",
                "worktree_root": str(root / "worktrees"),
            })
            ctx = repair._repair_context(base_req)
            local_branch = ctx["local_branch"]
            wt_path = ctx["worktree_path"]
            request = req({
                "repo": "o/r", "pr_number": 1, "branch": "ai/fix/1",
                "worktree_root": str(root / "worktrees"),
                "conduction": {
                    "read_repair_completed_receipt": {"ok": True, "status": "absent"},
                    "read_repair_attempt_state": {
                        "ok": True, "status": "found",
                        "reservation_path": str(root / "reservation.json"),
                        "attempt_state": {
                            "repo": "o/r", "pr_number": 1, "verified_head": "head-a",
                            "pre_head": "head-a", "pre_status": "",
                            "repo_branch": "ai/fix/1", "local_branch": local_branch,
                            "worktree_path": wt_path, "candidate": "cand", "run_id": "run"
                        }
                    },
                    "read_repair_remote_head": {"ok": True, "remote_oid": "head-a"},
                    "read_repair_worktree_inventory": {"ok": True, "worktrees": [{"path": wt_path, "branch": local_branch}]},
                    "read_repair_branch_provenance": {
                        "ok": True, "exists": True, "branch_head": "head-a",
                        "provenance": {"repo": "o/r", "pr": "1", "remote_oid": "stale-head", "target_branch": "ai/fix/1"}
                    }
                }
            })
            with (
                mock.patch("lokay.steps.repair.rev_parse", return_value="head-a"),
                mock.patch("lokay.steps.repair.git", return_value=""),
            ):
                out = repair.read_repair_attempt_reconciliation(request)
            self.assertEqual(out["status"], "unchanged")
            self.assertTrue(out["authorize_reinvoke"])

    def test_read_repair_attempt_reconciliation_committed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_req = req({
                "repo": "o/r", "pr_number": 1, "branch": "ai/fix/1",
                "worktree_root": str(root / "worktrees"),
            })
            ctx = repair._repair_context(base_req)
            local_branch = ctx["local_branch"]
            wt_path = ctx["worktree_path"]
            request = req({
                "repo": "o/r", "pr_number": 1, "branch": "ai/fix/1",
                "worktree_root": str(root / "worktrees"),
                "conduction": {
                    "read_repair_completed_receipt": {"ok": True, "status": "absent"},
                    "read_repair_attempt_state": {
                        "ok": True, "status": "found",
                        "attempt_state": {
                            "repo": "o/r", "pr_number": 1, "verified_head": "head-a",
                            "pre_head": "head-a", "pre_status": "",
                            "repo_branch": "ai/fix/1", "local_branch": local_branch,
                            "worktree_path": wt_path, "candidate": "cand", "run_id": "run"
                        }
                    },
                    "read_repair_remote_head": {"ok": True, "remote_oid": "head-a"},
                    "read_repair_worktree_inventory": {"ok": True, "worktrees": [{"path": wt_path, "branch": local_branch}]},
                    "read_repair_branch_provenance": {
                        "ok": True, "exists": True, "branch_head": "head-a",
                        "provenance": {"repo": "o/r", "pr": "1", "remote_oid": "head-a", "target_branch": "ai/fix/1"}
                    }
                }
            })
            with (
                mock.patch("lokay.steps.repair.rev_parse", return_value="head-committed-new"),
                mock.patch("lokay.steps.repair.git", side_effect=["", "file.py"]),
            ):
                out = repair.read_repair_attempt_reconciliation(request)
            self.assertEqual(out["status"], "committed")
            self.assertFalse(out["authorize_reinvoke"])
            self.assertTrue(out["resume_postconditions"])

    def test_read_repair_attempt_reconciliation_committed_with_advanced_branch_head(self) -> None:
        """Live provenance tip may advance after OMP while remote stays at pre_head."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_req = req({
                "repo": "o/r", "pr_number": 1, "branch": "ai/fix/1",
                "worktree_root": str(root / "worktrees"),
            })
            ctx = repair._repair_context(base_req)
            local_branch = ctx["local_branch"]
            wt_path = ctx["worktree_path"]
            request = req({
                "repo": "o/r", "pr_number": 1, "branch": "ai/fix/1",
                "worktree_root": str(root / "worktrees"),
                "conduction": {
                    "read_repair_completed_receipt": {"ok": True, "status": "absent"},
                    "read_repair_attempt_state": {
                        "ok": True, "status": "found",
                        "attempt_state": {
                            "repo": "o/r", "pr_number": 1, "verified_head": "head-a",
                            "pre_head": "head-a", "pre_status": "",
                            "repo_branch": "ai/fix/1", "local_branch": local_branch,
                            "worktree_path": wt_path, "candidate": "cand", "run_id": "run"
                        }
                    },
                    "read_repair_remote_head": {"ok": True, "remote_oid": "head-a"},
                    "read_repair_worktree_inventory": {"ok": True, "worktrees": [{"path": wt_path, "branch": local_branch}]},
                    "read_repair_branch_provenance": {
                        "ok": True, "exists": True, "branch_head": "head-committed-new",
                        "provenance": {"repo": "o/r", "pr": "1", "remote_oid": "head-a", "target_branch": "ai/fix/1"}
                    }
                }
            })
            with (
                mock.patch("lokay.steps.repair.rev_parse", return_value="head-committed-new"),
                mock.patch("lokay.steps.repair.git", side_effect=["", "file.py"]),
            ):
                out = repair.read_repair_attempt_reconciliation(request)
            self.assertEqual(out["status"], "committed")
            self.assertFalse(out["authorize_reinvoke"])
            self.assertTrue(out["resume_postconditions"])
            self.assertEqual(out["snapshot"]["actual_head"], "head-committed-new")
            self.assertEqual(out["snapshot"]["pre_head"], "head-a")

    def test_read_repair_attempt_reconciliation_empty_tree_delta_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_req = req({
                "repo": "o/r", "pr_number": 1, "branch": "ai/fix/1",
                "worktree_root": str(root / "worktrees"),
            })
            ctx = repair._repair_context(base_req)
            local_branch = ctx["local_branch"]
            wt_path = ctx["worktree_path"]
            request = req({
                "repo": "o/r", "pr_number": 1, "branch": "ai/fix/1",
                "worktree_root": str(root / "worktrees"),
                "conduction": {
                    "read_repair_completed_receipt": {"ok": True, "status": "absent"},
                    "read_repair_attempt_state": {
                        "ok": True, "status": "found",
                        "attempt_state": {
                            "repo": "o/r", "pr_number": 1, "verified_head": "head-a",
                            "pre_head": "head-a", "pre_status": "",
                            "repo_branch": "ai/fix/1", "local_branch": local_branch,
                            "worktree_path": wt_path, "candidate": "cand", "run_id": "run",
                        },
                    },
                    "read_repair_remote_head": {"ok": True, "remote_oid": "head-a"},
                    "read_repair_worktree_inventory": {
                        "ok": True, "worktrees": [{"path": wt_path, "branch": local_branch}],
                    },
                    "read_repair_branch_provenance": {
                        "ok": True, "exists": True, "branch_head": "head-empty",
                        "provenance": {
                            "repo": "o/r", "pr": "1", "remote_oid": "head-a",
                            "target_branch": "ai/fix/1",
                        },
                    },
                },
            })
            with (
                mock.patch("lokay.steps.repair.rev_parse", return_value="head-empty"),
                mock.patch("lokay.steps.repair.git", side_effect=["", ""]),
            ):
                out = repair.read_repair_attempt_reconciliation(request)
            self.assertFalse(out["ok"])
            self.assertEqual(out["reason"], "repair_attempt_empty_tree_delta")
            self.assertEqual(out["failure_class"], "terminal")
            self.assertFalse(out.get("authorize_reinvoke"))
            self.assertFalse(out.get("resume_postconditions"))
            self.assertEqual(out["before_oid"], "head-a")
            self.assertEqual(out["after_oid"], "head-empty")


    def test_read_repair_attempt_reconciliation_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_req = req({
                "repo": "o/r", "pr_number": 1, "branch": "ai/fix/1",
                "worktree_root": str(root / "worktrees"),
            })
            ctx = repair._repair_context(base_req)
            local_branch = ctx["local_branch"]
            wt_path = ctx["worktree_path"]
            request = req({
                "repo": "o/r", "pr_number": 1, "branch": "ai/fix/1",
                "worktree_root": str(root / "worktrees"),
                "conduction": {
                    "read_repair_completed_receipt": {"ok": True, "status": "absent"},
                    "read_repair_attempt_state": {
                        "ok": True, "status": "found",
                        "attempt_state": {
                            "repo": "o/r", "pr_number": 1, "verified_head": "head-a",
                            "pre_head": "head-a", "pre_status": "",
                            "repo_branch": "ai/fix/1", "local_branch": local_branch,
                            "worktree_path": wt_path, "candidate": "cand", "run_id": "run"
                        }
                    },
                    "read_repair_remote_head": {"ok": True, "remote_oid": "head-a"},
                    "read_repair_worktree_inventory": {"ok": True, "worktrees": [{"path": wt_path, "branch": local_branch}]},
                    "read_repair_branch_provenance": {
                        "ok": True, "exists": True, "branch_head": "head-a",
                        "provenance": {"repo": "o/r", "pr": "1", "remote_oid": "head-a", "target_branch": "ai/fix/1"}
                    }
                }
            })
            with (
                mock.patch("lokay.steps.repair.rev_parse", return_value="head-a"),
                mock.patch("lokay.steps.repair.git", return_value="M file.py"),
            ):
                out = repair.read_repair_attempt_reconciliation(request)
            self.assertEqual(out["status"], "failed")
            self.assertEqual(out["reason"], "repair_attempt_reconciliation_dirty")

    def test_read_repair_attempt_reconciliation_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_req = req({
                "repo": "o/r", "pr_number": 1, "branch": "ai/fix/1",
                "worktree_root": str(root / "worktrees"),
            })
            ctx = repair._repair_context(base_req)
            local_branch = ctx["local_branch"]
            wt_path = ctx["worktree_path"]
            request = req({
                "repo": "o/r", "pr_number": 1, "branch": "ai/fix/1",
                "worktree_root": str(root / "worktrees"),
                "conduction": {
                    "read_repair_completed_receipt": {"ok": True, "status": "absent"},
                    "read_repair_attempt_state": {
                        "ok": True, "status": "found",
                        "attempt_state": {
                            "repo": "o/r", "pr_number": 1, "verified_head": "head-a",
                            "pre_head": "head-a", "pre_status": "",
                            "repo_branch": "ai/fix/1", "local_branch": local_branch,
                            "worktree_path": wt_path, "candidate": "cand", "run_id": "run"
                        }
                    },
                    "read_repair_remote_head": {"ok": True, "remote_oid": "head-a"},
                    "read_repair_worktree_inventory": {"ok": True, "worktrees": [{"path": wt_path, "branch": local_branch}]},
                    "read_repair_branch_provenance": {
                        "ok": True, "exists": True, "branch_head": "head-a",
                        "provenance": {"repo": "wrong/repo", "pr": "1", "remote_oid": "head-a", "target_branch": "ai/fix/1"}
                    }
                }
            })
            out = repair.read_repair_attempt_reconciliation(request)
            self.assertEqual(out["status"], "failed")
            self.assertEqual(out["reason"], "repair_attempt_reconciliation_mismatch")

    def test_reconciliation_legacy_reservation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = req({
                "repo": "o/r", "pr_number": 1, "branch": "ai/fix/1",
                "worktree_root": str(root / "worktrees"),
                "conduction": {
                    "read_repair_completed_receipt": {"ok": True, "status": "absent"},
                    "read_repair_attempt_state": {
                        "ok": True, "status": "found",
                        "attempt_state": {
                            "repo": "o/r", "pr_number": 1, "verified_head": "head-a",
                            # pre_head/pre_status/repo_branch etc. are missing
                        }
                    }
                }
            })
            out = repair.read_repair_attempt_reconciliation(request)
            self.assertEqual(out["status"], "failed")
            self.assertEqual(out["reason"], "repair_attempt_reconciliation_legacy_missing_baseline")

    def test_reconciliation_with_mutated_invoke_evidence_fails_closed(self) -> None:
        """Unchanged + recovery mutation evidence stays reinvoke-blocked."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = {"repo": "o/r", "pr_number": 1, "branch": "ai/fix/1", "worktree_root": str(root / "worktrees")}
            ctx = repair._repair_context(req(base))
            res_path = root / "reservation.json"
            res_path.write_text("{}\n")
            process_id = "run:auto_worker:triage_invoke_repair_omp"
            started = {"kind": "repair_invoke_evidence", "process_id": process_id, "status": "started", "pre_head": "head-a", "pre_status": "", "mutated": None}
            terminal = {**started, "status": "succeeded", "post_head": "head-a", "post_status": "", "mutated": True}
            repair._repair_invoke_evidence_path(res_path, process_id).parent.mkdir(parents=True, exist_ok=True)
            repair._repair_invoke_evidence_path(res_path, process_id).write_text(json.dumps(started) + "\n", encoding="utf-8")
            repair._repair_invoke_terminal_evidence_path(res_path, process_id).write_text(json.dumps(terminal) + "\n", encoding="utf-8")
            request = req({
                **base,
                "attempt_recovery": {"run_id": "run", "path_id": "auto_worker", "candidate": "cand"},
                "conduction": {
                    "read_repair_completed_receipt": {"ok": True, "status": "absent"},
                    "read_repair_attempt_state": {
                        "ok": True, "status": "found", "reservation_path": str(res_path),
                        "attempt_state": {
                            "repo": "o/r", "pr_number": 1, "verified_head": "head-a",
                            "pre_head": "head-a", "pre_status": "",
                            "repo_branch": "ai/fix/1", "local_branch": ctx["local_branch"],
                            "worktree_path": ctx["worktree_path"], "candidate": "cand", "run_id": "run",
                        },
                    },
                    "read_repair_remote_head": {"ok": True, "remote_oid": "head-a"},
                    "read_repair_worktree_inventory": {
                        "ok": True, "worktrees": [{"path": ctx["worktree_path"], "branch": ctx["local_branch"]}],
                    },
                    "read_repair_branch_provenance": {
                        "ok": True, "exists": True, "branch_head": "head-a",
                        "provenance": {"repo": "o/r", "pr": "1", "remote_oid": "head-a", "target_branch": "ai/fix/1"},
                    },
                },
            })
            with (
                mock.patch("lokay.steps.repair.rev_parse", return_value="head-a"),
                mock.patch("lokay.steps.repair.git", return_value=""),
            ):
                out = repair.read_repair_attempt_reconciliation(request)
            self.assertEqual(out["status"], "failed")
            self.assertEqual(out["reason"], "repair_attempt_reconciliation_mutated_blocked")

    def test_reconciliation_voided_empty_mutated_authorizes_reinvoke(self) -> None:
        """Succeeded+mutated empty-tree rewind is voided, not mutation_unknown."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = {"repo": "o/r", "pr_number": 1, "branch": "ai/fix/1", "worktree_root": str(root / "worktrees")}
            ctx = repair._repair_context(req(base))
            res_path = root / "reservation.json"
            res_path.write_text("{}\n")
            process_id = "run:auto_worker:triage_invoke_repair_omp"
            started = {
                "kind": "repair_invoke_evidence",
                "process_id": process_id,
                "status": "started",
                "pre_head": "head-a",
                "pre_status": "",
                "mutated": None,
            }
            terminal = {
                **started,
                "status": "succeeded",
                "post_head": "head-empty",
                "post_status": "",
                "mutated": True,
            }
            repair._repair_invoke_evidence_path(res_path, process_id).parent.mkdir(parents=True, exist_ok=True)
            repair._repair_invoke_evidence_path(res_path, process_id).write_text(
                json.dumps(started) + "\n", encoding="utf-8"
            )
            repair._repair_invoke_terminal_evidence_path(res_path, process_id).write_text(
                json.dumps(terminal) + "\n", encoding="utf-8"
            )
            request = req({
                **base,
                "conduction": {
                    "read_repair_completed_receipt": {"ok": True, "status": "absent"},
                    "read_repair_attempt_state": {
                        "ok": True, "status": "found", "reservation_path": str(res_path),
                        "attempt_state": {
                            "repo": "o/r", "pr_number": 1, "verified_head": "head-a",
                            "pre_head": "head-a", "pre_status": "",
                            "repo_branch": "ai/fix/1", "local_branch": ctx["local_branch"],
                            "worktree_path": ctx["worktree_path"], "candidate": "cand", "run_id": "run",
                        },
                    },
                    "read_repair_remote_head": {"ok": True, "remote_oid": "head-a"},
                    "read_repair_worktree_inventory": {
                        "ok": True, "worktrees": [{"path": ctx["worktree_path"], "branch": ctx["local_branch"]}],
                    },
                    "read_repair_branch_provenance": {
                        "ok": True, "exists": True, "branch_head": "head-a",
                        "provenance": {"repo": "o/r", "pr": "1", "remote_oid": "head-a", "target_branch": "ai/fix/1"},
                    },
                },
            })
            with (
                mock.patch("lokay.steps.repair.rev_parse", return_value="head-a"),
                mock.patch("lokay.steps.repair.git", return_value=""),
            ):
                out = repair.read_repair_attempt_reconciliation(request)
            self.assertEqual(out["status"], "unchanged")
            self.assertTrue(out["ok"])
            self.assertTrue(out["authorize_reinvoke"])
            self.assertTrue(out["voided_empty_attempt"])
            self.assertEqual(out["before_oid"], "head-a")
            self.assertEqual(out["after_oid"], "head-empty")

    def test_reconciliation_rewound_nonempty_mutation_stays_blocked(self) -> None:
        """Rewound real content mutation remains mutated_blocked."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = {"repo": "o/r", "pr_number": 1, "branch": "ai/fix/1", "worktree_root": str(root / "worktrees")}
            ctx = repair._repair_context(req(base))
            res_path = root / "reservation.json"
            res_path.write_text("{}\n")
            process_id = "run:auto_worker:triage_invoke_repair_omp"
            started = {
                "kind": "repair_invoke_evidence",
                "process_id": process_id,
                "status": "started",
                "pre_head": "head-a",
                "pre_status": "",
                "mutated": None,
            }
            terminal = {
                **started,
                "status": "succeeded",
                "post_head": "head-real",
                "post_status": "",
                "mutated": True,
            }
            repair._repair_invoke_evidence_path(res_path, process_id).parent.mkdir(parents=True, exist_ok=True)
            repair._repair_invoke_evidence_path(res_path, process_id).write_text(
                json.dumps(started) + "\n", encoding="utf-8"
            )
            repair._repair_invoke_terminal_evidence_path(res_path, process_id).write_text(
                json.dumps(terminal) + "\n", encoding="utf-8"
            )
            request = req({
                **base,
                "conduction": {
                    "read_repair_completed_receipt": {"ok": True, "status": "absent"},
                    "read_repair_attempt_state": {
                        "ok": True, "status": "found", "reservation_path": str(res_path),
                        "attempt_state": {
                            "repo": "o/r", "pr_number": 1, "verified_head": "head-a",
                            "pre_head": "head-a", "pre_status": "",
                            "repo_branch": "ai/fix/1", "local_branch": ctx["local_branch"],
                            "worktree_path": ctx["worktree_path"], "candidate": "cand", "run_id": "run",
                        },
                    },
                    "read_repair_remote_head": {"ok": True, "remote_oid": "head-a"},
                    "read_repair_worktree_inventory": {
                        "ok": True, "worktrees": [{"path": ctx["worktree_path"], "branch": ctx["local_branch"]}],
                    },
                    "read_repair_branch_provenance": {
                        "ok": True, "exists": True, "branch_head": "head-a",
                        "provenance": {"repo": "o/r", "pr": "1", "remote_oid": "head-a", "target_branch": "ai/fix/1"},
                    },
                },
            })
            with (
                mock.patch("lokay.steps.repair.rev_parse", return_value="head-a"),
                mock.patch("lokay.steps.repair.git", side_effect=["", "src/fix.py"]),
            ):
                out = repair.read_repair_attempt_reconciliation(request)
            self.assertEqual(out["status"], "failed")
            self.assertEqual(out["reason"], "repair_attempt_reconciliation_mutated_blocked")
            self.assertFalse(out.get("authorize_reinvoke"))

    def test_reconciliation_classifies_invoke_without_attempt_recovery(self) -> None:
        """Always classify this reservation's invoke evidence even without attempt_recovery."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = {"repo": "o/r", "pr_number": 1, "branch": "ai/fix/1", "worktree_root": str(root / "worktrees")}
            ctx = repair._repair_context(req(base))
            res_path = root / "reservation.json"
            res_path.write_text("{}\n")
            process_id = "run:auto_worker:triage_invoke_repair_omp"
            started = {
                "kind": "repair_invoke_evidence",
                "process_id": process_id,
                "status": "started",
                "pre_head": "head-a",
                "pre_status": "",
                "mutated": None,
            }
            terminal = {
                **started,
                "status": "succeeded",
                "post_head": "head-a",
                "post_status": "",
                "mutated": True,
            }
            repair._repair_invoke_evidence_path(res_path, process_id).parent.mkdir(parents=True, exist_ok=True)
            repair._repair_invoke_evidence_path(res_path, process_id).write_text(
                json.dumps(started) + "\n", encoding="utf-8"
            )
            repair._repair_invoke_terminal_evidence_path(res_path, process_id).write_text(
                json.dumps(terminal) + "\n", encoding="utf-8"
            )
            request = req({
                **base,
                # no attempt_recovery — must still see mutated evidence
                "conduction": {
                    "read_repair_completed_receipt": {"ok": True, "status": "absent"},
                    "read_repair_attempt_state": {
                        "ok": True, "status": "found", "reservation_path": str(res_path),
                        "attempt_state": {
                            "repo": "o/r", "pr_number": 1, "verified_head": "head-a",
                            "pre_head": "head-a", "pre_status": "",
                            "repo_branch": "ai/fix/1", "local_branch": ctx["local_branch"],
                            "worktree_path": ctx["worktree_path"], "candidate": "cand", "run_id": "run",
                        },
                    },
                    "read_repair_remote_head": {"ok": True, "remote_oid": "head-a"},
                    "read_repair_worktree_inventory": {
                        "ok": True, "worktrees": [{"path": ctx["worktree_path"], "branch": ctx["local_branch"]}],
                    },
                    "read_repair_branch_provenance": {
                        "ok": True, "exists": True, "branch_head": "head-a",
                        "provenance": {"repo": "o/r", "pr": "1", "remote_oid": "head-a", "target_branch": "ai/fix/1"},
                    },
                },
            })
            with (
                mock.patch("lokay.steps.repair.rev_parse", return_value="head-a"),
                mock.patch("lokay.steps.repair.git", return_value=""),
            ):
                out = repair.read_repair_attempt_reconciliation(request)
            self.assertEqual(out["status"], "failed")
            self.assertEqual(out["reason"], "repair_attempt_reconciliation_mutated_blocked")

    def test_decide_voided_empty_reinvoke_before_already_repaired(self) -> None:
        from lokay.steps.repair import decide_repair_attempt

        head = "head-a"
        out = decide_repair_attempt(req({
            "enabled": True, "live": True, "dry_run": False,
            "repo": "o/r", "number": 1, "verified_head": head, "candidate": "c1", "run_id": "r1",
            "checks": [{"name": "ci", "conclusion": "FAILURE"}],
            "attempt_state": {
                "repo": "o/r", "pr_number": 1, "verified_head": head, "candidate": "c1", "run_id": "r1",
                "status": "reserved", "attempted": True, "pre_head": head, "pre_status": "",
            },
            "conduction": {
                "read_repair_attempt_state": {
                    "ok": True, "status": "found", "reservation_path": "/state/res.json",
                    "attempt_state": {
                        "repo": "o/r", "pr_number": 1, "verified_head": head, "candidate": "c1", "run_id": "r1",
                        "status": "reserved", "attempted": True, "pre_head": head, "pre_status": "",
                    },
                },
                "read_repair_attempt_reconciliation": {
                    "ok": True, "status": "unchanged", "authorize_reinvoke": True,
                    "voided_empty_attempt": True,
                    "snapshot": {
                        "pre_head": head, "pre_status": "", "actual_head": head,
                        "actual_status": "", "remote_oid": head,
                    },
                },
            },
        }))
        self.assertEqual(out["status"], "invoke")
        self.assertEqual(out["reason"], "voided_empty_attempt_reinvoke")
        self.assertTrue(out["authorize"])
        self.assertTrue(out["authorize_reinvoke"])

    def test_decide_recovery_still_beats_generic_authorize_reinvoke(self) -> None:
        """Verified recovery claim wins over plain unchanged+authorize_reinvoke."""
        from lokay.steps.repair import decide_repair_attempt

        head = "head-a"
        claim = {
            "repo": "o/r", "pr_number": 1, "verified_head": head,
            "recovery_candidate": "c1", "recovery_run_id": "r1",
        }
        out = decide_repair_attempt(req({
            "enabled": True, "live": True, "dry_run": False,
            "repo": "o/r", "number": 1, "verified_head": head, "candidate": "c1", "run_id": "r1",
            "checks": [{"name": "ci", "conclusion": "FAILURE"}],
            "conduction": {
                "read_repair_attempt_state": {
                    "ok": True, "status": "found", "reservation_path": "/state/res.json",
                    "attempt_state": {
                        "repo": "o/r", "pr_number": 1, "verified_head": head,
                        "candidate": "old", "run_id": "old-run",
                        "status": "reserved", "attempted": True, "pre_head": head, "pre_status": "",
                    },
                },
                "read_repair_attempt_reconciliation": {
                    "ok": True, "status": "unchanged", "authorize_reinvoke": True,
                    # no voided_empty_attempt — must not steal recovery
                    "snapshot": {
                        "pre_head": head, "pre_status": "", "actual_head": head,
                        "actual_status": "", "remote_oid": head,
                    },
                },
                "verify_repair_attempt_recovery": {
                    "ok": True, "status": "verified", "recovery_verified": True,
                    "recovery_claim": claim, "recovery_claim_path": "/claim.json",
                },
                "verify_repair_recovery_continuation": {
                    "ok": True, "status": "original", "continuation_verified": True,
                },
            },
        }))
        self.assertEqual(out["status"], "invoke")
        self.assertEqual(out["reason"], "verified_failed_attempt_recovery")
        self.assertTrue(out["authorize"])

    def test_reserve_and_verify_reopened_voided_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            branch = "ai/fix/10"
            worktree_root = str(Path(tmp) / "worktrees")
            local_branch = repair._repair_local_branch("o/r", "11", branch)
            worktree_path = str(Path(worktree_root) / local_branch)
            head = "head-a"
            identity = {
                "repo": "o/r", "pr_number": 11, "verified_head": head,
                "candidate": "a" * 64, "run_id": "run-old",
            }
            payload = {
                **identity,
                "checks": [{"identity": "ci", "conclusion": "FAILURE"}],
                "status": "reserved",
                "attempted": True,
                "kind": "repair_attempt_reservation",
                "pre_head": head,
                "pre_status": "",
                "repo_branch": branch,
                "local_branch": local_branch,
                "worktree_path": worktree_path,
            }
            path = Path(tmp) / "reservation.json"
            original = json.dumps(payload, sort_keys=True) + "\n"
            path.write_text(original, encoding="utf-8")
            decision = {
                "ok": True,
                "status": "invoke",
                "authorize": True,
                "reason": "voided_empty_attempt_reinvoke",
                "authorize_reinvoke": True,
                "reservation_path": str(path),
                **identity,
                "candidate": "b" * 64,
                "run_id": "run-new",
                "checks": payload["checks"],
            }
            reserved = repair.reserve_repair_attempt(req({
                "enabled": True, "live": True, "dry_run": False,
                "repo": "o/r", "number": 11, "verified_head": head,
                "candidate": "b" * 64, "run_id": "run-new",
                "repair_state_root": tmp,
                "conduction": {
                    "decide_repair_attempt": decision,
                    "read_repair_context": {
                        "ok": True, "status": "read",
                        "repo": "o/r", "pr_number": "11", "branch": branch,
                        "local_branch": local_branch, "worktree_root": worktree_root,
                        "worktree_path": worktree_path,
                    },
                    "read_repair_attempt_baseline": {
                        "ok": True, "status": "read", "baseline_verified": True,
                        "pre_head": head, "pre_status": "",
                    },
                    "verify_repair_attempt_recovery": {"ok": True, "status": "inactive"},
                    "verify_repair_recovery_continuation": {"ok": True, "status": "inactive"},
                },
            }))
            self.assertEqual(reserved["status"], "reopened")
            self.assertFalse(reserved["mutated"])
            self.assertTrue(reserved["authorize_reinvoke"])
            self.assertEqual(path.read_text(encoding="utf-8"), original)

            verified = repair.verify_repair_attempt_reservation(req({
                "enabled": True, "live": True, "dry_run": False,
                "candidate": "b" * 64, "run_id": "run-new",
                "conduction": {
                    "decide_repair_attempt": decision,
                    "reserve_repair_attempt": reserved,
                    "read_repair_context": {
                        "ok": True, "status": "read",
                        "repo": "o/r", "pr_number": "11", "branch": branch,
                        "local_branch": local_branch, "worktree_root": worktree_root,
                        "worktree_path": worktree_path,
                    },
                    "verify_repair_attempt_recovery": {"ok": True, "status": "inactive"},
                    "verify_repair_recovery_continuation": {"ok": True, "status": "inactive"},
                },
            }))
            self.assertEqual(verified["status"], "verified")
            self.assertTrue(verified["reopened"])
            self.assertTrue(verified["authorize_reinvoke"])
            self.assertEqual(verified["pre_head"], head)
            self.assertEqual(path.read_text(encoding="utf-8"), original)


    def test_reconciliation_committed_ignores_global_recovery_mutation_gate(self) -> None:
        """Post-OMP clean advance is resume, even when stale global attempt_recovery is set."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = {"repo": "o/r", "pr_number": 1, "branch": "ai/fix/1", "worktree_root": str(root / "worktrees")}
            ctx = repair._repair_context(req(base))
            res_path = root / "reservation.json"
            res_path.write_text("{}\n")
            process_id = "run:auto_worker:triage_invoke_repair_omp"
            started = {"kind": "repair_invoke_evidence", "process_id": process_id, "status": "started", "pre_head": "head-a", "pre_status": "", "mutated": None}
            terminal = {**started, "status": "succeeded", "post_head": "head-committed-new", "post_status": "", "mutated": True}
            repair._repair_invoke_evidence_path(res_path, process_id).parent.mkdir(parents=True, exist_ok=True)
            repair._repair_invoke_evidence_path(res_path, process_id).write_text(json.dumps(started) + "\n", encoding="utf-8")
            repair._repair_invoke_terminal_evidence_path(res_path, process_id).write_text(json.dumps(terminal) + "\n", encoding="utf-8")
            request = req({
                **base,
                "attempt_recovery": {
                    "run_id": "other-run", "path_id": "auto_worker", "candidate": "other",
                    "repo": "mikolaj92/lokay", "pr_number": 11, "verified_head": "ccc",
                },
                "conduction": {
                    "read_repair_completed_receipt": {"ok": True, "status": "absent"},
                    "read_repair_attempt_state": {
                        "ok": True, "status": "found", "reservation_path": str(res_path),
                        "attempt_state": {
                            "repo": "o/r", "pr_number": 1, "verified_head": "head-a",
                            "pre_head": "head-a", "pre_status": "",
                            "repo_branch": "ai/fix/1", "local_branch": ctx["local_branch"],
                            "worktree_path": ctx["worktree_path"], "candidate": "cand", "run_id": "run",
                        },
                    },
                    "read_repair_remote_head": {"ok": True, "remote_oid": "head-a"},
                    "read_repair_worktree_inventory": {
                        "ok": True, "worktrees": [{"path": ctx["worktree_path"], "branch": ctx["local_branch"]}],
                    },
                    "read_repair_branch_provenance": {
                        "ok": True, "exists": True, "branch_head": "head-committed-new",
                        "provenance": {"repo": "o/r", "pr": "1", "remote_oid": "head-a", "target_branch": "ai/fix/1"},
                    },
                },
            })
            with (
                mock.patch("lokay.steps.repair.rev_parse", return_value="head-committed-new"),
                mock.patch("lokay.steps.repair.git", side_effect=["", "file.py"]),
            ):
                out = repair.read_repair_attempt_reconciliation(request)
            self.assertEqual(out["status"], "committed")
            self.assertTrue(out["resume_postconditions"])
            self.assertFalse(out["authorize_reinvoke"])

    def test_read_repair_remote_ancestry_classifies_equal_behind_ahead_diverged(self) -> None:
        context = {
            "repo": "owner/repo",
            "issue": 10,
            "pr_number": 11,
            "branch": "ai/fix/10",
            "clone_path": "/clone",
            "worktree_root": "/worktrees",
        }
        resolved = repair._repair_context(req(context))
        remote = "a" * 40
        local = "b" * 40
        acquired_ref = f"refs/lokay-repair/{remote}"
        base_conduction = {
            "read_repair_context": {"ok": True, "status": "read", **resolved},
            "read_repair_remote_head": {"ok": True, "status": "read", "remote_oid": remote},
            "fetch_repair_remote_head": {"ok": True, "status": "fetched", "remote_oid": remote, "acquired_oid": remote, "acquired_ref": acquired_ref},
            "verify_fetched_repair_remote_head": {
                "ok": True, "status": "verified", "verified": True,
                "remote_oid": remote, "acquired_oid": remote, "acquired_ref": acquired_ref,
            },
            "read_repair_worktree_inventory": {
                "ok": True, "status": "read",
                "worktrees": [{"path": resolved["worktree_path"], "branch": resolved["local_branch"], "head": remote}],
            },
            "read_repair_branch_provenance": {"ok": True, "status": "read", "exists": True, "branch_head": remote},
        }

        equal = repair.read_repair_remote_ancestry(req({**context, "conduction": base_conduction}))
        self.assertEqual(equal["relation"], "equal")
        self.assertTrue(equal["descendant"])
        self.assertFalse(equal["local_ahead"])

        behind_conduction = {
            **base_conduction,
            "read_repair_worktree_inventory": {
                "ok": True, "status": "read",
                "worktrees": [{"path": resolved["worktree_path"], "branch": resolved["local_branch"], "head": local}],
            },
            "read_repair_branch_provenance": {"ok": True, "status": "read", "exists": True, "branch_head": local},
        }
        with mock.patch("lokay.steps.repair.run_cmd", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
            behind = repair.read_repair_remote_ancestry(req({**context, "conduction": behind_conduction}))
        self.assertEqual(behind["relation"], "behind")
        self.assertTrue(behind["descendant"])
        self.assertFalse(behind["local_ahead"])

        def _ahead_side_effect(cmd, **_kwargs):
            # first call: local ancestor of remote? no; second: remote ancestor of local? yes
            if cmd[3] == local:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("lokay.steps.repair.run_cmd", side_effect=_ahead_side_effect):
            ahead = repair.read_repair_remote_ancestry(req({**context, "conduction": behind_conduction}))
        self.assertEqual(ahead["relation"], "ahead")
        self.assertTrue(ahead["local_ahead"])
        self.assertFalse(ahead["descendant"])

        with mock.patch("lokay.steps.repair.run_cmd", return_value=SimpleNamespace(returncode=1, stdout="", stderr="")):
            diverged = repair.read_repair_remote_ancestry(req({**context, "conduction": behind_conduction}))
        self.assertFalse(diverged["ok"])
        self.assertEqual(diverged["reason"], "repair_worktree_diverged")
        self.assertEqual(diverged["relation"], "diverged")

    def test_decide_repair_fast_forward_inactive_when_local_ahead(self) -> None:
        context = {
            "repo": "owner/repo", "issue": 10, "pr_number": 11, "branch": "ai/fix/10",
            "clone_path": "/clone", "worktree_root": "/worktrees", "task_id": "",
            "repair_state_root": "/state", "run_id": "run-one",
        }
        resolved = repair._repair_context(req(context))
        remote, local = "a" * 40, "b" * 40
        provenance = {
            "task": "", "issue": "10", "repo": "owner/repo", "pr": "11",
            "receipt": resolved["receipt"], "remote_oid": remote, "target_branch": context["branch"],
        }
        out = repair.decide_repair_worktree_fast_forward(req({
            **context,
            "conduction": {
                "read_repair_context": {"ok": True, "status": "read", **resolved},
                "read_repair_remote_head": {"ok": True, "status": "read", "remote_oid": remote},
                "read_repair_worktree_inventory": {
                    "ok": True, "status": "read",
                    "worktrees": [{"path": resolved["worktree_path"], "branch": resolved["local_branch"], "head": local}],
                },
                "read_repair_branch_provenance": {
                    "ok": True, "status": "read", "exists": True, "branch_head": local, "provenance": provenance,
                },
                "read_repair_worktree_cleanliness": {"ok": True, "status": "read", "clean": True},
                "read_repair_remote_ancestry": {
                    "ok": True, "status": "read", "relation": "ahead", "local_ahead": True,
                    "descendant": False, "local_oid": local, "remote_oid": remote,
                },
            },
        }))
        self.assertEqual(out["status"], "inactive")
        self.assertFalse(out["should_fast_forward"])
        self.assertTrue(out["local_ahead"])

    def test_ownership_reuses_clean_local_ahead_and_rejects_stale_behind(self) -> None:
        context = {
            "repo": "owner/repo", "issue": "10", "pr_number": "11", "branch": "ai/fix/10",
            "clone_path": "/clone", "worktree_root": "/worktrees", "task_id": "",
            "repair_state_root": "/state", "run_id": "run-one",
        }
        resolved = repair._repair_context(req(context))
        remote, local = "a" * 40, "b" * 40
        provenance = {
            "task": "", "issue": "10", "repo": "owner/repo", "pr": "11",
            "receipt": resolved["receipt"], "remote_oid": remote, "target_branch": context["branch"],
        }
        base = {
            "read_repair_context": {"ok": True, "status": "read", **resolved},
            "read_repair_remote_head": {"ok": True, "status": "read", "remote_oid": remote},
            "read_repair_worktree_inventory": {
                "ok": True, "status": "read",
                "worktrees": [{"path": resolved["worktree_path"], "branch": resolved["local_branch"], "head": local}],
            },
            "read_repair_branch_provenance": {
                "ok": True, "status": "read", "exists": True, "branch_head": local, "provenance": provenance,
            },
            "read_repair_creation_evidence": {"ok": True, "status": "absent", "verified": False},
            "read_repair_worktree_cleanliness": {"ok": True, "status": "read", "clean": True},
            "decide_repair_worktree_fast_forward": {"ok": True, "status": "inactive", "should_fast_forward": False, "local_ahead": True},
            "fast_forward_repair_worktree": {"ok": True, "status": "inactive"},
        }
        with mock.patch("lokay.steps.repair.git", return_value="file.py"):
            ahead = repair.decide_repair_worktree_ownership(req({
                **context,
                "conduction": {
                    **base,
                    "read_repair_remote_ancestry": {
                        "ok": True, "status": "read", "relation": "ahead", "local_ahead": True,
                        "local_oid": local, "remote_oid": remote,
                    },
                },
            }))
        self.assertTrue(ahead["ok"])
        self.assertTrue(ahead["reuse"])
        self.assertTrue(ahead["local_ahead"])
        self.assertEqual(ahead["expected_head"], local)
        self.assertEqual(ahead["resume_local_head"], local)

        with mock.patch("lokay.steps.repair.git", return_value=""):
            empty = repair.decide_repair_worktree_ownership(req({
                **context,
                "conduction": {
                    **base,
                    "read_repair_remote_ancestry": {
                        "ok": True, "status": "read", "relation": "ahead", "local_ahead": True,
                        "local_oid": local, "remote_oid": remote,
                    },
                },
            }))
        self.assertFalse(empty["ok"])
        self.assertEqual(empty["reason"], "repair_worktree_empty_tree_delta")
        self.assertFalse(empty.get("authorize_reinvoke"))
        self.assertFalse(empty.get("resume_postconditions"))
        self.assertEqual(empty["local_oid"], local)
        self.assertEqual(empty["remote_oid"], remote)

        # Behind without a successful FF remains stale when FF did not advance.
        stale = repair.decide_repair_worktree_ownership(req({
            **context,
            "conduction": {
                **base,
                "read_repair_remote_ancestry": {
                    "ok": True, "status": "read", "relation": "behind", "local_ahead": False,
                    "descendant": True, "local_oid": local, "remote_oid": remote,
                },
                "decide_repair_worktree_fast_forward": {
                    "ok": True, "status": "authorized", "should_fast_forward": True, "local_ahead": False,
                },
                "fast_forward_repair_worktree": {
                    "ok": True, "status": "inactive", "should_fast_forward": False, "mutated": False,
                },
            },
        }))
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["reason"], "stale_repair_remote_head")



    def test_verify_repair_worktree_accepts_local_ahead_expected_head(self) -> None:
        context = {
            "repo": "owner/repo", "issue": 10, "pr_number": 11, "branch": "ai/fix/10",
            "clone_path": "/clone", "worktree_root": "/worktrees",
        }
        resolved = repair._repair_context(req(context))
        remote, local = "a" * 40, "b" * 40
        with (
            mock.patch("lokay.steps.repair.git", side_effect=[resolved["worktree_path"], resolved["local_branch"]]),
            mock.patch("lokay.steps.repair.rev_parse", return_value=local),
        ):
            out = repair.verify_repair_worktree(req({
                **context,
                "conduction": {
                    "add_repair_worktree": {"ok": True, "status": "reused", "worktree_path": resolved["worktree_path"]},
                    "decide_repair_worktree_ownership": {
                        "ok": True, "status": "reuse", "reuse": True, "local_ahead": True,
                        "expected_head": local, "resume_local_head": local, "remote_oid": remote,
                    },
                },
            }))
        self.assertEqual(out["status"], "verified")
        self.assertEqual(out["head"], local)
        self.assertTrue(out["local_ahead"])
        self.assertEqual(out["remote_oid"], remote)

    def test_decide_repair_attempt_authorizes_resume_postconditions_before_already_repaired(self) -> None:
        from lokay.steps.repair import decide_repair_attempt

        pre, actual = "head-pre", "head-post"
        out = decide_repair_attempt(req({
            "enabled": True, "live": True, "dry_run": False,
            "repo": "o/r", "number": 1, "verified_head": pre, "candidate": "c1", "run_id": "r1",
            "checks": [{"name": "ci", "conclusion": "FAILURE"}],
            "attempt_state": {
                "repo": "o/r", "pr_number": 1, "verified_head": pre, "candidate": "c1", "run_id": "r1",
                "status": "reserved", "attempted": True, "pre_head": pre, "pre_status": "",
            },
            "conduction": {
                "read_repair_attempt_state": {
                    "ok": True, "status": "found", "reservation_path": "/state/res.json",
                    "attempt_state": {
                        "repo": "o/r", "pr_number": 1, "verified_head": pre, "candidate": "c1", "run_id": "r1",
                        "status": "reserved", "attempted": True, "pre_head": pre, "pre_status": "",
                    },
                },
                "read_repair_attempt_reconciliation": {
                    "ok": True, "status": "committed", "authorize_reinvoke": False, "resume_postconditions": True,
                    "snapshot": {
                        "pre_head": pre, "pre_status": "", "actual_head": actual,
                        "actual_status": "", "remote_oid": pre,
                    },
                },
            },
        }))
        self.assertEqual(out["status"], "resume_postconditions")
        self.assertEqual(out["reason"], "resume_postconditions")
        self.assertTrue(out["authorize"])
        self.assertTrue(out["resume_postconditions"])
        self.assertFalse(out["authorize_reinvoke"])
        self.assertEqual(out["attempt_state"]["pre_head"], pre)
        self.assertEqual(out["attempt_state"]["actual_head"], actual)

    def test_reserve_and_verify_resume_without_rebaseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            branch = "ai/fix/10"
            worktree_root = str(Path(tmp) / "worktrees")
            local_branch = repair._repair_local_branch("o/r", "11", branch)
            worktree_path = str(Path(worktree_root) / local_branch)
            pre = "head-pre"
            actual = "head-post"
            identity = {
                "repo": "o/r", "pr_number": 11, "verified_head": pre,
                "candidate": "a" * 64, "run_id": "run-a",
            }
            payload = {
                **identity,
                "checks": [{"identity": "ci", "conclusion": "FAILURE"}],
                "status": "reserved",
                "attempted": True,
                "kind": "repair_attempt_reservation",
                "pre_head": pre,
                "pre_status": "",
                "repo_branch": branch,
                "local_branch": local_branch,
                "worktree_path": worktree_path,
            }
            path = Path(tmp) / "reservation.json"
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            decision = {
                "ok": True,
                "status": "resume_postconditions",
                "authorize": True,
                "reason": "resume_postconditions",
                "resume_postconditions": True,
                "authorize_reinvoke": False,
                "reservation_path": str(path),
                **identity,
                "checks": payload["checks"],
            }
            reserved = repair.reserve_repair_attempt(req({
                "enabled": True, "live": True, "dry_run": False,
                "repair_state_root": tmp,
                "conduction": {
                    "decide_repair_attempt": decision,
                    "read_repair_context": {
                        "ok": True, "status": "read",
                        "repo": "o/r", "pr_number": "11", "branch": branch,
                        "local_branch": local_branch, "worktree_root": worktree_root,
                        "worktree_path": worktree_path,
                    },
                    "read_repair_attempt_baseline": {
                        "ok": True, "status": "read", "baseline_verified": True,
                        "pre_head": pre, "pre_status": "", "local_ahead": True,
                        "resume_local_head": actual,
                    },
                    "verify_repair_attempt_recovery": {"ok": True, "status": "inactive"},
                    "verify_repair_recovery_continuation": {"ok": True, "status": "inactive"},
                },
            }))
            self.assertEqual(reserved["status"], "resumed")
            self.assertFalse(reserved["mutated"])
            self.assertTrue(reserved["resume_postconditions"])
            self.assertEqual(reserved["reservation"]["pre_head"], pre)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["pre_head"], pre)

            verified = repair.verify_repair_attempt_reservation(req({
                "enabled": True, "live": True, "dry_run": False,
                "candidate": identity["candidate"], "run_id": identity["run_id"],
                "conduction": {
                    "decide_repair_attempt": decision,
                    "reserve_repair_attempt": reserved,
                    "read_repair_context": {
                        "ok": True, "status": "read",
                        "repo": "o/r", "pr_number": "11", "branch": branch,
                        "local_branch": local_branch, "worktree_root": worktree_root,
                        "worktree_path": worktree_path,
                    },
                    "verify_repair_attempt_recovery": {"ok": True, "status": "inactive"},
                    "verify_repair_recovery_continuation": {"ok": True, "status": "inactive"},
                },
            }))
            self.assertEqual(verified["status"], "verified")
            self.assertTrue(verified["verified"])
            self.assertTrue(verified["resumed"])
            self.assertTrue(verified["resume_postconditions"])
            self.assertEqual(verified["pre_head"], pre)

    def test_invoke_repair_omp_reuses_without_running_omp_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            res_path = root / "reservation.json"
            pre, actual = "head-pre", "head-post"
            reservation = {
                "repo": "o/r", "pr_number": 1, "verified_head": pre,
                "candidate": "c" * 64, "run_id": "run-r",
                "status": "reserved", "attempted": True,
                "pre_head": pre, "pre_status": "",
                "repo_branch": "ai/fix/1", "local_branch": "lokay/repair/x",
                "worktree_path": str(root / "worktree"),
            }
            res_path.write_text(json.dumps(reservation) + "\n", encoding="utf-8")
            process_id = "run-r:auto_worker:triage_invoke_repair_omp"
            request = invoke_req({
                "repo": "o/r", "number": 1, "verified_head": pre,
                "candidate": "c" * 64, "run_id": "run-r",
                "worktree_path": str(root / "worktree"),
                "prompt": "must not run",
                "conduction": {
                    "verify_repair_attempt_reservation": {
                        "ok": True, "status": "verified", "verified": True,
                        "resumed": True, "resume_postconditions": True,
                        "reservation_path": str(res_path), "reservation": reservation,
                        "pre_head": pre,
                    },
                    "read_repair_omp_preconditions": {
                        "ok": True, "status": "ready", "worktree_path": str(root / "worktree"),
                        "pre_head": pre, "resume_local_head": actual, "resume_postconditions": True,
                    },
                    "decide_repair_attempt": {
                        "ok": True, "status": "resume_postconditions", "authorize": True,
                        "reason": "resume_postconditions", "resume_postconditions": True,
                        "repo": "o/r", "pr_number": 1, "verified_head": pre,
                        "candidate": "c" * 64, "run_id": "run-r",
                    },
                    "build_repair_prompt": {"ok": True, "status": "built", "prompt": "must not run"},
                },
            }, process_id=process_id)
            with (
                mock.patch("lokay.steps.repair.rev_parse", return_value=actual),
                mock.patch("lokay.steps.repair.git", return_value=""),
                mock.patch("lokay.steps.repair.run_omp") as run_omp,
            ):
                out = repair.invoke_repair_omp(request)
            self.assertEqual(out["status"], "reused")
            self.assertFalse(out["mutated"])
            self.assertTrue(out["resume_postconditions"])
            self.assertEqual(out["pre_head"], pre)
            self.assertEqual(out["resume_local_head"], actual)
            run_omp.assert_not_called()

    def test_verify_repair_omp_postconditions_accepts_reused_with_delta(self) -> None:
        path = "/worktrees/lokay/repair/resume"
        pre, actual = "a" * 40, "b" * 40
        with (
            mock.patch("lokay.steps.repair.git", side_effect=[path, "file.py"]),
            mock.patch("lokay.steps.repair.rev_parse", return_value=actual),
        ):
            out = repair.verify_repair_omp_postconditions(req({
                "enabled": True, "live": True, "dry_run": False,
                "repo": "o/r", "issue": 10, "pr_number": 11, "branch": "ai/fix/10",
                "worktree_path": path,
                "conduction": {
                    "decide_repair_attempt": {
                        "ok": True, "status": "resume_postconditions", "authorize": True,
                        "reason": "resume_postconditions", "resume_postconditions": True,
                    },
                    "read_repair_omp_preconditions": {
                        "ok": True, "status": "ready", "pre_head": pre,
                        "worktree_path": path, "resume_postconditions": True,
                        "repo": "o/r", "issue": "10", "pr_number": "11", "branch": "ai/fix/10",
                    },
                    "invoke_repair_omp": {
                        "ok": True, "status": "reused", "mutated": False,
                        "pre_head": pre, "resume_local_head": actual,
                        "resume_postconditions": True, "worktree_path": path,
                    },
                },
            }))
        self.assertEqual(out["status"], "verified")
        self.assertEqual(out["before_oid"], pre)
        self.assertEqual(out["after_oid"], actual)
        self.assertEqual(out["changed_paths"], ["file.py"])
        self.assertTrue(out["resume_postconditions"])
        self.assertEqual(out["worktree_path"], path)

    def test_verify_repair_omp_postconditions_rejects_empty_resume_delta(self) -> None:
        path = "/worktrees/lokay/repair/resume"
        oid = "a" * 40
        with (
            mock.patch("lokay.steps.repair.git", side_effect=[path, ""]),
            mock.patch("lokay.steps.repair.rev_parse", return_value=oid),
        ):
            out = repair.verify_repair_omp_postconditions(req({
                "enabled": True, "live": True, "dry_run": False,
                "worktree_path": path,
                "conduction": {
                    "decide_repair_attempt": {
                        "ok": True, "status": "resume_postconditions", "authorize": True,
                        "reason": "resume_postconditions",
                    },
                    "read_repair_omp_preconditions": {
                        "ok": True, "status": "ready", "pre_head": oid,
                        "worktree_path": path, "resume_postconditions": True,
                    },
                    "invoke_repair_omp": {
                        "ok": True, "status": "reused", "mutated": False,
                        "pre_head": oid, "resume_postconditions": True, "worktree_path": path,
                    },
                },
            }))
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "repair_omp_head_unchanged")

    def test_missing_reservation_cannot_silently_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = repair.reserve_repair_attempt(req({
                "enabled": True, "live": True, "dry_run": False,
                "repair_state_root": tmp,
                "conduction": {
                    "decide_repair_attempt": {
                        "ok": True, "status": "resume_postconditions", "authorize": True,
                        "reason": "resume_postconditions", "resume_postconditions": True,
                        "repo": "o/r", "pr_number": 11, "verified_head": "head-pre",
                        "candidate": "a" * 64, "run_id": "run-a",
                        "reservation_path": str(Path(tmp) / "missing.json"),
                    },
                    "verify_repair_attempt_recovery": {"ok": True, "status": "inactive"},
                    "verify_repair_recovery_continuation": {"ok": True, "status": "inactive"},
                },
            }))
            self.assertFalse(out["ok"])
            self.assertEqual(out["reason"], "repair_attempt_reservation_readback_failed")

    def test_invoke_repair_omp_writes_started_and_success_invoke_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            res_path = root / "reservation.json"
            res_path.write_text("{}\n")
            process_id = "run-p:auto_worker:triage_invoke_repair_omp"
            request = invoke_req({"repo": "o/r", "number": 1, "verified_head": "head-a", "worktree_path": str(root / "worktree"), "prompt": "fix it", "conduction": {"verify_repair_attempt_reservation": {"ok": True, "status": "verified", "verified": True, "reservation_path": str(res_path)}, "read_repair_omp_preconditions": {"ok": True, "status": "ready", "worktree_path": str(root / "worktree"), "pre_head": "head-a"}, "decide_repair_attempt": {"ok": True, "status": "invoke", "authorize": True, "verified_head": "head-a"}}}, process_id=process_id)
            started_path = repair._repair_invoke_evidence_path(res_path, process_id)
            terminal_path = repair._repair_invoke_terminal_evidence_path(res_path, process_id)
            with mock.patch("lokay.steps.repair.rev_parse", return_value="head-a"), mock.patch("lokay.steps.repair.git", return_value=""), mock.patch("lokay.steps.repair.run_omp", return_value={"status": "completed"}):
                out = repair.invoke_repair_omp(request)
            self.assertEqual(out["status"], "invoked")
            started = json.loads(started_path.read_text(encoding="utf-8"))
            terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
            self.assertEqual(started["status"], "started")
            self.assertIsNone(started["mutated"])
            self.assertEqual(terminal["status"], "succeeded")
            self.assertFalse(terminal["mutated"])
            self.assertEqual((terminal["pre_head"], terminal["pre_status"], terminal["post_head"], terminal["post_status"]), ("head-a", "", "head-a", ""))

    def test_invoke_repair_omp_writes_failed_invoke_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            res_path = root / "reservation.json"
            res_path.write_text("{}\n")
            process_id = "run-p:auto_worker:triage_invoke_repair_omp"
            request = invoke_req({
                "repo": "o/r", "number": 1, "verified_head": "head-a",
                "worktree_path": str(root / "worktree"), "prompt": "fix it",
                "conduction": {
                    "verify_repair_attempt_reservation": {"ok": True, "status": "verified", "verified": True, "reservation_path": str(res_path)},
                    "read_repair_omp_preconditions": {"ok": True, "status": "ready", "worktree_path": str(root / "worktree"), "pre_head": "head-a"},
                    "decide_repair_attempt": {"ok": True, "status": "invoke", "authorize": True, "verified_head": "head-a"},
                }
            }, process_id=process_id)
            started_path = repair._repair_invoke_evidence_path(res_path, process_id)
            terminal_path = repair._repair_invoke_terminal_evidence_path(res_path, process_id)
            with (
                mock.patch("lokay.steps.repair.rev_parse", return_value="head-a"),
                mock.patch("lokay.steps.repair.git", return_value=""),
                mock.patch("lokay.steps.repair.run_omp", side_effect=subprocess.TimeoutExpired(cmd=["omp"], timeout=10.0)),
            ):
                out = repair.invoke_repair_omp(request)
            self.assertEqual(out["status"], "failed")
            self.assertFalse(out["mutated"])
            started = json.loads(started_path.read_text(encoding="utf-8"))
            terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
            self.assertEqual(started["status"], "started")
            self.assertIsNone(started["mutated"])
            self.assertEqual(terminal["status"], "timed_out")
            self.assertFalse(terminal["mutated"])
            self.assertEqual((terminal["pre_head"], terminal["pre_status"], terminal["post_head"], terminal["post_status"]), ("head-a", "", "head-a", ""))

    def test_repair_base_head_uses_conducted_context_repo(self) -> None:
        with mock.patch("lokay.steps.repair.run_cmd", return_value=SimpleNamespace(stdout="a" * 40)) as run:
            out = repair.read_repair_base_head(req({"base_branch": "main", "conduction": {"triage_read_repair_context": {"ok": True, "repo": "owner/repo"}}}))
        self.assertTrue(out["ok"])
        self.assertEqual(run.call_args.args[0][2], "repos/owner/repo/git/ref/heads/main")


    def test_existing_repair_pr_uses_head_repository_field(self) -> None:
        context = {
            "repo": "o/r",
            "pr_number": 7,
            "branch": "ai/fix/1-x",
            "issue": 1,
        }
        pr = {
            "number": 7,
            "headRefName": "ai/fix/1-x",
            "headRefOid": "deadbeef",
            "baseRefName": "main",
            "headRepository": {"nameWithOwner": "o/r"},
            "url": "https://github.com/o/r/pull/7",
        }
        with mock.patch("lokay.steps.repair.run_cmd", return_value=SimpleNamespace(stdout=json.dumps([pr]))) as run:
            with mock.patch("lokay.steps.repair._repair_execution_gate", return_value=None), mock.patch(
                "lokay.steps.repair._repair_upstream", return_value=None
            ), mock.patch("lokay.steps.repair._repair_context", return_value=context):
                out = repair.read_existing_repair_pr(req({}))
        self.assertEqual(out["status"], "read")
        self.assertEqual(out["pr"]["headRepository"]["nameWithOwner"], "o/r")
        fields = run.call_args.args[0][-1]
        self.assertIn("headRepository", fields)
        self.assertNotIn(",repository,", f",{fields},")
        with mock.patch("lokay.steps.repair._repair_execution_gate", return_value=None), mock.patch(
            "lokay.steps.repair._repair_upstream", return_value=None
        ), mock.patch("lokay.steps.repair._repair_context", return_value=context):
            verified = repair.verify_existing_repair_pr(req({
                "conduction": {
                    "read_existing_repair_pr": {"ok": True, "status": "read", "pr": pr},
                    "verify_repair_push_oid": {"ok": True, "status": "verified", "remote_oid": "deadbeef"},
                }
            }))
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(verified["repo"], "o/r")

    def test_legacy_base_refresh_eligible_and_partial_fail_closed(self) -> None:
        state = {
            "repo": "mikolaj92/lokay",
            "pr_number": 11,
            "verified_head": "ccc470458c0f4eb3cc96da7ea1cfcfc7915c98a7",
            "attempted": True,
            "status": "reserved",
        }
        pr = {
            "repository": {"nameWithOwner": "mikolaj92/lokay"},
            "number": 11,
            "state": "OPEN",
            "headRefName": "ai/fix/10-issue-mikolaj92-lokay-10-canary-test-ta",
            "headRefOid": "ccc470458c0f4eb3cc96da7ea1cfcfc7915c98a7",
            "baseRefName": "main",
            "baseRefOid": "738508805bd4c089ac22efc8e89dfce88b5b7560",
            "mergeStateStatus": "UNSTABLE",
        }
        base = {
            "action": "repair",
            "attempt_state": state,
            "pr": pr,
            "branch": pr["headRefName"],
            "base_branch": "main",
            "conduction": {
                "read_repair_attempt_state": {"ok": True, "status": "found", "attempt_state": state},
                "read_repair_base_head": {"ok": True, "status": "read", "base_ref_oid": "f34b909af9d948179cf630540b94da87bed9465b"},
            },
        }
        out = repair.decide_legacy_repair_head_refresh(req(base))
        self.assertTrue(out["should_refresh"])
        self.assertEqual(out["observed_base_ref_oid"], "738508805bd4c089ac22efc8e89dfce88b5b7560")
        self.assertEqual(out["authoritative_base_ref_oid"], "f34b909af9d948179cf630540b94da87bed9465b")
        self.assertEqual(out["refresh_kind"], "legacy_base_synchronization")
        partial = repair.decide_legacy_repair_head_refresh(req({**base, "attempt_state": {**state, "pre_head": "old"}, "conduction": {**base["conduction"], "read_repair_attempt_state": {"ok": True, "status": "found", "attempt_state": {**state, "pre_head": "old"}}}}))
        self.assertFalse(partial["ok"])
        self.assertEqual(partial["reason"], "legacy_repair_reservation_partial_baseline")

    def test_legacy_base_refresh_dry_run_mutation_conflict_and_no_force(self) -> None:
        decision = {"status": "refresh", "ok": True, "should_refresh": True, "repo": "o/r", "pr_number": 7, "old_head": "old", "refresh_kind": "legacy_base_synchronization"}
        planned = repair.update_legacy_repair_pr_branch(req({"dry_run": True, "conduction": {"decide_legacy_repair_head_refresh": decision}}))
        self.assertEqual(planned["status"], "planned")
        with mock.patch("lokay.steps.repair.run_cmd") as run:
            run.return_value = SimpleNamespace(stdout="", stderr="")
            out = repair.update_legacy_repair_pr_branch(req({"dry_run": False, "conduction": {"decide_legacy_repair_head_refresh": decision}}))
        self.assertEqual(out["status"], "updated")
        command = run.call_args.args[0]
        self.assertEqual(command, ["gh", "api", "--method", "PUT", "repos/o/r/pulls/7/update-branch", "-f", "expected_head_sha=old", "-f", "update_method=merge"])
        self.assertNotIn("--force", command)
        with mock.patch("lokay.steps.repair.run_cmd", side_effect=CommandError(["gh"], 409, "", "conflict")):
            conflict = repair.update_legacy_repair_pr_branch(req({"dry_run": False, "conduction": {"decide_legacy_repair_head_refresh": decision}}))
        self.assertEqual(conflict["failure_class"], "reconcile_then_retry")
        self.assertTrue(conflict["mutated"])

    def test_legacy_base_refresh_readback_requires_new_head_identity_and_base(self) -> None:
        decision = {"status": "refresh", "ok": True, "should_refresh": True, "repo": "o/r", "pr_number": 7, "branch": "feat", "base_branch": "main", "old_head": "old", "authoritative_base_ref_oid": "base-new"}
        update = {"status": "updated", "ok": True, "mutated": True}
        good = {"repository": {"nameWithOwner": "o/r"}, "number": 7, "state": "OPEN", "headRefName": "feat", "headRefOid": "new", "baseRefName": "main", "baseRefOid": "base-new"}
        request_data = {"dry_run": False, "conduction": {"decide_legacy_repair_head_refresh": decision, "update_legacy_repair_pr_branch": update}}
        with mock.patch("lokay.steps.repair.run_cmd", return_value=SimpleNamespace(stdout=json.dumps(good))):
            refreshed = repair.verify_legacy_repair_pr_head(req(request_data))
        self.assertEqual(refreshed["status"], "refreshed")
        for field, value in (("headRefOid", "old"), ("baseRefOid", "wrong"), ("headRefName", "other"), ("number", 8)):
            bad = dict(good, **{field: value})
            with mock.patch("lokay.steps.repair.run_cmd", return_value=SimpleNamespace(stdout=json.dumps(bad))):
                rejected = repair.verify_legacy_repair_pr_head(req(request_data))
            self.assertFalse(rejected["ok"])

    def test_failed_reservation_state_blocks_refresh_and_fast_forward(self) -> None:
        failed_state = {
            "ok": False,
            "status": "failed",
            "reason": "repair_attempt_state_malformed",
        }
        decision = repair.decide_legacy_repair_head_refresh(req({
            "action": "repair",
            "conduction": {"read_repair_attempt_state": failed_state},
        }))
        self.assertFalse(decision["ok"])
        self.assertEqual(decision["reason"], "upstream_failed")
        with mock.patch("lokay.steps.repair.git") as git:
            out = repair.fast_forward_repair_worktree(req({
                "action": "repair",
                "conduction": {
                    "verify_legacy_repair_pr_head": decision,
                    "decide_repair_worktree_fast_forward_execution": {
                        "ok": True,
                        "status": "authorized",
                        "should_fast_forward": True,
                    },
                },
            }))
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "upstream_failed")
        git.assert_not_called()

    def test_nested_failed_reservation_blocks_refresh_and_fast_forward(self) -> None:
        state_read = {
            "ok": True,
            "status": "found",
            "attempt_state": {
                "attempted": True,
                "status": "failed",
                "repo": "o/r",
                "pr_number": 7,
                "verified_head": "a" * 40,
            },
        }
        decision = repair.decide_legacy_repair_head_refresh(req({
            "action": "repair",
            "conduction": {"read_repair_attempt_state": state_read},
        }))
        self.assertFalse(decision["ok"])
        self.assertEqual(decision["reason"], "legacy_repair_reservation_invalid_state")
        with mock.patch("lokay.steps.repair.git") as git:
            out = repair.fast_forward_repair_worktree(req({
                "action": "repair",
                "conduction": {
                    "verify_legacy_repair_pr_head": decision,
                    "decide_repair_worktree_fast_forward_execution": {
                        "ok": True,
                        "status": "authorized",
                        "should_fast_forward": True,
                    },
                },
            }))
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "upstream_failed")
        git.assert_not_called()

    def test_legacy_base_refresh_downstream_gate(self) -> None:
        refreshed = {"status": "refreshed", "ok": True, "refresh_kind": "legacy_base_synchronization"}
        out = repair._repair_upstream(req({"conduction": {"verify_legacy_repair_pr_head": refreshed}}), "next_repair_atom", "verify_legacy_repair_pr_head")
        self.assertEqual(out["status"], "noop")
        self.assertEqual(out["reason"], "legacy_base_refreshed")

    def test_non_repair_action_noops_before_review_task_read(self) -> None:
        decision = {"status": "decided", "ok": True, "action": "comment_block", "reason": "missing_test_evidence"}
        with mock.patch("lokay.steps.repair.hermes_kanban_json") as read:
            out = repair.read_review_tasks(req({"conduction": {"triage_decide_triage_action": decision}}))
        self.assertEqual(out["status"], "noop")
        self.assertEqual(out["reason"], "not_selected")
        self.assertFalse(out["mutated"])
        read.assert_not_called()

class TriageTests(unittest.TestCase):
    def test_verify_merge_receipt_propagates_publisher_noop(self) -> None:
        published = {"status": "noop", "ok": True, "mutated": False, "reason": "not_selected", "action": "skip"}
        out = triage.verify_merge_receipt(req({"receipt_path": "/missing/receipt.json", "dry_run": False, "conduction": {"triage_publish_merge_receipt": published}}))
        self.assertEqual(out["status"], "noop")
        self.assertEqual(out["reason"], "not_selected")
        self.assertEqual(out["operation"], "verify_merge_receipt")
        self.assertFalse(out["mutated"])

    def test_non_merge_action_noops_before_assignee_read(self) -> None:
        decision = {"status": "decided", "ok": True, "action": "comment_block", "reason": "missing_test_evidence"}
        with mock.patch("lokay.steps.triage._pr_view") as read:
            out = triage.read_pr_assignees(req({"conduction": {"triage_decide_triage_action": decision}}))
        self.assertEqual(out["status"], "noop")
        self.assertEqual(out["reason"], "not_selected")
        self.assertFalse(out["mutated"])
        read.assert_not_called()

    def test_merge_action_carries_identity_into_assignee_read(self) -> None:
        decision = {
            "status": "decided",
            "ok": True,
            "action": "merge",
            "reason": "ready",
            "repo": "o/r",
            "number": 5,
        }
        readback = mock.Mock(stdout='{"assignees": [{"login": "mikolaj92"}]}')
        with mock.patch("lokay.steps.triage._pr_view", return_value=readback) as read:
            out = triage.read_pr_assignees(req({"conduction": {"triage_decide_triage_action": decision}}))
        self.assertEqual((out["repo"], out["number"]), ("o/r", 5))
        read.assert_called_once_with("gh", "o/r", 5, "assignees")

    def test_merge_chain_propagates_identity_and_head(self) -> None:
        decision = {
            "status": "decided",
            "ok": True,
            "action": "merge",
            "repo": "o/r",
            "number": 5,
            "head_oid": "abc",
        }
        readback = mock.Mock(stdout='{"assignees": []}')
        with mock.patch("lokay.steps.triage._pr_view", return_value=readback):
            assignees = triage.read_pr_assignees(req({
                "conduction": {"triage_decide_triage_action": decision},
            }))
        selected = triage.decide_pr_assignee(req({
            "conduction": {"triage_read_pr_assignees": assignees},
        }))
        with mock.patch("lokay.steps.triage.run_cmd"):
            assigned = triage.assign_pr(req({
                "conduction": {
                    "triage_decide_pr_assignee": selected,
                    "triage_decide_triage_action": decision,
                },
            }))
        assigned_readback = mock.Mock(stdout='{"assignees": [{"login": "mikolaj92"}]}')
        with mock.patch("lokay.steps.triage._pr_view", return_value=assigned_readback):
            verified = triage.verify_pr_assignee(req({
                "conduction": {
                    "triage_assign_pr": assigned,
                    "triage_decide_pr_assignee": selected,
                },
            }))
        comment_noop = {
            "status": "noop",
            "ok": True,
            "reason": "not_selected",
            "operation": "verify_pr_comment",
        }
        merge_view = {"state": "OPEN", "headRefOid": "abc"}
        with mock.patch("lokay.steps.triage._read_merge_view", return_value=merge_view):
            preconditions = triage.read_merge_preconditions(req({
                "dry_run": False,
                "conduction": {
                    "triage_verify_pr_assignee": verified,
                    "triage_verify_pr_comment": comment_noop,
                },
            }))
        self.assertEqual(
            (
                preconditions["status"],
                preconditions["repo"],
                preconditions["number"],
                preconditions["head_oid"],
            ),
            ("merge_preconditions_read", "o/r", 5, "abc"),
        )
        merged = triage.merge_pr(req({
            "dry_run": True,
            "conduction": {
                "triage_read_merge_preconditions": preconditions,
                "triage_decide_triage_action": decision,
            },
        }))
        self.assertEqual((merged["status"], merged["repo"], merged["number"]), ("planned", "o/r", 5))

    def test_existing_assignee_is_verified_for_merge(self) -> None:
        decision = {
            "status": "noop",
            "ok": True,
            "reason": "already_assigned",
            "repo": "o/r",
            "number": 5,
            "head_oid": "abc",
            "assignee": "mikolaj92",
        }
        assigned = {**decision, "operation": "assign_pr"}
        readback = mock.Mock(stdout='{"assignees": [{"login": "mikolaj92"}]}')
        with mock.patch("lokay.steps.triage._pr_view", return_value=readback):
            result = triage.verify_pr_assignee(req({
                "dry_run": False,
                "conduction": {
                    "triage_assign_pr": assigned,
                    "triage_decide_pr_assignee": decision,
                },
            }))
        self.assertEqual((result["status"], result["repo"], result["number"]), ("assignee_verified", "o/r", 5))

    def test_unselected_assignee_verification_remains_noop(self) -> None:
        result = triage.verify_pr_assignee(req({
            "conduction": {
                "triage_assign_pr": {
                    "status": "noop",
                    "ok": True,
                    "reason": "not_selected",
                    "operation": "assign_pr",
                },
            },
        }))
        self.assertEqual((result["status"], result["reason"]), ("noop", "not_selected"))

    def test_linked_issue_is_derived_and_already_closed_reconciles(self) -> None:
        provenance = {
            "status": "merge_provenance_verified",
            "ok": True,
            "repo": "o/r",
            "verified_provenance": {"head_ref": "ai/fix/10-repair", "merge_oid": "abc"},
        }
        linked = triage.verify_linked_merge_provenance(req({
            "conduction": {"triage_verify_merge_provenance": provenance},
        }))
        self.assertEqual((linked["status"], linked["issue"]), ("linked_merge_provenance_verified", 10))
        closed_view = mock.Mock(stdout='{"state": "CLOSED"}')
        with mock.patch("lokay.steps.triage.run_cmd", return_value=closed_view) as run:
            state = triage.read_linked_issue_state(req({
                "dry_run": False,
                "conduction": {"triage_verify_linked_merge_provenance": linked},
            }))
        run.assert_called_once_with(["gh", "issue", "view", "10", "--repo", "o/r", "--json", "state"], timeout=60)
        decision = {"status": "decided", "ok": True, "action": "merge"}
        closed = triage.close_linked_issue(req({
            "dry_run": False,
            "conduction": {
                "triage_read_linked_issue_state": state,
                "triage_decide_triage_action": decision,
            },
        }))
        self.assertEqual(
            (state["status"], state["issue"], closed["status"], closed["mutated"]),
            ("issue_state_read", 10, "already_closed", False),
        )

    def test_merge_preconditions_reject_failed_comment_verification(self) -> None:
        result = triage.read_merge_preconditions(req({
            "repo": "o/r",
            "number": 5,
            "head_oid": "abc",
            "conduction": {
                "triage_verify_pr_assignee": {"status": "assignee_verified", "ok": True},
                "triage_verify_pr_comment": {
                    "status": "failed",
                    "ok": False,
                    "reason": "comment_verify_failed",
                },
            },
        }))
        self.assertEqual((result["status"], result["reason"]), ("failed", "upstream_failed"))


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
        comment_hit = triage.evaluate_test_evidence(req({
            "pr": {"body": "", "comments": [{"body": "Test evidence: python -m unittest"}]},
            "require_test_evidence": True,
        }))
        self.assertTrue(comment_hit["pass_"])
        commit_hit = triage.evaluate_test_evidence(req({
            "pr": {
                "body": "Closes #3650.\n\nAutomated fix via lokay.",
                "comments": [{"body": "You have reached your Codex usage limits for code reviews."}],
                "commits": [{
                    "messageHeadline": "#3650 preserve client job route id",
                    "messageBody": (
                        "Use the route run_id for client job status polling.\n\n"
                        "Evidence: uv run pytest tests/test_fala_pilot_web.py -q"
                    ),
                }],
            },
            "require_test_evidence": True,
        }))
        self.assertTrue(commit_hit["pass_"])
        self.assertIn("pytest", commit_hit["hits"])
        automated_comment = triage.evaluate_test_evidence(req({
            "pr": {"body": "", "comments": [{
                "body": "Please add test evidence or address blockers.\n\n<!-- lokay:o/r:5:triage -->",
            }]},
            "require_test_evidence": True,
        }))
        self.assertFalse(automated_comment["pass_"])

    def test_decide_triage_action_routes(self) -> None:
        base = {"pr": {"state": "OPEN", "mergeable": "MERGEABLE", "reviewDecision": "APPROVED", "labels": [], "author": {"login": "o"}, "headRefName": "ai/fix/1", "baseRefName": "main"}, "checks_pass": True, "evidence_pass": True, "automerge": True, "repo": "o/r"}
        self.assertEqual(triage.decide_triage_action(req(base))["action"], "merge")
        self.assertEqual(triage.decide_triage_action(req({**base, "checks_pass": False}))["action"], "repair")
        conducted = {
            "conduction": {
                "triage_load_pr_fields": {
                    "repo": "o/r",
                    "number": 5,
                    "pr": {
                        **base["pr"],
                        "headRefName": "ai/fix/5",
                        "baseRefName": "main",
                    },
                },
                "triage_evaluate_checks": {"pass_": True},
                "triage_evaluate_test_evidence": {"pass_": True},
            },
            "automerge": True,
        }
        merge = triage.decide_triage_action(req(conducted))
        self.assertEqual((merge["repo"], merge["number"]), ("o/r", 5))
        self.assertEqual(triage.decide_triage_action(req({**base, "evidence_pass": False}))["action"], "repair")
        approval_required = {**base, "require_human_approval": True}
        approval_required["pr"] = {**base["pr"], "reviewDecision": ""}
        self.assertEqual(triage.decide_triage_action(req(approval_required))["action"], "comment_block")

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

    def test_read_worktree_ownership_already_absent_when_missing(self) -> None:
        with mock.patch("lokay.steps.cleanup.worktree_list", return_value=""), mock.patch(
            "lokay.steps.cleanup.parse_worktree_porcelain", return_value=[]
        ):
            out = cleanup.read_worktree_ownership(req({
                "clone_path": "/c",
                "worktree_path": "/wt",
                "branch": "ai/fix/3-x",
                "conduction": {
                    "verify_cleanup_guards": {"ok": True, "status": "verified"},
                    "validate_cleanup_identity": {
                        "ok": True,
                        "status": "validated",
                        "identity": {
                            "clone_path": "/c",
                            "worktree_path": "/wt",
                            "local_branch": "ai/fix/3-x",
                            "branch": "ai/fix/3-x",
                        },
                    },
                },
            }))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "already_absent")
        self.assertTrue(out.get("absent"))
        self.assertEqual(out["matches"], [])

    def test_read_worktree_ownership_mismatch_on_ambiguous_match(self) -> None:
        rows = [
            {"path": "/wt", "branch": "ai/fix/3-x"},
            {"path": "/wt", "branch": "ai/fix/3-x"},
        ]
        with mock.patch("lokay.steps.cleanup.worktree_list", return_value=""), mock.patch(
            "lokay.steps.cleanup.parse_worktree_porcelain", return_value=rows
        ):
            out = cleanup.read_worktree_ownership(req({
                "clone_path": "/c",
                "worktree_path": "/wt",
                "branch": "ai/fix/3-x",
                "conduction": {
                    "verify_cleanup_guards": {"ok": True, "status": "verified"},
                    "validate_cleanup_identity": {
                        "ok": True,
                        "status": "validated",
                        "identity": {
                            "clone_path": "/c",
                            "worktree_path": "/wt",
                            "local_branch": "ai/fix/3-x",
                            "branch": "ai/fix/3-x",
                        },
                    },
                },
            }))
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["reason"], "worktree_ownership_mismatch")

    def test_absent_worktree_chain_allows_claim_release_evidence(self) -> None:
        ownership = {
            "ok": True,
            "status": "already_absent",
            "clone_path": "/c",
            "worktree_path": "/wt",
            "branch": "ai/fix/3-x",
            "absent": True,
            "matches": [],
            "mutated": False,
        }
        clean = cleanup.read_worktree_cleanliness(req({
            "clone_path": "/c",
            "worktree_path": "/wt",
            "conduction": {"read_worktree_ownership": ownership},
        }))
        self.assertEqual(clean["status"], "already_absent")
        self.assertTrue(clean["clean"])
        removed = cleanup.remove_worktree(req({
            "clone_path": "/c",
            "worktree_path": "/wt",
            "conduction": {
                "verify_cleanup_guards": {"ok": True, "status": "verified"},
                "read_worktree_ownership": ownership,
                "read_worktree_cleanliness": clean,
            },
        }))
        self.assertEqual(removed["status"], "already_absent")
        self.assertFalse(removed.get("mutated"))
        deleted = cleanup.delete_local_branch(req({
            "clone_path": "/c",
            "branch": "ai/fix/3-x",
            "conduction": {
                "verify_branch_delete_guards": {"ok": True, "status": "verified"},
                "read_local_branch_ownership": {
                    "ok": True,
                    "status": "read",
                    "clone_path": "/c",
                    "branch": "ai/fix/3-x",
                    "exists": False,
                    "owned": True,
                },
            },
        }))
        self.assertEqual(deleted["status"], "already_absent")
        evidence = cleanup.verify_claim_release_evidence(req({
            "conduction": {
                "verify_cleanup_guards": {"ok": True, "status": "verified"},
                "verify_local_branch_absent": {"ok": True, "status": "verified", "absent": True},
                "verify_worktree_absent": {"ok": True, "status": "verified", "absent": True},
                "remove_worktree": removed,
                "delete_local_branch": deleted,
                "check_issue_closed": {"ok": True, "status": "checked", "closed": True},
                "check_no_open_pr_for_branch": {"ok": True, "status": "checked", "safe_to_cleanup": True},
            },
        }))
        self.assertTrue(evidence["ok"], evidence)
        self.assertEqual(evidence["status"], "verified")

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
