#!/usr/bin/env bash
set -euo pipefail

# Controlled Hermes updater. Skips updates while repo-agent workers are active.

export HOME="${HOME:-/Users/mini-m4-main}"
export PATH="/Users/mini-m4-main/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

DRY_RUN="${HERMES_REPO_AGENT_UPDATE_DRY_RUN:-1}"
LOG_FILE="${HERMES_REPO_AGENT_UPDATE_LOG:-/Users/mini-m4-main/.hermes/logs/repo-agent-hermes-update.log}"
LOCK_DIR="${HERMES_REPO_AGENT_UPDATE_LOCK_DIR:-/tmp/hermes-repo-agent-update.lock}"
WORKTREE_ROOT="${HERMES_WORKTREE_ROOT:-/Users/mini-m4-main/.hermes/worktrees/repo-fixer}"

usage() {
  cat <<'USAGE'
Usage: repo_agent_hermes_update.sh [--check|--live]

Checks for Hermes updates. In live mode it runs `hermes update --backup --yes`
only when no repo-agent worker lock is active. This avoids updating the agent
while it is in the middle of producing a PR.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check|--dry-run) DRY_RUN=1 ;;
    --live) DRY_RUN=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

mkdir -p "$(dirname "$LOG_FILE")"

log() {
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" | tee -a "$LOG_FILE"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { log "MISSING_COMMAND name=$1"; exit 1; }
}

require_cmd hermes
require_cmd find

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "LOCK_HELD path=$LOCK_DIR"
  exit 0
fi
cleanup_lock() { rmdir "$LOCK_DIR" 2>/dev/null || true; }
trap cleanup_lock EXIT

active_locks="$(find "$WORKTREE_ROOT" -maxdepth 5 -type f -path '*/.agent.lock/pid' 2>/dev/null || true)"
if [[ -n "$active_locks" ]]; then
  log "SKIP reason=active-worker-locks"
  exit 0
fi

check_output="$(hermes update --check 2>&1 || true)"
if grep -Eiq 'up.to.date|latest|no update|already' <<<"$check_output"; then
  log "NO_UPDATE details=$(printf '%q' "$(printf '%s' "$check_output" | tr '\n' ' ' | sed 's/  */ /g')")"
  exit 0
fi

log "UPDATE_AVAILABLE details=$(printf '%q' "$(printf '%s' "$check_output" | tr '\n' ' ' | sed 's/  */ /g')")"
if [[ "$DRY_RUN" == 1 ]]; then
  log "DRY_RUN action=skip-update"
  exit 0
fi

if hermes update --backup --yes >>"$LOG_FILE" 2>&1; then
  log "UPDATE_APPLIED"
else
  log "UPDATE_FAILED"
  exit 1
fi
