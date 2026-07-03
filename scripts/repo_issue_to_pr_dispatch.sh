#!/usr/bin/env bash
set -euo pipefail

# Managed by the Hermes repo-agent harness.
# Purpose: dry-run-first issue triage -> explicit PR-fix work dispatcher.
# Safety: keep intake separate; create PR work only from explicit [fix-pr] tasks.

export HOME="${HOME:-/Users/mini-m4-main}"
export PATH="/Users/mini-m4-main/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

DRY_RUN="${HERMES_ISSUE_TO_PR_DRY_RUN:-1}"
MAX_PER_BOARD="${HERMES_ISSUE_TO_PR_MAX_PER_BOARD:-20}"
RUN_OPENCODE="${HERMES_ISSUE_TO_PR_RUN_OPENCODE:-0}"
ALLOW_UNSAFE_CLAUDE="${HERMES_ALLOW_UNSAFE_CLAUDE:-0}"
BLOCK_INTAKE="${HERMES_ISSUE_TO_PR_BLOCK_INTAKE:-0}"
MAX_CLAUDE_AGENTS="${HERMES_ISSUE_TO_PR_MAX_CLAUDE_AGENTS:-3}"
CLAUDE_TIMEOUT_SECONDS="${HERMES_CLAUDE_TIMEOUT_SECONDS:-5400}"
STALE_LOCK_MINUTES="${HERMES_STALE_LOCK_MINUTES:-180}"
LOG_FILE="${HERMES_ISSUE_TO_PR_LOG:-/Users/mini-m4-main/.hermes/logs/repo-issue-to-pr-dispatch.log}"
LOCK_DIR="${HERMES_ISSUE_TO_PR_LOCK_DIR:-/tmp/hermes-repo-issue-to-pr-dispatch.lock}"
WORKTREE_ROOT="${HERMES_WORKTREE_ROOT:-/Users/mini-m4-main/.hermes/worktrees/repo-fixer}"
KANBAN_FIXER_ASSIGNEE="${HERMES_KANBAN_FIXER_ASSIGNEE:-repo-agent-fixer}"
MAX_TASK_ATTEMPTS="${HERMES_REPO_AGENT_MAX_TASK_ATTEMPTS:-3}"
RETRY_BACKOFF_SECONDS="${HERMES_REPO_AGENT_RETRY_BACKOFF_SECONDS:-1800}"
OPENCODE_DEFERRED_RC=10
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/repo_agent_repos.sh"

usage() {
  cat <<'USAGE'
Usage: repo_issue_to_pr_dispatch.sh [--dry-run|--live] [--max N] [--run-opencode] [--block-intake]

Dry-run is the default. Live mode may create [fix-pr] Kanban tasks or block
non-actionable intake tasks. OpenCode execution requires BOTH --live and
--run-opencode (or HERMES_ISSUE_TO_PR_RUN_OPENCODE=1).
Claude execution is skipped unless HERMES_ALLOW_UNSAFE_CLAUDE=1 is set after
explicit human approval and sandboxing review.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      ;;
    --live)
      DRY_RUN=0
      ;;
    --max)
      shift
      [[ $# -gt 0 ]] || { echo "missing value for --max" >&2; exit 2; }
      MAX_PER_BOARD="$1"
      ;;
    --run-opencode)
      RUN_OPENCODE=1
      ;;
    --block-intake)
      BLOCK_INTAKE=1
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

case "$MAX_PER_BOARD" in
  ''|*[!0-9]*) echo "--max must be a non-negative integer" >&2; exit 2 ;;
esac

mkdir -p "$(dirname "$LOG_FILE")"

log() {
  local message="$1"
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$message" | tee -a "$LOG_FILE"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { log "MISSING_COMMAND name=$1"; exit 1; }
}

require_cmd hermes
require_cmd python3
require_cmd git
require_cmd gh
# Claude execution is permitted only by the combined gate:
# "$RUN_OPENCODE" == 1 && "$ALLOW_UNSAFE_CLAUDE" == 1
# Check for the claude binary lazily at the actual spawn point so non-spawn
# paths such as existing-PR finalization and retry recovery stay available.

if [[ -d "$LOCK_DIR" ]]; then
  find "$LOCK_DIR" -maxdepth 0 -mmin "+$STALE_LOCK_MINUTES" -exec rmdir {} \; 2>/dev/null || true
fi
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "LOCK_HELD path=$LOCK_DIR"
  exit 0
fi
cleanup() { rmdir "$LOCK_DIR" 2>/dev/null || true; }
trap cleanup EXIT

REPOS=()
while IFS= read -r repo_entry; do
  REPOS+=("$repo_entry")
done < <(repo_agent_repos)

slugify() {
  python3 - "$1" <<'PY'
import re, sys
value = sys.argv[1].lower()
value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
print(value[:80] or "task")
PY
}

extract_records() {
  local tasks_json="$1" repo_priority="$2"
  TASKS_JSON="$tasks_json" python3 - "$MAX_PER_BOARD" "$repo_priority" <<'PY'
import json, os, re, sys
try:
    limit = int(sys.argv[1])
    repo_priority = int(sys.argv[2])
    if limit <= 0:
        sys.exit(0)
    tasks = json.loads(os.environ.get("TASKS_JSON", "[]"))
except Exception as error:
    print(f"extract-records-error: {error}", file=sys.stderr)
    sys.exit(2)
rows = []
for task in tasks:
    title = str(task.get("title") or "")
    status = str(task.get("status") or "")
    is_review_fix = title.startswith("[fix-pr-review]")
    is_fix = title.startswith("[fix-pr]")
    if status not in {"ready", "todo", "triage"} and not ((title.startswith("[issue]") or is_fix or is_review_fix) and status == "blocked"):
        continue
    if not (title.startswith("[issue]") or title.startswith("[fix-pr]") or is_review_fix):
        continue
    body = str(task.get("body") or "")
    workspace_path = str(task.get("workspace_path") or "")
    workspace = task.get("workspace") or {}
    if isinstance(workspace, dict):
        workspace_path = workspace_path or workspace.get("path") or workspace.get("value") or ""
    elif not workspace_path:
        workspace_path = str(workspace or "")
    workspace_path = workspace_path or "-"
    issue_match = re.search(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)", title)
    repo = issue_match.group(1) if issue_match else ""
    issue = issue_match.group(2) if issue_match else ""
    body_lower = body.lower()
    branch = str(task.get("branch_name") or task.get("branch") or "")
    labels_line = ""
    for line in body.splitlines():
        if "label" in line.lower():
            labels_line += " " + line.lower()
    frozen = "frozen" in labels_line or "frozen" in body_lower or "frozen" in title.lower()
    text = f"{title} {body} {labels_line}".lower()
    score = repo_priority
    reasons = [f"repo={repo_priority}"]
    if is_review_fix:
        score += 240
        reasons.append("review-fix=240")
    elif is_fix:
        score += 200
        reasons.append("fix-pr=200")
    elif title.startswith("[issue]"):
        score += 100
        reasons.append("issue=100")
    if status == "blocked":
        score -= 40
        reasons.append("blocked=-40")
    if any(token in text for token in ("p0", "critical", "urgent")):
        score += 120
        reasons.append("urgent=120")
    if "security" in text:
        score += 100
        reasons.append("security=100")
    if any(token in text for token in ("bug", "regression", "crash", "failing")):
        score += 40
        reasons.append("bug=40")
    if any(token in text for token in ("docs", "documentation", "readme")):
        score -= 20
        reasons.append("docs=-20")
    if frozen:
        score -= 1000
        reasons.append("frozen=-1000")
    rows.append([
        str(task.get("id") or ""), title.replace("\t", " ").replace("\n", " "),
        status, repo, issue, workspace_path, "1" if frozen else "0",
        branch,
        body.replace("\t", " ").replace("\n", "\\n")[:500],
        str(score),
        ",".join(reasons),
    ])
rows.sort(key=lambda row: int(row[9]), reverse=True)
candidate_limit = max(limit * 5, limit + 5)
rows = rows[:candidate_limit]
for row in rows:
    print("\x1f".join(row))
PY
}

ensure_clean_clone() {
  local clone_path="$1"
  GIT_MASTER=1 git -C "$clone_path" rev-parse --is-inside-work-tree >/dev/null
  GIT_MASTER=1 git -C "$clone_path" diff --quiet
  GIT_MASTER=1 git -C "$clone_path" diff --cached --quiet
}

create_fix_task() {
  local board="$1" clone_path="$2" source_task_id="$3" repo="$4" issue="$5" title="$6"
  local slug branch key body fix_title
  slug="$(slugify "$repo-$issue-$title")"
  branch="ai/fix/${issue}-${slug}"
  key="fix-pr:${repo}:${issue}"
  fix_title="[fix-pr] ${repo}#${issue}: ${title#*: }"
  body="Source intake task: ${source_task_id}
Repository: ${repo}
Issue: ${issue}

Goal: create a small, tested fix for this issue and open a PR.

Required policy:
- Use gh for every GitHub operation.
- Use GIT_MASTER=1 for every git command.
- Work in an isolated worktree and branch ${branch}.
- Before opening a PR, inspect existing open PRs and linked PRs for ${repo}#${issue}.
- If another author's PR already addresses this issue or feature, do not recreate,
  replace, supersede, or close it. Stop and report/comment requested improvements.
- Do not claim or rewrite another contributor's authorship; preserve authorship or
  add Co-authored-by only after explicit human approval.
- Characterize/reproduce before editing when a seam exists.
- Make the smallest safe change, run relevant tests, and capture evidence.
- Open the PR with gh pr create only after tests pass.
- Add/keep ai:generated and ai:pr-opened labels when applicable.
- Do not merge, delete branches, force-push, expose secrets, or bypass safeguards."

  hermes kanban --board "$board" create "$fix_title" \
    --body "$body" \
    --assignee "$KANBAN_FIXER_ASSIGNEE" \
    --workspace "worktree:${clone_path}" \
    --branch "$branch" \
    --priority 1 \
    --idempotency-key "$key" \
    --skill repo-gh-cli-policy \
    --skill repo-fix-issue-pr \
    --json >/dev/null
}

complete_task() {
  local board="$1" task_id="$2" result="$3"
  hermes kanban --board "$board" complete "$task_id" --result "$result" --summary "$result" >/dev/null
}

open_pr_for_branch() {
  local repo="$1" branch="$2"
  local prs_json
  prs_json="$(gh pr list --repo "$repo" --head "$branch" --state open --json number,url --limit 1)"
  PRS_JSON="$prs_json" python3 - <<'PY'
import json, sys
import os
prs = json.loads(os.environ.get("PRS_JSON", "[]"))
if not prs:
    sys.exit(1)
pr = prs[0]
print(f"{pr.get('number')}\t{pr.get('url')}")
PY
}

repair_ai_pr_labels() {
  local repo="$1" pr_number="$2"
  if gh pr edit "$pr_number" --repo "$repo" --add-label ai:generated --add-label ai:pr-opened >/dev/null 2>&1; then
    log "LABELS_REPAIRED repo=$repo pr=$pr_number"
  else
    log "LABEL_REPAIR_FAILED repo=$repo pr=$pr_number"
  fi
}

pr_state() {
  local repo="$1" number="$2"
  gh pr view "$number" --repo "$repo" --json state --jq .state 2>/dev/null || printf '%s\n' "UNKNOWN"
}

issue_state() {
  local repo="$1" number="$2"
  gh issue view "$number" --repo "$repo" --json state --jq .state 2>/dev/null || printf '%s\n' "UNKNOWN"
}

board_lock_dir() {
  local board="$1"
  printf '%s/%s/.agent.lock\n' "$WORKTREE_ROOT" "$board"
}

board_agent_active() {
  local board="$1" lock pid_file pid
  lock="$(board_lock_dir "$board")"
  pid_file="$lock/pid"
  if [[ ! -f "$pid_file" ]]; then
    if [[ -d "$lock" ]]; then
      log "STALE_BOARD_LOCK board=$board lock=$lock reason=missing-pid-file"
      rmdir "$lock" 2>/dev/null || true
    fi
    return 1
  fi
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  log "STALE_BOARD_LOCK board=$board lock=$lock pid=${pid:-none}"
  rm -f "$pid_file"
  rmdir "$lock" 2>/dev/null || true
  return 1
}

blocked_task_retriable() {
  local board="$1" task_id="$2" show_text
  show_text="$(hermes kanban --board "$board" show "$task_id" 2>/dev/null || true)"
  if grep -Fq "repo-agent worker finished without an open PR" <<<"$show_text"; then
    return 0
  fi
  grep -Eq "Hermes repo-agent started Claude worker|protocol_violation|worker exited cleanly .* protocol violation" <<<"$show_text"
}

blocked_task_manual_only() {
  local board="$1" task_id="$2" show_text
  show_text="$(hermes kanban --board "$board" show "$task_id" 2>/dev/null || true)"
  grep -Fq "worktree-dirty-after-claude" <<<"$show_text"
}

retry_gate() {
  local board="$1" task_id="$2" show_text
  show_text="$(hermes kanban --board "$board" show "$task_id" 2>/dev/null || true)"
  TASK_SHOW="$show_text" python3 - "$MAX_TASK_ATTEMPTS" <<'PY'
import datetime as dt
import os
import re
import sys

max_attempts = int(sys.argv[1])
text = os.environ.get("TASK_SHOW", "")
attempts = [int(value) for value in re.findall(r"repo-agent retry attempt=(\d+)/\d+", text)]
attempt = max(attempts) if attempts else 0
if attempt >= max_attempts:
    print(f"attempts-exhausted attempts={attempt}/{max_attempts}")
    sys.exit(1)

next_values = re.findall(r"next_retry_after=([0-9T:\-]+Z)", text)
if next_values:
    next_time = dt.datetime.strptime(next_values[-1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    now = dt.datetime.now(dt.timezone.utc)
    if next_time > now:
        print(f"backoff-active attempts={attempt}/{max_attempts} next_retry_after={next_values[-1]}")
        sys.exit(1)

print(f"retry-ready attempts={attempt}/{max_attempts}")
PY
}

retry_failure_note() {
  local board="$1" task_id="$2" show_text
  show_text="$(hermes kanban --board "$board" show "$task_id" 2>/dev/null || true)"
  TASK_SHOW="$show_text" python3 - "$MAX_TASK_ATTEMPTS" "$RETRY_BACKOFF_SECONDS" <<'PY'
import datetime as dt
import os
import re
import sys

max_attempts = int(sys.argv[1])
backoff = int(sys.argv[2])
text = os.environ.get("TASK_SHOW", "")
attempts = [int(value) for value in re.findall(r"repo-agent retry attempt=(\d+)/\d+", text)]
attempt = (max(attempts) if attempts else 0) + 1
next_retry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=backoff)
print(f"repo-agent retry attempt={attempt}/{max_attempts} next_retry_after={next_retry.strftime('%Y-%m-%dT%H:%M:%SZ')}")
PY
}

worktree_for_branch() {
  local clone_path="$1" branch="$2"
  local line current_worktree worktree_listing
  if ! worktree_listing="$(GIT_MASTER=1 git -C "$clone_path" worktree list --porcelain)"; then
    return 1
  fi
  current_worktree=""
  while IFS= read -r line; do
    case "$line" in
      worktree\ *)
        current_worktree="${line#worktree }"
        ;;
      branch\ refs/heads/*)
        if [[ "${line#branch refs/heads/}" == "$branch" && -n "$current_worktree" ]]; then
          printf '%s\n' "$current_worktree"
          return 0
        fi
        ;;
      "")
        current_worktree=""
        ;;
    esac
  done <<<"$worktree_listing"
  return 1
}

branch_exists() {
  local clone_path="$1" branch="$2"
  GIT_MASTER=1 git -C "$clone_path" show-ref --verify --quiet "refs/heads/$branch"
}

ensure_existing_worktree_ready() {
  local worktree="$1" branch="$2"
  if ! GIT_MASTER=1 git -C "$worktree" rev-parse --is-inside-work-tree >/dev/null; then
    log "OPENCODE_BLOCKED reason=worktree-invalid branch=$branch worktree=$worktree"
    return 1
  fi
  local current_branch status
  current_branch="$(GIT_MASTER=1 git -C "$worktree" branch --show-current)"
  if [[ "$current_branch" != "$branch" ]]; then
    log "OPENCODE_BLOCKED reason=worktree-branch-mismatch expected=$branch actual=$current_branch worktree=$worktree"
    return 1
  fi
  status="$(GIT_MASTER=1 git -C "$worktree" status --short)"
  if [[ -n "$status" ]]; then
    log "OPENCODE_BLOCKED reason=worktree-not-clean branch=$branch worktree=$worktree"
    return 1
  fi
  ENSURE_WORKTREE_READY_PATH="$worktree"
}

ensure_worktree_ready() {
  local clone_path="$1" worktree="$2" branch="$3"
  local existing_worktree
  ENSURE_WORKTREE_READY_PATH="$worktree"
  if [[ -d "$worktree" ]]; then
    ensure_existing_worktree_ready "$worktree" "$branch"
    return $?
  fi
  if existing_worktree="$(worktree_for_branch "$clone_path" "$branch")"; then
    if ensure_existing_worktree_ready "$existing_worktree" "$branch"; then
      log "WORKTREE_ADOPTED branch=$branch worktree=$existing_worktree requested=$worktree"
      return 0
    fi
    return 1
  fi
  if branch_exists "$clone_path" "$branch"; then
    if ! GIT_MASTER=1 git -C "$clone_path" worktree add "$worktree" "$branch" >/dev/null; then
      if existing_worktree="$(worktree_for_branch "$clone_path" "$branch")"; then
        if ensure_existing_worktree_ready "$existing_worktree" "$branch"; then
          log "WORKTREE_ADOPTED branch=$branch worktree=$existing_worktree requested=$worktree"
          return 0
        fi
        return 1
      fi
      log "OPENCODE_BLOCKED reason=worktree-checkout-failed branch=$branch worktree=$worktree"
      return 1
    fi
    ENSURE_WORKTREE_READY_PATH="$worktree"
    return 0
  fi
  if ! GIT_MASTER=1 git -C "$clone_path" worktree add -b "$branch" "$worktree" HEAD >/dev/null; then
    if existing_worktree="$(worktree_for_branch "$clone_path" "$branch")"; then
      if ensure_existing_worktree_ready "$existing_worktree" "$branch"; then
        log "WORKTREE_ADOPTED branch=$branch worktree=$existing_worktree requested=$worktree"
        return 0
      fi
      return 1
    fi
    log "OPENCODE_BLOCKED reason=worktree-create-failed branch=$branch worktree=$worktree"
    return 1
  fi
  ENSURE_WORKTREE_READY_PATH="$worktree"
}

active_claude_agents() {
  pgrep -c -f "claude.*Hermes task" 2>/dev/null || echo 0
}

run_claude_for_fix_worker() {
  local board="$1" task_id="$2" repo="$3" issue="$4" branch="$5" worktree="$6" prompt="$7" log_file="$8" lock="$9" pid_file="${10}"
  local child="" timer="" rc=1 pr_info pr_number pr_url retry_note worktree_status status_inline
  trap 'if [[ -n "${timer:-}" ]]; then kill "$timer" 2>/dev/null || true; fi; if [[ -n "${child:-}" ]] && kill -0 "$child" 2>/dev/null; then kill "$child" 2>/dev/null || true; sleep 1; kill -9 "$child" 2>/dev/null || true; fi; rm -f "$pid_file"; rmdir "$lock" 2>/dev/null || true' EXIT TERM INT

  printf '%s CLAUDE_START task=%s repo=%s branch=%s timeout=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$task_id" "$repo" "$branch" "$CLAUDE_TIMEOUT_SECONDS"
  claude --dangerously-skip-permissions -p "$prompt" \
    --add-dir "$worktree" \
    --model sonnet &
  child=$!
  (
    sleep "$CLAUDE_TIMEOUT_SECONDS"
    if kill -0 "$child" 2>/dev/null; then
      printf '%s CLAUDE_TIMEOUT task=%s pid=%s timeout=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$task_id" "$child" "$CLAUDE_TIMEOUT_SECONDS"
      kill "$child" 2>/dev/null || true
      sleep 10
      kill -9 "$child" 2>/dev/null || true
    fi
  ) &
  timer=$!
  set +e
  wait "$child"
  rc=$?
  set -e
  kill "$timer" 2>/dev/null || true
  wait "$timer" 2>/dev/null || true
  timer=""
  printf '%s CLAUDE_EXIT task=%s pid=%s rc=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$task_id" "$child" "$rc"

  if [[ "$rc" -ne 0 ]]; then
    retry_note="$(retry_failure_note "$board" "$task_id")"
    hermes kanban --board "$board" block "$task_id" "repo-agent worker exited with rc=${rc}; ${retry_note}; log: ${log_file}" >/dev/null 2>&1 || true
    printf '%s CLAUDE_FINALIZED task=%s outcome=failed rc=%s branch=%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$task_id" "$rc" "$branch" "$retry_note"
  elif ! worktree_status="$(GIT_MASTER=1 git -C "$worktree" status --short 2>&1)"; then
    retry_note="$(retry_failure_note "$board" "$task_id")"
    hermes kanban --board "$board" block "$task_id" "worktree-status-failed-after-claude for branch ${branch}; ${retry_note}; log: ${log_file}" >/dev/null 2>&1 || true
    printf '%s CLAUDE_FINALIZED task=%s outcome=worktree-status-failed rc=%s branch=%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$task_id" "$rc" "$branch" "$retry_note"
  elif [[ -n "$worktree_status" ]]; then
    retry_note="$(retry_failure_note "$board" "$task_id")"
    status_inline="${worktree_status//$'\n'/; }"
    hermes kanban --board "$board" block "$task_id" "worktree-dirty-after-claude for branch ${branch}; ${retry_note}; log: ${log_file}" >/dev/null 2>&1 || true
    printf '%s CLAUDE_FINALIZED task=%s outcome=worktree-dirty-after-claude rc=%s branch=%s status=%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$task_id" "$rc" "$branch" "$status_inline" "$retry_note"
  elif pr_info="$(open_pr_for_branch "$repo" "$branch" 2>/dev/null)"; then
    pr_number="${pr_info%%$'\t'*}"
    pr_url="${pr_info#*$'\t'}"
    gh pr edit "$pr_number" --repo "$repo" --add-label ai:generated --add-label ai:pr-opened >/dev/null 2>&1 || true
    complete_task "$board" "$task_id" "Open PR for ${repo}#${issue}: ${pr_url}" || true
    printf '%s CLAUDE_FINALIZED task=%s outcome=pr-open pr=%s url=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$task_id" "$pr_number" "$pr_url"
  else
    retry_note="$(retry_failure_note "$board" "$task_id")"
    hermes kanban --board "$board" block "$task_id" "repo-agent worker finished without an open PR for branch ${branch}; ${retry_note}; manual inspection required if attempts are exhausted." >/dev/null 2>&1 || true
    printf '%s CLAUDE_FINALIZED task=%s outcome=no-pr rc=%s branch=%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$task_id" "$rc" "$branch" "$retry_note"
  fi
}

run_claude_for_fix() {
  local board="$1" clone_path="$2" task_id="$3" title="$4" repo="$5" issue="$6" task_branch="$7" existing_worktree="${8:-}"
  local slug branch worktree prompt log_file lock pid_file worker_pid
  if [[ -n "$task_branch" && "$task_branch" == ai/fix/* ]]; then
    branch="$task_branch"
  else
    slug="$(slugify "$repo-$issue-$title")"
    branch="ai/fix/${issue}-${slug}"
  fi
  if [[ -n "$existing_worktree" && -d "$existing_worktree" ]]; then
    worktree="$existing_worktree"
  else
    worktree="${WORKTREE_ROOT}/${board}/${task_id}"
  fi
  log_file="$(dirname "$LOG_FILE")/claude-${task_id}.log"
  lock="$(board_lock_dir "$board")"
  pid_file="$lock/pid"

  if [[ "$ALLOW_UNSAFE_CLAUDE" != 1 ]]; then
    log "CLAUDE_SKIPPED task=$task_id reason=unsafe-claude-disabled repo=$repo branch=$branch opt_in=HERMES_ALLOW_UNSAFE_CLAUDE=1 guidance=repo-agent unsafe Claude execution disabled by default; set HERMES_ALLOW_UNSAFE_CLAUDE=1 only after human approval and sandboxing review"
    return "$OPENCODE_DEFERRED_RC"
  fi
  if ! command -v claude >/dev/null 2>&1; then
    log "CLAUDE_SKIPPED task=$task_id reason=missing-command command=claude repo=$repo branch=$branch"
    return "$OPENCODE_DEFERRED_RC"
  fi

  local active
  active="$(active_claude_agents)"
  if [[ "$active" -ge "$MAX_CLAUDE_AGENTS" ]]; then
    log "CLAUDE_SKIPPED task=$task_id reason=agent-cap active=$active max=$MAX_CLAUDE_AGENTS"
    return "$OPENCODE_DEFERRED_RC"
  fi

  if ! ensure_clean_clone "$clone_path"; then
    log "CLAUDE_BLOCKED task=$task_id reason=base-clone-not-clean clone=$clone_path"
    return "$OPENCODE_DEFERRED_RC"
  fi

  mkdir -p "$(dirname "$worktree")"
  if ! ensure_worktree_ready "$clone_path" "$worktree" "$branch"; then
    return "$OPENCODE_DEFERRED_RC"
  fi
  worktree="$ENSURE_WORKTREE_READY_PATH"
  if ! mkdir "$lock" 2>/dev/null; then
    log "CLAUDE_SKIPPED task=$task_id reason=board-agent-active board=$board lock=$lock"
    return "$OPENCODE_DEFERRED_RC"
  fi

  if [[ "$title" == \[fix-pr-review\]* ]]; then
    prompt="TASK: Update existing PR ${repo}#${issue} (Hermes task ${task_id}) so it becomes merge-ready.

DELIVERABLE: update branch ${branch} on the existing PR. Do not create a replacement PR.

CONSTRAINTS:
- Use gh for all GitHub operations.
- Use GIT_MASTER=1 for every git command.
- Do not expose secrets, merge, delete branches, or force-push.
- Work only in this worktree: ${worktree}. Branch must remain ${branch}.
- Inspect PR ${repo}#${issue}, review comments, checks, and merge/conflict state before editing.
- Make the smallest safe update, run relevant tests, push the existing branch, and comment evidence on the PR if useful.
- Do not open a new PR.

TITLE: ${title}"
  else
    prompt="TASK: Fix ${repo}#${issue} (Hermes task ${task_id}) and open a PR.

DELIVERABLE: a merged-ready GitHub PR for the smallest safe fix.

CONSTRAINTS:
- Use gh for all GitHub operations.
- Use GIT_MASTER=1 for every git command.
- Do not use raw GitHub clients, expose secrets, merge, delete branches, or force-push.
- Work only in this worktree: ${worktree}. Branch must remain ${branch}.
- Before gh pr create, check for existing open PRs for ${repo}#${issue}.
- If another author's PR already exists, stop and do not open a replacement.
- Reproduce before editing. Run relevant tests. Include evidence in the PR body.
- After gh pr create, add labels: ai:generated and ai:pr-opened.

TITLE: ${title}"
  fi

  hermes kanban --board "$board" comment --author repo-agent "$task_id" "Hermes repo-agent started Claude worker for ${repo}#${issue}; log: ${log_file}" >/dev/null 2>&1 || true
  (
    while [[ ! -f "$pid_file" ]]; do
      sleep 0.05
    done
    run_claude_for_fix_worker "$board" "$task_id" "$repo" "$issue" "$branch" "$worktree" "$prompt" "$log_file" "$lock" "$pid_file"
  ) >>"$log_file" 2>&1 &
  worker_pid=$!
  printf '%s\n' "$worker_pid" >"$pid_file"
  disown "$worker_pid" 2>/dev/null || true
  log "CLAUDE_SPAWNED task=$task_id repo=$repo branch=$branch worker_pid=$worker_pid log=$log_file timeout=$CLAUDE_TIMEOUT_SECONDS board=$board"
  return 0
}

processed=0
created=0
blocked=0
deferred=0
claude_spawned=0
failures=0

log "START mode=$([[ "$DRY_RUN" == 1 ]] && echo dry-run || echo live) max_per_board=$MAX_PER_BOARD run_claude=$RUN_OPENCODE unsafe_claude=$ALLOW_UNSAFE_CLAUDE max_agents=$MAX_CLAUDE_AGENTS block_intake=$BLOCK_INTAKE max_attempts=$MAX_TASK_ATTEMPTS backoff_seconds=$RETRY_BACKOFF_SECONDS"

for mapping in "${REPOS[@]}"; do
  IFS='|' read -r repo board clone_path repo_priority <<<"$mapping"
  repo_priority="${repo_priority:-0}"
  board_spawned=0
  if [[ ! -d "$clone_path" ]]; then
    log "CLONE_MISSING repo=$repo clone=$clone_path"
    failures=$((failures + 1))
    continue
  fi
  if ! GIT_MASTER=1 git -C "$clone_path" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    log "CLONE_INVALID repo=$repo clone=$clone_path"
    failures=$((failures + 1))
    continue
  fi
  board_agent_active "$board" >/dev/null || true

  if ! json="$(hermes kanban --board "$board" list --json --sort created-desc)"; then
    log "KANBAN_LIST_FAILED board=$board repo=$repo"
    failures=$((failures + 1))
    continue
  fi
  while IFS=$'\x1f' read -r task_id title status parsed_repo issue workspace_path frozen task_branch body_preview task_score selection_reason; do
    [[ -n "${task_id:-}" ]] || continue
    processed=$((processed + 1))
    task_repo="${parsed_repo:-$repo}"
    [[ -n "$task_repo" ]] || task_repo="$repo"
    task_clone="$clone_path"
    task_existing_worktree=""
    if [[ -n "$workspace_path" && -d "$workspace_path" ]]; then
      if [[ "$title" == \[fix-pr\]* || "$title" == \[fix-pr-review\]* ]]; then
        [[ "$workspace_path" != "$clone_path" ]] && task_existing_worktree="$workspace_path"
      else
        task_clone="$workspace_path"
      fi
    fi

    if [[ "$title" == \[issue\]* ]]; then
      if [[ "$frozen" == 1 ]]; then
        log "DECISION board=$board task=$task_id action=block reason=frozen-or-non-actionable title=$(printf '%q' "$title")"
        if [[ "$DRY_RUN" == 0 && "$BLOCK_INTAKE" == 1 ]]; then
          hermes kanban --board "$board" block "$task_id" "Blocked by repo_issue_to_pr_dispatch: issue is frozen/non-actionable; no PR work created." >/dev/null
          blocked=$((blocked + 1))
        fi
        continue
      fi
      if [[ -z "$issue" ]]; then
        log "DECISION board=$board task=$task_id action=skip reason=missing-issue-number title=$(printf '%q' "$title")"
        continue
      fi
      state="$(issue_state "$task_repo" "$issue")"
      if [[ "$state" == "UNKNOWN" ]]; then
        log "ISSUE_STATE_UNKNOWN repo=$task_repo issue=$issue action=skip"
        continue
      fi
      if [[ "$state" != "OPEN" ]]; then
        log "DECISION board=$board task=$task_id action=complete-stale-issue reason=issue-${state} repo=$task_repo issue=$issue"
        if [[ "$DRY_RUN" == 0 ]]; then
          complete_task "$board" "$task_id" "Skipped stale intake task because ${task_repo}#${issue} is ${state}."
        fi
        continue
      fi

        log "DECISION board=$board task=$task_id action=create-fix-pr-task repo=$task_repo issue=$issue clone=$task_clone status=$status score=$task_score reason=$selection_reason"
      if [[ "$DRY_RUN" == 0 ]]; then
        if create_fix_task "$board" "$task_clone" "$task_id" "$task_repo" "$issue" "$title"; then
          complete_task "$board" "$task_id" "Created or confirmed idempotent explicit [fix-pr] task for ${task_repo}#${issue}."
          created=$((created + 1))
        else
          log "CREATE_FIX_TASK_FAILED board=$board task=$task_id repo=$task_repo issue=$issue"
          failures=$((failures + 1))
        fi
      fi
      continue
    fi

    if [[ "$title" == \[fix-pr\]* || "$title" == \[fix-pr-review\]* ]]; then
      if [[ -z "$issue" ]]; then
        log "DECISION board=$board task=$task_id action=skip reason=missing-issue-number title=$(printf '%q' "$title")"
        continue
      fi
      if [[ "$title" == \[fix-pr-review\]* ]]; then
        state="$(pr_state "$task_repo" "$issue")"
        if [[ "$state" == "UNKNOWN" ]]; then
          log "PR_STATE_UNKNOWN repo=$task_repo pr=$issue action=skip"
          continue
        fi
        if [[ "$state" != "OPEN" ]]; then
          log "DECISION board=$board task=$task_id action=complete-stale-review reason=pr-${state} repo=$task_repo pr=$issue"
          if [[ "$DRY_RUN" == 0 ]]; then
            complete_task "$board" "$task_id" "Skipped stale PR follow-up because ${task_repo}#${issue} is ${state}."
          fi
          continue
        fi
      else
        state="$(issue_state "$task_repo" "$issue")"
        if [[ "$state" == "UNKNOWN" ]]; then
          log "ISSUE_STATE_UNKNOWN repo=$task_repo issue=$issue action=skip"
          continue
        fi
        if [[ "$state" != "OPEN" ]]; then
          log "DECISION board=$board task=$task_id action=complete-stale-fix reason=issue-${state} repo=$task_repo issue=$issue"
          if [[ "$DRY_RUN" == 0 ]]; then
            complete_task "$board" "$task_id" "Skipped stale fixer task because ${task_repo}#${issue} is ${state}."
          fi
          continue
        fi
        if [[ "$status" == "blocked" ]]; then
          if blocked_task_manual_only "$board" "$task_id"; then
            log "DECISION board=$board task=$task_id action=skip reason=manual-blocked-fix-task repo=$task_repo issue=$issue"
            continue
          fi
          if [[ -n "$task_branch" ]] && pr_row="$(open_pr_for_branch "$task_repo" "$task_branch" 2>/dev/null)"; then
            pr_number="${pr_row%%$'\t'*}"
            pr_url="${pr_row#*$'\t'}"
            log "DECISION board=$board task=$task_id action=complete-blocked-with-existing-pr repo=$task_repo issue=$issue pr=$pr_url"
            if [[ "$DRY_RUN" == 0 ]]; then
              repair_ai_pr_labels "$task_repo" "$pr_number"
              complete_task "$board" "$task_id" "Open PR for ${task_repo}#${issue}: ${pr_url}"
            fi
            continue
          fi
          if blocked_task_retriable "$board" "$task_id"; then
            if ! retry_status="$(retry_gate "$board" "$task_id")"; then
              if [[ "$retry_status" == attempts-exhausted* ]]; then
                log "NO_PR_RETRIES_EXHAUSTED task=$task_id repo=$task_repo issue=$issue retry=$(printf '%q' "$retry_status")"
                continue
              fi
              log "DECISION board=$board task=$task_id action=skip reason=retry-gate repo=$task_repo issue=$issue retry=$(printf '%q' "$retry_status")"
              continue
            fi
            log "DECISION board=$board task=$task_id action=recover-blocked-fix-task repo=$task_repo issue=$issue retry=$(printf '%q' "$retry_status")"
            if [[ "$DRY_RUN" == 0 ]]; then
              hermes kanban --board "$board" reassign "$task_id" "$KANBAN_FIXER_ASSIGNEE" --reason "repo-agent recovery owns this fixer task" >/dev/null 2>&1 || true
              hermes kanban --board "$board" unblock "$task_id" --reason "repo-agent retrying stale worker/protocol-violation task" >/dev/null 2>&1 || true
            fi
          else
            log "DECISION board=$board task=$task_id action=skip reason=blocked-fix-task repo=$task_repo issue=$issue"
            continue
          fi
        fi
      fi
      if [[ "$title" == \[fix-pr\]* && -n "$task_branch" ]] && pr_row="$(open_pr_for_branch "$task_repo" "$task_branch" 2>/dev/null)"; then
        pr_number="${pr_row%%$'\t'*}"
        pr_url="${pr_row#*$'\t'}"
        log "DECISION board=$board task=$task_id action=complete-existing-pr repo=$task_repo issue=$issue pr=$pr_url"
        if [[ "$DRY_RUN" == 0 ]]; then
          repair_ai_pr_labels "$task_repo" "$pr_number"
          complete_task "$board" "$task_id" "Open PR for ${task_repo}#${issue}: ${pr_url}"
        fi
        continue
      fi
      if [[ "$board_spawned" == 1 ]] || board_agent_active "$board"; then
        log "DECISION board=$board task=$task_id action=skip reason=board-agent-active repo=$task_repo issue=$issue"
        continue
      fi
      log "DECISION board=$board task=$task_id action=run-claude repo=$task_repo issue=$issue clone=$task_clone score=$task_score reason=$selection_reason"
      if [[ "$DRY_RUN" == 1 ]]; then
        board_spawned=1
        continue
      fi
      if [[ "$DRY_RUN" == 0 && "$RUN_OPENCODE" == 1 ]]; then
        hermes kanban --board "$board" comment --author repo-agent "$task_id" "repo-agent selected this task now: score=${task_score}; reason=${selection_reason}; repo_priority=${repo_priority}; one-worker-per-board=${board}; max_agents=${MAX_CLAUDE_AGENTS}." >/dev/null 2>&1 || true
        opencode_rc=0
        run_claude_for_fix "$board" "$task_clone" "$task_id" "$title" "$task_repo" "$issue" "$task_branch" "$task_existing_worktree" || opencode_rc=$?
        if [[ "$opencode_rc" == 0 ]]; then
          claude_spawned=$((claude_spawned + 1))
          board_spawned=1
        elif [[ "$opencode_rc" == "$OPENCODE_DEFERRED_RC" ]]; then
          log "OPENCODE_DEFERRED task=$task_id repo=$task_repo issue=$issue rc=$opencode_rc"
          deferred=$((deferred + 1))
        else
          failures=$((failures + 1))
        fi
      fi
    fi
  done < <(extract_records "$json" "$repo_priority")
done

log "DONE mode=$([[ "$DRY_RUN" == 1 ]] && echo dry-run || echo live) processed=$processed created_fix_tasks=$created blocked=$blocked deferred=$deferred claude_spawned=$claude_spawned failures=$failures"
[[ "$failures" -eq 0 ]]
