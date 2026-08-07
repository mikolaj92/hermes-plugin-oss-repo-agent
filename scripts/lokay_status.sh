#!/usr/bin/env bash
set -euo pipefail

# One-screen operational status for the Hermes lokay pipeline.
# Production topology: one resident supervisor LaunchAgent; twelve child
# commands are logical inventory only (never installed process agents).

export HOME="${HOME:-/Users/mini-m4-main}"
export PATH="${PATH:-/Users/mini-m4-main/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}"

LOG_DIR="${HERMES_LOKAY_LOG_DIR:-$HOME/.hermes/logs}"
WORKTREE_ROOT="${HERMES_LOKAY_WORKTREE_ROOT:-$HOME/.hermes/worktrees/lokay}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lokay_repos.sh"

valid_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }
FALA_MAX_RUN_AGE_SECONDS="${HERMES_LOKAY_FALA_MAX_RUN_AGE_SECONDS:-1800}"
FALA_REQUIRE_LIVE="${HERMES_LOKAY_FALA_REQUIRE_LIVE:-1}"
# 2x singleton TTL (90s); status.loop_timestamp older than this is stale.
SUPERVISOR_STATUS_MAX_AGE_SECONDS="${HERMES_LOKAY_SUPERVISOR_STATUS_MAX_AGE_SECONDS:-180}"
if ! valid_uint "$FALA_MAX_RUN_AGE_SECONDS" || ! valid_uint "$SUPERVISOR_STATUS_MAX_AGE_SECONDS" || [[ "$FALA_REQUIRE_LIVE" != 0 && "$FALA_REQUIRE_LIVE" != 1 ]]; then
  printf 'invalid-env FALA_MAX_RUN_AGE_SECONDS=%s SUPERVISOR_STATUS_MAX_AGE_SECONDS=%s FALA_REQUIRE_LIVE=%s\n' \
    "$FALA_MAX_RUN_AGE_SECONDS" "$SUPERVISOR_STATUS_MAX_AGE_SECONDS" "$FALA_REQUIRE_LIVE" >&2
  exit 2
fi

usage() {
  cat <<'USAGE'
Usage: lokay_status.sh

Prints launchd state, worker locks, repo queue counts, and recent lokay
decisions in one terminal-friendly view.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

status_failures=0
require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'missing-command name=%s\n' "$1"
    status_failures=$((status_failures + 1))
  fi
}
launchctl_query() {
  local output
  if ! output="$(launchctl print "$1" 2>&1)"; then
    [[ "$output" == *"could not find service"* || "$output" == *"No such process"* || "$output" == *"Could not find service"* ]] && return 1
    [[ "$output" == *"Domain does not support specified action"* ]] && return 3
    printf 'launchctl-error target=%s error=%s\n' "$1" "$output" >&2
    return 2
  fi
  printf '%s\n' "$output"
}
uid="$(id -u)"
launchctl_label_query() {
  local label="$1" domain output found="" available=0 query_status
  for domain in "user/$uid" "gui/$uid"; do
    if output="$(launchctl_query "$domain/$label")"; then
      available=1
      if [[ -n "$found" ]]; then
        printf 'launchctl-ambiguous label=%s domains=user/%s,gui/%s\n' "$label" "$uid" "$uid" >&2
        return 2
      fi
      found="$output"
    else
      query_status=$?
      [[ "$query_status" -eq 1 ]] && available=1
      [[ "$query_status" -ne 2 ]] || return 2
    fi
  done
  if [[ -z "$found" && "$available" -eq 0 ]]; then
    printf 'launchctl-unavailable label=%s\n' "$label" >&2
    return 2
  fi
  [[ -n "$found" ]] || return 1
  printf '%s\n' "$found"
}

validate_supervisor_status() {
  # Fail-closed observational read of supervisor status.json.
  # Args: fala_db max_age_seconds deployment_root config_path [launchctl_dump]
  local fala_db="$1" max_age="$2" deployment_root="$3" config_path="${4:-}" launchctl_dump="${5:-}"
  HERMES_LOKAY_STATUS_LAUNCHCTL_DUMP="$launchctl_dump" \
    python3 "$SCRIPT_DIR/lokay_supervisor_status_check.py" \
      "$fala_db" "$max_age" "$deployment_root" "$config_path"
}

DEPLOYMENT_ROOT="${HERMES_LOKAY_DEPLOYMENT_ROOT:-$HOME/.hermes/lokay/deployment}"
FALA_DB="${HERMES_LOKAY_FALA_DB:-$HOME/.hermes/lokay/fala/state.sqlite}"
LAUNCH_AGENTS_DIR="${HERMES_LOKAY_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
SUPERVISOR_LABEL="com.mikolaj92.lokay.supervisor"
AGGREGATE_FALA_LABEL="com.mikolaj92.lokay.fala-tick-all"
PROCESS_LABELS=(
  com.mikolaj92.lokay.repo-issue-poll
  com.mikolaj92.lokay.issue-triage
  com.mikolaj92.lokay.issue-feedback
  com.mikolaj92.lokay.issue-split
  com.mikolaj92.lokay.issue-close
  com.mikolaj92.lokay.issue-ready
  com.mikolaj92.lokay.issue-to-pr
  com.mikolaj92.lokay.pr-triage
  com.mikolaj92.lokay.pr-repair
  com.mikolaj92.lokay.pr-merge
  com.mikolaj92.lokay.cleanup
  com.mikolaj92.lokay.cleanup-reconcile
)
status_failures=0

supervisor_loaded=0
query_status=0
if launchctl_label_query "$SUPERVISOR_LABEL" >/dev/null; then
  supervisor_loaded=1
else
  query_status=$?
fi
[[ "$query_status" -ne 2 ]] || status_failures=$((status_failures + 1))
process_loaded=0
for process_label in "${PROCESS_LABELS[@]}"; do
  query_status=0
  if launchctl_label_query "$process_label" >/dev/null; then
    process_loaded=$((process_loaded + 1))
  else
    query_status=$?
  fi
  [[ "$query_status" -ne 2 ]] || status_failures=$((status_failures + 1))
done
aggregate_loaded=0
query_status=0
if launchctl_label_query "$AGGREGATE_FALA_LABEL" >/dev/null; then
  aggregate_loaded=1
else
  query_status=$?
fi
[[ "$query_status" -ne 2 ]] || status_failures=$((status_failures + 1))
legacy_loaded_labels=()
for legacy_label in \
  com.mikolaj92.hermes.repo-issue-intake \
  com.mikolaj92.hermes.repo-issue-to-pr-dispatch \
  com.mikolaj92.hermes.repo-pr-triage \
  com.mikolaj92.hermes.repo-agent-cleanup \
  com.mikolaj92.hermes.repo-agent-fala-tick-all; do
  query_status=0
  if launchctl_label_query "$legacy_label" >/dev/null; then legacy_loaded_labels+=("$legacy_label"); else query_status=$?; fi
  [[ "$query_status" -ne 2 ]] || status_failures=$((status_failures + 1))
done
health_repair_loaded=0
query_status=0
if launchctl_label_query "com.mikolaj92.hermes.repo-agent-health" >/dev/null; then
  if grep -q -- '--repair' "$HOME/Library/LaunchAgents/com.mikolaj92.hermes.repo-agent-health.plist" 2>/dev/null; then health_repair_loaded=1; fi
else query_status=$?; fi
[[ "$query_status" -ne 2 ]] || status_failures=$((status_failures + 1))

printf '\nFala gate\n'
if [[ "$supervisor_loaded" -ne 1 ]]; then
  printf '  ERROR supervisor-job-missing label=%s loaded=%s expected=1\n' "$SUPERVISOR_LABEL" "$supervisor_loaded"
  status_failures=$((status_failures + 1))
else
  printf '  supervisor job loaded=1 expected=1 label=%s\n' "$SUPERVISOR_LABEL"
fi
if [[ ! -f "$LAUNCH_AGENTS_DIR/$SUPERVISOR_LABEL.plist" ]]; then
  printf '  ERROR supervisor-plist-missing path=%s\n' "$LAUNCH_AGENTS_DIR/$SUPERVISOR_LABEL.plist"
  status_failures=$((status_failures + 1))
fi
if [[ "$process_loaded" -ne 0 ]]; then
  printf '  ERROR process-production-jobs-present loaded=%s expected=0\n' "$process_loaded"
  status_failures=$((status_failures + 1))
fi
if [[ "$aggregate_loaded" -eq 1 ]]; then
  printf '  ERROR aggregate-production-job-loaded label=%s\n' "$AGGREGATE_FALA_LABEL"
  status_failures=$((status_failures + 1))
fi
if [[ -f "$LAUNCH_AGENTS_DIR/$AGGREGATE_FALA_LABEL.plist" ]]; then
  printf '  ERROR aggregate-production-plist-present path=%s\n' "$LAUNCH_AGENTS_DIR/$AGGREGATE_FALA_LABEL.plist"
  status_failures=$((status_failures + 1))
fi
for process_label in "${PROCESS_LABELS[@]}"; do
  process_plist="$LAUNCH_AGENTS_DIR/$process_label.plist"
  loaded=no
  if launchctl_label_query "$process_label" >/dev/null; then loaded=yes; fi
  if [[ "$loaded" == yes || -f "$process_plist" ]]; then
    printf '  ERROR process-production residual label=%s loaded=%s plist=%s\n' "$process_label" "$loaded" "$process_plist"
    status_failures=$((status_failures + 1))
  fi
done
current_target=""
if [[ -L "$DEPLOYMENT_ROOT/current" ]]; then
  current_target="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$DEPLOYMENT_ROOT/current" 2>/dev/null || true)"
  printf '  deployment current=%s candidate=%s\n' "$DEPLOYMENT_ROOT/current" "${current_target:-unknown}"
else
  printf '  deployment current=missing candidate=unknown\n'
fi
if [[ -z "$current_target" || ! -f "$current_target/manifest.json" ]]; then
  printf '  candidate gate=FAIL current=%s reason=missing-or-invalid\n' "$DEPLOYMENT_ROOT/current"
  status_failures=$((status_failures + 1))
else
  fala_check=""
  managed_python=""
  pythonpath=""
  source_dir=""
  tools_parent="$(cd "$SCRIPT_DIR/.." && pwd)"
  if [[ "$current_target" == /* && -d "$current_target" && ! -L "$current_target" ]]; then
    source_dir="$current_target/source/project/src"
    managed_python="$current_target/source/project/.venv/bin/python"
    pythonpath="$source_dir"
  fi
  if [[ -n "$managed_python" && "$managed_python" == /* && -x "$managed_python" && ! -L "$managed_python" && -d "$source_dir" && ! -L "$source_dir" ]] \
    && PYTHONPATH="$pythonpath" "$managed_python" -c 'import sys,tomllib; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1 \
    && fala_check="$(PYTHONPATH="$pythonpath" "$managed_python" - "$current_target" "$LAUNCH_AGENTS_DIR" "$FALA_REQUIRE_LIVE" "$DEPLOYMENT_ROOT" "$tools_parent" "$SUPERVISOR_LABEL" <<'PY'
import hashlib, json, pathlib, plistlib, sys

candidate = pathlib.Path(sys.argv[1]).resolve()
installed_root = pathlib.Path(sys.argv[2]).expanduser()
require_live = sys.argv[3] == "1"
deployment_root = pathlib.Path(sys.argv[4]).expanduser().resolve()
tools_parent = pathlib.Path(sys.argv[5]).resolve()
supervisor_label = sys.argv[6]
sys.path.insert(0, str(tools_parent))

from tools.deployment_parity import (
    AGGREGATE_FALA_LABEL,
    AGGREGATE_FALA_MODULE,
    DeploymentParityError,
    sha256,
    validate_fala_candidate,
)

RESIDUAL_PROCESS_LABELS = (
    "com.mikolaj92.lokay.repo-issue-poll",
    "com.mikolaj92.lokay.issue-triage",
    "com.mikolaj92.lokay.issue-feedback",
    "com.mikolaj92.lokay.issue-split",
    "com.mikolaj92.lokay.issue-close",
    "com.mikolaj92.lokay.issue-ready",
    "com.mikolaj92.lokay.issue-to-pr",
    "com.mikolaj92.lokay.pr-triage",
    "com.mikolaj92.lokay.pr-repair",
    "com.mikolaj92.lokay.pr-merge",
    "com.mikolaj92.lokay.cleanup",
    "com.mikolaj92.lokay.cleanup-reconcile",
)

errors = []
try:
    if candidate.parent != (deployment_root / "versions").resolve():
        errors.append("current-outside-versions")
    result = validate_fala_candidate(candidate, deployment_root=deployment_root)
    manifest = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
    mode = manifest.get("mode")
    if require_live and mode != "live":
        errors.append("production-gate-requires-live")
    processes = manifest.get("processes") or []
    if not isinstance(processes, list):
        errors.append("process-catalog-count-invalid")
        processes = []
    dispatch_commands = manifest.get("dispatch_commands") or []
    if not isinstance(dispatch_commands, list):
        dispatch_commands = []
    inventory_count = len(dispatch_commands) if dispatch_commands else len(processes)
    if inventory_count != 12:
        errors.append("process-catalog-count-invalid")
    if (installed_root / f"{AGGREGATE_FALA_LABEL}.plist").exists():
        errors.append("aggregate-production-plist-present")
    for residual in RESIDUAL_PROCESS_LABELS:
        if (installed_root / f"{residual}.plist").exists():
            errors.append(f"process-production-plist-present:{residual}")
    for process in processes:
        if not isinstance(process, dict):
            errors.append("process-row-invalid")
            continue
        process_id = process.get("id")
        if not isinstance(process_id, str) or not process_id:
            continue
        residual = f"com.mikolaj92.lokay.{process_id.replace('_', '-')}"
        if (installed_root / f"{residual}.plist").exists() and residual != supervisor_label:
            errors.append(f"process-production-plist-present:{residual}")
    relative = f"launchd/{supervisor_label}.plist"
    candidate_plist = candidate / relative
    installed_plist = installed_root / f"{supervisor_label}.plist"
    if not candidate_plist.is_file():
        errors.append(f"candidate-plist-missing:{supervisor_label}")
    elif not installed_plist.is_file() or installed_plist.is_symlink():
        errors.append(f"installed-plist-missing:{supervisor_label}")
    else:
        if sha256(candidate_plist) != hashlib.sha256(installed_plist.read_bytes()).hexdigest():
            errors.append(f"installed-plist-not-current:{supervisor_label}")
        try:
            document = plistlib.loads(candidate_plist.read_bytes())
        except Exception:
            errors.append(f"candidate-plist-invalid:{supervisor_label}")
        else:
            args = document.get("ProgramArguments")
            label = document.get("Label")
            if label != supervisor_label:
                errors.append(f"supervisor-label-mismatch:{label}")
            if not isinstance(args, list) or AGGREGATE_FALA_MODULE in args or AGGREGATE_FALA_LABEL in map(str, args):
                errors.append(f"aggregate-or-invalid-args:{supervisor_label}")
            elif "lokay.supervisor" not in args:
                errors.append(f"supervisor-args-invalid:{supervisor_label}")
            if require_live and (not isinstance(args, list) or "--live" not in args):
                errors.append(f"production-gate-requires-live:{supervisor_label}")
    if errors:
        print(";".join(errors))
        raise SystemExit(1)
    print(
        f"candidate_id={result['candidate_id']} supervisor_label={supervisor_label} "
        f"inventory_count={inventory_count} mode={mode}"
    )
except DeploymentParityError as exc:
    details = ";".join(str(item) for item in (exc.result.get("errors") or ["candidate-invalid"]))
    print(details)
    raise SystemExit(1)
except Exception as exc:
    print(f"candidate-validation-error={type(exc).__name__}")
    raise SystemExit(1)
PY
  )"; then
    printf '  candidate gate=PASS current=%s %s\n' "$current_target" "$fala_check"
  else
    printf '  candidate gate=FAIL current=%s reason=%s\n' "$current_target" "${fala_check:-validation-error}"
    status_failures=$((status_failures + 1))
  fi
fi
for cmd in gh hermes launchctl find tail date python3; do
  require_cmd "$cmd"
done
if [[ -f "$FALA_DB" ]]; then
  db_check=""
  if db_check="$(python3 - "$FALA_DB" "$FALA_MAX_RUN_AGE_SECONDS" "$FALA_REQUIRE_LIVE" <<'PY'
import json, sqlite3, sys
from datetime import datetime, timezone
path, max_age_text, require_live_text=sys.argv[1:]; max_age=int(max_age_text); require_live=require_live_text=="1"
try:
  with sqlite3.connect(path) as db:
    integrity=db.execute("PRAGMA integrity_check").fetchone()[0]; tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}; required={"runs","processes","schema_migrations"}; version=db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] if "schema_migrations" in tables else None; runs=db.execute("SELECT id,status,updated_at,metadata FROM runs ORDER BY updated_at DESC").fetchall() if "runs" in tables else []
    if integrity!="ok" or version!=6 or not required.issubset(tables) or not runs: raise ValueError("schema-or-runs-invalid")
    latest=runs[0]; unresolved=[]; unsafe={"created","active","waiting","retry_wait","cancel_requested","failed","cancelled","timed_out"}
    for row in runs:
      if len(row)!=4 or not row[0] or not row[1] or not row[2]: raise ValueError("run-row-invalid")
      stamp=datetime.fromisoformat(str(row[2]).replace("Z","+00:00")); stamp=stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc); age=max(0,int((datetime.now(timezone.utc)-stamp).total_seconds()))
      if str(row[1]) in unsafe: unresolved.append((str(row[0]),str(row[1]),age))
    if not latest[0] or not latest[1] or not latest[2]: raise ValueError("latest-run-missing-or-invalid")
    stamp=datetime.fromisoformat(str(latest[2]).replace("Z","+00:00")); stamp=stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc); age=max(0,int((datetime.now(timezone.utc)-stamp).total_seconds()))
    if age>max_age: raise ValueError(f"latest-run-stale:{age}")
    status=str(latest[1])
    if status!="completed": raise ValueError(f"latest-run-not-completed:{status}")
    if unresolved: raise ValueError(f"unresolved-runs:{unresolved}")
    metadata=json.loads(latest[3] or "{}"); mode=metadata.get("mode")
    if mode not in {"live","dry-run"}: mode="dry-run" if metadata.get("dry_run") is True else ("live" if metadata.get("dry_run") is False else None)
    if mode is None: raise ValueError("latest-run-mode-missing")
    if require_live and mode!="live": raise ValueError(f"latest-run-not-live:{mode}")
    counts={str(r[0]):int(r[1]) for r in db.execute("SELECT status,COUNT(*) FROM processes WHERE run_id=? GROUP BY status",(latest[0],))}; failed=sum(counts.get(k,0) for k in ("failed","cancelled","timed_out")); waiting=sum(counts.get(k,0) for k in ("waiting","retry_wait"))
    if failed: raise ValueError(f"failed-processes:{failed}")
    if waiting: raise ValueError(f"waiting-processes:{waiting}")
  print(f"integrity={integrity} schema={version} latest_id={latest[0]} latest_status={status} run_mode={mode} run_age_seconds={age} unresolved_runs={len(unresolved)} failed_processes={failed} waiting_processes={waiting}")
except Exception as exc:
  print(f"integrity=unknown error={type(exc).__name__}:{exc}"); raise SystemExit(1)
PY
  )"; then
    printf '  db path=%s %s\n' "$FALA_DB" "$db_check"
  else
    printf '  db path=%s %s\n' "$FALA_DB" "${db_check:-integrity=unknown}"
    status_failures=$((status_failures + 1))
  fi
else
  printf '  db path=%s presence=missing integrity=unknown\n' "$FALA_DB"
  status_failures=$((status_failures + 1))
fi

printf '\nSupervisor status\n'
supervisor_status_dump=""
if [[ "$supervisor_loaded" -eq 1 ]]; then
  supervisor_status_dump="$(launchctl_label_query "$SUPERVISOR_LABEL" 2>/dev/null || true)"
fi
status_check=""
if status_check="$(validate_supervisor_status "$FALA_DB" "$SUPERVISOR_STATUS_MAX_AGE_SECONDS" "$DEPLOYMENT_ROOT" "${HERMES_LOKAY_CONFIG:-}" "$supervisor_status_dump")"; then
  printf '  OK %s\n' "$status_check"
else
  printf '  ERROR supervisor-status %s\n' "${status_check:-validation-failed}"
  status_failures=$((status_failures + 1))
fi
legacy_count="${#legacy_loaded_labels[@]}"
mutator_gate="single-or-none"
if [[ "$legacy_count" -gt 0 ]]; then
  mutator_gate=FAIL
  status_failures=$((status_failures + 1))
  printf '  ERROR legacy-mutator-unexpected-loaded labels=%s
' "${legacy_loaded_labels[*]}"
fi
if [[ "$aggregate_loaded" -eq 1 || "$health_repair_loaded" -eq 1 || "$process_loaded" -gt 0 ]]; then
  mutator_gate=FAIL
  status_failures=$((status_failures + 1))
fi
if [[ "$mutator_gate" == FAIL && ("$aggregate_loaded" -eq 1 || "$process_loaded" -gt 0 || "$supervisor_loaded" -eq 1) && ("$legacy_count" -gt 0 || "$health_repair_loaded" -eq 1 || "$aggregate_loaded" -eq 1 || "$process_loaded" -gt 0) ]]; then
  printf '  ERROR dual-mutator legacy-health-repair-or-aggregate-active process_loaded=%s aggregate_loaded=%s supervisor_loaded=%s
' "$process_loaded" "$aggregate_loaded" "$supervisor_loaded"
fi
uid="$(id -u)"
jobs=(
  "supervisor|$SUPERVISOR_LABEL"
  "update|com.mikolaj92.lokay.hermes-update"
  "health|com.mikolaj92.lokay.health"
)

printf 'lokay status %s\n\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

printf 'Launchd\n'
for item in "${jobs[@]}"; do
  IFS='|' read -r name label <<<"$item"
  if info="$(launchctl_label_query "$label" 2>/dev/null)"; then
    state="$(printf '%s\n' "$info" | awk -F '= ' '/state =/ {print $2; exit}')"
    runs="$(printf '%s\n' "$info" | awk -F '= ' '/runs =/ {gsub(/[^0-9].*/, "", $2); print $2; exit}')"
    last="$(printf '%s\n' "$info" | awk -F '= ' '/last exit code =/ {gsub(/[^0-9-].*/, "", $2); print $2; exit}')"
    printf '  %-9s state=%s runs=%s last_exit=%s\n' "$name" "${state:-unknown}" "${runs:-0}" "${last:-unknown}"
    if [[ "$name" == supervisor && ( -z "$last" || "$last" != 0) ]]; then
      printf '  ERROR supervisor-last-exit-invalid label=%s exit_code=%s
' "$label" "${last:-unknown}"
      status_failures=$((status_failures + 1))
    elif [[ "$name" == health && -z "$last" ]]; then
      status_failures=$((status_failures + 1))
    fi
  else
    printf '  %-9s missing label=%s\n' "$name" "$label"
    if [[ "$name" == supervisor ]]; then
      status_failures=$((status_failures + 1))
    fi
  fi
done

printf '\nWorkers\n'
locks="$(find "$WORKTREE_ROOT" -maxdepth 5 -type f -path '*/.agent.lock/pid' 2>/dev/null || true)"
if [[ -z "$locks" ]]; then
  printf '  none\n'
else
  while IFS= read -r pid_file; do
    [[ -n "$pid_file" ]] || continue
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
      printf '  active pid=%s lock=%s\n' "$pid" "$(dirname "$pid_file")"
    else
      printf '  dead pid=%s lock=%s\n' "${pid:-missing}" "$(dirname "$pid_file")"
    fi
  done <<<"$locks"
fi

printf '\nQueues\n'
if ! repo_data="$(lokay_repos)"; then
  printf '  ERROR registry-unavailable\n'
  status_failures=$((status_failures + 1))
else
  while IFS='|' read -r repo board clone_path repo_priority; do
    [[ -n "$repo" ]] || continue
    if ! open_prs="$(gh pr list --repo "$repo" --state open --json number --jq 'length' 2>&1)"; then printf '  %-36s prs=ERROR:%s\n' "$repo" "$open_prs"; status_failures=$((status_failures + 1)); continue; fi
    if ! open_issues="$(gh issue list --repo "$repo" --state open --json number --jq 'length' 2>&1)"; then printf '  %-36s issues=ERROR:%s\n' "$repo" "$open_issues"; status_failures=$((status_failures + 1)); continue; fi
    if ! stats="$(hermes kanban --board "$board" stats 2>&1)"; then printf '  %-36s kanban=ERROR:%s\n' "$repo" "$stats"; status_failures=$((status_failures + 1)); continue; fi
    printf '  %-36s issues=%s prs=%s %s\n' "$repo" "$open_issues" "$open_prs" "$(printf '%s\n' "$stats" | tr '\n' ' ' | sed 's/  */ /g')"
  done <<<"$repo_data"
fi

printf '\nRecent Fala Runs\n'
if [[ -f "$FALA_DB" ]]; then
  python3 - "$FALA_DB" <<'PY' || status_failures=$((status_failures + 1))
import json, sqlite3, sys

try:
    with sqlite3.connect(sys.argv[1]) as db:
        runs = db.execute(
            "SELECT id,status,created_at,metadata FROM runs ORDER BY created_at DESC LIMIT 8"
        ).fetchall()
        for run_id, status, created_at, metadata_raw in runs:
            metadata = json.loads(metadata_raw or "{}")
            outputs = db.execute(
                "SELECT status,output_json FROM processes WHERE run_id=?", (run_id,)
            ).fetchall()
            failed = sum(item_status in {"failed", "cancelled", "timed_out"} for item_status, _ in outputs)
            waiting = sum(item_status in {"waiting", "retry_wait", "running", "pending"} for item_status, _ in outputs)
            worked = False
            for _, raw in outputs:
                output = json.loads(raw or "{}")
                values = output.get("values", output)
                if not isinstance(values, dict):
                    continue
                worked = worked or bool(values.get("mutated") or values.get("selected"))

            activity = "worked" if worked else "noop"
            mode = metadata.get("mode", "unknown")
            print(f"  {created_at} run_id={run_id} mode={mode} status={status} activity={activity} failed={failed} waiting={waiting}")
except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
    print(f"  ERROR fala-run-history {type(exc).__name__}:{exc}")
    raise SystemExit(1)
PY
else
  printf '  unavailable db=%s\n' "$FALA_DB"
fi

printf '\nRecent Decisions\n'
RECENT_SIGNAL_PATTERN='DECISION|CLAUDE_|WORKTREE_|LOCAL_BRANCH_|DONE|WARN|ERROR|ASSIGN_FAILED|PR_ASSIGNED|FIX_TASK_CREATED|FIX_TASK_FAILED|LOCK_HELD|KANBAN_LIST_FAILED|PR_LIST_FAILED|MERGE_FAILED|watchdog-worker-'
for log in "$LOG_DIR/repo-issue-to-pr-dispatch.log" "$LOG_DIR/repo-pr-triage.log" "$LOG_DIR/lokay-cleanup.log" "$LOG_DIR/lokay-hermes-update.log" "$LOG_DIR/lokay-health.log" "$LOG_DIR/lokay-supervisor.out.log"; do
  [[ -f "$log" ]] || continue
  printf '  %s\n' "$(basename "$log")"
  recent="$(tail -n 80 "$log" | grep -E "$RECENT_SIGNAL_PATTERN" | tail -n 8 || true)"
  if [[ -n "$recent" ]]; then printf '%s\n' "$recent" | sed 's/^/    /'; else printf '    no recent decisions\n'; fi
done
printf '\nGate summary failures=%s\n' "$status_failures"
if [[ "$status_failures" -ne 0 || "$mutator_gate" == FAIL ]]; then
  exit 1
fi
