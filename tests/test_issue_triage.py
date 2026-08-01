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

    def test_mutate_returns_post_stamp_updated_at(self):
        from lokay.steps import issue_triage_mutations as m
        from unittest import mock

        after = {
            "labels": ["ai:needs-feedback"],
            "state": "OPEN",
            "stateReason": "",
            "updatedAt": "2026-08-01T17:52:37Z",
            "comments": [],
        }
        request = {
            "input": {
                "repo": "owner/repo",
                "number": 12,
                "dry_run": False,
                "action": "feedback",
                "decision_digest": "a" * 64,
                "conduction": {
                    "read_triage_labels": {
                        "ok": True,
                        "status": "triage_labels_read",
                        "labels": [],
                        "updatedAt": "2026-07-29T14:23:58Z",
                        "state": "OPEN",
                        "comments": [],
                    },
                    "decide_triage_mutation": {
                        "ok": True,
                        "status": "mutation_decided",
                        "action": "feedback",
                        "label": "ai:needs-feedback",
                        "classification": "needs_feedback",
                        "decision_digest": "a" * 64,
                    },
                },
            },
            "config": {},
        }
        with mock.patch.object(m, "run_cmd", return_value=mock.Mock(stdout="", stderr="", returncode=0)), mock.patch.object(
            m, "_read_issue", return_value=after
        ):
            result = m.mutate_triage_issue_labels(request)
        self.assertEqual(result["status"], "labels_verified", result)
        self.assertEqual(result["issue_updated_at"], "2026-08-01T17:52:37Z")
        self.assertEqual(result["updatedAt"], "2026-08-01T17:52:37Z")

        already = {
            "input": {
                "repo": "owner/repo",
                "number": 12,
                "dry_run": False,
                "action": "feedback",
                "decision_digest": "a" * 64,
                "conduction": {
                    "read_triage_labels": {
                        "ok": True,
                        "status": "triage_labels_read",
                        "labels": ["ai:needs-feedback"],
                        "updatedAt": "2026-08-01T17:52:37Z",
                        "state": "OPEN",
                        "comments": [],
                    },
                    "decide_triage_mutation": {
                        "ok": True,
                        "status": "mutation_decided",
                        "action": "feedback",
                        "label": "ai:needs-feedback",
                        "classification": "needs_feedback",
                        "decision_digest": "a" * 64,
                    },
                },
            },
            "config": {},
        }
        with mock.patch.object(m, "run_cmd", side_effect=AssertionError("already labeled must not mutate")):
            result = m.mutate_triage_issue_labels(already)
        self.assertEqual(result["status"], "labels_verified", result)
        self.assertEqual(result.get("reason"), "already_labeled")
        self.assertEqual(result["issue_updated_at"], "2026-08-01T17:52:37Z")

    def test_out_of_scope_feedback_stamps_class_label(self):
        from lokay.steps import issue_triage_mutations as m
        from unittest import mock

        request = {
            "input": {
                "repo": "owner/repo",
                "number": 3736,
                "dry_run": True,
                "conduction": {
                    "read_triage_labels": {
                        "ok": True,
                        "status": "triage_labels_read",
                        "labels": [],
                        "comments": [],
                    },
                    "classify_triage_issue": {
                        "ok": True,
                        "status": "classified",
                        "classification": {
                            "classification": "out_of_scope",
                            "reason": "Already shipped",
                            "question": "",
                            "canonical_issue": 0,
                        },
                        "action": "out_of_scope",
                        "decision_digest": "c" * 64,
                    },
                },
            },
            "config": {
                "needs_feedback_label": "ai:needs-feedback",
                "out_of_scope_label": "ai:out-of-scope",
                "duplicate_label": "duplicate",
            },
        }
        decided = m.decide_triage_mutation(request)
        self.assertEqual(decided["action"], "feedback")
        self.assertEqual(decided["classification"], "out_of_scope")
        self.assertEqual(decided["label"], "ai:out-of-scope")
        ensured_request = {
            "input": {
                **request["input"],
                "conduction": {
                    **request["input"]["conduction"],
                    "decide_triage_mutation": decided,
                },
            },
            "config": request["config"],
        }
        with mock.patch.object(m, "_repo_labels", return_value=[{"name": "ai:out-of-scope", "color": "B60205", "description": ""}]):
            ensured = m.ensure_triage_label(ensured_request)
        self.assertEqual(ensured["status"], "label_resolved")
        self.assertEqual(ensured["label"], "ai:out-of-scope")
        mutated = m.mutate_triage_issue_labels(ensured_request)
        self.assertEqual(mutated["status"], "planned")
        self.assertEqual(mutated["label"], "ai:out-of-scope")

    def test_duplicate_feedback_stamps_class_label(self):
        from lokay.steps import issue_triage_mutations as m

        decided = m.decide_triage_mutation(
            {
                "input": {
                    "repo": "owner/repo",
                    "number": 9,
                    "dry_run": True,
                    "conduction": {
                        "read_triage_labels": {
                            "ok": True,
                            "status": "triage_labels_read",
                            "labels": [],
                        },
                        "classify_triage_issue": {
                            "ok": True,
                            "status": "classified",
                            "classification": {
                                "classification": "duplicate",
                                "reason": "Same as #1",
                                "question": "",
                                "canonical_issue": 1,
                            },
                            "action": "duplicate",
                            "decision_digest": "d" * 64,
                        },
                    },
                },
                "config": {
                    "needs_feedback_label": "ai:needs-feedback",
                    "duplicate_label": "duplicate",
                    "out_of_scope_label": "ai:out-of-scope",
                },
            }
        )
        self.assertEqual(decided["action"], "feedback")
        self.assertEqual(decided["classification"], "duplicate")
        self.assertEqual(decided["label"], "duplicate")

    def test_needs_feedback_still_stamps_needs_feedback(self):
        from lokay.steps import issue_triage_mutations as m

        decided = m.decide_triage_mutation(
            {
                "input": {
                    "repo": "owner/repo",
                    "number": 12,
                    "dry_run": True,
                    "conduction": {
                        "read_triage_labels": {
                            "ok": True,
                            "status": "triage_labels_read",
                            "labels": [],
                        },
                        "classify_triage_issue": {
                            "ok": True,
                            "status": "classified",
                            "classification": {
                                "classification": "needs_feedback",
                                "reason": "Need intent",
                                "question": "What next?",
                                "canonical_issue": 0,
                            },
                            "action": "needs_feedback",
                            "decision_digest": "e" * 64,
                        },
                    },
                },
                "config": {
                    "needs_feedback_label": "ai:needs-feedback",
                    "out_of_scope_label": "ai:out-of-scope",
                },
            }
        )
        self.assertEqual(decided["action"], "feedback")
        self.assertEqual(decided["classification"], "needs_feedback")
        self.assertEqual(decided["label"], "ai:needs-feedback")

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

    def test_feedback_dedupes_existing_issue_marker_prefix(self):
        from lokay.steps import issue_triage_mutations as m

        classification = payload("needs_feedback", question="Which behavior?")
        digest = issue_triage.decision_digest(classification)
        request = {
            "input": {
                "repo": "owner/repo",
                "number": 4,
                "dry_run": False,
                "conduction": {
                    "read_triage_labels": {
                        "ok": True,
                        "status": "triage_labels_read",
                        "labels": [],
                        "comments": [
                            {
                                "id": "IC_old",
                                "body": "Please provide maintainer confirmation for this issue.\n\n<!-- lokay:issue-triage:owner/repo:4:olddigest -->",
                            }
                        ],
                    },
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
        result = m.post_triage_feedback(request)
        self.assertEqual(result["status"], "noop", result)
        self.assertEqual(result["reason"], "feedback_already_posted")
        self.assertEqual(result["comment_id"], "IC_old")

    def test_feedback_prefers_newest_marker_when_multiple_exist(self):
        from lokay.steps import issue_triage_mutations as m

        classification = payload("needs_feedback", question="Which behavior?")
        digest = issue_triage.decision_digest(classification)
        request = {
            "input": {
                "repo": "owner/repo",
                "number": 4,
                "conduction": {
                    "read_triage_labels": {
                        "ok": True,
                        "status": "triage_labels_read",
                        "labels": [],
                        "comments": [
                            {"id": "IC_old", "createdAt": "2026-07-29T09:00:00Z", "body": "old\n\n<!-- lokay:issue-triage:owner/repo:4:old -->"},
                            {"id": "IC_new", "createdAt": "2026-07-29T10:00:00Z", "body": "new\n\n<!-- lokay:issue-triage:owner/repo:4:new -->"},
                        ],
                    },
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
        result = m.post_triage_feedback(request)
        self.assertEqual(result["status"], "noop", result)
        self.assertEqual(result["reason"], "feedback_already_posted")
        self.assertEqual(result["comment_id"], "IC_new")
        self.assertEqual(result["matches"], 2)

    def test_feedback_verifies_conducted_already_posted_noop(self):
        from lokay.steps import issue_triage_mutations as m

        marker = "<!-- lokay:issue-triage:owner/repo:4:digest -->"
        request = {
            "input": {
                "dry_run": False,
                "conduction": {
                    "post_triage_feedback": {
                        "ok": True,
                        "status": "noop",
                        "reason": "feedback_already_posted",
                        "repo": "owner/repo",
                        "number": 4,
                        "marker": marker,
                        "comment_id": 5116451350,
                        "decision_digest": "d" * 64,
                    },
                    "mutate_triage_issue_labels": {"ok": True, "status": "noop", "reason": "action_not_selected"},
                    "read_triage_labels": {"ok": True, "status": "triage_labels_read", "repo": "owner/repo", "number": 4, "labels": []},
                }
            }
        }
        payload = {
            "labels": [],
            "state": "OPEN",
            "stateReason": "",
            "updatedAt": "2026-07-29T10:27:00Z",
            "comments": [{
                "id": "IC_existing",
                "databaseId": 5116451350,
                "url": "https://github.com/owner/repo/issues/4#issuecomment-5116451350",
                "body": marker,
                "createdAt": "2026-07-29T10:23:39Z",
            }],
        }
        completed = unittest.mock.Mock(stdout=json.dumps(payload))
        with unittest.mock.patch("lokay.steps.issue_triage_mutations.run_cmd", return_value=completed):
            result = m.verify_triage_feedback(request)
        self.assertEqual(result["status"], "feedback_verified", result)
        self.assertTrue(result["verified"])
        self.assertEqual(result["comment_id"], 5116451350)
        self.assertEqual(result["decision_digest"], "d" * 64)
        self.assertEqual(result["issue_updated_at"], "2026-07-29T10:27:00Z")

    def test_feedback_receipt_publishes_after_already_posted_verify(self):
        from lokay.steps import issue_triage_receipts as receipts
        import tempfile
        from pathlib import Path

        digest = "a" * 64
        marker = f"<!-- lokay:issue-triage:owner/repo:4:{digest} -->"
        with tempfile.TemporaryDirectory() as tmp:
            decision = receipts.publish_triage_decision_receipt(
                {
                    "input": {
                        "triage_receipts": tmp,
                        "repo": "owner/repo",
                        "issue": 4,
                        "dry_run": False,
                        "payload": {
                            "schema_version": 1,
                            "stage": "decision",
                            "repo": "owner/repo",
                            "issue": 4,
                            "updated_at": "2026-07-29T10:27:00Z",
                            "issue_updated_at": "2026-07-29T10:27:00Z",
                            "decision_digest": digest,
                            "selected": {"repo": "owner/repo", "number": 4, "updatedAt": "2026-07-29T10:27:00Z"},
                        },
                    },
                    "config": {},
                }
            )
            self.assertEqual(decision["status"], "written", decision)
            request = {
                "input": {
                    "triage_receipts": tmp,
                    "dry_run": False,
                    "conduction": {
                        "verify_triage_feedback": {
                            "ok": True,
                            "status": "feedback_verified",
                            "verified": True,
                            "repo": "owner/repo",
                            "number": 4,
                            "marker": marker,
                            "comment_id": 5116451350,
                            "decision_digest": digest,
                            "issue_updated_at": "2026-07-29T10:27:00Z",
                            "verified_readback_state": "verified",
                        },
                        "observe_triage_feedback": {
                            "ok": True,
                            "status": "noop",
                            "reason": "no_human_response",
                            "repo": "owner/repo",
                            "number": 4,
                        },
                        "decide_triage_mutation": {
                            "ok": True,
                            "status": "mutation_decided",
                            "action": "feedback",
                            "repo": "owner/repo",
                            "number": 4,
                            "decision_digest": digest,
                        },
                    },
                },
                "config": {},
            }
            out = receipts.publish_triage_feedback_receipt(request)
            self.assertEqual(out["status"], "written", out)
            path = Path(out["receipt_path"])
            self.assertTrue(path.name.startswith("feedback-verified-5116451350"))
            payload = json.loads(path.read_text())
            self.assertEqual(payload["stage"], "feedback-verified")
            self.assertEqual(payload["decision_digest"], digest)
            self.assertEqual(payload["issue_updated_at"], "2026-07-29T10:27:00Z")
            index = receipts.read_triage_receipt_index(
                {
                    "input": {
                        "triage_receipts": tmp,
                        "repo": "owner/repo",
                        "issue": 4,
                    },
                    "config": {},
                }
            )
            summary = index["index"]
            self.assertTrue(summary["decision_recorded"])
            self.assertTrue(summary["triage_verified"])
            self.assertEqual(summary["feedback_watermark"], "2026-07-29T10:27:00Z")
            self.assertEqual(summary["decision_watermark"], "2026-07-29T10:27:00Z")
    def test_ensure_uses_configured_ready_label(self):
        from lokay.steps import issue_triage_mutations as m

        request = {
            "input": {
                "repo": "owner/repo",
                "number": 4,
                "ready_label": "ai:ready",
                "dry_run": True,
                "conduction": {
                    "read_triage_labels": {"ok": True, "status": "triage_labels_read", "labels": [], "repo": "owner/repo", "number": 4},
                    "decide_triage_mutation": {
                        "ok": True,
                        "status": "mutation_decided",
                        "action": "add_ready",
                        "classification": "ready",
                        "repo": "owner/repo",
                        "number": 4,
                    },
                },
            },
            "config": {},
        }
        with unittest.mock.patch("lokay.steps.issue_triage_mutations.run_cmd") as run_cmd:
            run_cmd.return_value = unittest.mock.Mock(stdout=json.dumps([{"name": "ai:ready", "color": "B60205", "description": ""}]))
            result = m.ensure_triage_label(request)
        self.assertEqual(result["status"], "label_resolved", result)
        self.assertEqual(result["label"], "ai:ready")


    def test_mutation_atoms_use_conducted_repo_number_identity(self):
        from lokay.steps import issue_triage_mutations as m

        request = {
            "input": {
                "dry_run": True,
                "conduction": {
                    "read_triage_labels": {
                        "ok": True,
                        "status": "triage_labels_read",
                        "repo": "owner/repo",
                        "number": 4,
                        "labels": [],
                        "selected": {"repo": "owner/repo", "number": 4},
                    },
                    "decide_triage_mutation": {
                        "ok": True,
                        "status": "mutation_decided",
                        "action": "add_ready",
                        "classification": "ready",
                        "repo": "owner/repo",
                        "number": 4,
                    },
                },
            }
        }
        ensure = m.ensure_triage_label(request)
        self.assertNotEqual(ensure.get("reason"), "no_triage_selection", ensure)
        self.assertEqual(ensure.get("repo"), "owner/repo")
        self.assertEqual(ensure.get("number"), 4)
        mutate = m.mutate_triage_issue_labels(request)
        self.assertEqual(mutate["status"], "planned", mutate)
        self.assertEqual(mutate["action"], "add_ready")

    def test_decide_fails_closed_when_classifier_failed(self):
        from lokay.steps import issue_triage_mutations as m

        result = m.decide_triage_mutation(
            {
                "input": {
                    "repo": "owner/repo",
                    "number": 4,
                    "conduction": {
                        "read_triage_labels": {"ok": True, "status": "triage_labels_read", "labels": [], "selected": {"repo": "owner/repo", "number": 4}},
                        "classify_triage_issue": {"ok": False, "status": "failed", "reason": "classifier_failed:unverifiable_evidence_quote"},
                    },
                }
            }
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "upstream_failed")

    def test_terminal_requires_verified_receipt(self):
        from lokay.steps import issue_triage_mutations as m
        rows = [{"repo": "owner/repo", "number": 4}, {"repo": "other/repo", "number": 9}]
        result = m.build_triage_terminal({"input": {"repo": "owner/repo", "number": 4, "rows": rows, "conduction": {}}})
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "triage_not_verified")
        self.assertEqual(result["rows"], rows)
        self.assertNotIn("triage_verified", result.get("selected") or {})
        self.assertNotIn("triage_receipt", result.get("selected") or {})

    def test_terminal_accepts_mutation_verified_readback(self):
        from lokay.steps import issue_triage_mutations as m

        rows = [{"repo": "owner/repo", "number": 4, "labels": []}, {"repo": "other/repo", "number": 9}]
        receipt = {
            "stage": "mutation-verified",
            "decision_digest": "a" * 64,
            "verified_readback_state": "verified",
            "label": "ai:ready",
            "repo": "owner/repo",
            "number": 4,
        }
        result = m.build_triage_terminal(
            {
                "input": {
                    "repo": "owner/repo",
                    "number": 4,
                    "rows": rows,
                    "conduction": {
                        "select_triage_candidate": {
                            "ok": True,
                            "status": "selected",
                            "selected": {"repo": "owner/repo", "number": 4, "labels": []},
                        },
                        "reserve_triage_run_budget": {
                            "ok": True,
                            "status": "reserved",
                            "selected": {"repo": "owner/repo", "number": 4, "labels": []},
                        },
                        "mutate_triage_issue_labels": {
                            "ok": True,
                            "status": "labels_verified",
                            "verified": True,
                            "label": "ai:ready",
                            "labels": ["ai:ready"],
                            "repo": "owner/repo",
                            "number": 4,
                        },
                        "verify_triage_receipt": {
                            "ok": True,
                            "status": "verified",
                            "payload": receipt,
                        },
                    },
                }
            }
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "triage_terminal", result)
        self.assertTrue(result["triage_verified"])
        self.assertEqual(result["selected"]["labels"], ["ai:ready"])
        self.assertTrue(result["selected"]["triage_verified"])

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
        labels_request = {
            "input": {
                "repo": "owner/repo",
                "number": 4,
                "dry_run": True,
                "action": "feedback",
                "classification": "needs_feedback",
                "conduction": request["input"]["conduction"],
            }
        }
        with unittest.mock.patch("lokay.steps.issue_triage_mutations.run_cmd", side_effect=no_command):
            result = m.mutate_triage_issue_labels(labels_request)
        self.assertEqual(result["status"], "planned", result)
        self.assertEqual(result.get("label"), "ai:needs-feedback", result)
        self.assertEqual(result.get("action"), "feedback", result)

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
