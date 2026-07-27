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
LANE_ROOTS = {
    "issue_intake": ("select_issue_candidate",),
    "issue_to_pr": ("select_dispatch_task",),
    "pr_triage": ("select_fix_pr",),
    "auto_worker": (
        "intake_select_issue_candidate",
        "triage_read_open_prs",
        "lifecycle_read_lifecycle_github_state",
        "lifecycle_read_lifecycle_local_evidence",
    ),
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

            lane_roots = LANE_ROOTS.get(path["id"])
            if lane_roots is None:
                continue
            reachable: set[str] = set()
            first_selector_position = min(positions[root] for root in lane_roots)
            for selector in lane_roots:
                lane_reachable = {selector}
                changed = True
                while changed:
                    changed = False
                    for effector in effectors:
                        if effector["id"] not in lane_reachable and lane_reachable.intersection(effector.get("conduction", [])):
                            lane_reachable.add(effector["id"])
                            changed = True
                reachable.update(lane_reachable)
            self.assertTrue(
                all(effector["id"] in reachable for effector in effectors[first_selector_position + 1:]),
                f"{path['id']} has an operation outside its declared selector lanes",
            )

            if path["id"] in {"pr_triage", "auto_worker"}:
                prefix = "triage_" if path["id"] == "auto_worker" else ""
                by_id = {effector["id"]: effector for effector in effectors}
                self.assertEqual(by_id[f"{prefix}read_existing_repair_pr"]["conduction"], [f"{prefix}decide_repair_attempt", f"{prefix}verify_repair_push_oid"])
                self.assertEqual(
                    by_id[f"{prefix}build_repair_receipt"]["conduction"],
                    [
                        f"{prefix}decide_repair_attempt",
                        f"{prefix}verify_existing_repair_pr",
                        f"{prefix}verify_repair_push_oid",
                        f"{prefix}verify_repair_omp_postconditions",
                        f"{prefix}invoke_repair_omp",
                    ],
                )
                self.assertEqual(
                    by_id[f"{prefix}verify_repair_receipt"]["conduction"],
                    [f"{prefix}decide_repair_attempt", f"{prefix}publish_repair_receipt", f"{prefix}build_repair_receipt"],
                )
                self.assertEqual(by_id[f"{prefix}update_repair_branch_provenance"]["conduction"], [f"{prefix}decide_repair_attempt", f"{prefix}verify_repair_receipt", f"{prefix}verify_repair_push_oid"])
                self.assertEqual(by_id[f"{prefix}verify_updated_repair_branch_provenance"]["conduction"], [f"{prefix}update_repair_branch_provenance"])
                expected_completed = [f"{prefix}load_pr_fields", f"{prefix}read_repair_remote_head"]
                if path["id"] == "auto_worker":
                    expected_completed.append("lifecycle_decide_lifecycle_transition")
                self.assertEqual(by_id[f"{prefix}read_repair_completed_receipt"]["conduction"], expected_completed)
                self.assertIn(f"{prefix}read_repair_completed_receipt", by_id[f"{prefix}decide_repair_attempt"]["conduction"])
                self.assertEqual(by_id[f"{prefix}read_repair_attempt_recovery_evidence"]["conduction"], [f"{prefix}read_repair_attempt_state"])
                self.assertEqual(by_id[f"{prefix}claim_repair_attempt_recovery"]["conduction"], [f"{prefix}read_repair_attempt_recovery_evidence"])
                self.assertEqual(by_id[f"{prefix}verify_repair_attempt_recovery"]["conduction"], [f"{prefix}claim_repair_attempt_recovery"])
                self.assertIn(f"{prefix}verify_repair_attempt_recovery", by_id[f"{prefix}decide_repair_attempt"]["conduction"])
                self.assertEqual(by_id[f"{prefix}reserve_repair_attempt"]["conduction"], [f"{prefix}decide_repair_attempt", f"{prefix}verify_repair_attempt_recovery"])
                self.assertEqual(by_id[f"{prefix}verify_repair_attempt_reservation"]["conduction"], [f"{prefix}reserve_repair_attempt", f"{prefix}verify_repair_attempt_recovery"])

            if path["id"] == "auto_worker":
                self.assertTrue(
                    all(
                        effector["id"].startswith(
                            ("intake_", "dispatch_", "triage_", "lifecycle_", "aggregate_", "cleanup_")
                        )
                        for effector in effectors
                    )
                )


if __name__ == "__main__":
    unittest.main()
