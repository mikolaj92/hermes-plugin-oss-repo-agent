#!/usr/bin/env bash
set -euo pipefail

# Health/watchdog for the Hermes repo-agent launchd pipeline.

export HOME="${HOME:-/Users/mini-m4-main}"
export PATH="/Users/mini-m4-main/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

LOG_FILE="${HERMES_REPO_AGENT_HEALTH_LOG:-/Users/mini-m4-main/.hermes/logs/repo-agent-health.log}"
STALE_LOCK_MINUTES="${HERMES_STALE_LOCK_MINUTES:-180}"
MAX_LOG_AGE_SECONDS="${HERMES_REPO_AGENT_MAX_LOG_AGE_SECONDS:-1800}"
MIN_FREE_GB="${HERMES_REPO_AGENT_MIN_FREE_GB:-5}"
REPAIR=0

usage() {
  cat <<'USAGE'
Usage: repo_agent_health.sh [--repair]

Checks launchd, gh auth, disk space, stale locks, recent logs, active workers,
and the watched GitHub/Kanban queues. With --repair it enables/bootstrap missing
LaunchAgents and removes stale lock directories.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repair)
      REPAIR=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

mkdir -p "$(dirname "$LOG_FILE")"

log() {
  local level="$1" message="$2"
  printf '%s %s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$level" "$message" | tee -a "$LOG_FILE"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { log ERROR "missing-command name=$1"; return 1; }
}

uid="$(id -u)"
jobs=(
  "com.mikolaj92.hermes.repo-issue-intake|$HOME/Library/LaunchAgents/com.mikolaj92.hermes.repo-issue-intake.plist|$HOME/.hermes/logs/repo-issue-intake.log"
  "com.mikolaj92.hermes.repo-issue-to-pr-dispatch|$HOME/Library/LaunchAgents/com.mikolaj92.hermes.repo-issue-to-pr-dispatch.plist|$HOME/.hermes/logs/repo-issue-to-pr-dispatch.log"
  "com.mikolaj92.hermes.repo-pr-triage|$HOME/Library/LaunchAgents/com.mikolaj92.hermes.repo-pr-triage.plist|$HOME/.hermes/logs/repo-pr-triage.log"
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

failures=0
warnings=0

for cmd in gh hermes git python3 launchctl df find; do
  require_cmd "$cmd" || failures=$((failures + 1))
done

if gh auth status >/dev/null 2>&1; then
  log OK "gh-auth account=$(gh api user --jq .login 2>/dev/null || echo unknown)"
else
  log ERROR "gh-auth bad"
  failures=$((failures + 1))
fi

free_kb="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
free_gb=$((free_kb / 1024 / 1024))
if [[ "$free_gb" -lt "$MIN_FREE_GB" ]]; then
  log ERROR "disk-free-low home_gb=$free_gb min_gb=$MIN_FREE_GB"
  failures=$((failures + 1))
else
  log OK "disk-free home_gb=$free_gb"
fi

now="$(date +%s)"
for item in "${jobs[@]}"; do
  IFS='|' read -r label plist runtime_log <<<"$item"
  if launchctl print "user/$uid/$label" >/dev/null 2>&1; then
    launch_info="$(launchctl print "user/$uid/$label")"
    summary="$(printf '%s\n' "$launch_info" | awk '/runs =|last exit code =|run interval =/ {gsub(/^[ \t]+/, ""); printf "%s; ", $0}')"
    log OK "launchd label=$label ${summary:-loaded}"
    last_exit="$(printf '%s\n' "$launch_info" | awk -F '= ' '/last exit code =/ {gsub(/[^0-9-].*/, "", $2); print $2; exit}')"
    if [[ -n "$last_exit" && "$last_exit" != "0" ]]; then
      log WARN "launchd-last-exit-nonzero label=$label exit_code=$last_exit"
      warnings=$((warnings + 1))
    fi
  else
    log ERROR "launchd-missing label=$label plist=$plist"
    failures=$((failures + 1))
    if [[ "$REPAIR" == 1 && -f "$plist" ]]; then
      launchctl enable "user/$uid/$label" >/dev/null 2>&1 || true
      if launchctl bootstrap "user/$uid" "$plist" >/dev/null 2>&1; then
        log OK "launchd-repaired label=$label"
      else
        log ERROR "launchd-repair-failed label=$label"
      fi
    fi
  fi

  if [[ -f "$runtime_log" ]]; then
    mtime="$(stat -f %m "$runtime_log")"
    age=$((now - mtime))
    if [[ "$age" -gt "$MAX_LOG_AGE_SECONDS" ]]; then
      log WARN "stale-log label=$label age_seconds=$age path=$runtime_log"
      warnings=$((warnings + 1))
    else
      log OK "recent-log label=$label age_seconds=$age"
    fi
  else
    log WARN "missing-log label=$label path=$runtime_log"
    warnings=$((warnings + 1))
  fi
done

while IFS= read -r lock; do
  [[ -n "$lock" ]] || continue
  log WARN "stale-lock path=$lock"
  warnings=$((warnings + 1))
  if [[ "$REPAIR" == 1 ]]; then
    rmdir "$lock" 2>/dev/null && log OK "stale-lock-removed path=$lock" || true
  fi
done < <(find /tmp "$HOME/.hermes/worktrees" -maxdepth 4 -type d \( -name 'hermes-repo-*.lock' -o -name '.agent.lock' \) -mmin "+$STALE_LOCK_MINUTES" 2>/dev/null)

active_workers="$(pgrep -fl 'claude.*Hermes task' 2>/dev/null || true)"
if [[ -n "$active_workers" ]]; then
  while IFS= read -r line; do
    log OK "active-worker $line"
  done <<<"$active_workers"
else
  log OK "active-worker none"
fi

for entry in "${repos[@]}"; do
  IFS='|' read -r repo board <<<"$entry"
  open_prs="$(gh pr list --repo "$repo" --state open --json number --jq 'length' 2>/dev/null || echo unknown)"
  open_issues="$(gh issue list --repo "$repo" --state open --json number --jq 'length' 2>/dev/null || echo unknown)"
  board_counts="$(hermes kanban --board "$board" stats 2>/dev/null | tr '\n' ' ' | sed 's/  */ /g' || echo kanban-unavailable)"
  log OK "queue repo=$repo open_prs=$open_prs open_issues=$open_issues board=$board stats=${board_counts:-empty}"
done

log OK "summary failures=$failures warnings=$warnings repair=$REPAIR"
[[ "$failures" -eq 0 ]]
