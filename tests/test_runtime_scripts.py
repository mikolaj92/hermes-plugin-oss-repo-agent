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
        self.assertIn("HERMES_CLAUDE_TIMEOUT_SECONDS:-1800", dispatch)
        self.assertIn("HERMES_REPO_AGENT_SOURCE:-github", dispatch)
        self.assertIn("github_issue_rows", dispatch)
        self.assertIn("github_agent_pr_rows", dispatch)
        self.assertIn("GITHUB_PR_OPEN", dispatch)
        self.assertIn('then "NONE"', dispatch)
        self.assertIn("source=github", dispatch)
        self.assertIn("ai:in-progress", dispatch)
        self.assertIn("ai:pr-opened", dispatch)
        self.assertIn("ALLOW_UNSAFE_CLAUDE", dispatch)
        self.assertIn("HERMES_REPO_AGENT_MAX_TASK_ATTEMPTS", dispatch)
        self.assertIn("HERMES_REPO_AGENT_RETRY_BACKOFF_SECONDS", dispatch)
        self.assertIn("CLAUDE_TIMEOUT", dispatch)
        self.assertIn("board_agent_active", dispatch)
        self.assertIn("board_repo_busy", dispatch)
        self.assertIn("BOARD_BUSY", dispatch)
        self.assertIn("skip-dispatch", dispatch)
        self.assertIn("board_spawned=1", dispatch)
        self.assertIn(".agent.lock", dispatch)
        self.assertIn('"$WORKTREE_ROOT"/*/.agent.lock/pid', dispatch)
        self.assertNotIn('pgrep -c -f "claude.*Hermes task"', dispatch)
        self.assertIn("Hermes task", dispatch)
        self.assertIn("STALE_BOARD_LOCK", dispatch)

    def test_dispatch_requires_explicit_unsafe_claude_opt_in(self):
        dispatch = self.read("scripts/repo_issue_to_pr_dispatch.sh")
        self.assertIn("HERMES_ALLOW_UNSAFE_CLAUDE", dispatch)
        self.assertIn("ALLOW_UNSAFE_CLAUDE", dispatch)
        self.assertIn('"$RUN_OPENCODE" == 1 && "$ALLOW_UNSAFE_CLAUDE" == 1', dispatch)
        self.assertIn("unsafe-claude-disabled", dispatch)
        self.assertIn("CLAUDE_SKIPPED", dispatch)
        self.assertIn("repo-agent unsafe Claude execution disabled by default", dispatch)
        self.assertIn("opt_in=HERMES_ALLOW_UNSAFE_CLAUDE=1", dispatch)
        self.assertIn("unsafe_claude=$ALLOW_UNSAFE_CLAUDE", dispatch)

    def test_dispatch_treats_opencode_deferrals_as_nonfatal(self):
        dispatch = self.read("scripts/repo_issue_to_pr_dispatch.sh")
        self.assertIn("OPENCODE_DEFERRED_RC=10", dispatch)
        self.assertIn('return "$OPENCODE_DEFERRED_RC"', dispatch)
        self.assertIn("opencode_rc=$?", dispatch)
        self.assertIn('elif [[ "$opencode_rc" == "$OPENCODE_DEFERRED_RC" ]]', dispatch)
        self.assertIn('log "OPENCODE_DEFERRED task=$task_id repo=$task_repo issue=$issue rc=$opencode_rc"', dispatch)
        self.assertIn("deferred=$((deferred + 1))", dispatch)
        self.assertIn("deferred=$deferred", dispatch)

    def test_deployment_surfaces_keep_unsafe_claude_disabled_by_default(self):
        cron = self.read("scripts/cron_repo_issue_to_pr_dispatch.sh")
        self.assertIn("HERMES_ISSUE_TO_PR_RUN_OPENCODE=1", cron)
        self.assertIn("HERMES_ALLOW_UNSAFE_CLAUDE=0", cron)
        self.assertIn("explicit human approval", cron)

        launchd = self.read("launchd/com.hermes.oss-repo-agent.dispatch.plist.template")
        self.assertIn("<key>HERMES_ALLOW_UNSAFE_CLAUDE</key>", launchd)
        self.assertRegex(
            launchd,
            r"<key>HERMES_ALLOW_UNSAFE_CLAUDE</key>\s*<string>0</string>",
        )
        self.assertIn("explicit human approval", launchd)

        template = self.read("templates/launchd/oss-repo-agent-dispatch.plist.template")
        self.assertIn("<key>EnvironmentVariables</key>", template)
        self.assertIn("<key>HERMES_ALLOW_UNSAFE_CLAUDE</key>", template)
        self.assertRegex(
            template,
            r"<key>HERMES_ALLOW_UNSAFE_CLAUDE</key>\s*<string>0</string>",
        )

        readme = self.read("README.md")
        self.assertIn("HERMES_ALLOW_UNSAFE_CLAUDE=0", readme)
        self.assertIn("HERMES_ALLOW_UNSAFE_CLAUDE=1", readme)
        self.assertIn("--run-opencode` does not start Claude unless", readme)

        after_install = self.read("after-install.md")
        self.assertIn("HERMES_ALLOW_UNSAFE_CLAUDE=0", after_install)
        self.assertIn("HERMES_ALLOW_UNSAFE_CLAUDE=1", after_install)
        self.assertIn("--run-opencode` is not enough to start Claude", after_install)

    def test_dispatch_cleans_stale_issue_tasks(self):
        dispatch = self.read("scripts/repo_issue_to_pr_dispatch.sh")
        self.assertIn("issue_state", dispatch)
        self.assertIn("complete-stale-issue", dispatch)
        self.assertIn("complete-stale-fix", dispatch)

    def test_dispatch_finalizes_workers_and_recovers_stale_blocks(self):
        dispatch = self.read("scripts/repo_issue_to_pr_dispatch.sh")
        self.assertIn("blocked_task_retriable", dispatch)
        self.assertIn("retry_gate", dispatch)
        self.assertIn("retry_failure_note", dispatch)
        self.assertIn("recover-blocked-fix-task", dispatch)
        self.assertIn("reassign", dispatch)
        self.assertIn("diff --quiet", dispatch)
        self.assertIn("existing_worktree", dispatch)
        self.assertIn("refresh_clone_base", dispatch)
        self.assertIn("origin/main", dispatch)
        self.assertIn('base_ref="origin/main"', dispatch)
        self.assertIn('refs/remotes/origin/$branch', dispatch)
        self.assertIn('worktree add "$worktree" "$branch"', dispatch)
        self.assertIn("repo-agent selected this task now", dispatch)
        self.assertIn("selection_reason", dispatch)
        self.assertIn("repo-agent worker finished without an open PR", dispatch)
        self.assertIn("repo-agent worker exited with rc=", dispatch)
        self.assertNotIn('return 1\n  fi\n  grep -Eq "Hermes repo-agent started Claude worker', dispatch)
        self.assertIn("CLAUDE_FINALIZED", dispatch)
        self.assertIn("CLAUDE_SPAWNED", dispatch)
        self.assertIn("run_claude_for_fix_worker", dispatch)
        self.assertIn("worktree-dirty-after-claude", dispatch)
        self.assertIn("blocked_task_manual_only", dispatch)
        self.assertIn('>>"$log_file" 2>&1 &', dispatch)
        self.assertIn("complete-blocked-with-existing-pr", dispatch)
        self.assertIn("repo-agent-fixer", dispatch)
        self.assertNotIn('kanban --board "$board" block "$task_id" "Hermes repo-agent started Claude worker', dispatch)

    def test_triage_uses_checks_comments_and_fix_tasks(self):
        triage = self.read("scripts/repo_pr_triage.sh")
        self.assertIn('checks_pass "$repo" "$number"', triage)
        self.assertIn("--add-assignee", triage)
        self.assertIn("comment_pr_once", triage)
        self.assertIn("pr_repair_context", triage)
        self.assertIn("Repair context:", triage)
        self.assertIn("review-not-approved", triage)
        self.assertIn("checks-not-passing", triage)
        self.assertIn("test-evidence-missing", triage)
        self.assertIn("pr_has_test_evidence", triage)
        self.assertIn("FIX_TASK_CREATED", triage)
        self.assertIn("HERMES_REPO_AGENT_SOURCE:-github", triage)
        self.assertIn("ai:needs-fix", triage)
        self.assertIn("board_repo_busy", triage)
        self.assertIn("BOARD_BUSY", triage)
        self.assertIn("skip-pr-triage", triage)
        self.assertIn("release_clean_worktree_for_branch", triage)
        self.assertIn("WORKTREE_ROOT", triage)
        self.assertIn('"$path" == "$WORKTREE_ROOT/"*', triage)
        self.assertIn("refresh_clone_base", triage)
        self.assertIn("sync_local_branch_from_origin", triage)
        self.assertIn('branch -f "$branch" "origin/$branch"', triage)
        self.assertIn('worktree remove "$path"', triage)

    def test_health_and_launchd_templates_exist(self):
        health = self.read("scripts/repo_agent_health.sh")
        self.assertIn("gh auth status", health)
        self.assertIn("launchctl bootstrap", health)
        self.assertIn("stale-lock", health)
        self.assertIn("active-worker-lock", health)
        self.assertIn("dead-worker-lock", health)
        self.assertNotIn("claude.*Hermes task", health)
        self.assertIn("launchd-last-exit-nonzero", health)
        self.assertIn("duplicate-hermes-cron", health)
        self.assertIn("hermes-update-available", health)
        self.assertIn("repo-agent-cleanup", health)
        self.assertIn("repo-agent-hermes-update", health)
        for name in [
            "oss-repo-agent-intake.plist.template",
            "oss-repo-agent-dispatch.plist.template",
            "oss-repo-agent-pr-triage.plist.template",
            "oss-repo-agent-cleanup.plist.template",
            "oss-repo-agent-hermes-update.plist.template",
            "oss-repo-agent-health.plist.template",
        ]:
            template = self.read(f"templates/launchd/{name}")
            self.assertIn("LimitLoadToSessionType", template)
            self.assertIn("Background", template)

    def test_runtime_visibility_contracts_cover_anti_stall_failures(self):
        dispatch = self.read("scripts/repo_issue_to_pr_dispatch.sh")
        triage = self.read("scripts/repo_pr_triage.sh")
        health = self.read("scripts/repo_agent_health.sh")
        status = self.read("scripts/repo_agent_status.sh")
        smoke = self.read("scripts/repo_agent_smoke.sh")

        self.assertIn("KANBAN_LIST_FAILED", dispatch)
        self.assertIn("candidate_limit = max(limit * 5, limit + 5)", dispatch)
        self.assertIn("PR_LIST_FAILED", triage)
        self.assertIn("MERGE_FAILED", triage)
        self.assertIn("HERMES_PR_TRIAGE_LIST_LIMIT", triage)
        self.assertIn("PR_LIST_LIMIT", triage)
        self.assertIn('--limit "$PR_LIST_LIMIT"', triage)
        for signal in [
            "watchdog-worker-runtime-ok",
            "watchdog-worker-runtime-timeout",
            "watchdog-worker-runtime-none",
            "watchdog-worker-log-recent",
            "watchdog-worker-log-stale",
            "watchdog-worker-log-missing",
        ]:
            self.assertIn(signal, health)
        for signal in [
            "ASSIGN_FAILED",
            "PR_ASSIGNED",
            "FIX_TASK_CREATED",
            "FIX_TASK_FAILED",
            "LOCK_HELD",
            "KANBAN_LIST_FAILED",
            "PR_LIST_FAILED",
            "MERGE_FAILED",
            "watchdog-worker-",
        ]:
            self.assertIn(signal, status)
        for signal in [
            "KANBAN_LIST_FAILED",
            "candidate_limit = max(limit * 5, limit + 5)",
            "PR_LIST_FAILED",
            "MERGE_FAILED",
            "HERMES_PR_TRIAGE_LIST_LIMIT",
            '--limit "$PR_LIST_LIMIT"',
            "watchdog-worker-runtime-timeout",
            "watchdog-worker-log-stale",
            "ASSIGN_FAILED",
            "PR_ASSIGNED",
        ]:
            self.assertIn(signal, smoke)

    def test_cleanup_and_status_scripts_cover_autonomy_gaps(self):
        cleanup = self.read("scripts/repo_agent_cleanup.sh")
        status = self.read("scripts/repo_agent_status.sh")
        updater = self.read("scripts/repo_agent_hermes_update.sh")
        self.assertIn("WORKTREE_REMOVED", cleanup)
        self.assertIn("LOCAL_BRANCH_REMOVED", cleanup)
        self.assertIn("[maintenance] dirty worktree", cleanup)
        self.assertIn("MAINTENANCE_TASK_ENSURED", cleanup)
        self.assertIn("open_pr_for_branch", cleanup)
        self.assertIn("Recent Decisions", status)
        self.assertIn("repo-agent status", status)
        self.assertIn("repo-agent-cleanup", status)
        self.assertIn("repo-agent-hermes-update", status)
        self.assertIn("hermes update --backup --yes", updater)
        self.assertIn("active-worker-locks", updater)

    def test_repo_registry_is_shared_by_runtime_scripts(self):
        registry = self.read("scripts/repo_agent_repos.sh")
        for repo in [
            "mikolaj92/Fala",
            "mikolaj92/datasource-kit",
            "mikolaj92/reviewkit",
            "mikolaj92/anonimizator3000",
            "mikolaj92/splot",
            "mikolaj92/my-auth",
            "mikolaj92/my-usermanager",
            "mikolaj92/msds-portal",
            "mikolaj92/swift-openapi-dynamic",
            "mikolaj92/OpenAPITransportKit",
        ]:
            self.assertIn(repo, registry)

        for relative in [
            "scripts/repo_issue_intake.sh",
            "scripts/repo_issue_to_pr_dispatch.sh",
            "scripts/repo_pr_triage.sh",
            "scripts/repo_agent_health.sh",
            "scripts/repo_agent_cleanup.sh",
            "scripts/repo_agent_status.sh",
        ]:
            text = self.read(relative)
            self.assertIn('source "$SCRIPT_DIR/repo_agent_repos.sh"', text)

    def test_backfill_and_webhook_are_thin_reconciliation_wrappers(self):
        backfill = self.read("scripts/repo_agent_backfill.sh")
        webhook = self.read("scripts/repo_agent_webhook.sh")
        self.assertIn("repo_issue_intake.sh", backfill)
        self.assertIn("repo_issue_to_pr_dispatch.sh", backfill)
        self.assertIn("repo_pr_triage.sh", backfill)
        self.assertIn("repo_agent_cleanup.sh", backfill)
        self.assertIn("HERMES_ISSUE_TO_PR_RUN_OPENCODE=0", backfill)
        self.assertIn("not an HTTP listener", webhook)
        self.assertIn("issues|issue_comment", webhook)
        self.assertIn("pull_request|pull_request_review", webhook)
        self.assertIn("repo_pr_triage.sh", webhook)

    def test_intake_claims_github_issue_before_kanban_task(self):
        intake = self.read("scripts/repo_issue_intake.sh")
        self.assertIn("HERMES_REPO_AGENT_ASSIGNEE", intake)
        self.assertIn("HERMES_REPO_AGENT_SOURCE:-github", intake)
        self.assertIn("HERMES_KANBAN_INTAKE_ASSIGNEE", intake)
        self.assertIn("repo-agent-intake", intake)
        self.assertIn("existing_issue_task", intake)
        self.assertIn('if [ "$QUEUE_SOURCE" = "kanban" ]', intake)
        self.assertIn("GITHUB_ISSUE_READY", intake)
        self.assertIn("KANBAN_TASK_EXISTS", intake)
        self.assertIn("--add-assignee", intake)
        self.assertIn("ready_label_missing", intake)
        self.assertIn("claim-and-kanban-without-label", intake)
        self.assertLess(intake.index("--add-assignee"), intake.index("create \"$task_title\""))


if __name__ == "__main__":
    unittest.main()
