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
            "## Reconciliation",
        ]:
            self.assertIn(section, doc)
        self.assertIn("GitHub owns repository facts", doc)
        self.assertIn("Hermes Kanban owns", doc)
        self.assertIn("com.mikolaj92.lokay.supervisor", doc)
        self.assertIn("twelve catalog process", doc)
        self.assertIn("manual diagnostics", doc)
        self.assertNotIn("repo_issue_intake.sh", doc)
        self.assertNotIn("lokay_backfill.sh", doc)

    def test_readme_declares_single_scheduled_mutator(self):
        readme = self.read("README.md")
        self.assertIn("com.mikolaj92.lokay.supervisor", readme)
        self.assertIn("only scheduled mutator", readme)
        self.assertIn("manual diagnostics", readme)
        self.assertIn("twelve catalog process IDs", readme)

if __name__ == "__main__":
    unittest.main()
