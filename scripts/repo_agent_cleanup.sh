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
BASE_BRANCH="${HERMES_REPO_AGENT_BASE_BRANCH:-main}"
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

RECEIPT_DIR="${HERMES_REPO_AGENT_CLEANUP_RECEIPT_DIR:-${HERMES_REPO_AGENT_MERGE_RECEIPT_DIR:-}}"
QUARANTINE_DIR="${HERMES_REPO_CLEANUP_QUARANTINE_DIR:-${RECEIPT_DIR:+$RECEIPT_DIR/quarantine}}"
CLEANUP_OUTCOME_DIR="${HERMES_REPO_AGENT_CLEANUP_OUTCOME_DIR:-${RECEIPT_DIR:+$RECEIPT_DIR/cleanup-outcomes}}"

write_cleanup_outcome_receipt() {
  local status="$1" repo="$2" issue="$3" branch="$4" clone_path="$5" worktree_path="$6" task_id="$7" merge_sha="$8" origin_main_sha="$9" base_sha="${10}" branch_deleted="${11}"
  local path tmp
  [[ -n "$CLEANUP_OUTCOME_DIR" ]] || return 1
  mkdir -p "$CLEANUP_OUTCOME_DIR" || return 1
  path="$CLEANUP_OUTCOME_DIR/$(printf '%s' "$repo-$issue-$branch" | tr '/:' '__').json"
  tmp="$(mktemp "$CLEANUP_OUTCOME_DIR/.cleanup.XXXXXX")" || return 1
  if ! CLEANUP_TMP="$tmp" CLEANUP_PATH="$path" python3 - "$status" "$repo" "$issue" "$branch" "$clone_path" "$worktree_path" "$task_id" "$merge_sha" "$origin_main_sha" "$base_sha" "$branch_deleted" <<'PY'
import datetime as dt
import json
import os
import sys

tmp = os.environ["CLEANUP_TMP"]
path = os.environ["CLEANUP_PATH"]
status, repo, issue, branch, clone_path, worktree_path, task_id, merge_sha, origin_main_sha, base_sha, branch_deleted = sys.argv[1:12]
if status not in {"CLEANUP_CONFIRMED", "NO_TARGET_RECONCILED"}:
    raise RuntimeError("invalid cleanup outcome")
expected = {
    "version": 1,
    "status": status,
    "repo": repo,
    "issue": int(issue),
    "branch": branch,
    "clone_path": clone_path,
    "worktree_path": worktree_path,
    "task_id": task_id,
    "mergeSha": merge_sha,
    "originMainSha": origin_main_sha,
    "baseSha": base_sha,
    "local_branch_deleted": branch_deleted == "1",
    "remote_branch_deleted": False,
}
payload = dict(expected, cleaned_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
try:
    with open(tmp, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    with open(tmp, encoding="utf-8") as stream:
        readback = json.load(stream)
    if any(readback.get(key) != value for key, value in expected.items()):
        raise RuntimeError("cleanup receipt temp read-back mismatch")
    try:
        os.link(tmp, path)
    except FileExistsError:
        with open(path, encoding="utf-8") as stream:
            existing = json.load(stream)
        if any(existing.get(key) != value for key, value in expected.items()):
            raise RuntimeError("conflicting cleanup receipt already exists")
    with open(path, encoding="utf-8") as stream:
        published = json.load(stream)
    if any(published.get(key) != value for key, value in expected.items()):
        raise RuntimeError("cleanup receipt publication read-back mismatch")
    os.unlink(tmp)
    directory = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
except Exception:
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    raise
PY
  then
    rm -f "$tmp" || true
    log "CLEANUP_RECEIPT_WRITE_FAILED status=$status repo=$repo issue=$issue branch=$branch"
    return 1
  fi
  log "CLEANUP_RECEIPT_WRITTEN status=$status repo=$repo issue=$issue branch=$branch path=$path"
}

controlled_worktree_path() {
  local clone_path="$1" path="$2" path_real root_real clone_worktrees_real
  path_real="$(cd "$path" 2>/dev/null && pwd -P || true)"
  root_real="$(cd "$WORKTREE_ROOT" 2>/dev/null && pwd -P || true)"
  clone_worktrees_real="$(cd "$clone_path/.worktrees" 2>/dev/null && pwd -P || true)"
  [[ -n "$path_real" &&
    ( ( -n "$root_real" && "$path_real" == "$root_real/"* ) ||
      ( -n "$clone_worktrees_real" && "$path_real" == "$clone_worktrees_real/"* ) ) ]]
}

receipt_field() {
  local payload="$1" field="$2"
  RECEIPT_JSON="$payload" RECEIPT_FIELD="$field" python3 - <<'PY'
import json, os
try:
    value = json.loads(os.environ["RECEIPT_JSON"])
    value = value.get(os.environ["RECEIPT_FIELD"], "")
    if isinstance(value, (dict, list)):
        print(json.dumps(value, separators=(",", ":")))
    else:
        print(value)
except Exception:
    raise SystemExit(1)
PY
}

quarantine_receipt() {
  local path="$1" reason="$2" target tmp
  [[ -n "$QUARANTINE_DIR" ]] || { log "KEEP_RECEIPT path=$path reason=$reason"; return 0; }
  if ! mkdir -p "$QUARANTINE_DIR"; then
    log "QUARANTINE_WRITE_FAILED path=$path reason=mkdir"
    return 1
  fi
  tmp="$(mktemp "$QUARANTINE_DIR/.quarantine.XXXXXX")" || {
    log "QUARANTINE_WRITE_FAILED path=$path reason=mktemp"
    return 1
  }
  if ! QUARANTINE_TMP="$tmp" QUARANTINE_DIR="$QUARANTINE_DIR" QUARANTINE_SOURCE="$path" python3 - "$reason" <<'PY'
import hashlib
import json
import os
import sys

source = os.environ["QUARANTINE_SOURCE"]
directory = os.environ["QUARANTINE_DIR"]
with open(source, "rb") as stream:
    content = stream.read()
digest = hashlib.sha256(content).hexdigest()
stem = os.path.basename(source).removesuffix(".json")
target = os.path.join(directory, f"quarantine-{stem}-{digest[:32]}.json")
temporary = os.environ["QUARANTINE_TMP"]
with open(temporary, "wb") as stream:
    stream.write(content)
    stream.flush()
    os.fsync(stream.fileno())
try:
    os.link(temporary, target)
except FileExistsError:
    with open(target, "rb") as stream:
        if hashlib.sha256(stream.read()).hexdigest() != digest:
            raise RuntimeError("quarantine destination collision")
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
fd = os.open(directory, os.O_RDONLY)
try:
    os.fsync(fd)
finally:
    os.close(fd)
os.unlink(source)
print(target)
PY
  then
    rm -f "$tmp" || true
    log "QUARANTINE_WRITE_FAILED path=$path reason=write"
    return 1
  fi
  log "RECEIPT_QUARANTINED path=$path target=$QUARANTINE_DIR reason=$reason"
}

cleanup_receipt_worktree() {
  local repo="$1" board="$2" clone_path="$3" branch="$4" issue="$5" task_id="$6" merge_sha="$7" origin_base_sha="$8" base_sha="$9"
  local path wt_branch pid_file pid status_output status_rc branch_check_rc branch_deleted found=0
  pid_file="$WORKTREE_ROOT/$board/.agent.lock/pid"
  if [[ -f "$pid_file" ]]; then
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
      log "KEEP repo=$repo branch=$branch reason=active-worker-lock"
      return 0
    fi
  fi
  while IFS=$'\t' read -r path wt_branch; do
    [[ "$wt_branch" == "$branch" ]] || continue
    found=1
    controlled_worktree_path "$clone_path" "$path" || {
      log "KEEP repo=$repo branch=$branch path=$path reason=worktree-provenance-unverifiable"
      return 0
    }
    status_rc=0
    status_output="$(GIT_MASTER=1 git -C "$path" status --porcelain 2>/dev/null)" || status_rc=$?
    if [[ "$status_rc" -ne 0 ]]; then
      log "KEEP repo=$repo branch=$branch path=$path reason=status-check-failed rc=$status_rc"
      failures=$((failures + 1))
      return 0
    fi
    if [[ -n "$status_output" ]]; then
      log "KEEP repo=$repo branch=$branch path=$path reason=dirty-worktree"
      return 0
    fi
    if [[ "$DRY_RUN" == 1 ]]; then
      log "DRY_RUN repo=$repo branch=$branch path=$path reason=terminal-receipt"
      return 0
    fi
    if ! controlled_worktree_path "$clone_path" "$path"; then
      log "KEEP repo=$repo branch=$branch path=$path reason=cleanup-race-before-remove"
      return 0
    fi
    if GIT_MASTER=1 git -C "$clone_path" worktree remove "$path" >/dev/null 2>&1; then
      log "WORKTREE_REMOVED repo=$repo branch=$branch path=$path reason=terminal-receipt"
      branch_deleted=0
      if [[ "$DELETE_LOCAL_BRANCHES" == 1 ]]; then
        branch_check_rc=0
        GIT_MASTER=1 git -C "$clone_path" show-ref --verify --quiet "refs/heads/$branch" || branch_check_rc=$?
        if [[ "$branch_check_rc" -gt 1 ]]; then
          log "KEEP repo=$repo branch=$branch reason=branch-check-failed rc=$branch_check_rc"
          failures=$((failures + 1))
          return 0
        fi
        if [[ "$branch_check_rc" -eq 0 ]]; then
          if ! GIT_MASTER=1 git -C "$clone_path" branch -D "$branch" >/dev/null 2>&1; then
            log "LOCAL_BRANCH_DELETE_FAILED repo=$repo branch=$branch"
            failures=$((failures + 1))
            return 0
          fi
          branch_check_rc=0
          GIT_MASTER=1 git -C "$clone_path" show-ref --verify --quiet "refs/heads/$branch" || branch_check_rc=$?
          if [[ "$branch_check_rc" -eq 0 ]]; then
            log "LOCAL_BRANCH_DELETE_FAILED repo=$repo branch=$branch reason=still-present"
            failures=$((failures + 1))
            return 0
          elif [[ "$branch_check_rc" -gt 1 ]]; then
            log "LOCAL_BRANCH_DELETE_FAILED repo=$repo branch=$branch reason=verify-failed rc=$branch_check_rc"
            failures=$((failures + 1))
            return 0
          fi
          branch_deleted=1
          log "LOCAL_BRANCH_REMOVED repo=$repo branch=$branch"
        fi
      fi
      if write_cleanup_outcome_receipt "CLEANUP_CONFIRMED" "$repo" "$issue" "$branch" "$clone_path" "$path" "$task_id" "$merge_sha" "$origin_base_sha" "$base_sha" "$branch_deleted"; then
        removed=$((removed + 1))
      else
        failures=$((failures + 1))
      fi
    else
      log "REMOVE_FAILED repo=$repo branch=$branch path=$path reason=terminal-receipt"
      failures=$((failures + 1))
    fi
    return 0
  done < <(worktree_rows "$clone_path")
  if [[ "$found" -eq 0 && "$DRY_RUN" == 0 ]]; then
    if write_cleanup_outcome_receipt "NO_TARGET_RECONCILED" "$repo" "$issue" "$branch" "$clone_path" "" "$task_id" "$merge_sha" "$origin_base_sha" "$base_sha" "0"; then
      removed=$((removed + 1))
      log "NO_TARGET_RECONCILED repo=$repo issue=$issue branch=$branch"
    else
      failures=$((failures + 1))
      log "KEEP repo=$repo branch=$branch reason=worktree-not-found-receipt-failed"
    fi
  else
    log "KEEP repo=$repo branch=$branch reason=worktree-not-found"
  fi
}

process_receipts_for_repo() {
  local repo="$1" board="$2" clone_path="$3" path payload receipt_repo issue phase branch state open_prs merge_sha origin_base_sha current_origin task_id base_sha
  [[ -n "$RECEIPT_DIR" && -d "$RECEIPT_DIR" ]] || return 0
  shopt -s nullglob
  for path in "$RECEIPT_DIR"/*.json; do
    payload="$(cat "$path" 2>/dev/null || true)"
    receipt_repo="$(receipt_field "$payload" repo 2>/dev/null || true)"
    if [[ -z "$receipt_repo" ]]; then
      quarantine_receipt "$path" "malformed-repo" || failures=$((failures + 1))
      continue
    fi
    [[ "$receipt_repo" == "$repo" ]] || continue
    issue="$(receipt_field "$payload" issue 2>/dev/null || true)"
    branch="$(receipt_field "$payload" branch 2>/dev/null || true)"
    if [[ -z "$branch" ]]; then
      quarantine_receipt "$path" "malformed-branch" || failures=$((failures + 1))
      continue
    fi
    phase="$(receipt_field "$payload" phase 2>/dev/null || true)"
    if [[ "$phase" != "ISSUE_CLOSED_CONFIRMED" ]]; then
      quarantine_receipt "$path" "phase=${phase:-missing}" || failures=$((failures + 1))
      continue
    fi
    if [[ ! "$issue" =~ ^[0-9]+$ ]]; then
      quarantine_receipt "$path" "invalid-issue" || failures=$((failures + 1))
      continue
    fi
    state="$(issue_state "$repo" "$issue")"
    open_prs="$(open_pr_for_branch "$repo" "$branch")"
    if [[ "$state" != "CLOSED" || "$open_prs" != "0" ]]; then
      quarantine_receipt "$path" "issue_state=$state open_prs=$open_prs" || failures=$((failures + 1))
      continue
    fi
    merge_sha="$(receipt_field "$payload" mergeSha 2>/dev/null || true)"
    origin_base_sha="$(receipt_field "$payload" originMainSha 2>/dev/null || true)"
    current_origin="$(GIT_MASTER=1 git -C "$clone_path" rev-parse "refs/remotes/origin/$BASE_BRANCH" 2>/dev/null || true)"
    if [[ ! "$merge_sha" =~ ^[0-9a-fA-F]{40}$ || ! "$origin_base_sha" =~ ^[0-9a-fA-F]{40}$ || -z "$current_origin" || "$origin_base_sha" != "$current_origin" ]] ||
      ! GIT_MASTER=1 git -C "$clone_path" merge-base --is-ancestor "$merge_sha" "$current_origin" >/dev/null 2>&1; then
      log "KEEP repo=$repo issue=$issue branch=$branch reason=merge-provenance-unverifiable base=$BASE_BRANCH"
      continue
    fi
    task_id="$(receipt_field "$payload" task_id 2>/dev/null || true)"
    base_sha="$(receipt_field "$payload" preMergeBaseSha 2>/dev/null || true)"
    cleanup_receipt_worktree "$repo" "$board" "$clone_path" "$branch" "$issue" "$task_id" "$merge_sha" "$origin_base_sha" "$base_sha"
  done
  shopt -u nullglob
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
  process_receipts_for_repo "$repo" "$board" "$clone_path"
  worker_pid_file="$WORKTREE_ROOT/$board/.agent.lock/pid"
  if [[ -f "$worker_pid_file" ]] && worker_pid="$(cat "$worker_pid_file" 2>/dev/null || true)" && [[ "$worker_pid" =~ ^[0-9]+$ ]] && kill -0 "$worker_pid" 2>/dev/null; then
    log "KEEP repo=$repo board=$board reason=active-worker-lock"
    continue
  fi

  while IFS=$'\t' read -r path branch; do
    [[ -n "${path:-}" && -n "${branch:-}" ]] || continue
    [[ "$branch" == ai/fix/* ]] || continue
    processed=$((processed + 1))
    log "KEEP repo=$repo branch=$branch path=$path reason=no-matching-terminal-receipt"
    skipped=$((skipped + 1))
  done < <(worktree_rows "$clone_path")
done

log "DONE mode=$([[ "$DRY_RUN" == 1 ]] && echo dry-run || echo live) processed=$processed removed=$removed skipped=$skipped failures=$failures"
[[ "$failures" -eq 0 ]]
