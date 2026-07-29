from __future__ import annotations

import json
import unittest
import unittest.mock

from lokay.steps import issue_triage


SOURCES = {
    "issue:42": "Fix dropped responses in the local worker.",
    "comment:7": "This is a duplicate of #12.",
    "repository_context:README.md": "Lokay automates issue triage.",
}


def payload(classification: str = "ready", **overrides):
    value = {
        "schema_version": 1,
        "classification": classification,
        "reason": "The request matches the repository goal.",
        "question": "",
        "canonical_issue": 0,
        "evidence": [
            {
                "kind": "issue",
                "identity": "issue:42",
                "quote": "dropped responses",
            }
        ],
    }
    value.update(overrides)
    return value


class ClassificationContractTests(unittest.TestCase):
    def test_accepts_every_classification(self) -> None:
        for classification in ("ready", "ambiguous", "out_of_scope"):
            with self.subTest(classification=classification):
                parsed = issue_triage.parse_classification_output(
                    json.dumps(payload(classification)), sources=SOURCES
                )
                self.assertEqual(parsed["classification"], classification)
        feedback = payload("needs_feedback", question="Which behavior is expected?")
        self.assertEqual(
            issue_triage.parse_classification_output(json.dumps(feedback), sources=SOURCES)["question"],
            feedback["question"],
        )
        duplicate = payload("duplicate", canonical_issue=12)
        self.assertEqual(
            issue_triage.parse_classification_output(json.dumps(duplicate), sources=SOURCES)["canonical_issue"],
            12,
        )
        spaced = json.dumps(payload()) + "\n  \t"
        self.assertEqual(issue_triage.parse_classification_output(spaced, sources=SOURCES)["classification"], "ready")

    def test_requires_exactly_one_closed_json_object(self) -> None:
        good = json.dumps(payload())
        invalid = [
            f"```json\n{good}\n```",
            f"result: {good}",
            f"{good}\ntrailing",
            f"{good}\nprose",
            good[:-1],
            good.replace('"reason":', '"extra": 1, "reason":', 1),
            good.replace('"reason":', '"reason": "first", "reason":', 1),
        ]
        for stdout in invalid:
            with self.subTest(stdout=stdout[:30]):
                with self.assertRaises(ValueError):
                    issue_triage.parse_classification_output(stdout, sources=SOURCES)

    def test_rejects_wrong_or_conditional_fields(self) -> None:
        invalid = [
            payload(schema_version=2),
            payload(classification="unknown"),
            payload(reason=""),
            payload(question="unexpected"),
            payload("needs_feedback", question=""),
            payload("duplicate", canonical_issue=0),
            payload("duplicate", canonical_issue=42),
            payload(canonical_issue=12),
            payload(evidence=[]),
            payload(evidence=[{"kind": "unknown", "identity": "issue:42", "quote": "Fix"}]),
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    issue_triage.validate_classification(value, sources=SOURCES, issue_number=42)

    def test_rejects_oversize_and_unverifiable_quotes(self) -> None:
        raw = json.dumps(payload())
        with self.assertRaises(ValueError):
            issue_triage.parse_classification_output(raw, max_bytes=len(raw.encode()) - 1, sources=SOURCES)
        with self.assertRaises(ValueError):
            issue_triage.validate_classification(
                payload(evidence=[{"kind": "issue", "identity": "issue:42", "quote": "not present"}]),
                sources=SOURCES,
            )

    def test_digest_is_stable_for_key_order(self) -> None:
        value = payload()
        reversed_value = dict(reversed(list(value.items())))
        self.assertEqual(issue_triage.decision_digest(value), issue_triage.decision_digest(reversed_value))


class CloseGateTests(unittest.TestCase):
    def test_duplicate_requires_policy_unchanged_state_and_trusted_exact_comment(self) -> None:
        classification = payload("duplicate", canonical_issue=12)
        state = {"repo": "owner/repo", "number": 42, "state": "OPEN", "updatedAt": "2026-07-28T10:00:00Z", "classified_updatedAt": "2026-07-28T10:00:00Z", "labels": [], "canonical": {"number": 12, "state": "OPEN"}}
        comments = [{"databaseId": 7, "author": {"login": "maintainer"}, "authorAssociation": "MEMBER", "createdAt": "2026-07-28T09:00:00Z", "body": "This is a duplicate of #12."}]
        decision = issue_triage.authorize_duplicate_close(classification, state, comments, auto_close=True)
        self.assertTrue(decision["authorized"])
        self.assertEqual(decision["evidence"]["comment_id"], 7)
        for change in (
            {"auto_close": False},
            {"state": {**state, "updatedAt": "2026-07-28T10:01:00Z"}},
            {"comments": [{**comments[0], "authorAssociation": "NONE"}]},
            {"comments": [{**comments[0], "body": "Looks similar."}]},
            {"comments": [{**comments[0], "body": "<!-- lokay:auto --> duplicate of #12"}]},
        ):
            args = {"auto_close": True, "state": state, "comments": comments, **change}
            result = issue_triage.authorize_duplicate_close(classification, args["state"], args["comments"], auto_close=args["auto_close"])
            self.assertFalse(result["authorized"])

    def test_out_of_scope_requires_goal_policy_and_independent_evidence(self) -> None:
        classification = payload("out_of_scope")
        state = {"repo": "owner/repo", "number": 42, "state": "OPEN", "updatedAt": "2026-07-28T10:00:00Z", "classified_updatedAt": "2026-07-28T10:00:00Z", "labels": ["wontfix"], "preexisting_labels": ["wontfix"]}
        result = issue_triage.authorize_out_of_scope_close(classification, state, [], auto_close=True, triage_goal="Automate issue triage", reject_labels=("wontfix",))
        self.assertTrue(result["authorized"])
        self.assertEqual(result["evidence"]["label"], "wontfix")
        for auto_close, goal, labels in ((False, "goal", ["wontfix"]), (True, "", ["wontfix"]), (True, "goal", [])):
            changed = {**state, "labels": labels, "preexisting_labels": labels}
            result = issue_triage.authorize_out_of_scope_close(classification, changed, [], auto_close=auto_close, triage_goal=goal, reject_labels=("wontfix",))
            self.assertFalse(result["authorized"])

    def test_trusted_actor_and_verified_precedence(self) -> None:
        self.assertTrue(issue_triage.is_trusted_maintainer({"authorAssociation": "OWNER", "author": {"login": "alice"}}))
        self.assertFalse(issue_triage.is_trusted_maintainer({"authorAssociation": "NONE", "author": {"login": "alice"}}))
        self.assertFalse(issue_triage.is_non_lokay({"body": "<!-- lokay:issue-triage:x -->", "author": {"login": "alice"}}))
        self.assertIsNone(issue_triage.triage_precedence_action({"triage_verified": True}))
        self.assertEqual(issue_triage.triage_precedence_action({"triage_verified": True, "triage_receipt": {"verified": True}})["reason"], "triage_verified")
        self.assertIsNone(issue_triage.triage_precedence_action({"triage_verified": False}))


class MutationAtomTests(unittest.TestCase):
    def test_frozen_ready_only_removes_ready(self):
        from lokay.steps import issue_triage_mutations as m

        request = {"input": {"repo": "owner/repo", "number": 4, "dry_run": True, "action": "add_ready", "conduction": {"read_triage_labels": {"ok": True, "status": "triage_labels_read", "labels": ["FROZEN", "AI:READY"]}}}}
        decided = m.decide_triage_mutation(request)
        self.assertEqual(decided["action"], "remove_ready")
        result = m.mutate_triage_issue_labels({"input": {"repo": "owner/repo", "number": 4, "dry_run": True, "action": "add_ready", "conduction": {"read_triage_labels": request["input"]["conduction"]["read_triage_labels"], "decide_triage_mutation": decided}}})
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["label"], "AI:READY")

    def test_verified_triage_precedes_legacy_direction_heuristic(self):
        from lokay.steps import issue_direction

        selected = {
            "repo": "owner/repo",
            "number": 4,
            "title": "Unrelated wording",
            "body": "No goal tokens",
            "labels": ["AI:READY"],
            "triage_verified": True,
            "triage_receipt": {"verified": True, "receipt_path": "/tmp/decision.json"},
        }
        result = issue_direction.decide_issue_action({"input": {"selected": selected, "repo_goal": "strictly different vocabulary"}, "config": {}})
        self.assertEqual(result["action"], "accept")
        self.assertEqual(result["reason"], "triage_verified")

    def test_feedback_and_close_are_dry_run_no_writes(self):
        from lokay.steps import issue_triage_mutations as m
        request = {"input": {"repo": "owner/repo", "number": 4, "dry_run": True, "action": "feedback", "question": "Which behavior?", "decision_digest": "abc", "conduction": {"read_triage_labels": {"ok": True, "status": "triage_labels_read", "labels": [], "comments": []}}}}
        self.assertEqual(m.post_triage_feedback(request)["status"], "planned")
        close_request = {**request, "input": {**request["input"], "action": "close", "conduction": {**request["input"]["conduction"], "publish_triage_close_authorization": {"ok": True, "status": "published", "payload": {"authorized": True, "verified": True}}}}}
        self.assertEqual(m.close_triage_issue(close_request)["status"], "planned")

    def test_feedback_uses_conducted_digest_and_gh_comment_id(self):
        from lokay.steps import issue_triage_mutations as m
        from unittest import mock

        classification = payload("needs_feedback", question="Which behavior?")
        digest = issue_triage.decision_digest(classification)
        request = {
            "input": {
                "repo": "owner/repo",
                "number": 4,
                "dry_run": False,
                "conduction": {
                    "read_triage_labels": {"ok": True, "status": "triage_labels_read", "labels": [], "comments": []},
                    "decide_triage_mutation": {"ok": True, "status": "mutation_decided", "action": "feedback", "classification": "needs_feedback"},
                    "classify_triage_issue": {
                        "ok": True,
                        "status": "classified",
                        "classification": classification,
                        "action": "needs_feedback",
                        "question": "Which behavior?",
                        "decision_digest": digest,
                    },
                },
            }
        }
        planned = m.post_triage_feedback({**request, "input": {**request["input"], "dry_run": True}})
        self.assertEqual(planned["status"], "planned")
        self.assertIn(digest, planned["marker"])
        self.assertIn("Which behavior?", planned["body"])
        after = {
            "labels": [],
            "state": "OPEN",
            "stateReason": "",
            "updatedAt": "2026-07-29T09:00:00Z",
            "comments": [{"id": "IC_kwDOExample", "body": planned["body"]}],
        }
        with mock.patch.object(m, "run_cmd", return_value=mock.Mock(stdout="", stderr="", returncode=0)), mock.patch.object(m, "_read_issue", return_value=after):
            result = m.post_triage_feedback(request)
        self.assertEqual(result["status"], "feedback_verified", result)
        self.assertEqual(result["comment"]["id"], "IC_kwDOExample")

    def test_terminal_requires_verified_receipt(self):
        from lokay.steps import issue_triage_mutations as m
        rows = [{"repo": "owner/repo", "number": 4}, {"repo": "other/repo", "number": 9}]
        result = m.build_triage_terminal({"input": {"repo": "owner/repo", "number": 4, "rows": rows, "conduction": {}}})
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "triage_not_verified")
        self.assertEqual(result["rows"], rows)
        self.assertNotIn("triage_verified", result.get("selected") or {})
        self.assertNotIn("triage_receipt", result.get("selected") or {})

    def test_nonselected_branches_noop_without_commands(self):
        from lokay.steps import issue_triage_mutations as m

        def no_command(*args, **kwargs):
            raise AssertionError("command should not run")

        request = {"input": {"repo": "owner/repo", "number": 4, "dry_run": False, "action": "add_ready", "conduction": {"read_triage_labels": {"ok": True, "status": "triage_labels_read", "labels": [], "comments": [], "state": "OPEN"}}}}
        with unittest.mock.patch("lokay.steps.issue_triage_mutations.run_cmd", side_effect=no_command):
            for result in (
                m.post_triage_feedback(request),
                m.observe_triage_feedback(request),
                m.close_triage_issue(request),
            ):
                self.assertEqual(result["status"], "noop")
                self.assertEqual(result["reason"], "action_not_selected")
        labels_request = {"input": {"repo": "owner/repo", "number": 4, "dry_run": False, "action": "feedback", "conduction": request["input"]["conduction"]}}
        with unittest.mock.patch("lokay.steps.issue_triage_mutations.run_cmd", side_effect=no_command):
            result = m.mutate_triage_issue_labels(labels_request)
        self.assertEqual((result["status"], result["reason"]), ("noop", "action_not_selected"))

    def test_unauthorized_close_never_runs_command(self):
        from lokay.steps import issue_triage_mutations as m

        request = {"input": {"repo": "owner/repo", "number": 4, "dry_run": False, "action": "close", "conduction": {"read_triage_labels": {"ok": True, "status": "triage_labels_read", "labels": [], "comments": [], "state": "OPEN"}}}}
        with unittest.mock.patch("lokay.steps.issue_triage_mutations.run_cmd", side_effect=AssertionError("command should not run")):
            result = m.close_triage_issue(request)
        self.assertEqual((result["status"], result["reason"]), ("noop", "close_not_authorized"))

    def test_terminal_no_selection_and_disabled_preserve_rows(self):
        from lokay.steps import issue_triage_mutations as m

        rows = [{"repo": "owner/repo", "number": 4, "labels": []}]
        for data in (
            {"triage_enabled": False, "rows": rows},
            {"rows": rows, "conduction": {"select_triage_candidate": {"ok": True, "status": "noop", "reason": "no_triage_candidate", "selected": None}}},
        ):
            result = m.build_triage_terminal({"input": data, "config": {}})
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "noop")
            self.assertEqual(result["rows"], rows)

    def test_terminal_upstream_failure_remains_terminal(self):
        from lokay.steps import issue_triage_mutations as m

        failure = {"ok": False, "status": "failed", "reason": "run_budget_conflict"}
        result = m.build_triage_terminal({"input": {"rows": [{"repo": "owner/repo", "number": 4}], "selected": {"repo": "owner/repo", "number": 4}, "conduction": {"reserve_triage_run_budget": failure}}, "config": {}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "upstream_failed")
        self.assertEqual(result["failure_class"], "terminal")

    def test_mutation_decision_uses_conducted_classifier_output(self):
        from lokay.steps import issue_triage_mutations as m

        classification = payload("ready")
        request = {"input": {"repo": "owner/repo", "number": 4, "conduction": {"read_triage_labels": {"ok": True, "status": "triage_labels_read", "labels": []}, "classify_triage_issue": {"ok": True, "status": "classified", "classification": classification, "action": "ready"}}}}
        result = m.decide_triage_mutation(request)
        self.assertEqual(result["status"], "mutation_decided", result)
        self.assertEqual(result["action"], "add_ready")
        self.assertEqual(result["classification"], "ready")

if __name__ == "__main__":
    unittest.main()
