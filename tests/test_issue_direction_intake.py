"""Hermetic issue direction gate: reject comments, no claim/kanban; accept claims."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from lokay.steps import claim, issue_direction, kanban_intake


def req(input_data=None, config=None):
    return {
        "input": input_data or {},
        "config": config or {},
        "process_id": "p1",
        "impulse_id": None,
        "work_dir": None,
        "adapter": None,
    }


class IssueDirectionIntakeTests(unittest.TestCase):
    def _poll(self, **issue_overrides):
        selected = {
            "repo": "owner/repo",
            "board": "board",
            "number": 99,
            "title": "Add a marketing landing page",
            "body": "CSS polish only",
            "url": "https://example.invalid/issues/99",
            "labels": ["ai:ready"],
            "assignees": ["owner"],
        }
        selected.update(issue_overrides)
        return {
            "status": "polled",
            "selected": selected,
            "eligible_count": 1,
            "dry_run": False,
        }

    def test_reject_out_of_direction_posts_comment_and_skips_claim(self) -> None:
        poll = self._poll()
        decide = issue_direction.decide_issue_action(
            req({"selected": poll["selected"], "conduction": {"select_issue_candidate": poll}, "repo_goal": "automate GitHub issue PR merge lifecycle for hermes lokay", "dry_run": False})
        )
        self.assertEqual(decide["action"], "reject_comment")
        self.assertEqual(decide["reason"], "out_of_direction_goal")

        read = {"status": "comments_read", "ok": True, "comments": [], "selected": poll["selected"], "repo": "owner/repo", "number": 99, "comment_marker": "lokay:owner/repo:99:issue-direction"}
        comment_decision = issue_direction.decide_issue_comment(req({"conduction": {"read_issue_comments": read}, "dry_run": False}))
        with mock.patch("lokay.steps.issue_direction.run_cmd", return_value=SimpleNamespace(stdout="", stderr="", returncode=0)) as run_cmd:
            posted = issue_direction.post_issue_comment(req({"dry_run": False, "conduction": {"read_issue_comments": read, "decide_issue_comment": comment_decision}, "reason": decide["reason"], "comment_marker": read["comment_marker"]}, {"gh_cli": "gh"}))
        self.assertEqual(posted["status"], "comment_posted")
        self.assertTrue(posted["mutated"])
        self.assertTrue(any("comment" in list(c.args[0]) for c in run_cmd.call_args_list))
        verified = issue_direction.verify_issue_comment(req({"dry_run": True, "conduction": {"post_issue_comment": posted}}))
        self.assertEqual(verified["status"], "comment_verified")

        reserve = claim.reserve_claim_file(req({"dry_run": False, "conduction": {"select_issue_candidate": poll, "decide_issue_action": decide}} , {"active_issue_path": "/tmp/no-claim", "assignee": "owner"}))
        self.assertEqual(reserve["status"], "noop")
        self.assertIn("no_selected_issue", reserve.get("reason", ""))

        intake = kanban_intake.read_intake_tasks(req({"conduction": {"build_issue_claim_result": reserve}, "selected": poll["selected"]}))
        self.assertEqual(intake["reason"], "no_selected_issue")

    def test_accept_aligned_issue_claims(self) -> None:
        poll = self._poll(title="Harden issue PR merge lifecycle receipts", body="Hermes lokay automation for GitHub issue merge")
        decide = issue_direction.decide_issue_action(req({"selected": poll["selected"], "conduction": {"select_issue_candidate": poll}, "repo_goal": "automate GitHub issue PR merge lifecycle for hermes lokay", "dry_run": False}))
        self.assertEqual(decide["action"], "accept")
        read = {"status": "comments_read", "ok": True, "comments": [], "selected": poll["selected"], "repo": "owner/repo", "number": 99, "comment_marker": "lokay:owner/repo:99:issue-direction"}
        comment_decision = issue_direction.decide_issue_comment(req({"conduction": {"read_issue_comments": read}, "dry_run": False}))
        self.assertTrue(comment_decision["should_post"])
        posted = issue_direction.post_issue_comment(req({"dry_run": False, "conduction": {"read_issue_comments": read, "decide_issue_comment": {**comment_decision, "should_post": False}}}))
        self.assertEqual(posted["status"], "noop")
        planned = claim.reserve_claim_file(req({"dry_run": True, "conduction": {"select_issue_candidate": poll, "decide_issue_action": decide}}, {"assignee": "owner"}))
        self.assertEqual(planned["status"], "planned")
        self.assertEqual(planned["selected"]["number"], 99)

    def test_reject_label_is_durable_not_silent(self) -> None:
        poll = self._poll(labels=["ai:ready", "wontfix"], title="Anything")
        decide = issue_direction.decide_issue_action(req({"selected": poll["selected"], "conduction": {"select_issue_candidate": poll}, "dry_run": True}))
        self.assertEqual(decide["action"], "reject_comment")
        planned = issue_direction.post_issue_comment(req({"dry_run": True, "conduction": {"read_issue_comments": {"status": "comments_read", "comments": [], "selected": poll["selected"], "repo": "owner/repo", "number": 99, "comment_marker": "lokay:owner/repo:99:issue-direction"}, "decide_issue_comment": {"status": "comment_decided", "should_post": True, "comment_marker": "lokay:owner/repo:99:issue-direction"}}}))
        self.assertEqual(planned["status"], "planned")
        self.assertIn("lokay:owner/repo:99:issue-direction", planned["comment_marker"])


if __name__ == "__main__":
    unittest.main()
