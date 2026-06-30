#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

bash -n "$ROOT/scripts/repo_issue_intake.sh"
bash -n "$ROOT/scripts/repo_issue_to_pr_dispatch.sh"
bash -n "$ROOT/scripts/repo_pr_triage.sh"
bash -n "$ROOT/scripts/repo_agent_health.sh"

grep -Fq '[fix-pr-review]' "$ROOT/scripts/repo_issue_to_pr_dispatch.sh"
grep -Fq 'complete-stale-review' "$ROOT/scripts/repo_issue_to_pr_dispatch.sh"
grep -Fq 'HERMES_CLAUDE_TIMEOUT_SECONDS' "$ROOT/scripts/repo_issue_to_pr_dispatch.sh"
grep -Fq 'board-agent-active' "$ROOT/scripts/repo_issue_to_pr_dispatch.sh"
grep -Fq 'checks_pass "$repo" "$number"' "$ROOT/scripts/repo_pr_triage.sh"
grep -Fq 'comment_pr_once' "$ROOT/scripts/repo_pr_triage.sh"

python3 -m unittest discover -s "$ROOT/tests"

if [[ "${HERMES_REPO_AGENT_SMOKE_HEALTH:-0}" == 1 ]]; then
  "$ROOT/scripts/repo_agent_health.sh"
fi

printf '%s\n' 'repo-agent smoke ok'
