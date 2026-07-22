"""Hermetic issue direction gate: reject comments, no claim/kanban; accept claims."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from repo_agent.steps.claim import claim_github_issue
from repo_agent.steps.issue_direction import comment_issue_once, decide_issue_action
from repo_agent.steps.kanban_intake import ensure_kanban_intake


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
        decide = decide_issue_action(
            req(
                {
                    "conduction": {"poll": poll},
                    "repo_goal": "automate GitHub issue PR merge lifecycle for hermes repo-agent",
                    "dry_run": False,
                }
            )
        )
        self.assertEqual(decide["action"], "reject_comment")
        self.assertEqual(decide["reason"], "out_of_direction_goal")

        with mock.patch(
            "repo_agent.steps.issue_direction.run_cmd",
            side_effect=[
                SimpleNamespace(stdout=json.dumps({"comments": []}), stderr="", returncode=0),
                SimpleNamespace(stdout="", stderr="", returncode=0),
            ],
        ) as run_cmd:
            comment = comment_issue_once(
                req(
                    {
                        "dry_run": False,
                        "conduction": {
                            "poll": poll,
                            "decide_issue_action": decide,
                        },
                    },
                    config={"gh_cli": "gh"},
                )
            )
        self.assertEqual(comment["status"], "commented")
        self.assertTrue(comment["mutated"])
        cmds = [list(c.args[0]) for c in run_cmd.call_args_list]
        self.assertTrue(any(c[:2] == ["gh", "issue"] and "comment" in c for c in cmds))

        claim = claim_github_issue(
            req(
                {
                    "dry_run": False,
                    "conduction": {
                        "poll": poll,
                        "decide_issue_action": decide,
                    },
                },
                config={"assignee": "owner"},
            )
        )
        self.assertEqual(claim["status"], "noop")
        self.assertIn("out_of_direction", claim.get("reason", ""))

        kanban = ensure_kanban_intake(
            req(
                {
                    "dry_run": False,
                    "conduction": {
                        "poll": poll,
                        "decide_issue_action": decide,
                        "claim": claim,
                    },
                },
                config={"kanban_intake_assignee": "repo-agent-intake"},
            )
        )
        self.assertEqual(kanban["status"], "noop")

    def test_accept_aligned_issue_claims(self) -> None:
        poll = self._poll(
            title="Harden issue PR merge lifecycle receipts",
            body="Hermes repo-agent automation for GitHub issue merge",
        )
        decide = decide_issue_action(
            req(
                {
                    "conduction": {"poll": poll},
                    "repo_goal": "automate GitHub issue PR merge lifecycle for hermes repo-agent",
                    "dry_run": False,
                }
            )
        )
        self.assertEqual(decide["action"], "accept")
        self.assertEqual(decide["reason"], "goal_aligned")

        comment = comment_issue_once(
            req(
                {
                    "dry_run": False,
                    "conduction": {"poll": poll, "decide_issue_action": decide},
                }
            )
        )
        self.assertEqual(comment["status"], "noop")

        claim = claim_github_issue(
            req(
                {
                    "dry_run": True,
                    "conduction": {"poll": poll, "decide_issue_action": decide},
                },
                config={"assignee": "owner"},
            )
        )
        self.assertEqual(claim["status"], "planned")
        self.assertEqual(claim["selected"]["number"], 99)

    def test_reject_label_is_durable_not_silent(self) -> None:
        poll = self._poll(labels=["ai:ready", "wontfix"], title="Anything")
        decide = decide_issue_action(
            req({"conduction": {"poll": poll}, "dry_run": True})
        )
        self.assertEqual(decide["action"], "reject_comment")
        planned = comment_issue_once(
            req(
                {
                    "dry_run": True,
                    "conduction": {"poll": poll, "decide_issue_action": decide},
                }
            )
        )
        self.assertEqual(planned["status"], "planned")
        self.assertIn("repo-agent:owner/repo:99:issue-direction", planned["comment_marker"])


if __name__ == "__main__":
    unittest.main()
