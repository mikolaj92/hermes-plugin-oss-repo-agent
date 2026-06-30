#!/usr/bin/env bash
# Hermes cron wrapper: repo PR triage (live, automerge, with comments).
# Register with: hermes cron add "every 10m" --name repo-pr-triage \
#   --script cron_repo_pr_triage.sh --no-agent
export HERMES_PR_TRIAGE_DRY_RUN=0
export HERMES_PR_TRIAGE_COMMENT=1
export HERMES_PR_AUTOMERGE=1
export HERMES_PR_REQUIRE_APPROVED=0
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
exec bash "$(dirname "$0")/repo_pr_triage.sh"
