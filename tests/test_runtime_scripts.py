from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RuntimeScriptTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text()

    def test_dispatch_handles_review_fix_tasks_and_stale_closed_prs(self):
        dispatch = self.read("scripts/repo_issue_to_pr_dispatch.sh")
        self.assertIn("[fix-pr-review]", dispatch)
        self.assertIn("complete-stale-review", dispatch)
        self.assertIn("pr_state", dispatch)
        self.assertIn('status == "blocked"', dispatch)

    def test_dispatch_serializes_workers_and_times_out_claude(self):
        dispatch = self.read("scripts/repo_issue_to_pr_dispatch.sh")
        self.assertIn("HERMES_CLAUDE_TIMEOUT_SECONDS", dispatch)
        self.assertIn("CLAUDE_TIMEOUT", dispatch)
        self.assertIn("board_agent_active", dispatch)
        self.assertIn(".agent.lock", dispatch)
        self.assertIn("Hermes task", dispatch)

    def test_dispatch_cleans_stale_issue_tasks(self):
        dispatch = self.read("scripts/repo_issue_to_pr_dispatch.sh")
        self.assertIn("issue_state", dispatch)
        self.assertIn("complete-stale-issue", dispatch)
        self.assertIn("complete-stale-fix", dispatch)

    def test_triage_uses_checks_comments_and_fix_tasks(self):
        triage = self.read("scripts/repo_pr_triage.sh")
        self.assertIn('checks_pass "$repo" "$number"', triage)
        self.assertIn("comment_pr_once", triage)
        self.assertIn("review-not-approved", triage)
        self.assertIn("checks-not-passing", triage)
        self.assertIn("FIX_TASK_CREATED", triage)

    def test_health_and_launchd_templates_exist(self):
        health = self.read("scripts/repo_agent_health.sh")
        self.assertIn("gh auth status", health)
        self.assertIn("launchctl bootstrap", health)
        self.assertIn("stale-lock", health)
        for name in [
            "oss-repo-agent-intake.plist.template",
            "oss-repo-agent-dispatch.plist.template",
            "oss-repo-agent-pr-triage.plist.template",
            "oss-repo-agent-health.plist.template",
        ]:
            template = self.read(f"templates/launchd/{name}")
            self.assertIn("LimitLoadToSessionType", template)
            self.assertIn("Background", template)


if __name__ == "__main__":
    unittest.main()
