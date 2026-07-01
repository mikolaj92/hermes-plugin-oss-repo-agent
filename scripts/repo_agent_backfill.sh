#!/usr/bin/env bash
set -euo pipefail

# Reconcile GitHub and Hermes Kanban without starting code workers.

export HOME="${HOME:-/Users/mini-m4-main}"
export PATH="/Users/mini-m4-main/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

DRY_RUN="${HERMES_REPO_AGENT_BACKFILL_DRY_RUN:-1}"
COMMENT_ENABLED="${HERMES_REPO_AGENT_BACKFILL_COMMENT:-0}"
LOG_FILE="${HERMES_REPO_AGENT_BACKFILL_LOG:-/Users/mini-m4-main/.hermes/logs/repo-agent-backfill.log}"
LOCK_DIR="${HERMES_REPO_AGENT_BACKFILL_LOCK_DIR:-/tmp/hermes-repo-agent-backfill.lock}"
STALE_LOCK_MINUTES="${HERMES_STALE_LOCK_MINUTES:-180}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'USAGE'
Usage: repo_agent_backfill.sh [--dry-run|--live] [--comment]

Runs intake, dispatch, PR triage, and cleanup reconciliation. It never starts
Claude/OpenCode workers; it only repairs Kanban/GitHub drift and local cleanup.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --live) DRY_RUN=0 ;;
    --comment) COMMENT_ENABLED=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

mkdir -p "$(dirname "$LOG_FILE")"

log() {
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" | tee -a "$LOG_FILE"
}

if [[ -d "$LOCK_DIR" ]]; then
  find "$LOCK_DIR" -maxdepth 0 -mmin "+$STALE_LOCK_MINUTES" -exec rmdir {} \; 2>/dev/null || true
fi
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "LOCK_HELD path=$LOCK_DIR"
  exit 0
fi
cleanup() { rmdir "$LOCK_DIR" 2>/dev/null || true; }
trap cleanup EXIT

failures=0
mode_arg="--dry-run"
if [[ "$DRY_RUN" == 0 ]]; then
  mode_arg="--live"
fi

run_step() {
  local name="$1"
  shift
  log "STEP_START name=$name mode=$([[ "$DRY_RUN" == 1 ]] && echo dry-run || echo live)"
  if "$@"; then
    log "STEP_DONE name=$name"
  else
    log "STEP_FAILED name=$name"
    failures=$((failures + 1))
  fi
}

triage_args=("$mode_arg")
if [[ "$COMMENT_ENABLED" == 1 ]]; then
  triage_args+=("--comment")
fi

log "START mode=$([[ "$DRY_RUN" == 1 ]] && echo dry-run || echo live) comment=$COMMENT_ENABLED"
run_step intake "$SCRIPT_DIR/repo_issue_intake.sh" "$mode_arg"
run_step dispatch env HERMES_ISSUE_TO_PR_RUN_OPENCODE=0 "$SCRIPT_DIR/repo_issue_to_pr_dispatch.sh" "$mode_arg"
run_step pr-triage env HERMES_PR_TRIAGE_COMMENT="$COMMENT_ENABLED" "$SCRIPT_DIR/repo_pr_triage.sh" "${triage_args[@]}"
run_step cleanup "$SCRIPT_DIR/repo_agent_cleanup.sh" "$mode_arg"
log "DONE failures=$failures"

[[ "$failures" -eq 0 ]]
