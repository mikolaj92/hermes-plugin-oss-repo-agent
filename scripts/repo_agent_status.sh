#!/usr/bin/env bash
set -euo pipefail

# One-screen operational status for the Hermes repo-agent pipeline.

export HOME="${HOME:-/Users/mini-m4-main}"
export PATH="/Users/mini-m4-main/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

LOG_DIR="${HERMES_REPO_AGENT_LOG_DIR:-/Users/mini-m4-main/.hermes/logs}"
WORKTREE_ROOT="${HERMES_WORKTREE_ROOT:-/Users/mini-m4-main/.hermes/worktrees/repo-fixer}"

usage() {
  cat <<'USAGE'
Usage: repo_agent_status.sh

Prints launchd state, worker locks, repo queue counts, and recent repo-agent
decisions in one terminal-friendly view.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { printf 'missing-command name=%s\n' "$1"; exit 1; }
}

for cmd in gh hermes launchctl find tail date; do
  require_cmd "$cmd"
done

uid="$(id -u)"
jobs=(
  "intake|com.mikolaj92.hermes.repo-issue-intake"
  "dispatch|com.mikolaj92.hermes.repo-issue-to-pr-dispatch"
  "triage|com.mikolaj92.hermes.repo-pr-triage"
  "cleanup|com.mikolaj92.hermes.repo-agent-cleanup"
  "update|com.mikolaj92.hermes.repo-agent-hermes-update"
  "health|com.mikolaj92.hermes.repo-agent-health"
)
repos=(
  "mikolaj92/Fala|mikolaj92-fala"
  "mikolaj92/reviewkit|mikolaj92-reviewkit"
  "mikolaj92/anonimizator3000|mikolaj92-anonimizator3000"
  "mikolaj92/datasource-kit|mikolaj92-datasource-kit"
  "mikolaj92/splot|mikolaj92-splot"
  "mikolaj92/my-auth|mikolaj92-my-auth"
  "mikolaj92/my-usermanager|mikolaj92-my-usermanager"
  "mikolaj92/msds-portal|mikolaj92-msds-portal"
  "mikolaj92/swift-openapi-dynamic|mikolaj92-swift-openapi-dynamic"
  "mikolaj92/OpenAPITransportKit|mikolaj92-openapi-transport-kit"
)

printf 'repo-agent status %s\n\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

printf 'Launchd\n'
for item in "${jobs[@]}"; do
  IFS='|' read -r name label <<<"$item"
  if info="$(launchctl print "user/$uid/$label" 2>/dev/null)"; then
    state="$(printf '%s\n' "$info" | awk -F '= ' '/state =/ {print $2; exit}')"
    runs="$(printf '%s\n' "$info" | awk -F '= ' '/runs =/ {gsub(/[^0-9].*/, "", $2); print $2; exit}')"
    last="$(printf '%s\n' "$info" | awk -F '= ' '/last exit code =/ {gsub(/[^0-9-].*/, "", $2); print $2; exit}')"
    printf '  %-9s state=%s runs=%s last_exit=%s\n' "$name" "${state:-unknown}" "${runs:-0}" "${last:-unknown}"
  else
    printf '  %-9s missing label=%s\n' "$name" "$label"
  fi
done

printf '\nWorkers\n'
locks="$(find "$WORKTREE_ROOT" -maxdepth 5 -type f -path '*/.agent.lock/pid' 2>/dev/null || true)"
if [[ -z "$locks" ]]; then
  printf '  none\n'
else
  while IFS= read -r pid_file; do
    [[ -n "$pid_file" ]] || continue
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
      printf '  active pid=%s lock=%s\n' "$pid" "$(dirname "$pid_file")"
    else
      printf '  dead pid=%s lock=%s\n' "${pid:-missing}" "$(dirname "$pid_file")"
    fi
  done <<<"$locks"
fi

printf '\nQueues\n'
for entry in "${repos[@]}"; do
  IFS='|' read -r repo board <<<"$entry"
  open_prs="$(gh pr list --repo "$repo" --state open --json number --jq 'length' 2>/dev/null || echo '?')"
  open_issues="$(gh issue list --repo "$repo" --state open --json number --jq 'length' 2>/dev/null || echo '?')"
  stats="$(hermes kanban --board "$board" stats 2>/dev/null | tr '\n' ' ' | sed 's/  */ /g' || echo 'kanban=?')"
  printf '  %-36s issues=%s prs=%s %s\n' "$repo" "$open_issues" "$open_prs" "$stats"
done

printf '\nRecent Decisions\n'
for log in "$LOG_DIR/repo-issue-to-pr-dispatch.log" "$LOG_DIR/repo-pr-triage.log" "$LOG_DIR/repo-agent-cleanup.log" "$LOG_DIR/repo-agent-hermes-update.log"; do
  [[ -f "$log" ]] || continue
  printf '  %s\n' "$(basename "$log")"
  recent="$(tail -n 80 "$log" | grep -E 'DECISION|CLAUDE_|WORKTREE_|LOCAL_BRANCH_|DONE|WARN|ERROR' | tail -n 8 || true)"
  if [[ -n "$recent" ]]; then
    printf '%s\n' "$recent" | sed 's/^/    /'
  else
    printf '    no recent decisions\n'
  fi
done
