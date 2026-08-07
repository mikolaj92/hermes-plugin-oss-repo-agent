"""Pure decide matrix: issue accept/reject and PR merge/comment/repair/skip.

Drives the real shipped decide functions (not reimplemented stubs).
"""

from __future__ import annotations

from types import SimpleNamespace

import json
import unittest
from unittest import mock

from lokay.steps import issue_direction, triage
from lokay.process_runtime import payload_digest


def durable_merge_request(*, action="merge", path_id="pr_merge", repo="owner/repo", number=42, head_oid="head-42"):
    subject = {"repo": repo, "number": number, "head_oid": head_oid}
    decision = {"status": "decided", "ok": True, "action": action, **subject}
    identity = {
        "schema_version": 1,
        "process_id": "pr_triage",
        "candidate_id": "candidate-42",
        "config_sha256": "config-42",
        "generation": "generation-42",
        "correlation_id": payload_digest({"process_id": "pr_triage", "kind": "pr_decision", "subject": payload_digest(subject)}),
        "subject": subject,
        "predecessor_digests": [],
        "operation": "publish",
        "source": "lokay.process_runtime",
        "mutation_status": "mutated",
        "payload": decision,
    }
    digest = payload_digest(identity)
    body = {**identity, "content_digest": digest, "verified_readback_state": "verified"}
    return req({
        "path_id": path_id,
        "repo": repo,
        "number": number,
        "head_oid": head_oid,
        "pr_decision": digest,
        "generation": identity["generation"],
        "candidate_id": identity["candidate_id"],
        "config_sha256": identity["config_sha256"],
        "predecessor_evidence": {
            "groups": [["pr_decision"]],
            "required_inputs": ["pr_decision"],
            "subject": subject,
            "receipts": {"pr_decision": {
                "process_id": "pr_triage",
                "receipt_kind": "pr_decision",
                "digest": digest,
                "payload": body,
            }},
        },
    })


def req(input_data=None, config=None):
    return {"input": input_data or {}, "config": config or {}}


class IssueDecideMatrixTests(unittest.TestCase):
    def _selected(self, **overrides):
        base = {
            "repo": "owner/repo",
            "number": 42,
            "title": "Fix dispatcher timeout handling",
            "body": "Make the OMP worker timeout kill the process group.",
            "labels": ["ai:ready"],
            "assignees": ["owner"],
        }
        base.update(overrides)
        return base

    def test_accept_when_direction_not_configured(self) -> None:
        out = issue_direction.decide_issue_action(
            req({"selected": self._selected()})
        )
        self.assertEqual(out["action"], "accept")
        self.assertEqual(out["reason"], "direction_not_configured")

    def test_reject_out_of_scope_label(self) -> None:
        out = issue_direction.decide_issue_action(
            req({"selected": self._selected(labels=["ai:ready", "ai:out-of-scope"])})
        )
        self.assertEqual(out["action"], "reject_comment")
        self.assertEqual(out["reason"], "out_of_direction_label")

    def test_reject_empty_title(self) -> None:
        out = issue_direction.decide_issue_action(
            req({"selected": self._selected(title="   ", body="Has body text")})
        )
        self.assertEqual(out["action"], "reject_comment")
        self.assertEqual(out["reason"], "empty_title")

    def test_empty_title_precedes_deny_keyword(self) -> None:
        out = issue_direction.decide_issue_action(
            req(
                {
                    "selected": self._selected(title="", body="Please redesign everything"),
                    "direction_deny_keywords": ["redesign"],
                }
            )
        )
        self.assertEqual(out["action"], "reject_comment")
        self.assertEqual(out["reason"], "empty_title")

    def test_reject_deny_keyword(self) -> None:
        out = issue_direction.decide_issue_action(
            req(
                {
                    "selected": self._selected(title="Please redesign the whole UI"),
                    "direction_deny_keywords": ["redesign", "rewrite-all"],
                }
            )
        )
        self.assertEqual(out["action"], "reject_comment")
        self.assertEqual(out["reason"], "deny_keyword")
        self.assertEqual(out["keyword"], "redesign")

    def test_reject_missing_require_keyword(self) -> None:
        out = issue_direction.decide_issue_action(
            req(
                {
                    "selected": self._selected(
                        title="Random docs typo",
                        body="No automation keywords here",
                    ),
                    "direction_require_keywords": ["dispatcher", "omp"],
                }
            )
        )
        self.assertEqual(out["action"], "reject_comment")
        self.assertEqual(out["reason"], "missing_require_keyword")

    def test_accept_require_keyword_hit(self) -> None:
        out = issue_direction.decide_issue_action(
            req(
                {
                    "selected": self._selected(title="Harden OMP dispatcher recovery"),
                    "direction_require_keywords": ["dispatcher", "omp"],
                }
            )
        )
        self.assertEqual(out["action"], "accept")
        self.assertIn(out["reason"], {"direction_ok", "goal_aligned"})

    def test_reject_goal_no_overlap(self) -> None:
        out = issue_direction.decide_issue_action(
            req(
                {
                    "selected": self._selected(
                        title="Add a marketing landing page",
                        body="CSS polish only",
                    ),
                    "repo_goal": "automate GitHub issue to PR merge lifecycle for hermes lokay",
                }
            )
        )
        self.assertEqual(out["action"], "reject_comment")
        self.assertEqual(out["reason"], "out_of_direction_goal")

    def test_accept_goal_overlap(self) -> None:
        out = issue_direction.decide_issue_action(
            req(
                {
                    "selected": self._selected(
                        title="Issue triage should comment on out-of-direction work",
                        body="Hermes lokay PR merge lifecycle",
                    ),
                    "repo_goal": "automate GitHub issue PR merge lifecycle for hermes lokay",
                }
            )
        )
        self.assertEqual(out["action"], "accept")
        self.assertEqual(out["reason"], "goal_aligned")
        self.assertTrue(out.get("overlap"))

    def test_noop_without_selected(self) -> None:
        out = issue_direction.decide_issue_action(req({}))
        self.assertEqual(out["status"], "noop")
        self.assertEqual(out["action"], "skip")


class PrDecideMatrixTests(unittest.TestCase):
    def _pr(self, **overrides):
        base = {
            "number": 42,
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "headRefName": "ai/fix/42-timeout",
            "headRefOid": "head-42",
            "baseRefName": "main",
            "isDraft": False,
            "author": {"login": "owner"},
            "reviewDecision": "APPROVED",
            "labels": [{"name": "ai:generated"}, {"name": "ai:pr-opened"}],
        }
        base.update(overrides)
        return base

    def test_merge_when_green_automerge(self) -> None:
        out = triage.decide_triage_action(
            req(
                {
                    "pr": self._pr(),
                    "repo": "owner/repo",
                    "checks_pass": True,
                    "evidence_pass": True,
                    "automerge": True,
                }
            )
        )
        self.assertEqual(out["action"], "merge")
        self.assertEqual(out["reason"], "ready")
    def test_failed_check_upstream_cannot_route_repair(self) -> None:
        out = triage.decide_triage_action(
            req(
                {
                    "pr": self._pr(),
                    "checks_pass": False,
                    "evidence_pass": True,
                    "conduction": {
                        "evaluate_checks": {"status": "failed", "ok": False, "reason": "read_failed"},
                    },
                }
            )
        )
        self.assertEqual(out["reason"], "upstream_failed")
        self.assertFalse(out["mutated"])

    def test_standalone_merge_uses_durable_predecessor(self) -> None:
        request = durable_merge_request()
        request["input"]["dry_run"] = True
        request["input"]["conduction"] = {
            "read_merge_preconditions": {
                "status": "merge_preconditions_read",
                "ok": True,
                "repo": "owner/repo",
                "number": 42,
                "head_oid": "head-42",
            }
        }
        out = triage.merge_pr(request)
        self.assertEqual(
            (out["status"], out["repo"], out["number"], out["head_oid"]),
            ("planned", "owner/repo", 42, "head-42"),
        )
    def test_foreign_triage_path_cannot_mutate_with_durable_merge_evidence(self) -> None:
        request = durable_merge_request(path_id="pr_triage")
        request["input"]["dry_run"] = False
        request["input"]["conduction"] = {
            "decide_triage_action": {
                "status": "decided",
                "ok": True,
                "action": "merge",
                "repo": "owner/repo",
                "number": 42,
                "head_oid": "head-42",
            },
            "read_merge_preconditions": {
                "status": "merge_preconditions_read",
                "ok": True,
                "repo": "owner/repo",
                "number": 42,
                "head_oid": "head-42",
            },
        }
        with mock.patch("lokay.steps.triage.run_cmd") as run:
            out = triage.merge_pr(request)
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["reason"], "decision_gate_identity_mismatch")
        self.assertFalse(out["mutated"])
        run.assert_not_called()
    def test_merge_requires_verified_predecessor_evidence(self) -> None:
        request = durable_merge_request()
        request["input"].pop("predecessor_evidence")
        request["input"]["dry_run"] = False
        with mock.patch("lokay.steps.triage.run_cmd") as run:
            out = triage.merge_pr(request)
        self.assertEqual((out["status"], out["reason"]), ("failed", "merge_predecessor_missing"))
        self.assertFalse(out["mutated"])
        run.assert_not_called()

    def test_merge_rejects_predecessor_digest_mismatch(self) -> None:
        request = durable_merge_request()
        request["input"]["pr_decision"] = "f" * 64
        request["input"]["dry_run"] = False
        with mock.patch("lokay.steps.triage.run_cmd") as run:
            out = triage.merge_pr(request)
        self.assertEqual((out["status"], out["reason"]), ("failed", "merge_predecessor_digest_mismatch"))
        self.assertFalse(out["mutated"])
        run.assert_not_called()
    def test_merge_rejects_self_consistent_forged_receipt_digest(self) -> None:
        request = durable_merge_request()
        forged_digest = "a" * 64
        request["input"]["pr_decision"] = forged_digest
        receipt = request["input"]["predecessor_evidence"]["receipts"]["pr_decision"]
        receipt["digest"] = forged_digest
        receipt["payload"]["content_digest"] = forged_digest
        request["input"]["dry_run"] = False
        with mock.patch("lokay.steps.triage.run_cmd") as run:
            out = triage.merge_pr(request)
        self.assertEqual((out["status"], out["reason"]), ("failed", "merge_predecessor_digest_mismatch"))
        self.assertFalse(out["mutated"])
        run.assert_not_called()

    def test_merge_non_merge_durable_action_is_noop(self) -> None:
        request = durable_merge_request(action="comment_block")
        request["input"]["dry_run"] = False
        with mock.patch("lokay.steps.triage.run_cmd") as run:
            out = triage.merge_pr(request)
        self.assertEqual((out["status"], out["reason"], out["action"]), ("noop", "not_selected", "comment_block"))
        self.assertFalse(out["mutated"])
        run.assert_not_called()

    def test_merge_preconditions_reject_live_head_drift(self) -> None:
        request = durable_merge_request()
        request["input"]["dry_run"] = False
        with mock.patch("lokay.steps.triage._read_merge_view", return_value={"state": "OPEN", "headRefOid": "head-drift"}):
            out = triage.read_merge_preconditions(request)
        self.assertEqual((out["status"], out["reason"]), ("failed", "merge_head_mismatch"))
        self.assertEqual(out["head_oid"], "head-42")


    def test_direct_merge_action_cannot_bypass_decision_gate(self) -> None:
        out = triage.merge_pr(
            req({"action": "merge", "repo": "owner/repo", "number": 42, "dry_run": True})
        )
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["reason"], "decision_gate_identity_missing")
        self.assertFalse(out["mutated"])

    def test_string_false_evidence_cannot_merge(self) -> None:
        out = triage.decide_triage_action(
            req({"pr": self._pr(), "checks_pass": True, "evidence_pass": "false", "automerge": True})
        )
        self.assertEqual(out["reason"], "invalid_decision_input")
        self.assertFalse(out["mutated"])
    def test_approval_and_mergeability_matrix(self) -> None:
        cases = (
            (True, "APPROVED", "MERGEABLE", "merge"),
            (True, None, "MERGEABLE", "comment_block"),
            (True, "CHANGES_REQUESTED", "MERGEABLE", "comment_block"),
            (True, "APPROVED", "UNKNOWN", "skip"),
            (True, "APPROVED", "CONFLICTING", "repair"),
            (False, "APPROVED", "MERGEABLE", "merge"),
            (False, None, "MERGEABLE", "merge"),
            (False, "CHANGES_REQUESTED", "MERGEABLE", "merge"),
            (False, None, "UNKNOWN", "skip"),
            (False, None, "CONFLICTING", "repair"),
        )
        for require_approval, review, mergeable, action in cases:
            with self.subTest(
                require_approval=require_approval,
                review=review,
                mergeable=mergeable,
            ):
                out = triage.decide_triage_action(
                    req(
                        {
                            "pr": self._pr(
                                reviewDecision=review,
                                mergeable=mergeable,
                            ),
                            "repo": "owner/repo",
                            "checks_pass": True,
                            "evidence_pass": True,
                            "automerge": True,
                            "require_human_approval": require_approval,
                        }
                    )
                )
                self.assertEqual(out["action"], action, out)

    def test_malformed_required_approval_blocks_merge(self) -> None:
        out = triage.decide_triage_action(
            req(
                {
                    "pr": self._pr(reviewDecision={"state": "APPROVED"}),
                    "repo": "owner/repo",
                    "checks_pass": True,
                    "evidence_pass": True,
                    "automerge": True,
                    "require_human_approval": True,
                }
            )
        )
        self.assertEqual(out["action"], "comment_block")
        self.assertEqual(out["reason"], "approval_required")

    def test_repair_when_checks_fail(self) -> None:
        out = triage.decide_triage_action(
            req(
                {
                    "pr": self._pr(),
                    "repo": "owner/repo",
                    "checks_pass": False,
                    "evidence_pass": True,
                    "automerge": True,
                }
            )
        )
        self.assertEqual(out["action"], "repair")
        self.assertEqual(out["reason"], "checks_not_green")

    def test_repair_when_test_evidence_is_missing(self) -> None:
        out = triage.decide_triage_action(
            req(
                {
                    "pr": self._pr(),
                    "repo": "owner/repo",
                    "checks_pass": True,
                    "evidence_pass": False,
                    "automerge": True,
                }
            )
        )
        self.assertEqual(out["action"], "repair")
        self.assertEqual(out["reason"], "missing_test_evidence")

    def test_repair_on_conflict(self) -> None:
        out = triage.decide_triage_action(
            req(
                {
                    "pr": self._pr(mergeable="CONFLICTING"),
                    "repo": "owner/repo",
                    "checks_pass": True,
                    "evidence_pass": True,
                    "automerge": True,
                }
            )
        )
        self.assertEqual(out["action"], "repair")
        self.assertEqual(out["reason"], "merge_conflict")

    def test_skip_external_author(self) -> None:
        out = triage.decide_triage_action(
            req(
                {
                    "pr": self._pr(author={"login": "contributor"}),
                    "repo": "owner/repo",
                    "checks_pass": True,
                    "evidence_pass": True,
                    "automerge": True,
                }
            )
        )
        self.assertEqual(out["action"], "skip")
        self.assertEqual(out["reason"], "external_author")

    def test_skip_non_ai_fix_branch(self) -> None:
        out = triage.decide_triage_action(
            req(
                {
                    "pr": self._pr(headRefName="feature/manual"),
                    "repo": "owner/repo",
                    "checks_pass": True,
                    "evidence_pass": True,
                    "automerge": True,
                }
            )
        )
        self.assertEqual(out["action"], "skip")
        self.assertEqual(out["reason"], "non_ai_fix_branch")

    def test_skip_wrong_base(self) -> None:
        out = triage.decide_triage_action(
            req(
                {
                    "pr": self._pr(baseRefName="develop"),
                    "repo": "owner/repo",
                    "checks_pass": True,
                    "evidence_pass": True,
                    "automerge": True,
                }
            )
        )
        self.assertEqual(out["action"], "skip")
        self.assertEqual(out["reason"], "wrong_base")

    def test_skip_draft(self) -> None:
        out = triage.decide_triage_action(
            req(
                {
                    "pr": self._pr(isDraft=True),
                    "repo": "owner/repo",
                    "checks_pass": True,
                    "evidence_pass": True,
                    "automerge": True,
                }
            )
        )
        self.assertEqual(out["action"], "skip")
        self.assertEqual(out["reason"], "draft_pr")

    def test_comment_block_when_automerge_disabled(self) -> None:
        out = triage.decide_triage_action(
            req(
                {
                    "pr": self._pr(),
                    "repo": "owner/repo",
                    "checks_pass": True,
                    "evidence_pass": True,
                    "automerge": False,
                }
            )
        )
        self.assertEqual(out["action"], "comment_block")
        self.assertEqual(out["reason"], "automerge_disabled")

    def test_skip_ai_blocked_label(self) -> None:
        out = triage.decide_triage_action(
            req(
                {
                    "pr": self._pr(labels=[{"name": "ai:blocked"}]),
                    "repo": "owner/repo",
                    "checks_pass": True,
                    "evidence_pass": True,
                    "automerge": True,
                }
            )
        )
        self.assertEqual(out["action"], "skip")
        self.assertEqual(out["reason"], "ai_blocked_label")

    def test_eligibility_precedes_failing_readiness(self) -> None:
        cases = (
            ({"isDraft": True}, "draft_pr"),
            ({"labels": [{"name": "AI:BLOCKED"}]}, "ai_blocked_label"),
            ({"author": {"login": "contributor"}}, "external_author"),
            ({"headRefName": "feature/manual"}, "non_ai_fix_branch"),
            ({"baseRefName": "develop"}, "wrong_base"),
            ({"state": ""}, "missing_state"),
            ({"headRefName": ""}, "non_ai_fix_branch"),
            ({"baseRefName": ""}, "wrong_base"),
        )
        for pr_overrides, reason in cases:
            with self.subTest(reason=reason, pr=pr_overrides):
                out = triage.decide_triage_action(
                    req(
                        {
                            "pr": self._pr(**pr_overrides),
                            "repo": "owner/repo",
                            "checks_pass": False,
                            "evidence_pass": False,
                            "automerge": True,
                        }
                    )
                )
                self.assertEqual(out["action"], "skip", out)
                self.assertEqual(out["reason"], reason, out)

    def test_load_pr_fields_requests_is_draft_and_decide_skips_draft(self) -> None:
        """Regression: draft gate must use isDraft from load_pr_fields, not inject it."""
        gh_pr = {
            "number": 8,
            "title": "WIP fix",
            "url": "https://example.invalid/pr/8",
            "body": "Test plan: pytest",
            "state": "OPEN",
            "isDraft": True,
            "headRefName": "ai/fix/8-wip",
            "headRefOid": "abc123",
            "baseRefName": "main",
            "author": {"login": "owner"},
            "labels": [{"name": "ai:generated"}, {"name": "ai:pr-opened"}],
            "mergeable": "MERGEABLE",
            "reviewDecision": None,
            "statusCheckRollup": [],
            "commits": [],
        }
        captured: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            captured.append(list(cmd))
            return SimpleNamespace(stdout=json.dumps(gh_pr), stderr="", returncode=0)

        with mock.patch("lokay.steps.triage.run_cmd", side_effect=fake_run):
            loaded = triage.load_pr_fields(
                req({"repo": "owner/repo", "number": 8, "gh_cli": "input-gh"})
                | {"config": {"gh_cli": "config-gh"}}
            )
        self.assertEqual(loaded["status"], "loaded")
        self.assertTrue(captured, "expected gh pr view call")
        self.assertEqual(captured[0][0], "input-gh")

        fields_arg = ""
        for cmd in captured:
            if "pr" in cmd and "view" in cmd and "--json" in cmd:
                fields_arg = cmd[cmd.index("--json") + 1]
                break
        self.assertIn("isDraft", fields_arg.split(","), fields_arg)
        # Decide only via conduction pr from load — no isDraft in decide input.
        out = triage.decide_triage_action(
            req(
                {
                    "repo": "owner/repo",
                    "checks_pass": True,
                    "evidence_pass": True,
                    "automerge": True,
                    "conduction": {
                        "load_pr_fields": {
                            "status": "loaded",
                            "repo": "owner/repo",
                            "number": 8,
                            "pr": loaded["pr"],
                        }
                    },
                }
            )
        )
        self.assertEqual(out["action"], "skip")
        self.assertEqual(out["reason"], "draft_pr")
        self.assertTrue(loaded["pr"].get("isDraft"))


if __name__ == "__main__":
    unittest.main()
