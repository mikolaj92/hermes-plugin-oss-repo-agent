#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

bash -n "$ROOT/scripts/repo_issue_intake.sh"
bash -n "$ROOT/scripts/repo_issue_to_pr_dispatch.sh"
bash -n "$ROOT/scripts/repo_pr_triage.sh"
bash -n "$ROOT/scripts/repo_agent_health.sh"
bash -n "$ROOT/scripts/repo_agent_cleanup.sh"
bash -n "$ROOT/scripts/repo_agent_status.sh"
bash -n "$ROOT/scripts/repo_agent_hermes_update.sh"
bash -n "$ROOT/scripts/repo_agent_repos.sh"
bash -n "$ROOT/scripts/repo_agent_backfill.sh"
bash -n "$ROOT/scripts/repo_agent_webhook.sh"

grep -Fq '[fix-pr-review]' "$ROOT/scripts/repo_issue_to_pr_dispatch.sh"
grep -Fq 'complete-stale-review' "$ROOT/scripts/repo_issue_to_pr_dispatch.sh"
grep -Fq 'HERMES_CLAUDE_TIMEOUT_SECONDS' "$ROOT/scripts/repo_issue_to_pr_dispatch.sh"
grep -Fq 'board-agent-active' "$ROOT/scripts/repo_issue_to_pr_dispatch.sh"
grep -Fq 'checks_pass "$repo" "$number"' "$ROOT/scripts/repo_pr_triage.sh"
grep -Fq 'comment_pr_once' "$ROOT/scripts/repo_pr_triage.sh"
grep -Fq 'WORKTREE_REMOVED' "$ROOT/scripts/repo_agent_cleanup.sh"
grep -Fq '[maintenance] dirty worktree' "$ROOT/scripts/repo_agent_cleanup.sh"
grep -Fq 'Recent Decisions' "$ROOT/scripts/repo_agent_status.sh"
grep -Fq 'hermes update --backup --yes' "$ROOT/scripts/repo_agent_hermes_update.sh"
grep -Fq 'repo_issue_intake.sh' "$ROOT/scripts/repo_agent_backfill.sh"
grep -Fq 'repo_pr_triage.sh' "$ROOT/scripts/repo_agent_webhook.sh"
grep -Fq 'GitHub to Hermes Kanban Mapping' "$ROOT/docs/github-kanban-mapping.md"
grep -Fq 'KANBAN_LIST_FAILED' "$ROOT/scripts/repo_issue_to_pr_dispatch.sh"
grep -Fq 'candidate_limit = max(limit * 5, limit + 5)' "$ROOT/scripts/repo_issue_to_pr_dispatch.sh"
grep -Fq 'PR_LIST_FAILED' "$ROOT/scripts/repo_pr_triage.sh"
grep -Fq 'MERGE_FAILED' "$ROOT/scripts/repo_pr_triage.sh"
grep -Fq 'HERMES_PR_TRIAGE_LIST_LIMIT' "$ROOT/scripts/repo_pr_triage.sh"
grep -Fq -- '--limit "$PR_LIST_LIMIT"' "$ROOT/scripts/repo_pr_triage.sh"
grep -Fq 'watchdog-worker-runtime-timeout' "$ROOT/scripts/repo_agent_health.sh"
grep -Fq 'watchdog-worker-log-stale' "$ROOT/scripts/repo_agent_health.sh"
grep -Fq 'ASSIGN_FAILED' "$ROOT/scripts/repo_agent_status.sh"
grep -Fq 'PR_ASSIGNED' "$ROOT/scripts/repo_agent_status.sh"

python3 -m unittest discover -s "$ROOT/tests"

if [[ "${HERMES_REPO_AGENT_SMOKE_MODEL:-0}" == 1 ]]; then
  provider="${HERMES_REPO_AGENT_SMOKE_PROVIDER:-custom}"
  model="${HERMES_REPO_AGENT_SMOKE_MODEL_NAME:-auto/claude-sonnet}"
  response="$(
    cd /tmp
    HERMES_ACCEPT_HOOKS=1 hermes --provider "$provider" -m "$model" --ignore-rules -z 'Respond exactly OK'
  )"
  [[ "$response" == OK ]] || {
    printf 'repo-agent model smoke failed provider=%s model=%s response=%s\n' "$provider" "$model" "$response" >&2
    exit 1
  }
fi

if [[ "${HERMES_REPO_AGENT_SMOKE_HEALTH:-0}" == 1 ]]; then
  "$ROOT/scripts/repo_agent_health.sh"
fi

printf '%s\n' 'repo-agent smoke ok'
