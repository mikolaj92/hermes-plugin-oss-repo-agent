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
        self.assertIn("board_spawned=1", dispatch)
        self.assertIn(".agent.lock", dispatch)
        self.assertIn("Hermes task", dispatch)
        self.assertIn("STALE_BOARD_LOCK", dispatch)

    def test_dispatch_cleans_stale_issue_tasks(self):
        dispatch = self.read("scripts/repo_issue_to_pr_dispatch.sh")
        self.assertIn("issue_state", dispatch)
        self.assertIn("complete-stale-issue", dispatch)
        self.assertIn("complete-stale-fix", dispatch)

    def test_dispatch_finalizes_workers_and_recovers_stale_blocks(self):
        dispatch = self.read("scripts/repo_issue_to_pr_dispatch.sh")
        self.assertIn("blocked_task_retriable", dispatch)
        self.assertIn("recover-blocked-fix-task", dispatch)
        self.assertIn("reassign", dispatch)
        self.assertIn("repo-agent worker finished without an open PR", dispatch)
        self.assertIn("CLAUDE_FINALIZED", dispatch)
        self.assertIn("complete-blocked-with-existing-pr", dispatch)
        self.assertIn("repo-agent-fixer", dispatch)
        self.assertNotIn('kanban --board "$board" block "$task_id" "Hermes repo-agent started Claude worker', dispatch)

    def test_triage_uses_checks_comments_and_fix_tasks(self):
        triage = self.read("scripts/repo_pr_triage.sh")
        self.assertIn('checks_pass "$repo" "$number"', triage)
        self.assertIn("--add-assignee", triage)
        self.assertIn("comment_pr_once", triage)
        self.assertIn("review-not-approved", triage)
        self.assertIn("checks-not-passing", triage)
        self.assertIn("FIX_TASK_CREATED", triage)
        self.assertIn("release_clean_worktree_for_branch", triage)
        self.assertIn('worktree remove "$path"', triage)

    def test_health_and_launchd_templates_exist(self):
        health = self.read("scripts/repo_agent_health.sh")
        self.assertIn("gh auth status", health)
        self.assertIn("launchctl bootstrap", health)
        self.assertIn("stale-lock", health)
        self.assertIn("launchd-last-exit-nonzero", health)
        self.assertIn("duplicate-hermes-cron", health)
        for name in [
            "oss-repo-agent-intake.plist.template",
            "oss-repo-agent-dispatch.plist.template",
            "oss-repo-agent-pr-triage.plist.template",
            "oss-repo-agent-health.plist.template",
        ]:
            template = self.read(f"templates/launchd/{name}")
            self.assertIn("LimitLoadToSessionType", template)
            self.assertIn("Background", template)

    def test_runtime_scripts_include_added_public_repos(self):
        for relative in [
            "scripts/repo_issue_intake.sh",
            "scripts/repo_issue_to_pr_dispatch.sh",
            "scripts/repo_pr_triage.sh",
            "scripts/repo_agent_health.sh",
        ]:
            text = self.read(relative)
            self.assertIn("mikolaj92/splot", text)
            self.assertIn("mikolaj92/my-auth", text)
            self.assertIn("mikolaj92/my-usermanager", text)

    def test_intake_claims_github_issue_before_kanban_task(self):
        intake = self.read("scripts/repo_issue_intake.sh")
        self.assertIn("HERMES_REPO_AGENT_ASSIGNEE", intake)
        self.assertIn("HERMES_KANBAN_INTAKE_ASSIGNEE", intake)
        self.assertIn("repo-agent-intake", intake)
        self.assertIn("existing_issue_task", intake)
        self.assertIn("KANBAN_TASK_EXISTS", intake)
        self.assertIn("--add-assignee", intake)
        self.assertIn("ready_label_missing", intake)
        self.assertIn("claim-and-kanban-without-label", intake)
        self.assertLess(intake.index("--add-assignee"), intake.index("create \"$task_title\""))


if __name__ == "__main__":
    unittest.main()
