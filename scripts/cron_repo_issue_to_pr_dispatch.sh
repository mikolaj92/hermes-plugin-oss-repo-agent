#!/usr/bin/env bash
# Hermes cron wrapper: repo issue→PR dispatch (live, Claude disabled by default).
# Register with: hermes cron add "every 10m" --name repo-issue-to-pr-dispatch \
#   --script cron_repo_issue_to_pr_dispatch.sh --no-agent
export HERMES_ISSUE_TO_PR_DRY_RUN=0
export HERMES_ISSUE_TO_PR_RUN_OPENCODE=1
# Keep unsafe Claude execution disabled unless an operator has explicit human approval
# and has reviewed sandboxing for this host. Set to 1 only for that audited run.
export HERMES_ALLOW_UNSAFE_CLAUDE=0
export HERMES_ISSUE_TO_PR_BLOCK_INTAKE=0
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
exec bash "$(dirname "$0")/repo_issue_to_pr_dispatch.sh"
