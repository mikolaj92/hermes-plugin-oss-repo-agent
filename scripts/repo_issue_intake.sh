#!/usr/bin/env bash
set -euo pipefail

# Managed by local Hermes Agent repo-agent harness.
# Intake-only scheduler helper. Do not store tokens here.
# GitHub operations in this helper must use gh only.

export HOME="${HOME:-/Users/mini-m4-main}"
export PATH="/Users/mini-m4-main/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

LOG_FILE="${HERMES_INTAKE_LOG:-/Users/mini-m4-main/.hermes/logs/repo-issue-intake.log}"
LOCK_DIR="${HERMES_INTAKE_LOCK_DIR:-/tmp/hermes-repo-issue-intake.lock}"
LIMIT="${HERMES_INTAKE_LIMIT:-10}"
DRY_RUN="${HERMES_INTAKE_DRY_RUN:-0}"

usage() {
  printf '%s\n' \
    'Usage: repo_issue_intake.sh [--dry-run|--live] [--limit N]' \
    '' \
    'Poll selected GitHub issues with gh only and create idempotent Hermes Kanban intake tasks.' \
    'No PRs, merges, branches, or fixer dispatch are created by this script.'
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      ;;
    --live)
      DRY_RUN=0
      ;;
    --limit)
      shift
      [ "$#" -gt 0 ] || { printf '%s\n' 'missing value for --limit' >&2; exit 2; }
      LIMIT="$1"
      ;;
    --limit=*)
      LIMIT="${1#--limit=}"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if ! [[ "$LIMIT" =~ ^[0-9]+$ ]]; then
  printf 'HERMES_INTAKE_LIMIT/--limit must be numeric, got: %s\n' "$LIMIT" >&2
  exit 2
fi

mkdir -p "$(dirname "$LOG_FILE")"

log() {
  local message="$1"
  local timestamp
  timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf '%s %s\n' "$timestamp" "$message" | tee -a "$LOG_FILE"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    log "ERROR missing required command: $1"
    exit 127
  }
}

label_present() {
  local needle="$1"
  local haystack="$2"
  local line
  while IFS= read -r line; do
    [ "$line" = "$needle" ] && return 0
  done <<< "$haystack"
  return 1
}

json_escape_for_body() {
  python3 -c 'import json,sys; print(json.dumps(sys.stdin.read())[1:-1])'
}

require_command gh
require_command hermes
require_command git
require_command python3

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "SKIP already running lock=$LOCK_DIR"
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

readonly READY_LABEL='ai:ready'
readonly ISSUE_JQ='.[] | select(([.labels[].name] | index("ai:in-progress") | not) and ([.labels[].name] | index("ai:blocked") | not) and ([.labels[].name] | index("ai:pr-opened") | not)) | [.number, (.title | gsub("[\t\r\n]"; " ")), .url, ([.labels[].name] | join(", ")), (([.labels[].name] | index("ai:ready")) != null)] | @tsv'

repos=(
  'mikolaj92/Fala|mikolaj92-fala|/Users/mini-m4-main/Developer/hermes-repos/Fala'
  'mikolaj92/reviewkit|mikolaj92-reviewkit|/Users/mini-m4-main/Developer/hermes-repos/reviewkit'
  'mikolaj92/anonimizator3000|mikolaj92-anonimizator3000|/Users/mini-m4-main/Developer/hermes-repos/anonimizator3000'
  'mikolaj92/datasource-kit|mikolaj92-datasource-kit|/Users/mini-m4-main/Developer/hermes-repos/datasource-kit'
  'mikolaj92/splot|mikolaj92-splot|/Users/mini-m4-main/Developer/hermes-repos/splot'
  'mikolaj92/my-auth|mikolaj92-my-auth|/Users/mini-m4-main/Developer/hermes-repos/my-auth'
  'mikolaj92/my-usermanager|mikolaj92-my-usermanager|/Users/mini-m4-main/Developer/hermes-repos/my-usermanager'
  'mikolaj92/msds-portal|mikolaj92-msds-portal|/Users/mini-m4-main/Developer/hermes-repos/msds-portal'
  'mikolaj92/swift-openapi-dynamic|mikolaj92-swift-openapi-dynamic|/Users/mini-m4-main/Developer/hermes-repos/swift-openapi-dynamic'
  'mikolaj92/OpenAPITransportKit|mikolaj92-openapi-transport-kit|/Users/mini-m4-main/Developer/hermes-repos/OpenAPITransportKit'
)

log "START dry_run=$DRY_RUN limit=$LIMIT repos=${#repos[@]}"

processed=0
created_or_reused=0
skipped=0
failures=0

for entry in "${repos[@]}"; do
  IFS='|' read -r repo board clone_path <<< "$entry"

  if [ ! -d "$clone_path/.git" ]; then
    log "ERROR repo=$repo missing clone git dir path=$clone_path"
    failures=$((failures + 1))
    continue
  fi

  if ! GIT_MASTER=1 git -C "$clone_path" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    log "ERROR repo=$repo clone is not a git worktree path=$clone_path"
    failures=$((failures + 1))
    continue
  fi

  required_labels="$(gh label list --repo "$repo" --limit 200 --json name --jq '.[].name')"
  if ! label_present "$READY_LABEL" "$required_labels"; then
    log "ERROR repo=$repo required label missing: $READY_LABEL"
    failures=$((failures + 1))
    continue
  fi

  if ! issue_rows="$(gh issue list --repo "$repo" --state open --limit "$LIMIT" --json number,title,url,labels --jq "$ISSUE_JQ")"; then
    log "ERROR repo=$repo gh issue list failed"
    failures=$((failures + 1))
    continue
  fi

  if [ -z "$issue_rows" ]; then
    log "NO_ELIGIBLE_ISSUES repo=$repo board=$board"
    continue
  fi

  while IFS=$'\t' read -r number title url labels has_ready; do
    [ -n "${number:-}" ] || continue
    processed=$((processed + 1))

    key="github-issue:${repo}:${number}"
    task_title="[issue] ${repo}#${number}: ${title}"
    body="GitHub issue: ${url}
Repository: ${repo}
Issue: #${number}
Labels at intake: ${labels:-none}

Intake-only instructions:
- Triage this issue using repo-gh-cli-policy and repo-audit-finding-format.
- Use gh for every GitHub operation.
- Do not create a PR, merge, branch, or fixer worktree from this intake task.
- If the issue is actionable, prepare a finding and leave follow-up execution to an explicitly approved fixer task."

    if [ "$DRY_RUN" = "1" ] || [ "$DRY_RUN" = "true" ]; then
      if [ "$has_ready" = "true" ]; then
        log "DRY_RUN repo=$repo issue=$number ready_label=already-present key=$key"
      else
        log "DRY_RUN repo=$repo issue=$number would_add_label=$READY_LABEL key=$key"
      fi
      log "DRY_RUN repo=$repo issue=$number would_create_kanban board=$board title=$(printf '%s' "$task_title" | json_escape_for_body)"
      continue
    fi

    if [ "$has_ready" != "true" ]; then
      gh issue edit "$number" --repo "$repo" --add-label "$READY_LABEL" >/dev/null
      log "LABEL_ADDED repo=$repo issue=$number label=$READY_LABEL"
    else
      log "LABEL_PRESENT repo=$repo issue=$number label=$READY_LABEL"
    fi

    hermes kanban --board "$board" create "$task_title" \
      --body "$body" \
      --assignee repo-orchestrator \
      --workspace "dir:${clone_path}" \
      --priority 1 \
      --idempotency-key "$key" \
      --skill repo-gh-cli-policy \
      --skill repo-audit-finding-format \
      --json >/dev/null
    created_or_reused=$((created_or_reused + 1))
    log "KANBAN_TASK_ENSURED repo=$repo issue=$number board=$board key=$key"
  done <<< "$issue_rows"
done

if [ "$DRY_RUN" = "1" ] || [ "$DRY_RUN" = "true" ]; then
  log "DONE mode=dry-run processed=$processed skipped=$skipped failures=$failures"
else
  log "DONE mode=live processed=$processed created_or_reused=$created_or_reused skipped=$skipped failures=$failures"
fi

if [ "$failures" -gt 0 ]; then
  exit 1
fi
