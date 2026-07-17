"""Structural tests: correlation paths compose registered atomic effectors."""

from __future__ import annotations

import unittest

from repo_agent.flows.cleanup import CLEANUP_PATH
from repo_agent.flows.intake import INTAKE_PATH
from repo_agent.flows.issue_to_pr import ISSUE_TO_PR_PATH
from repo_agent.flows.triage import TRIAGE_PATH


class PathCompositionTests(unittest.TestCase):
    def test_paths_have_effectors_with_conduction(self) -> None:
        for path in (INTAKE_PATH, ISSUE_TO_PR_PATH, TRIAGE_PATH, CLEANUP_PATH):
            self.assertTrue(path.effectors, path.id)
            # first effector has no conduction (root)
            self.assertEqual(path.effectors[0].conduction, [])
            # later ones use conduction for composition
            if len(path.effectors) > 1:
                self.assertTrue(
                    any(e.conduction for e in path.effectors[1:]),
                    f"{path.id} should wire conduction",
                )

    def test_issue_to_pr_covers_core_stages(self) -> None:
        ids = [e.id for e in ISSUE_TO_PR_PATH.effectors]
        for needed in (
            "load_task",
            "parse_ref",
            "prepare_wt",
            "run_omp",
            "push",
            "open_pr",
            "complete_task",
        ):
            self.assertIn(needed, ids)

    def test_triage_ends_with_apply(self) -> None:
        ids = [e.id for e in TRIAGE_PATH.effectors]
        self.assertEqual(ids[-1], "apply")
        self.assertIn("decide", ids)

    def test_refs_are_python_functions(self) -> None:
        for path in (INTAKE_PATH, ISSUE_TO_PR_PATH, TRIAGE_PATH, CLEANUP_PATH):
            for e in path.effectors:
                self.assertEqual(e.adapter.kind, "python_function")
                self.assertTrue(e.adapter.ref.startswith("repo_agent."))


if __name__ == "__main__":
    unittest.main()
