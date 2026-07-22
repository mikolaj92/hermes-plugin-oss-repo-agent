"""Structural checks for the Fala v2 package path manifest."""

from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
import unittest
from pathlib import Path


PACKAGE_PATH = Path(__file__).resolve().parents[1] / "fala-package.toml"
EXPECTED_PATH_IDS = {"issue_intake", "issue_to_pr", "pr_triage", "cleanup", "auto_worker"}


class PackageStructureTests(unittest.TestCase):
    def test_v2_paths_have_unique_effectors_and_valid_conduction(self) -> None:
        package = tomllib.loads(PACKAGE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(package["version"], "2")
        paths = package["correlation_paths"]
        self.assertEqual({path["id"] for path in paths}, EXPECTED_PATH_IDS)

        effector_ids: set[str] = set()
        for path in paths:
            path_effector_ids = {effector["id"] for effector in path["effectors"]}
            self.assertEqual(len(path_effector_ids), len(path["effectors"]))
            self.assertTrue(path_effector_ids)
            for effector in path["effectors"]:
                self.assertNotIn(effector["id"], effector_ids)
                effector_ids.add(effector["id"])
                self.assertEqual(effector["adapter"]["kind"], "subprocess")
                self.assertTrue(effector["config"]["handler"].startswith("repo_agent.steps."))
                self.assertTrue(
                    set(effector.get("conduction", [])).issubset(path_effector_ids),
                    effector["id"],
                )


if __name__ == "__main__":
    unittest.main()
