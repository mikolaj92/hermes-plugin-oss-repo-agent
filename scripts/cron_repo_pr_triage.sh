#!/usr/bin/env bash
# Hermes cron wrapper: PR triage runs live only with explicit safety gates.
# Approval, passing checks, and test evidence are required before any merge.
# Register with: hermes cron add "every 10m" --name repo-pr-triage \
#   --script cron_repo_pr_triage.sh --no-agent
export HERMES_PR_TRIAGE_DRY_RUN=0
export HERMES_PR_TRIAGE_COMMENT=1
export HERMES_PR_AUTOMERGE=0
export HERMES_PR_REQUIRE_APPROVED=1
export HERMES_PR_ALLOW_NO_CHECKS=0
export HERMES_PR_REQUIRE_TEST_EVIDENCE=1
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
exec bash "$(dirname "$0")/repo_pr_triage.sh"
