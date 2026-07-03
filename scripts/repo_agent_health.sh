#!/usr/bin/env bash
set -euo pipefail

# Health/watchdog for the Hermes repo-agent launchd pipeline.

export HOME="${HOME:-/Users/mini-m4-main}"
export PATH="/Users/mini-m4-main/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

LOG_FILE="${HERMES_REPO_AGENT_HEALTH_LOG:-/Users/mini-m4-main/.hermes/logs/repo-agent-health.log}"
STALE_LOCK_MINUTES="${HERMES_STALE_LOCK_MINUTES:-180}"
MAX_LOG_AGE_SECONDS="${HERMES_REPO_AGENT_MAX_LOG_AGE_SECONDS:-1800}"
WORKER_TIMEOUT_SECONDS="${HERMES_REPO_AGENT_WORKER_TIMEOUT_SECONDS:-7200}"
MIN_FREE_GB="${HERMES_REPO_AGENT_MIN_FREE_GB:-5}"
REPAIR=0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/repo_agent_repos.sh"

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
  "com.mikolaj92.hermes.repo-agent-cleanup|$HOME/Library/LaunchAgents/com.mikolaj92.hermes.repo-agent-cleanup.plist|$HOME/.hermes/logs/repo-agent-cleanup.log"
  "com.mikolaj92.hermes.repo-agent-hermes-update|$HOME/Library/LaunchAgents/com.mikolaj92.hermes.repo-agent-hermes-update.plist|$HOME/.hermes/logs/repo-agent-hermes-update.log"
)
repos=()
while IFS= read -r repo_entry; do
  repos+=("$repo_entry")
done < <(repo_agent_repos)

failures=0
warnings=0

for cmd in gh hermes git python3 launchctl df find ps; do
  require_cmd "$cmd" || failures=$((failures + 1))
done

if gh auth status >/dev/null 2>&1; then
  log OK "gh-auth account=$(gh api user --jq .login 2>/dev/null || echo unknown)"
else
  log ERROR "gh-auth bad"
  failures=$((failures + 1))
fi

hermes_version="$(hermes --version 2>&1 || true)"
if grep -Eiq 'update available|commits? behind|new version' <<<"$hermes_version"; then
  compact_version="$(printf '%s' "$hermes_version" | tr '\n' ' ' | sed 's/  */ /g')"
  log WARN "hermes-update-available details=$(printf '%q' "$compact_version")"
  warnings=$((warnings + 1))
else
  log OK "hermes-version $(printf '%s' "$hermes_version" | head -n 1 | sed 's/  */ /g')"
fi

if [[ -f "$HOME/.hermes/cron/jobs.json" ]]; then
  duplicate_cron="$(python3 - "$HOME/.hermes/cron/jobs.json" <<'PY'
import json, sys
path = sys.argv[1]
watched = {"repo-pr-triage", "repo-issue-to-pr-dispatch"}
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    sys.exit(0)
names = []
for job in data.get("jobs", []):
    if job.get("enabled") and job.get("name") in watched:
        names.append(f"{job.get('name')}:{job.get('id')}")
if names:
    print(",".join(names))
PY
)"
  if [[ -n "$duplicate_cron" ]]; then
    log WARN "duplicate-hermes-cron jobs=$duplicate_cron"
    warnings=$((warnings + 1))
  fi
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
      log WARN "watchdog-worker-log-stale label=$label age_seconds=$age path=$runtime_log"
      warnings=$((warnings + 1))
    else
      log OK "recent-log label=$label age_seconds=$age"
      log OK "watchdog-worker-log-recent label=$label age_seconds=$age"
    fi
  else
    log WARN "missing-log label=$label path=$runtime_log"
    log WARN "watchdog-worker-log-missing label=$label path=$runtime_log"
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

active_worker_seen=0
while IFS= read -r pid_file; do
  [[ -n "$pid_file" ]] || continue
  lock="$(dirname "$pid_file")"
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    log OK "active-worker-lock pid=$pid path=$lock"
    worker_age="$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    [[ "$worker_age" =~ ^[0-9]+$ ]] || worker_age=0
    if [[ "$worker_age" -gt "$WORKER_TIMEOUT_SECONDS" ]]; then
      log WARN "watchdog-worker-runtime-timeout pid=$pid age_seconds=$worker_age timeout_seconds=$WORKER_TIMEOUT_SECONDS path=$lock"
      warnings=$((warnings + 1))
    else
      log OK "watchdog-worker-runtime-ok pid=$pid age_seconds=$worker_age timeout_seconds=$WORKER_TIMEOUT_SECONDS path=$lock"
    fi
    active_worker_seen=1
  else
    log WARN "dead-worker-lock pid=${pid:-missing} path=$lock"
    warnings=$((warnings + 1))
    if [[ "$REPAIR" == 1 ]]; then
      rm -f "$pid_file" 2>/dev/null || true
      rmdir "$lock" 2>/dev/null && log OK "dead-worker-lock-removed path=$lock" || true
    fi
  fi
done < <(find "$HOME/.hermes/worktrees" -maxdepth 5 -type f -path '*/.agent.lock/pid' 2>/dev/null)

if [[ "$active_worker_seen" == 0 ]]; then
  log OK "active-worker none"
  log OK "watchdog-worker-runtime-none"
fi

for entry in "${repos[@]}"; do
  IFS='|' read -r repo board clone_path repo_priority <<<"$entry"
  open_prs="$(gh pr list --repo "$repo" --state open --json number --jq 'length' 2>/dev/null || echo unknown)"
  open_issues="$(gh issue list --repo "$repo" --state open --json number --jq 'length' 2>/dev/null || echo unknown)"
  board_counts="$(hermes kanban --board "$board" stats 2>/dev/null | tr '\n' ' ' | sed 's/  */ /g' || echo kanban-unavailable)"
  log OK "queue repo=$repo open_prs=$open_prs open_issues=$open_issues board=$board stats=${board_counts:-empty}"
done

log OK "summary failures=$failures warnings=$warnings repair=$REPAIR"
[[ "$failures" -eq 0 ]]
