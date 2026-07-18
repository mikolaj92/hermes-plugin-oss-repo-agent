#!/usr/bin/env bash
set -euo pipefail

# Managed by local Hermes Agent repo-agent harness.
# Intake-only scheduler helper. Do not store tokens here.
# GitHub operations in this helper must use gh only.

export HOME="${HOME:-/Users/mini-m4-main}"
export PATH="${PATH:-/Users/mini-m4-main/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}"

LOG_FILE="${HERMES_INTAKE_LOG:-/Users/mini-m4-main/.hermes/logs/repo-issue-intake.log}"
LOCK_DIR="${HERMES_INTAKE_LOCK_DIR:-/tmp/hermes-repo-issue-intake.lock}"
LIMIT="${HERMES_INTAKE_LIMIT:-10}"
DRY_RUN="${HERMES_INTAKE_DRY_RUN:-0}"
CLAIM_ASSIGNEE="${HERMES_REPO_AGENT_ASSIGNEE:-mikolaj92}"
KANBAN_INTAKE_ASSIGNEE="${HERMES_KANBAN_INTAKE_ASSIGNEE:-repo-agent-intake}"
QUEUE_SOURCE="${HERMES_REPO_AGENT_SOURCE:-github}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/repo_agent_repos.sh"

usage() {
  printf '%s\n' \
    'Usage: repo_issue_intake.sh [--dry-run|--live] [--limit N]' \
    '' \
    'Poll selected GitHub issues with gh only and claim them for the repo-agent.' \
    'In HERMES_REPO_AGENT_SOURCE=kanban mode it also creates idempotent Hermes Kanban intake tasks.'
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

existing_issue_task() {
  local tasks_json="$1" repo="$2" issue="$3"
  TASKS_JSON="$tasks_json" python3 - "$repo" "$issue" <<'PY'
import json, os, sys
repo = sys.argv[1]
issue = sys.argv[2]
title_needle = f"{repo}#{issue}"
repo_line = f"Repository: {repo}"
issue_line = f"Issue: #{issue}"
try:
    tasks = json.loads(os.environ.get("TASKS_JSON", "[]"))
except Exception:
    sys.exit(1)
for task in tasks if isinstance(tasks, list) else []:
    status = str(task.get("status") or "")
    if status == "done":
        continue
    title = str(task.get("title") or "")
    if not title.startswith(("[issue]", "[fix-pr]", "[fix-pr-review]")):
        continue
    body = str(task.get("body") or "")
    if title_needle in title or (repo_line in body and issue_line in body):
        sys.exit(0)
sys.exit(1)
PY
}

require_command gh
require_command git
require_command python3
if [ "$QUEUE_SOURCE" = "kanban" ]; then
  require_command hermes
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "SKIP already running lock=$LOCK_DIR"
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

readonly READY_LABEL='ai:ready'
readonly ISSUE_JQ='.[] | select(([.labels[].name] | index("ai:in-progress") | not) and ([.labels[].name] | index("ai:blocked") | not) and ([.labels[].name] | index("ai:pr-opened") | not)) | [.number, (.title | gsub("[\t\r\n]"; " ")), .url, ([.labels[].name] | join(", ")), (([.labels[].name] | index("ai:ready")) != null), (([.assignees[].login] | join(",")) // "")] | @tsv'

repos=()
while IFS= read -r repo_entry; do
  repos+=("$repo_entry")
done < <(repo_agent_repos)

log "START dry_run=$DRY_RUN limit=$LIMIT repos=${#repos[@]}"

processed=0
created_or_reused=0
skipped=0
failures=0

for entry in "${repos[@]}"; do
  IFS='|' read -r repo board clone_path repo_priority <<< "$entry"

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

  ready_label_exists=0
  required_labels="$(gh label list --repo "$repo" --limit 200 --json name --jq '.[].name')"
  if label_present "$READY_LABEL" "$required_labels"; then
    ready_label_exists=1
  else
    log "WARN repo=$repo ready_label_missing=$READY_LABEL action=claim-and-kanban-without-label"
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

  board_tasks_json="[]"
  if [ "$QUEUE_SOURCE" = "kanban" ] && [ "$DRY_RUN" != "1" ] && [ "$DRY_RUN" != "true" ]; then
    board_tasks_json="$(hermes kanban --board "$board" list --json --sort created-desc 2>/dev/null || printf '[]')"
  fi

  while IFS=$'\t' read -r number title url labels has_ready existing_assignees; do
    [ -n "${number:-}" ] || continue
    processed=$((processed + 1))
    if [ -n "${existing_assignees:-}" ] && [ "$existing_assignees" != "$CLAIM_ASSIGNEE" ]; then
      skipped=$((skipped + 1))
      log "ISSUE_SKIPPED_NOT_READY repo=$repo issue=$number assignee=$existing_assignees expected=$CLAIM_ASSIGNEE"
      continue
    fi

    key="github-issue:${repo}:${number}"
    task_title="[issue] ${repo}#${number}: ${title}"
    kanban_priority="$(repo_agent_kanban_priority_for_text "$title $labels")"
    body="GitHub issue: ${url}
Repository: ${repo}
Issue: #${number}
Labels at intake: ${labels:-none}
Mapping: GitHub labels/title -> Kanban priority ${kanban_priority}

Intake-only instructions:
- Triage this issue using repo-gh-cli-policy and repo-audit-finding-format.
- Use gh for every GitHub operation.
- Do not create a PR, merge, branch, or fixer worktree from this intake task.
- If the issue is actionable, prepare a finding and leave follow-up execution to an explicitly approved fixer task."

    if [ "$DRY_RUN" = "1" ] || [ "$DRY_RUN" = "true" ]; then
      if [ "$has_ready" = "true" ]; then
        log "DRY_RUN repo=$repo issue=$number ready_label=already-present key=$key"
      elif [ "$ready_label_exists" = "1" ]; then
        log "DRY_RUN repo=$repo issue=$number would_add_label=$READY_LABEL key=$key"
      else
        log "DRY_RUN repo=$repo issue=$number label_skipped_missing=$READY_LABEL key=$key"
      fi
      if [ -n "$CLAIM_ASSIGNEE" ]; then
        log "DRY_RUN repo=$repo issue=$number would_assign=$CLAIM_ASSIGNEE key=$key"
      fi
      if [ "$QUEUE_SOURCE" = "kanban" ]; then
        log "DRY_RUN repo=$repo issue=$number would_create_kanban board=$board title=$(printf '%s' "$task_title" | json_escape_for_body)"
      else
        log "DRY_RUN repo=$repo issue=$number source=github action=would-claim"
      fi
      continue
    fi

    if [ "$QUEUE_SOURCE" = "kanban" ] && existing_issue_task "$board_tasks_json" "$repo" "$number"; then
      skipped=$((skipped + 1))
      log "KANBAN_TASK_EXISTS repo=$repo issue=$number board=$board action=skip-create"
      continue
    fi

    if [ -n "$CLAIM_ASSIGNEE" ]; then
      if gh issue edit "$number" --repo "$repo" --add-assignee "$CLAIM_ASSIGNEE" >/dev/null; then
        log "ISSUE_ASSIGNED repo=$repo issue=$number assignee=$CLAIM_ASSIGNEE"
      else
        log "ERROR repo=$repo issue=$number assignee_failed=$CLAIM_ASSIGNEE"
        failures=$((failures + 1))
        continue
      fi
    fi

    if [ "$has_ready" != "true" ] && [ "$ready_label_exists" = "1" ]; then
      gh issue edit "$number" --repo "$repo" --add-label "$READY_LABEL" >/dev/null
      log "LABEL_ADDED repo=$repo issue=$number label=$READY_LABEL"
    else
      log "LABEL_PRESENT_OR_SKIPPED repo=$repo issue=$number label=$READY_LABEL exists=$ready_label_exists"
    fi

    if [ "$QUEUE_SOURCE" != "kanban" ]; then
      created_or_reused=$((created_or_reused + 1))
      log "GITHUB_ISSUE_READY repo=$repo issue=$number source=github action=claimed"
      continue
    fi

    hermes kanban --board "$board" create "$task_title" \
      --body "$body" \
      --assignee "$KANBAN_INTAKE_ASSIGNEE" \
      --workspace "dir:${clone_path}" \
      --priority "$kanban_priority" \
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
