"""Structural checks for the Fala v2 package path manifest."""

from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
import unittest
from pathlib import Path


PACKAGE_PATH = Path(__file__).resolve().parents[1] / "fala-package.toml"
EXPECTED_PATH_IDS = {"issue_intake", "issue_to_pr", "pr_triage", "cleanup", "cleanup_reconcile", "auto_worker"}
SELECTOR_GATES = {
    "issue_intake": "select_issue_candidate",
    "issue_to_pr": "select_dispatch_task",
    "pr_triage": "select_fix_pr",
    "auto_worker": "intake_select_issue_candidate",
}


class PackageStructureTests(unittest.TestCase):
    def test_v2_paths_have_unique_effectors_and_valid_conduction(self) -> None:
        """Every declared path is a self-contained, ordered atomic graph."""
        package = tomllib.loads(PACKAGE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(package["version"], "2")
        paths = package["correlation_paths"]
        self.assertEqual({path["id"] for path in paths}, EXPECTED_PATH_IDS)

        for path in paths:
            effectors = path["effectors"]
            path_effector_ids = {effector["id"] for effector in effectors}
            self.assertEqual(len(path_effector_ids), len(effectors))
            self.assertTrue(path_effector_ids)
            positions = {effector["id"]: index for index, effector in enumerate(effectors)}
            for effector in effectors:
                self.assertEqual(effector["adapter"]["kind"], "subprocess")
                self.assertTrue(effector["config"]["handler"].startswith("lokay.steps."))
                self.assertTrue(
                    set(effector.get("conduction", [])).issubset(path_effector_ids),
                    effector["id"],
                )
                self.assertTrue(
                    all(positions[predecessor] < positions[effector["id"]] for predecessor in effector.get("conduction", [])),
                    effector["id"],
                )

            selector = SELECTOR_GATES.get(path["id"])
            if selector is None:
                continue
            selector_position = positions[selector]
            reachable = {selector}
            changed = True
            while changed:
                changed = False
                for effector in effectors:
                    if effector["id"] not in reachable and reachable.intersection(effector.get("conduction", [])):
                        reachable.add(effector["id"])
                        changed = True
            self.assertTrue(
                all(effector["id"] in reachable for effector in effectors[selector_position + 1:]),
                f"{path['id']} has an ungated operation after {selector}",
            )

            if path["id"] == "auto_worker":
                self.assertTrue(all(effector["id"].startswith(("intake_", "dispatch_", "triage_", "cleanup_")) for effector in effectors))


if __name__ == "__main__":
    unittest.main()
