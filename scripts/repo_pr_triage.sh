#!/usr/bin/env bash
set -euo pipefail

# Managed by the Hermes repo-agent harness.
# Purpose: dry-run-first PR triage with disabled-by-default merge gates.

export HOME="${HOME:-/Users/mini-m4-main}"
export PATH="/Users/mini-m4-main/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

DRY_RUN="${HERMES_PR_TRIAGE_DRY_RUN:-1}"
COMMENT_ENABLED="${HERMES_PR_TRIAGE_COMMENT:-0}"
AUTOMERGE="${HERMES_PR_AUTOMERGE:-0}"
REQUIRE_APPROVED="${HERMES_PR_REQUIRE_APPROVED:-1}"
ALLOW_NO_CHECKS="${HERMES_PR_ALLOW_NO_CHECKS:-0}"
REQUIRE_TEST_EVIDENCE="${HERMES_PR_REQUIRE_TEST_EVIDENCE:-1}"
PR_LIST_LIMIT="${HERMES_PR_TRIAGE_LIST_LIMIT:-100}"
LOG_FILE="${HERMES_PR_TRIAGE_LOG:-/Users/mini-m4-main/.hermes/logs/repo-pr-triage.log}"
LOCK_DIR="${HERMES_PR_TRIAGE_LOCK_DIR:-/tmp/hermes-repo-pr-triage.lock}"
STALE_LOCK_MINUTES="${HERMES_STALE_LOCK_MINUTES:-180}"
CLAIM_ASSIGNEE="${HERMES_REPO_AGENT_ASSIGNEE:-mikolaj92}"
WORKTREE_ROOT="${HERMES_WORKTREE_ROOT:-/Users/mini-m4-main/.hermes/worktrees/repo-fixer}"
MERGE_RECEIPT_DIR="${HERMES_PR_TRIAGE_MERGE_RECEIPT_DIR:-/Users/mini-m4-main/.hermes/receipts/repo-pr-triage}"
QUEUE_SOURCE="${HERMES_REPO_AGENT_SOURCE:-github}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/repo_agent_repos.sh"

usage() {
  cat <<'USAGE'
Usage: repo_pr_triage.sh [--dry-run|--live] [--comment]

Dry-run is the default and never comments or merges. Live comments require
--comment or HERMES_PR_TRIAGE_COMMENT=1. Merge requires live mode plus
HERMES_PR_AUTOMERGE=1 and all strict gates to pass.
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
    --comment)
      COMMENT_ENABLED=1
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

# Live automation must retain the approval gate regardless of environment
# overrides; dry-run remains useful for policy inspection.
if [[ "$DRY_RUN" == 0 ]]; then
  REQUIRE_APPROVED=1
fi

mkdir -p "$(dirname "$LOG_FILE")"

log() {
  local message="$1"
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$message" | tee -a "$LOG_FILE"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { log "MISSING_COMMAND name=$1"; exit 1; }
}

require_cmd gh
require_cmd python3

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

board_for_repo() {
  repo_agent_board_for_repo "$1"
}

clone_for_repo() {
  repo_agent_clone_for_repo "$1"
}

board_lock_dir() {
  local board="$1"
  printf '%s/%s/.agent.lock\n' "$WORKTREE_ROOT" "$board"
}

board_agent_active() {
  local board="$1" pid_file pid
  pid_file="$(board_lock_dir "$board")/pid"
  [[ -f "$pid_file" ]] || return 1
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

board_repo_busy() {
  local board="$1"
  board_agent_active "$board"
}

extract_prs() {
  PRS_JSON="$1" python3 <<'PY'
import json, os
prs = json.loads(os.environ.get("PRS_JSON", "[]"))
for pr in prs:
    labels = ",".join(sorted(label.get("name", "") for label in pr.get("labels", [])))
    author = pr.get("author") or {}
    row = [
        str(pr.get("number") or ""),
        str(pr.get("title") or "").replace("\t", " ").replace("\n", " "),
        str(pr.get("url") or ""),
        str(pr.get("headRefName") or ""),
        str(pr.get("baseRefName") or ""),
        str(pr.get("headRefOid") or ""),
        str(pr.get("baseRefOid") or ""),
        "1" if pr.get("isDraft") else "0",
        str(pr.get("mergeStateStatus") or ""),
        str(pr.get("reviewDecision") or ""),
        labels,
        str(author.get("login") or ""),
    ]
    print("\x1f".join(row))
PY
}


checks_pass() {
  local repo="$1" number="$2"
  local checks_json
  local checks_rc=0
  checks_json="$(gh pr checks "$number" --repo "$repo" --json name,state,bucket 2>/dev/null)" || checks_rc=$?
  CHECKS_JSON="$checks_json" python3 - "$ALLOW_NO_CHECKS" "$checks_rc" <<'PY'
import json, os, sys
allow_no_checks = sys.argv[1] == "1"
gh_rc = int(sys.argv[2])
raw = os.environ.get("CHECKS_JSON", "")
if gh_rc != 0 or not raw.strip():
    sys.exit(1)
try:
    checks = json.loads(raw)
except Exception:
    sys.exit(1)
if not isinstance(checks, list):
    sys.exit(1)
if not checks:
    sys.exit(0 if allow_no_checks else 1)
for check in checks:
    bucket = str(check.get("bucket") or "").lower()
    state = str(check.get("state") or "").lower()
    if bucket in {"fail", "failing", "cancel", "skipping", "pending"}:
        sys.exit(1)
    if state and state not in {"completed", "success"}:
        sys.exit(1)
sys.exit(0)
PY
}

issue_from_branch() {
  local branch="$1" rest
  [[ "$branch" == ai/fix/* ]] || return 1
  rest="${branch#ai/fix/}"
  [[ "$rest" =~ ^([0-9]+) ]] || return 1
  printf '%s\n' "${BASH_REMATCH[1]}"
}

write_merge_receipt() {
  local repo="$1" pr="$2" issue="$3" base_sha="$4" head_sha="$5" merge_sha="$6" merged_at="$7" base_ref="$8" path tmp
  mkdir -p "$MERGE_RECEIPT_DIR" || return 1
  path="$MERGE_RECEIPT_DIR/$(printf '%s' "$repo-$pr" | tr '/:' '__').json"
  tmp="${path}.tmp.$$"
  RECEIPT_TMP="$tmp" python3 - "$repo" "$pr" "$issue" "$base_sha" "$head_sha" "$merge_sha" "$merged_at" "$base_ref" <<'PY'
import json, os, sys
payload = {"repo": sys.argv[1], "pr": int(sys.argv[2]), "issue": int(sys.argv[3]), "baseSha": sys.argv[4], "headSha": sys.argv[5], "mergeSha": sys.argv[6], "mergedAt": sys.argv[7], "baseRef": sys.argv[8]}
with open(os.environ["RECEIPT_TMP"], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, sort_keys=True)
    handle.write("\n")
PY
  mv -f "$tmp" "$path" || return 1
  MERGE_RECEIPT_PATH="$path"
}

close_linked_issue() {
  local repo="$1" issue="$2" state
  state="$(gh issue view "$issue" --repo "$repo" --json state --jq .state 2>/dev/null)" || return 1
  if [[ "$state" == CLOSED ]]; then return 0; fi
  [[ "$state" == OPEN ]] || return 1
  gh issue close "$issue" --repo "$repo" --reason completed >/dev/null 2>&1 || return 1
  state="$(gh issue view "$issue" --repo "$repo" --json state --jq .state 2>/dev/null)" || return 1
  [[ "$state" == CLOSED ]]
}

merge_and_finalize() {
  local repo="$1" number="$2" clone_path="$3" head="$4"
  local readback readback_rc=0 validated base_sha head_sha merge_sha merged_at base_ref issue
  gh pr merge "$number" --repo "$repo" --merge >/dev/null || { log "MERGE_FAILED repo=$repo pr=$number"; return 10; }
  readback="$(gh pr view "$number" --repo "$repo" --json state,mergedAt,mergeCommit,baseRefName,baseRefOid,headRefName,headRefOid,body,closingIssuesReferences 2>/dev/null)" || readback_rc=$?
  [[ "$readback_rc" -eq 0 ]] || { log "MERGE_READBACK_FAILED repo=$repo pr=$number reason=api"; return 11; }
  if ! validated="$(MERGE_READBACK="$readback" MERGE_HEAD="$head" python3 - <<'PY'
import json, os, re, sys
try:
    row = json.loads(os.environ["MERGE_READBACK"])
    commit = row.get("mergeCommit") or {}
    if row.get("state") != "MERGED" or not row.get("mergedAt") or not commit.get("oid"):
        raise ValueError("unverified-merge")
    if row.get("baseRefName") != "main" or not row.get("baseRefOid") or not row.get("headRefOid"):
        raise ValueError("invalid-base-or-head")
    refs = row.get("closingIssuesReferences") or []
    issue = str(refs[0].get("number")) if refs and refs[0].get("number") else ""
    body = str(row.get("body") or "")
    if not issue:
        matches = re.findall(r"(?i)\b(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s+#(\d+)\b", body)
        issue = matches[0] if matches else ""
    if not issue:
        branch = str(row.get("headRefName") or os.environ.get("MERGE_HEAD") or "")
        match = re.match(r"ai/fix/(\d+)", branch)
        issue = match.group(1) if match else ""
    if not issue:
        raise ValueError("missing-linked-issue")
    print("\t".join((row["baseRefOid"], row["headRefOid"], commit["oid"], row["mergedAt"], row["baseRefName"], issue)))
except Exception:
    sys.exit(1)
PY
)"; then
    log "MERGE_READBACK_FAILED repo=$repo pr=$number reason=unverified"
    return 12
  fi
  IFS=$'\t' read -r base_sha head_sha merge_sha merged_at base_ref issue <<<"$validated"
  [[ -n "$base_sha" && -n "$head_sha" && -n "$merge_sha" && -n "$merged_at" && "$base_ref" == main && "$issue" =~ ^[0-9]+$ ]] || {
    log "MERGE_READBACK_FAILED repo=$repo pr=$number reason=missing-required-fields"
    return 12
  }
  [[ -d "$clone_path/.git" ]] || { log "MERGE_ANCESTRY_FAILED repo=$repo pr=$number reason=missing-clone"; return 13; }
  GIT_MASTER=1 git -C "$clone_path" fetch --prune origin main >/dev/null 2>&1 || { log "MERGE_ANCESTRY_FAILED repo=$repo pr=$number reason=fetch"; return 13; }
  GIT_MASTER=1 git -C "$clone_path" show-ref --verify --quiet refs/remotes/origin/main || { log "MERGE_ANCESTRY_FAILED repo=$repo pr=$number reason=missing-origin-main"; return 13; }
  GIT_MASTER=1 git -C "$clone_path" merge-base --is-ancestor "$merge_sha" refs/remotes/origin/main || { log "MERGE_ANCESTRY_FAILED repo=$repo pr=$number reason=not-ancestor sha=$merge_sha"; return 13; }
  write_merge_receipt "$repo" "$number" "$issue" "$base_sha" "$head_sha" "$merge_sha" "$merged_at" "$base_ref" || { log "MERGE_RECEIPT_FAILED repo=$repo pr=$number"; return 14; }
  close_linked_issue "$repo" "$issue" || { log "ISSUE_CLOSE_FAILED repo=$repo pr=$number issue=$issue receipt=$MERGE_RECEIPT_PATH"; return 15; }
  log "MERGE_VERIFIED repo=$repo pr=$number merge_sha=$merge_sha receipt=$MERGE_RECEIPT_PATH issue=$issue"
}

pr_has_test_evidence() {
  local repo="$1" number="$2" body
  body="$(gh pr view "$number" --repo "$repo" --json body --jq .body 2>/dev/null || true)"
  grep -Eiq 'test evidence|tests?|pytest|unittest|xcodebuild|swift test|uv run|npm test|cargo test|go test' <<<"$body"
}


release_clean_worktree_for_branch() {
  local clone_path="$1" branch="$2"
  local path="" wt_branch="" line
  [[ -d "$clone_path/.git" ]] || return 0

  while IFS= read -r line; do

    case "$line" in
      worktree\ *)
        path="${line#worktree }"
        wt_branch=""
        ;;
      branch\ refs/heads/*)
        wt_branch="${line#branch refs/heads/}"
        if [[ "$wt_branch" == "$branch" && ( "$path" == "$clone_path/.worktrees/"* || "$path" == "$WORKTREE_ROOT/"* ) ]]; then
          if [[ -z "$(GIT_MASTER=1 git -C "$path" status --porcelain 2>/dev/null)" ]]; then
            GIT_MASTER=1 git -C "$clone_path" worktree remove "$path" >/dev/null 2>&1 || true
          fi
        fi
        ;;
    esac
  done < <(GIT_MASTER=1 git -C "$clone_path" worktree list --porcelain 2>/dev/null || true)
}

retry_receipt_closures() {
  local repo="$1" path payload issue receipt_repo
  [[ -d "$MERGE_RECEIPT_DIR" ]] || return 0
  shopt -s nullglob
  for path in "$MERGE_RECEIPT_DIR"/*.json; do
    payload="$(cat "$path" 2>/dev/null || true)"
    receipt_repo="$(RECEIPT_JSON="$payload" python3 -c 'import json,os; print(json.loads(os.environ["RECEIPT_JSON"]).get("repo", ""))' 2>/dev/null || true)"
    [[ "$receipt_repo" == "$repo" ]] || continue
    issue="$(RECEIPT_JSON="$payload" python3 -c 'import json,os; print(json.loads(os.environ["RECEIPT_JSON"]).get("issue", ""))' 2>/dev/null || true)"
    [[ "$issue" =~ ^[0-9]+$ ]] || continue
    if close_linked_issue "$repo" "$issue"; then
      log "ISSUE_CLOSED repo=$repo issue=$issue action=retry receipt=$path"
    else
      log "ISSUE_CLOSE_RETRY_FAILED repo=$repo issue=$issue receipt=$path"
    fi
  done
  shopt -u nullglob
}

sync_local_branch_from_origin() {
  local clone_path="$1" branch="$2"
  [[ -d "$clone_path/.git" ]] || return 0
  if GIT_MASTER=1 git -C "$clone_path" show-ref --verify --quiet "refs/remotes/origin/$branch"; then
    GIT_MASTER=1 git -C "$clone_path" branch -f "$branch" "origin/$branch" >/dev/null 2>&1 || true
  fi
}

refresh_clone_base() {
  local clone_path="$1" current
  [[ -d "$clone_path/.git" ]] || return 0
  GIT_MASTER=1 git -C "$clone_path" fetch --prune origin >/dev/null 2>&1 || return 0
  current="$(GIT_MASTER=1 git -C "$clone_path" branch --show-current 2>/dev/null || true)"
  case "$current" in
    main|master)
      if [[ -z "$(GIT_MASTER=1 git -C "$clone_path" status --porcelain --untracked-files=no 2>/dev/null)" ]] &&
        GIT_MASTER=1 git -C "$clone_path" show-ref --verify --quiet "refs/remotes/origin/$current"; then
        GIT_MASTER=1 git -C "$clone_path" merge --ff-only "origin/$current" >/dev/null 2>&1 || true
      fi
      ;;
  esac
}

pr_repair_context() {
  local repo="$1" number="$2"
  local checks reviews comments

  checks="$(
    gh pr checks "$number" --repo "$repo" --json name,state,bucket,description \
      --jq '.[] | "- check: \(.name) bucket=\(.bucket) state=\(.state) \((.description // "") | gsub("[\r\n]+"; " ") | .[0:180])"' \
      2>/dev/null | head -n 20 || true
  )"
  reviews="$(
    gh api "/repos/${repo}/pulls/${number}/reviews" \
      --jq '.[] | select(.state == "CHANGES_REQUESTED" or .state == "COMMENTED") | "- review: \(.user.login) state=\(.state) \((.body // "") | gsub("[\r\n]+"; " ") | .[0:220])"' \
      2>/dev/null | tail -n 12 || true
  )"
  comments="$(
    gh api "/repos/${repo}/pulls/${number}/comments" \
      --jq '.[] | "- file: \(.path):\(.line // .original_line // 0) by \(.user.login) \((.body // "") | gsub("[\r\n]+"; " ") | .[0:220])"' \
      2>/dev/null | tail -n 12 || true
  )"

  printf 'Checks:\n%s\n\nReviews:\n%s\n\nReview comments:\n%s\n' \
    "${checks:-not available}" \
    "${reviews:-not available}" \
    "${comments:-not available}"
}



create_review_fix_task() {
  local repo="$1" number="$2" title="$3" url="$4" head="$5" reason="$6"
  local board clone_path task_title body idempotency_key context

  board="$(board_for_repo "$repo")" || return 2
  clone_path="$(clone_for_repo "$repo")" || return 2
  if [[ "$QUEUE_SOURCE" == "github" ]]; then
    gh pr edit "$number" --repo "$repo" --add-label ai:needs-fix >/dev/null 2>&1 || true
    return 0
  fi
  command -v hermes >/dev/null 2>&1 || return 2
  refresh_clone_base "$clone_path"
  release_clean_worktree_for_branch "$clone_path" "$head"
  sync_local_branch_from_origin "$clone_path" "$head"
  context="$(pr_repair_context "$repo" "$number")"

  task_title="[fix-pr-review] ${repo}#${number}: address review feedback"
  idempotency_key="fix-pr-review:${repo}:${number}:${reason}"
  body="Address review feedback for owner-authored PR ${repo}#${number}: ${title}

PR: ${url}
Head branch: ${head}
Triage reason: ${reason}

Repair context:
${context}

Policy:
- This is an owner/agent PR follow-up. Update the existing PR branch; do not create a replacement PR.
- Use gh for GitHub operations and GIT_MASTER=1 git for local git commands.
- Inspect review comments, check failures, and CI output before changing code.
- Preserve attribution; if touching work derived from another contributor, preserve commit metadata or add Co-authored-by trailers after explicit approval.
- Do not close, recreate, supersede, fix, merge, or take over any external contributor PR.
- Run relevant tests and update the existing PR with evidence. Do not merge; repo_pr_triage owns the merge gate."

  hermes kanban --board "$board" create \
    "$task_title" \
    --body "$body" \
    --assignee repo-fixer \
    --workspace "worktree:${clone_path}" \
    --branch "$head" \
    --priority 1 \
    --idempotency-key "$idempotency_key" \
    --skill repo-gh-cli-policy \
    --skill repo-fix-issue-pr >/dev/null
}

claim_pr_once() {
  local repo="$1" number="$2"
  local assignees
  [[ -n "$CLAIM_ASSIGNEE" ]] || return 0
  gh pr edit "$number" --repo "$repo" --add-assignee "$CLAIM_ASSIGNEE" >/dev/null 2>&1
  assignees="$(gh pr view "$number" --repo "$repo" --json assignees --jq '[.assignees[].login] | join(",")' 2>/dev/null || true)"
  [[ "$assignees" == "$CLAIM_ASSIGNEE" ]]
}

comment_pr_once() {
  local repo="$1" number="$2" reason="$3" title="$4"
  local marker body comments
  [[ "$COMMENT_ENABLED" == 1 && "$DRY_RUN" == 0 ]] || return 0
  marker="<!-- hermes-repo-agent:${reason} -->"
  comments="$(gh api "/repos/${repo}/issues/${number}/comments" --jq '.[].body' 2>/dev/null || true)"
  if grep -Fq "$marker" <<<"$comments"; then
    return 0
  fi
  body="${marker}
Hermes repo-agent blocked this PR: ${reason}.

${title}

The triage loop will keep watching this PR and queue Kanban repair work when the reason is fixable."
  gh pr comment "$number" --repo "$repo" --body "$body" >/dev/null 2>&1 || return 1
}

processed=0
merged=0
blocked=0
skipped=0
commented=0
failures=0

log "START mode=$([[ "$DRY_RUN" == 1 ]] && echo dry-run || echo live) comment=$COMMENT_ENABLED automerge=$AUTOMERGE require_approved=$REQUIRE_APPROVED allow_no_checks=$ALLOW_NO_CHECKS require_test_evidence=$REQUIRE_TEST_EVIDENCE pr_list_limit=$PR_LIST_LIMIT"

for entry in "${REPOS[@]}"; do
  IFS='|' read -r repo board clone_path repo_priority <<<"$entry"
  repo_owner="${repo%%/*}"
  retry_receipt_closures "$repo" || true
  if board_repo_busy "$board"; then
    log "BOARD_BUSY repo=$repo board=$board action=skip-pr-triage reason=active-task"
    continue
  fi
  if ! prs_json="$(gh pr list --repo "$repo" --state open --limit "$PR_LIST_LIMIT" --json number,title,url,headRefName,baseRefName,headRefOid,baseRefOid,isDraft,mergeStateStatus,reviewDecision,labels,author)"; then
    log "PR_LIST_FAILED repo=$repo"
    failures=$((failures + 1))
    continue
  fi
  if [[ "$prs_json" == "[]" ]]; then
    log "NO_OPEN_PRS repo=$repo"
    continue
  fi

  while IFS=$'\x1f' read -r number title url head base head_oid base_oid draft merge_state review_decision labels author; do
    [[ -n "${number:-}" ]] || continue
    processed=$((processed + 1))
    decision=""
    reason=""

    if [[ -n "$CLAIM_ASSIGNEE" && -n "$author" && "$author" == "$repo_owner" && "$head" == ai/fix/* ]]; then
      if [[ "$DRY_RUN" == 1 ]]; then
        log "DRY_RUN repo=$repo pr=$number action=would-assign assignee=$CLAIM_ASSIGNEE"
      elif ! claim_pr_once "$repo" "$number"; then
        log "ASSIGN_FAILED repo=$repo pr=$number assignee=$CLAIM_ASSIGNEE"
        failures=$((failures + 1))
        continue
      else
        log "PR_ASSIGNED repo=$repo pr=$number assignee=$CLAIM_ASSIGNEE"
      fi
    fi

    if [[ "$draft" == 1 ]]; then
      decision="skip"
      reason="draft-pr"
      skipped=$((skipped + 1))
    elif [[ -n "$author" && "$author" != "$repo_owner" ]]; then
      decision="skip"
      reason="external-author-pr-no-agent-action"
      skipped=$((skipped + 1))
    elif [[ "$head" != ai/fix/* ]]; then
      decision="skip"
      reason="head-branch-not-ai-fix"
      skipped=$((skipped + 1))
    elif [[ ",$labels," != *",ai:generated,"* || ",$labels," != *",ai:pr-opened,"* ]]; then
      if [[ "$DRY_RUN" == 1 ]]; then
        log "DRY_RUN repo=$repo pr=$number action=repair-labels"
        labels="${labels:+$labels,}ai:generated,ai:pr-opened"
      elif gh pr edit "$number" --repo "$repo" --add-label ai:generated --add-label ai:pr-opened >/dev/null; then
        log "LABELS_REPAIRED repo=$repo pr=$number"
        labels="${labels:+$labels,}ai:generated,ai:pr-opened"
      else
        log "LABEL_REPAIR_FAILED repo=$repo pr=$number"
        decision="skip"
        reason="label-repair-failed"
        skipped=$((skipped + 1))
        failures=$((failures + 1))
      fi
    fi

    if [[ -n "$decision" ]]; then
      :
    elif [[ ",$labels," != *",ai:generated,"* || ",$labels," != *",ai:pr-opened,"* ]]; then
      decision="skip"
      reason="missing-required-ai-labels"
      skipped=$((skipped + 1))
    elif [[ "$base" != "main" ]]; then
      decision="merge-blocked"
      reason="base-not-main"
      blocked=$((blocked + 1))
    elif [[ "$merge_state" == "CLEAN" ]]; then
      if [[ "$REQUIRE_APPROVED" == 1 && "$review_decision" != "APPROVED" ]]; then
        decision="merge-blocked"
        reason="review-not-approved"
        blocked=$((blocked + 1))
      elif ! checks_pass "$repo" "$number"; then
        decision="fix"
        reason="checks-not-passing"
        blocked=$((blocked + 1))
      elif [[ "$REQUIRE_TEST_EVIDENCE" == 1 ]] && ! pr_has_test_evidence "$repo" "$number"; then
        decision="fix"
        reason="test-evidence-missing"
        blocked=$((blocked + 1))
      elif [[ "$AUTOMERGE" != 1 ]]; then
        decision="merge-blocked"
        reason="automerge-disabled"
        blocked=$((blocked + 1))
      else
        decision="merge"
        reason="own-pr-clean"
      fi
    else
      decision="fix"
      reason="merge-state-${merge_state:-unknown}"
      blocked=$((blocked + 1))
    fi

    log "DECISION repo=$repo pr=$number decision=$decision reason=$reason head=$head base=$base merge_state=${merge_state:-none} labels=$(printf '%q' "$labels")"

    if [[ "$DRY_RUN" == 0 && "$decision" == "merge" ]]; then
      if merge_and_finalize "$repo" "$number" "$clone_path" "$head"; then
        merged=$((merged + 1))
      else
        merge_rc=$?
        log "MERGE_FINALIZATION_FAILED repo=$repo pr=$number rc=$merge_rc"
        failures=$((failures + 1))
        continue
      fi
    elif [[ "$DRY_RUN" == 0 && "$decision" == "fix" ]]; then
      if comment_pr_once "$repo" "$number" "$reason" "Queued Kanban follow-up for PR ${repo}#${number}."; then
        commented=$((commented + 1))
      fi
      if create_review_fix_task "$repo" "$number" "$title" "$url" "$head" "$reason"; then
        log "FIX_TASK_CREATED repo=$repo pr=$number reason=$reason head=$head"
      else
        log "FIX_TASK_FAILED repo=$repo pr=$number reason=$reason head=$head"
        failures=$((failures + 1))
      fi
    elif [[ "$DRY_RUN" == 0 && "$decision" == "merge-blocked" ]]; then
      if comment_pr_once "$repo" "$number" "$reason" "No repair task was queued for PR ${repo}#${number}."; then
        commented=$((commented + 1))
      fi
    elif [[ "$DRY_RUN" == 1 ]]; then
      log "DRY_RUN repo=$repo pr=$number action=would-$decision reason=$reason"
    fi
  done < <(extract_prs "$prs_json")
done

log "DONE mode=$([[ "$DRY_RUN" == 1 ]] && echo dry-run || echo live) processed=$processed skipped=$skipped blocked=$blocked commented=$commented merged=$merged failures=$failures"
[[ "$failures" -eq 0 ]]
