from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from fala.adapters import EffectorRunRequest, EffectorRunResult
from repo_agent.envelope import fail, noop, ok, planned
from repo_agent.flows import bridges


def request(*, input: dict | None = None, config: dict | None = None) -> EffectorRunRequest:
    return EffectorRunRequest(
        process_id="test",
        adapter=None,
        input=input or {},
        config=config or {},
    )


class BridgeTests(unittest.TestCase):
    def test_prepare_uses_current_conduction_id(self) -> None:
        with mock.patch("repo_agent.flows.bridges.issue_to_pr.prepare_worktree", return_value=ok(status="prepared")) as step:
            bridges.prepare_worktree_from_conduction(request(input={"conduction": {"parse_issue_ref": {"branch": "ai/fix/3-test"}}}, config={"clone_path": "/repo"}))
        self.assertEqual(step.call_args.args[0].input["branch"], "ai/fix/3-test")
    def test_bridge_preserves_parent_request_metadata(self) -> None:
        work_dir = __import__("pathlib").Path("/tmp/bridge-work")
        parent = EffectorRunRequest(
            process_id="process-7",
            impulse_id="impulse-8",
            adapter=None,
            work_dir=work_dir,
            input={"conduction": {"parse_issue_ref": {"branch": "ai/fix/3-test"}}},
            config={"clone_path": "/repo"},
        )
        with mock.patch(
            "repo_agent.flows.bridges.issue_to_pr.prepare_worktree",
            return_value=ok(status="prepared"),
        ) as step:
            bridges.prepare_worktree_from_conduction(parent)
        delegated = step.call_args.args[0]
        self.assertEqual(delegated.process_id, "process-7")
        self.assertEqual(delegated.impulse_id, "impulse-8")
        self.assertEqual(delegated.work_dir, work_dir)
        self.assertIsNone(delegated.adapter)
    def test_verify_bridge_preserves_dry_run(self) -> None:
        parent = request(
            input={
                "dry_run": True,
                "conduction": {
                    "prepare_worktree": {"worktree_path": "/planned/worktree"},
                },
            },
            config={"clone_path": "/planned/clone"},
        )
        with mock.patch(
            "repo_agent.flows.bridges.issue_to_pr.verify_branch_has_commits",
            return_value=__import__("repo_agent.envelope", fromlist=["planned"]).planned(),
        ) as verifier:
            result = bridges.verify_commits_from_conduction(parent)
        self.assertEqual(result.output["status"], "planned")
        delegated = verifier.call_args.args[0]
        self.assertTrue(delegated.input["dry_run"])

    def test_triage_parse_failure_stops_merge(self) -> None:
        with mock.patch("repo_agent.flows.bridges.cleanup.parse_issue_from_branch", return_value=fail("bad_branch")), mock.patch(
            "repo_agent.flows.bridges.triage.claim_pr_assignee"
        ) as claim:
            result = bridges.apply_triage_decision(request(input={"conduction": {"load_pr_fields": {"pr": {"number": 4, "headRefName": "bad"}}, "decide_triage_action": {"action": "merge"}}}))
        self.assertEqual(result.output["status"], "failed")
        claim.assert_not_called()

    def test_merge_noop_stops_receipt(self) -> None:
        with mock.patch("repo_agent.flows.bridges.cleanup.parse_issue_from_branch", return_value=ok(status="parsed", issue=3)), mock.patch(
            "repo_agent.flows.bridges.triage.claim_pr_assignee", return_value=ok(status="claimed", mutated=True)
        ), mock.patch("repo_agent.flows.bridges.triage.merge_pull_request", return_value=noop("already_merged")), mock.patch(
            "repo_agent.flows.bridges.triage.write_merge_receipt"
        ) as receipt:
            result = bridges.apply_triage_decision(request(input={"conduction": {"load_pr_fields": {"pr": {"number": 4, "headRefName": "ai/fix/3-test"}}, "decide_triage_action": {"action": "merge"}}}))
        self.assertEqual(result.output["status"], "noop")
        self.assertTrue(result.output["mutated"])
        receipt.assert_not_called()

    def test_cleanup_no_branch_is_controlled_noop(self) -> None:
        with mock.patch("repo_agent.flows.bridges.cleanup.parse_issue_from_branch") as parse:
            result = bridges.cleanup_candidate_from_input(request(input={"repo": "o/r"}))
        self.assertEqual(result.output["status"], "skipped")
        self.assertEqual(result.output["reason"], "no_branch")
        parse.assert_not_called()

    def test_cleanup_delete_noop_stops_release_and_preserves_mutation(self) -> None:
        with mock.patch("repo_agent.flows.bridges.cleanup.parse_issue_from_branch", return_value=ok(status="parsed", issue=3, branch="ai/fix/3-x")), mock.patch(
            "repo_agent.flows.bridges.cleanup.check_issue_closed", return_value=ok(closed=True)
        ), mock.patch("repo_agent.flows.bridges.cleanup.check_no_open_pr_for_branch", return_value=ok(safe_to_cleanup=True)), mock.patch(
            "repo_agent.flows.bridges.cleanup.remove_worktree", return_value=ok(status="removed", mutated=True)
        ), mock.patch("repo_agent.flows.bridges.cleanup.delete_local_fix_branch", return_value=noop("already_absent")), mock.patch(
            "repo_agent.flows.bridges.cleanup.release_active_issue_claim"
        ) as release:
            result = bridges.cleanup_candidate_from_input(request(input={"branch": "ai/fix/3-x", "repo": "o/r", "clone_path": "/repo", "worktree_path": "/wt"}))
        self.assertEqual(result.output["status"], "noop")
        self.assertTrue(result.output["mutated"])
        release.assert_not_called()

    def test_cleanup_list_input_precedence_and_later_failure_keeps_mutation(self) -> None:
        rows = [{"branch": "ai/fix/1-a", "path": "/wt/a"}, {"branch": "ai/fix/2-b", "path": "/wt/b"}]
        first = ok(status="cleaned", mutated=True)
        second = fail("boom")
        with mock.patch("repo_agent.flows.bridges.cleanup_candidate_from_input", side_effect=[first, second]) as child:
            result = bridges.cleanup_all_from_list(request(input={"repo": "input/repo", "clone_path": "/input", "conduction": {"list_worktrees": {"worktrees": rows}}}, config={"repo": "config/repo", "clone_path": "/config"}))
        self.assertEqual(result.output["status"], "failed")
        self.assertTrue(result.output["mutated"])
        self.assertEqual(child.call_args_list[0].args[0].input["repo"], "input/repo")
        self.assertEqual(child.call_args_list[0].args[0].input["clone_path"], "/input")


if __name__ == "__main__":
    unittest.main()
