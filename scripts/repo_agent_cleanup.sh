#!/usr/bin/env bash
set -euo pipefail

# Clean controlled repo-agent worktrees for closed GitHub issues.

export HOME="${HOME:-/Users/mini-m4-main}"
export PATH="/Users/mini-m4-main/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

DRY_RUN="${HERMES_REPO_CLEANUP_DRY_RUN:-1}"
DELETE_LOCAL_BRANCHES="${HERMES_REPO_CLEANUP_DELETE_LOCAL_BRANCHES:-1}"
LOG_FILE="${HERMES_REPO_CLEANUP_LOG:-/Users/mini-m4-main/.hermes/logs/repo-agent-cleanup.log}"
LOCK_DIR="${HERMES_REPO_CLEANUP_LOCK_DIR:-/tmp/hermes-repo-agent-cleanup.lock}"
STALE_LOCK_MINUTES="${HERMES_STALE_LOCK_MINUTES:-180}"
WORKTREE_ROOT="${HERMES_WORKTREE_ROOT:-/Users/mini-m4-main/.hermes/worktrees/repo-fixer}"
MAINTENANCE_ASSIGNEE="${HERMES_KANBAN_MAINTENANCE_ASSIGNEE:-repo-agent-fixer}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/repo_agent_repos.sh"

usage() {
  cat <<'USAGE'
Usage: repo_agent_cleanup.sh [--dry-run|--live]

Removes clean repo-agent worktrees whose ai/fix issue is closed. Local ai/fix
branches are deleted only after their controlled worktree is gone. No remote
branches, PRs, or user-authored branches are deleted.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
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

require_cmd git
require_cmd gh
require_cmd python3

if [[ -d "$LOCK_DIR" ]]; then
  find "$LOCK_DIR" -maxdepth 0 -mmin "+$STALE_LOCK_MINUTES" -exec rmdir {} \; 2>/dev/null || true
fi
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "LOCK_HELD path=$LOCK_DIR"
  exit 0
fi
cleanup_lock() { rmdir "$LOCK_DIR" 2>/dev/null || true; }
trap cleanup_lock EXIT

REPOS=()
while IFS= read -r repo_entry; do
  REPOS+=("$repo_entry")
done < <(repo_agent_repos)

issue_from_branch() {
  local branch="$1" rest
  [[ "$branch" == ai/fix/* ]] || return 1
  rest="${branch#ai/fix/}"
  [[ "$rest" =~ ^([0-9]+) ]] || return 1
  printf '%s\n' "${BASH_REMATCH[1]}"
}

issue_state() {
  local repo="$1" issue="$2"
  gh issue view "$issue" --repo "$repo" --json state --jq .state 2>/dev/null || printf '%s\n' UNKNOWN
}

open_pr_for_branch() {
  local repo="$1" branch="$2"
  gh pr list --repo "$repo" --head "$branch" --state open --json number --jq 'length' 2>/dev/null || printf '%s\n' unknown
}

create_dirty_worktree_task() {
  local repo="$1" board="$2" path="$3" branch="$4" issue="$5"
  local title body key
  title="[maintenance] dirty worktree ${repo}#${issue}: ${branch}"
  key="maintenance-dirty-worktree:${repo}:${branch}"
  body="Repository: ${repo}
Issue: #${issue}
Branch: ${branch}
Worktree: ${path}

GitHub issue is closed, but cleanup could not remove this controlled worktree
because it contains local changes. Inspect the worktree, preserve anything
valuable, then clean or remove it so repo_agent_cleanup can finish."

  if ! command -v hermes >/dev/null 2>&1; then
    log "MAINTENANCE_TASK_SKIPPED repo=$repo issue=$issue branch=$branch reason=missing-command command=hermes"
    return 0
  fi
  hermes kanban --board "$board" create "$title" \
    --body "$body" \
    --assignee "$MAINTENANCE_ASSIGNEE" \
    --workspace "dir:${path}" \
    --priority 2 \
    --idempotency-key "$key" \
    --skill repo-gh-cli-policy \
    --json >/dev/null 2>&1 || true
}

worktree_rows() {
  local clone_path="$1"
  GIT_MASTER=1 git -C "$clone_path" worktree list --porcelain 2>/dev/null | python3 -c '
import sys
path = ""
branch = ""
for raw in sys.stdin:
    line = raw.rstrip("\n")
    if line.startswith("worktree "):
        if path and branch:
            print(f"{path}\t{branch}")
        path = line.removeprefix("worktree ")
        branch = ""
    elif line.startswith("branch refs/heads/"):
        branch = line.removeprefix("branch refs/heads/")
if path and branch:
    print(f"{path}\t{branch}")
'
}

processed=0
removed=0
skipped=0
failures=0

log "START mode=$([[ "$DRY_RUN" == 1 ]] && echo dry-run || echo live) delete_local_branches=$DELETE_LOCAL_BRANCHES"

for entry in "${REPOS[@]}"; do
  IFS='|' read -r repo board clone_path repo_priority <<<"$entry"
  if [[ ! -d "$clone_path/.git" ]]; then
    log "SKIP repo=$repo reason=missing-clone clone=$clone_path"
    skipped=$((skipped + 1))
    continue
  fi

  while IFS=$'\t' read -r path branch; do
    [[ -n "${path:-}" && -n "${branch:-}" ]] || continue
    [[ "$branch" == ai/fix/* ]] || continue
    if [[ "$path" != "$WORKTREE_ROOT/$board/"* && "$path" != "$clone_path/.worktrees/"* ]]; then
      continue
    fi
    processed=$((processed + 1))
    issue="$(issue_from_branch "$branch" || true)"
    if [[ -z "$issue" ]]; then
      log "SKIP repo=$repo branch=$branch path=$path reason=no-issue-number"
      skipped=$((skipped + 1))
      continue
    fi
    state="$(issue_state "$repo" "$issue")"
    open_prs="$(open_pr_for_branch "$repo" "$branch")"
    if [[ "$state" == "UNKNOWN" ]]; then
      log "KEEP repo=$repo issue=$issue branch=$branch reason=issue-state-unknown path=$path open_prs=$open_prs"
      skipped=$((skipped + 1))
      continue
    fi
    if [[ "$state" == "OPEN" || "$open_prs" != "0" ]]; then
      log "KEEP repo=$repo issue=$issue branch=$branch path=$path issue_state=$state open_prs=$open_prs"
      skipped=$((skipped + 1))
      continue
    fi
    if [[ -n "$(GIT_MASTER=1 git -C "$path" status --porcelain 2>/dev/null)" ]]; then
      log "SKIP repo=$repo issue=$issue branch=$branch path=$path reason=dirty-worktree"
      if [[ "$DRY_RUN" == 0 ]]; then
        create_dirty_worktree_task "$repo" "$board" "$path" "$branch" "$issue"
        log "MAINTENANCE_TASK_ENSURED repo=$repo issue=$issue branch=$branch path=$path"
      fi
      skipped=$((skipped + 1))
      continue
    fi
    if [[ "$DRY_RUN" == 1 ]]; then
      log "DRY_RUN repo=$repo issue=$issue action=remove-worktree branch=$branch path=$path issue_state=$state"
      continue
    fi
    if GIT_MASTER=1 git -C "$clone_path" worktree remove "$path" >/dev/null 2>&1; then
      log "WORKTREE_REMOVED repo=$repo issue=$issue branch=$branch path=$path issue_state=$state"
      removed=$((removed + 1))
      if [[ "$DELETE_LOCAL_BRANCHES" == 1 ]]; then
        if GIT_MASTER=1 git -C "$clone_path" branch --list "$branch" | grep -Fq "$branch"; then
          GIT_MASTER=1 git -C "$clone_path" branch -D "$branch" >/dev/null 2>&1 && \
            log "LOCAL_BRANCH_REMOVED repo=$repo branch=$branch" || true
        fi
      fi
    else
      log "REMOVE_FAILED repo=$repo issue=$issue branch=$branch path=$path"
      failures=$((failures + 1))
    fi
  done < <(worktree_rows "$clone_path")
done

log "DONE mode=$([[ "$DRY_RUN" == 1 ]] && echo dry-run || echo live) processed=$processed removed=$removed skipped=$skipped failures=$failures"
[[ "$failures" -eq 0 ]]
