#!/usr/bin/env bash
# Hermes cron wrapper: repo issue→PR dispatch (live, claude agents in background).
# Register with: hermes cron add "every 10m" --name repo-issue-to-pr-dispatch \
#   --script cron_repo_issue_to_pr_dispatch.sh --no-agent
export HERMES_ISSUE_TO_PR_DRY_RUN=0
export HERMES_ISSUE_TO_PR_RUN_OPENCODE=1
export HERMES_ISSUE_TO_PR_BLOCK_INTAKE=0
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
exec bash "$(dirname "$0")/repo_issue_to_pr_dispatch.sh"
