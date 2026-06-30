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
BLOCK_INTAKE="${HERMES_ISSUE_TO_PR_BLOCK_INTAKE:-0}"
MAX_CLAUDE_AGENTS="${HERMES_ISSUE_TO_PR_MAX_CLAUDE_AGENTS:-3}"
LOG_FILE="${HERMES_ISSUE_TO_PR_LOG:-/Users/mini-m4-main/.hermes/logs/repo-issue-to-pr-dispatch.log}"
LOCK_DIR="${HERMES_ISSUE_TO_PR_LOCK_DIR:-/tmp/hermes-repo-issue-to-pr-dispatch.lock}"
WORKTREE_ROOT="${HERMES_WORKTREE_ROOT:-/Users/mini-m4-main/.hermes/worktrees/repo-fixer}"

usage() {
  cat <<'USAGE'
Usage: repo_issue_to_pr_dispatch.sh [--dry-run|--live] [--max N] [--run-opencode] [--block-intake]

Dry-run is the default. Live mode may create [fix-pr] Kanban tasks or block
non-actionable intake tasks. OpenCode execution requires BOTH --live and
--run-opencode (or HERMES_ISSUE_TO_PR_RUN_OPENCODE=1).
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
if [[ "$RUN_OPENCODE" == 1 ]]; then
  require_cmd claude
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "LOCK_HELD path=$LOCK_DIR"
  exit 0
fi
cleanup() { rmdir "$LOCK_DIR" 2>/dev/null || true; }
trap cleanup EXIT

REPOS=(
  "mikolaj92/Fala|mikolaj92-fala|/Users/mini-m4-main/Developer/hermes-repos/Fala"
  "mikolaj92/reviewkit|mikolaj92-reviewkit|/Users/mini-m4-main/Developer/hermes-repos/reviewkit"
  "mikolaj92/anonimizator3000|mikolaj92-anonimizator3000|/Users/mini-m4-main/Developer/hermes-repos/anonimizator3000"
  "mikolaj92/datasource-kit|mikolaj92-datasource-kit|/Users/mini-m4-main/Developer/hermes-repos/datasource-kit"
  "mikolaj92/msds-portal|mikolaj92-msds-portal|/Users/mini-m4-main/Developer/hermes-repos/msds-portal"
  "mikolaj92/swift-openapi-dynamic|mikolaj92-swift-openapi-dynamic|/Users/mini-m4-main/Developer/hermes-repos/swift-openapi-dynamic"
  "mikolaj92/OpenAPITransportKit|mikolaj92-openapi-transport-kit|/Users/mini-m4-main/Developer/hermes-repos/OpenAPITransportKit"
)

slugify() {
  python3 - "$1" <<'PY'
import re, sys
value = sys.argv[1].lower()
value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
print(value[:80] or "task")
PY
}

extract_records() {
  local tasks_json="$1"
  TASKS_JSON="$tasks_json" python3 - "$MAX_PER_BOARD" <<'PY'
import json, os, re, sys
try:
    limit = int(sys.argv[1])
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
    if status not in {"ready", "todo", "triage"}:
        continue
    if not (title.startswith("[issue]") or title.startswith("[fix-pr]")):
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
    rows.append([
        str(task.get("id") or ""), title.replace("\t", " ").replace("\n", " "),
        status, repo, issue, workspace_path, "1" if frozen else "0",
        branch,
        body.replace("\t", " ").replace("\n", "\\n")[:500],
    ])
    if len(rows) >= limit:
        break
for row in rows:
    print("\x1f".join(row))
PY
}

ensure_clean_clone() {
  local clone_path="$1"
  GIT_MASTER=1 git -C "$clone_path" rev-parse --is-inside-work-tree >/dev/null
  local status
  status="$(GIT_MASTER=1 git -C "$clone_path" status --short)"
  [[ -z "$status" ]]
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
    --assignee repo-fixer \
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

ensure_worktree_ready() {
  local clone_path="$1" worktree="$2" branch="$3"
  if [[ ! -d "$worktree" ]]; then
    if ! GIT_MASTER=1 git -C "$clone_path" worktree add -b "$branch" "$worktree" HEAD >/dev/null; then
      log "OPENCODE_BLOCKED reason=worktree-create-failed branch=$branch worktree=$worktree"
      return 1
    fi
    return
  fi
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
}

active_claude_agents() {
  pgrep -c -f "claude.*--dangerously-skip-permissions" 2>/dev/null || echo 0
}

run_claude_for_fix() {
  local board="$1" clone_path="$2" task_id="$3" title="$4" repo="$5" issue="$6" task_branch="$7"
  local slug branch worktree prompt log_file
  if [[ -n "$task_branch" && "$task_branch" == ai/fix/* ]]; then
    branch="$task_branch"
  else
    slug="$(slugify "$repo-$issue-$title")"
    branch="ai/fix/${issue}-${slug}"
  fi
  worktree="${WORKTREE_ROOT}/${board}/${task_id}"
  log_file="$(dirname "$LOG_FILE")/claude-${task_id}.log"

  local active
  active="$(active_claude_agents)"
  if [[ "$active" -ge "$MAX_CLAUDE_AGENTS" ]]; then
    log "CLAUDE_SKIPPED task=$task_id reason=agent-cap active=$active max=$MAX_CLAUDE_AGENTS"
    return 1
  fi

  if ! ensure_clean_clone "$clone_path"; then
    log "CLAUDE_BLOCKED task=$task_id reason=base-clone-not-clean clone=$clone_path"
    return 1
  fi

  mkdir -p "$(dirname "$worktree")"
  if ! ensure_worktree_ready "$clone_path" "$worktree" "$branch"; then
    return 1
  fi

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

  nohup claude --dangerously-skip-permissions -p "$prompt" \
    --add-dir "$worktree" \
    --model sonnet \
    >"$log_file" 2>&1 &
  log "CLAUDE_SPAWNED task=$task_id repo=$repo branch=$branch pid=$! log=$log_file"
}

processed=0
created=0
blocked=0
claude_spawned=0
failures=0

log "START mode=$([[ "$DRY_RUN" == 1 ]] && echo dry-run || echo live) max_per_board=$MAX_PER_BOARD run_claude=$RUN_OPENCODE max_agents=$MAX_CLAUDE_AGENTS block_intake=$BLOCK_INTAKE"

for mapping in "${REPOS[@]}"; do
  IFS='|' read -r repo board clone_path <<<"$mapping"
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

  json="$(hermes kanban --board "$board" list --json --sort created-desc)"
  while IFS=$'\x1f' read -r task_id title status parsed_repo issue workspace_path frozen task_branch body_preview; do
    [[ -n "${task_id:-}" ]] || continue
    processed=$((processed + 1))
    task_repo="${parsed_repo:-$repo}"
    [[ -n "$task_repo" ]] || task_repo="$repo"
    task_clone="$clone_path"
    [[ -n "$workspace_path" && -d "$workspace_path" ]] && task_clone="$workspace_path"

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

      log "DECISION board=$board task=$task_id action=create-fix-pr-task repo=$task_repo issue=$issue clone=$task_clone"
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

    if [[ "$title" == \[fix-pr\]* ]]; then
      if [[ -z "$issue" ]]; then
        log "DECISION board=$board task=$task_id action=skip reason=missing-issue-number title=$(printf '%q' "$title")"
        continue
      fi
      log "DECISION board=$board task=$task_id action=run-claude repo=$task_repo issue=$issue clone=$task_clone"
      if [[ "$DRY_RUN" == 0 && "$RUN_OPENCODE" == 1 ]]; then
        if run_claude_for_fix "$board" "$task_clone" "$task_id" "$title" "$task_repo" "$issue" "$task_branch"; then
          claude_spawned=$((claude_spawned + 1))
        else
          failures=$((failures + 1))
        fi
      fi
    fi
  done < <(extract_records "$json")
done

log "DONE mode=$([[ "$DRY_RUN" == 1 ]] && echo dry-run || echo live) processed=$processed created_fix_tasks=$created blocked=$blocked claude_spawned=$claude_spawned failures=$failures"
[[ "$failures" -eq 0 ]]
