#!/usr/bin/env bash
set -euo pipefail

# Trusted GitHub webhook entrypoint. This is not an HTTP listener and does not
# validate signatures; the caller owns authentication and payload storage.

export HOME="${HOME:-/Users/mini-m4-main}"
export PATH="/Users/mini-m4-main/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

DRY_RUN="${HERMES_REPO_AGENT_WEBHOOK_DRY_RUN:-1}"
COMMENT_ENABLED="${HERMES_REPO_AGENT_WEBHOOK_COMMENT:-0}"
LOG_FILE="${HERMES_REPO_AGENT_WEBHOOK_LOG:-/Users/mini-m4-main/.hermes/logs/repo-agent-webhook.log}"
EVENT=""
PAYLOAD=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'USAGE'
Usage: repo_agent_webhook.sh --event EVENT [--payload PATH] [--dry-run|--live] [--comment]

Maps trusted GitHub events to the same polling reconciliation scripts. Payload
is optional and logged only for traceability.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --event)
      shift
      [[ $# -gt 0 ]] || { echo "missing value for --event" >&2; exit 2; }
      EVENT="$1"
      ;;
    --event=*) EVENT="${1#--event=}" ;;
    --payload)
      shift
      [[ $# -gt 0 ]] || { echo "missing value for --payload" >&2; exit 2; }
      PAYLOAD="$1"
      ;;
    --payload=*) PAYLOAD="${1#--payload=}" ;;
    --dry-run) DRY_RUN=1 ;;
    --live) DRY_RUN=0 ;;
    --comment) COMMENT_ENABLED=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[[ -n "$EVENT" ]] || { echo "--event is required" >&2; usage >&2; exit 2; }
if [[ -n "$PAYLOAD" && ! -f "$PAYLOAD" ]]; then
  echo "payload not found: $PAYLOAD" >&2
  exit 2
fi

mkdir -p "$(dirname "$LOG_FILE")"

log() {
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" | tee -a "$LOG_FILE"
}

mode_arg="--dry-run"
if [[ "$DRY_RUN" == 0 ]]; then
  mode_arg="--live"
fi

run_step() {
  local name="$1"
  shift
  log "STEP_START name=$name event=$EVENT mode=$([[ "$DRY_RUN" == 1 ]] && echo dry-run || echo live)"
  set +e
  "$@"
  local rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    log "STEP_FAILED name=$name rc=$rc"
    failures=$((failures + 1))
  else
    log "STEP_DONE name=$name"
  fi
  return 0
}

payload_is_pr_comment() {
  [[ -n "$PAYLOAD" ]] || return 1
  python3 - "$PAYLOAD" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        payload = json.load(fh)
except Exception:
    sys.exit(1)

issue = payload.get("issue")
if isinstance(issue, dict) and isinstance(issue.get("pull_request"), dict):
    sys.exit(0)
sys.exit(1)
PY
}

triage_args=("$mode_arg")
if [[ "$COMMENT_ENABLED" == 1 ]]; then
  triage_args+=("--comment")
fi

failures=0

log "START event=$EVENT mode=$([[ "$DRY_RUN" == 1 ]] && echo dry-run || echo live) payload=${PAYLOAD:-none}"

case "$EVENT" in
  issues|issue_comment)
    if [[ "$EVENT" == "issue_comment" ]] && payload_is_pr_comment; then
      log "PAYLOAD_KIND kind=pr-comment"
      run_step pr-triage env HERMES_PR_TRIAGE_COMMENT="$COMMENT_ENABLED" "$SCRIPT_DIR/repo_pr_triage.sh" "${triage_args[@]}"
    else
      run_step intake "$SCRIPT_DIR/repo_issue_intake.sh" "$mode_arg"
      run_step dispatch env HERMES_ISSUE_TO_PR_RUN_OPENCODE=0 "$SCRIPT_DIR/repo_issue_to_pr_dispatch.sh" "$mode_arg"
      if [[ "$EVENT" == "issue_comment" ]]; then
        run_step pr-triage env HERMES_PR_TRIAGE_COMMENT="$COMMENT_ENABLED" "$SCRIPT_DIR/repo_pr_triage.sh" "${triage_args[@]}"
        run_step cleanup "$SCRIPT_DIR/repo_agent_cleanup.sh" "$mode_arg"
      fi
    fi
    ;;
  pull_request|pull_request_review|pull_request_review_comment|check_run|check_suite|status|workflow_run)
    run_step pr-triage env HERMES_PR_TRIAGE_COMMENT="$COMMENT_ENABLED" "$SCRIPT_DIR/repo_pr_triage.sh" "${triage_args[@]}"
    ;;
  ping)
    log "NOOP event=ping"
    ;;
  *)
    log "NOOP event=$EVENT reason=unsupported"
    ;;
esac

log "DONE event=$EVENT failures=$failures"
[[ "$failures" -eq 0 ]]
