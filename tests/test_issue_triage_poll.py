from __future__ import annotations

import unittest

from lokay.steps import poll


def row(repo: str, number: int, labels=(), priority=20, updated="2026-07-28T10:00:00Z"):
    return {"repo": repo, "number": number, "labels": list(labels), "priority": priority, "updatedAt": updated, "state": "OPEN"}

class TriageCandidateTests(unittest.TestCase):
    def test_eligibility_casefolds_configured_labels(self):
        rows = [
            row("a/r", 1, ["AI:READY"]),
            row("a/r", 2, ["AI:READY", "AI:BLOCKED"]),
            row("a/r", 3, ["READY-CUSTOM", "PROGRESS-CUSTOM"]),
        ]
        default = poll.filter_issue_eligibility(
            {"input": {"rows": rows, "dry_run": True}, "config": {}}
        )
        self.assertEqual([item["number"] for item in default["eligible"]], [1])
        custom = poll.filter_issue_eligibility(
            {
                "input": {"rows": rows[2:], "dry_run": True},
                "config": {"ready_label": "ready-custom", "in_progress_label": "progress-custom"},
            }
        )
        self.assertEqual(custom["eligible_count"], 0)
        self.assertEqual(custom["skipped"][0]["reason"], "progress-custom")

    def test_disabled_triage_preserves_normalized_rows(self):
        rows = [row("a/r", 1, ["ai:ready"])]
        out = poll.select_triage_candidate(
            {
                "input": {
                    "triage_enabled": False,
                    "conduction": {"normalize_issue_rows": {"ok": True, "status": "normalized", "rows": rows}},
                },
                "config": {},
            }
        )
        self.assertEqual(out["status"], "triage_disabled")
        self.assertEqual(out["rows"], rows)
        self.assertIsNone(out["selected"])


    def call(self, rows, index=None):
        return poll.select_triage_candidate({"input": {"rows": rows, "receipt_index": index or {}, "dry_run": False}, "config": {"ready_label": "ai:ready", "needs_feedback_label": "ai:needs-feedback", "duplicate_label": "duplicate", "out_of_scope_label": "ai:out-of-scope", "frozen_label": "frozen", "blocked_label": "ai:blocked", "in_progress_label": "ai:in-progress", "pr_opened_label": "ai:pr-opened"}})

    def test_precedence_and_deterministic_order(self):
        rows = [row("z/r", 3), row("a/r", 2, ["frozen", "ai:ready"]), row("b/r", 1)]
        out = self.call(rows, {"z/r#3": {"pending": True, "verified": False}})
        self.assertEqual(out["selected"]["repo"], "z/r")
        self.assertEqual(out["candidate_class"], "reconcile_pending")
        out = self.call(rows)
        self.assertEqual((out["selected"]["repo"], out["selected"]["number"]), ("a/r", 2))
        self.assertEqual(out["candidate_class"], "frozen_ready_conflict")

    def test_feedback_reenters_only_after_new_update(self):
        waiting = row("a/r", 1, ["ai:needs-feedback"], updated="2026-07-28T10:01:00Z")
        stale = self.call([waiting], {"a/r#1": {"feedback_watermark": "2026-07-28T10:01:00Z"}})
        self.assertIsNone(stale["selected"])
        fresh = self.call([waiting], {"a/r#1": {"feedback_watermark": "2026-07-28T10:00:00Z"}})
        self.assertEqual(fresh["candidate_class"], "feedback_updated")

    def test_malformed_timestamp_fails_closed(self):
        out = self.call([row("a/r", 1, ["ai:needs-feedback"], updated="yesterday")], {"a/r#1": {"feedback_watermark": "2026-07-28T10:00:00Z"}})
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["reason"], "malformed_triage_candidate")

    def test_casefold_terminal_labels_are_not_untriaged(self):
        out = self.call([row("a/r", 1, ["AI:READY"])])
        self.assertIsNone(out["selected"])


if __name__ == "__main__":
    unittest.main()
