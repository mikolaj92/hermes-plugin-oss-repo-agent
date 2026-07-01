from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MappingContractTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text()

    def test_mapping_doc_names_the_sources_of_truth(self):
        doc = self.read("docs/github-kanban-mapping.md")
        for section in [
            "## Source of Truth",
            "## Issue Mapping",
            "## Label and Priority Mapping",
            "## Work Mapping",
            "## PR Mapping",
            "## Reconciliation Commands",
        ]:
            self.assertIn(section, doc)
        self.assertIn("GitHub owns repository facts", doc)
        self.assertIn("Hermes Kanban owns", doc)
        self.assertIn("It intentionally does not mirror every Kanban status back into GitHub", self.read("README.md"))

    def test_default_registry_rows_have_repo_board_clone_and_priority(self):
        registry = self.read("scripts/repo_agent_repos.sh")
        rows = [
            line
            for line in registry.splitlines()
            if line.startswith("mikolaj92/")
        ]
        self.assertGreaterEqual(len(rows), 10)
        for row in rows:
            parts = row.split("|")
            self.assertEqual(len(parts), 4, row)
            self.assertTrue(parts[1].startswith("mikolaj92-"), row)
            self.assertIn("/Developer/hermes-repos/", parts[2], row)
            self.assertTrue(parts[3].isdigit(), row)

    def test_priority_mapping_is_runtime_code_not_only_docs(self):
        registry = self.read("scripts/repo_agent_repos.sh")
        intake = self.read("scripts/repo_issue_intake.sh")
        self.assertIn("repo_agent_kanban_priority_for_text", registry)
        self.assertIn("*security*|*vulnerability*", registry)
        self.assertIn("*bug*|*regression*|*crash*|*failing*", registry)
        self.assertIn('repo_agent_kanban_priority_for_text "$title $labels"', intake)
        self.assertIn("Mapping: GitHub labels/title -> Kanban priority", intake)

    def test_runtime_scripts_do_not_own_duplicate_repo_maps(self):
        for relative in [
            "scripts/repo_issue_intake.sh",
            "scripts/repo_issue_to_pr_dispatch.sh",
            "scripts/repo_pr_triage.sh",
            "scripts/repo_agent_cleanup.sh",
            "scripts/repo_agent_health.sh",
            "scripts/repo_agent_status.sh",
        ]:
            text = self.read(relative)
            self.assertIn('source "$SCRIPT_DIR/repo_agent_repos.sh"', text, relative)
            self.assertNotIn("mikolaj92/Fala|mikolaj92-fala", text, relative)
            self.assertNotIn("mikolaj92/Fala) printf", text, relative)


if __name__ == "__main__":
    unittest.main()
