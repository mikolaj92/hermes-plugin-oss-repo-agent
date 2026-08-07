#!/usr/bin/env bash
set -euo pipefail

# Health/watchdog for the Hermes lokay launchd pipeline.
# Production topology: one resident supervisor LaunchAgent; twelve child
# commands are logical inventory only (never installed process agents).

export HOME="${HOME:-/Users/mini-m4-main}"
export PATH="${PATH:-/Users/mini-m4-main/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}"

LOG_FILE="${HERMES_LOKAY_HEALTH_LOG:-$HOME/.hermes/logs/lokay-health.log}"
STALE_LOCK_MINUTES="${HERMES_LOKAY_STALE_LOCK_MINUTES:-180}"
MAX_LOG_AGE_SECONDS="${HERMES_LOKAY_MAX_LOG_AGE_SECONDS:-1800}"
WORKER_TIMEOUT_SECONDS="${HERMES_LOKAY_WORKER_TIMEOUT_SECONDS:-7200}"
MIN_FREE_GB="${HERMES_LOKAY_MIN_FREE_GB:-5}"
WORKTREE_ROOT=""
DEPLOYMENT_ROOT="${HERMES_LOKAY_DEPLOYMENT_ROOT:-$HOME/.hermes/lokay/deployment}"
FALA_DB="${HERMES_LOKAY_FALA_DB:-$HOME/.hermes/lokay/fala/state.sqlite}"
LAUNCH_AGENTS_DIR="${HERMES_LOKAY_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
SUPERVISOR_LABEL="com.mikolaj92.lokay.supervisor"
AGGREGATE_FALA_LABEL="com.mikolaj92.lokay.fala-tick-all"
# Residual per-process labels: forbidden in production install/load (inventory only).
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
FALA_MAX_RUN_AGE_SECONDS="${HERMES_LOKAY_FALA_MAX_RUN_AGE_SECONDS:-1800}"
FALA_REQUIRE_LIVE="${HERMES_LOKAY_FALA_REQUIRE_LIVE:-1}"
# 2x singleton TTL (90s); status.loop_timestamp older than this is stale.
SUPERVISOR_STATUS_MAX_AGE_SECONDS="${HERMES_LOKAY_SUPERVISOR_STATUS_MAX_AGE_SECONDS:-180}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

valid_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }
for _env in STALE_LOCK_MINUTES MAX_LOG_AGE_SECONDS WORKER_TIMEOUT_SECONDS MIN_FREE_GB FALA_MAX_RUN_AGE_SECONDS SUPERVISOR_STATUS_MAX_AGE_SECONDS; do
  _value="${!_env}"
  if ! valid_uint "$_value"; then printf 'invalid-env name=%s value=%s\n' "$_env" "$_value" >&2; exit 2; fi
done
if [[ "$FALA_REQUIRE_LIVE" != 0 && "$FALA_REQUIRE_LIVE" != 1 ]]; then
  printf 'invalid-env name=HERMES_LOKAY_FALA_REQUIRE_LIVE value=%s\n' "$FALA_REQUIRE_LIVE" >&2; exit 2
fi

validate_fala_current() {
  local managed_python pythonpath source_dir tools_parent
  if [[ "$1" != /* || ! -d "$1" || -L "$1" ]]; then
    printf 'toml-parser-unavailable interpreter=%s/source/project/.venv/bin/python\n' "$1"
    return 1
  fi
  source_dir="$1/source/project/src"
  managed_python="$1/source/project/.venv/bin/python"
  pythonpath="$source_dir"
  tools_parent="$(cd "$SCRIPT_DIR/.." && pwd)"
  if [[ "$managed_python" != /* || ! -x "$managed_python" || -L "$managed_python" || ! -d "$source_dir" || -L "$source_dir" ]] \
    || ! PYTHONPATH="$pythonpath" "$managed_python" -c 'import sys,tomllib; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; then
    printf 'toml-parser-unavailable interpreter=%s\n' "$managed_python"
    return 1
  fi
  PYTHONPATH="$pythonpath" "$managed_python" - "$1" "$2" "$3" "$4" "$tools_parent" "$SUPERVISOR_LABEL" <<'PY'
import hashlib, json, pathlib, plistlib, sys

candidate = pathlib.Path(sys.argv[1]).resolve()
installed_root = pathlib.Path(sys.argv[2]).expanduser()
require_live = sys.argv[3] == "1"
deployment_root = pathlib.Path(sys.argv[4]).expanduser().resolve()
tools_parent = pathlib.Path(sys.argv[5]).resolve()
supervisor_label = sys.argv[6]
sys.path.insert(0, str(tools_parent))

from tools.deployment_parity import (  # noqa: E402
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
    aggregate_plist = installed_root / f"{AGGREGATE_FALA_LABEL}.plist"
    if aggregate_plist.exists():
        errors.append("aggregate-production-plist-present")
    for residual in RESIDUAL_PROCESS_LABELS:
        residual_plist = installed_root / f"{residual}.plist"
        if residual_plist.exists():
            errors.append(f"process-production-plist-present:{residual}")
    for process in processes:
        if not isinstance(process, dict):
            errors.append("process-row-invalid")
            continue
        process_id = process.get("id")
        if not isinstance(process_id, str) or not process_id:
            continue
        residual = f"com.mikolaj92.lokay.{process_id.replace('_', '-')}"
        residual_plist = installed_root / f"{residual}.plist"
        if residual_plist.exists() and residual != supervisor_label:
            errors.append(f"process-production-plist-present:{residual}")
    relative = f"launchd/{supervisor_label}.plist"
    candidate_plist = candidate / relative
    installed_plist = installed_root / f"{supervisor_label}.plist"
    if not candidate_plist.is_file():
        errors.append(f"candidate-plist-missing:{supervisor_label}")
    elif not installed_plist.is_file() or installed_plist.is_symlink():
        errors.append(f"installed-plist-missing:{supervisor_label}")
    else:
        candidate_bytes = candidate_plist.read_bytes()
        installed_bytes = installed_plist.read_bytes()
        if sha256(candidate_plist) != hashlib.sha256(installed_bytes).hexdigest():
            errors.append(f"installed-plist-not-current:{supervisor_label}")
        try:
            document = plistlib.loads(candidate_bytes)
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
}

validate_supervisor_status() {
  # Fail-closed observational read of supervisor status.json.
  # Args: fala_db max_age_seconds deployment_root config_path [launchctl_dump]
  local fala_db="$1" max_age="$2" deployment_root="$3" config_path="${4:-}" launchctl_dump="${5:-}"
  HERMES_LOKAY_STATUS_LAUNCHCTL_DUMP="$launchctl_dump" \
    python3 "$SCRIPT_DIR/lokay_supervisor_status_check.py" \
      "$fala_db" "$max_age" "$deployment_root" "$config_path"
}

source "$SCRIPT_DIR/lokay_repos.sh"

usage() {
  cat <<'USAGE'
Usage: lokay_health.sh

Observational checks only: launchd, deployment parity and Fala candidate
provenance, Fala DB freshness and safe run/process state, gh auth, disk space,
stale locks, recent logs, active workers, and the watched GitHub/Kanban queues.
It never removes lock artifacts, never mutates deployment state, and does not
bootstrap, enable, or reload LaunchAgents; deployment and launchd changes
remain explicit metadata-only or separately controlled operations.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repair)
      echo "unsupported argument: --repair (health is observational only)" >&2
      usage >&2
      exit 2
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

mkdir -p "$(dirname "$LOG_FILE")"

log() {
  local level="$1" message="$2"
  printf '%s %s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$level" "$message" | tee -a "$LOG_FILE"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { log ERROR "missing-command name=$1"; return 1; }
}
launchctl_query() {
  local output
  if ! output="$(launchctl print "$1" 2>&1)"; then
    [[ "$output" == *"could not find service"* || "$output" == *"No such process"* || "$output" == *"Could not find service"* ]] && return 1
    [[ "$output" == *"Domain does not support specified action"* ]] && return 3
    log ERROR "launchctl-query-unknown target=$1 details=$(printf '%q' "$output")"
    return 2
  fi
  printf '%s\n' "$output"
}
launchctl_label_query() {
  local label="$1" domain output found="" available=0 query_status
  for domain in "user/$uid" "gui/$uid"; do
    if output="$(launchctl_query "$domain/$label")"; then
      available=1
      if [[ -n "$found" ]]; then
        log ERROR "launchctl-domain-ambiguous label=$label"
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
    log ERROR "launchctl-domain-unavailable label=$label"
    return 2
  fi
  [[ -n "$found" ]] || return 1
  printf '%s\n' "$found"
}

uid="$(id -u)"
jobs=()
supervisor_plist="$LAUNCH_AGENTS_DIR/$SUPERVISOR_LABEL.plist"
supervisor_log=""
if [[ -f "$supervisor_plist" ]]; then
  supervisor_log="$(plutil -extract StandardOutPath raw -o - "$supervisor_plist" 2>/dev/null || true)"
fi
if [[ -z "$supervisor_log" ]]; then
  supervisor_log="${HERMES_LOKAY_LOG_DIR:-$HOME/.hermes/logs}/lokay-supervisor.out.log"
fi
jobs+=("$SUPERVISOR_LABEL|$supervisor_plist|$supervisor_log")
jobs+=("com.mikolaj92.lokay.hermes-update|$LAUNCH_AGENTS_DIR/com.mikolaj92.lokay.hermes-update.plist|$HOME/.hermes/logs/lokay-hermes-update.log")
repo_data=""
if ! repo_data="$(lokay_repos)"; then
  printf 'registry-error unavailable\n' >&2; exit 1
fi
if ! WORKTREE_ROOT="$(lokay_worktree_root)"; then
  printf 'registry-error worktree-root-unavailable\n' >&2; exit 1
fi
MANAGED_PYTHON="$(lokay_managed_python)" || { printf 'registry-error interpreter-unavailable\n' >&2; exit 1; }
MANAGED_PYTHONPATH="$(lokay_candidate_pythonpath)" || { printf 'registry-error source-unavailable\n' >&2; exit 1; }
repos=()
while IFS= read -r repo_entry; do repos+=("$repo_entry"); done <<<"$repo_data"

failures=0
warnings=0
parity_enabled="${HERMES_LOKAY_PARITY_ENABLED:-1}"
if [[ "$parity_enabled" == 1 ]]; then
  parity_source_root="${HERMES_LOKAY_PARITY_SOURCE_ROOT:-$SCRIPT_DIR}"
  parity_active_root="${HERMES_LOKAY_PARITY_ACTIVE_ROOT:-$HOME/.hermes/scripts}"
  parity_template_root="${HERMES_LOKAY_PARITY_TEMPLATE_ROOT:-$SCRIPT_DIR/../templates/launchd}"
  parity_active_plist_root="${HERMES_LOKAY_PARITY_ACTIVE_PLIST_ROOT:-${HERMES_LOKAY_PARITY_PLIST_ROOT:-$HOME/Library/LaunchAgents}}"
  parity_render_root="${HERMES_LOKAY_PARITY_RENDER_ROOT:-${HERMES_LOKAY_PARITY_RENDERED_ROOT:-}}"
  parity_config_root="${HERMES_LOKAY_PARITY_CONFIG_ROOT:-${HERMES_LOKAY_PARITY_ACTIVE_CONFIG_ROOT:-$HOME/.hermes/lokay}}"
  parity_args=(--source-root "$parity_source_root" --active-root "$parity_active_root" --template-root "$parity_template_root" --active-plist-root "$parity_active_plist_root" --active-config-root "$parity_config_root")
  [[ -n "$parity_render_root" ]] && parity_args+=(--render-root "$parity_render_root")
  parity_output=""
  if parity_output="$(PYTHONPATH="$MANAGED_PYTHONPATH" "$MANAGED_PYTHON" "$SCRIPT_DIR/../tools/deployment_parity.py" "${parity_args[@]}" 2>&1)"; then
    log OK "deployment-parity source=$parity_source_root active=$parity_active_root active_plist=$parity_active_plist_root config=$parity_config_root"
  else
    log ERROR "deployment-parity mismatch details=${parity_output:-unknown}"
    failures=$((failures + 1))
  fi
fi
supervisor_loaded=0
if launchctl_label_query "$SUPERVISOR_LABEL" >/dev/null; then
  supervisor_loaded=1
  log OK "supervisor-job-loaded count=1 label=$SUPERVISOR_LABEL"
else
  query_status=$?
  [[ "$query_status" -eq 2 ]] && failures=$((failures + 1))
  log ERROR "supervisor-job-missing label=$SUPERVISOR_LABEL"
  failures=$((failures + 1))
fi
if [[ ! -f "$supervisor_plist" ]]; then
  log ERROR "supervisor-plist-missing path=$supervisor_plist"
  failures=$((failures + 1))
fi
process_loaded=0
for process_label in "${PROCESS_LABELS[@]}"; do
  if launchctl_label_query "$process_label" >/dev/null; then
    process_loaded=$((process_loaded + 1))
    log ERROR "process-production-job-loaded label=$process_label"
    failures=$((failures + 1))
  else
    query_status=$?
    [[ "$query_status" -eq 2 ]] && failures=$((failures + 1))
  fi
  if [[ -f "$LAUNCH_AGENTS_DIR/$process_label.plist" ]]; then
    log ERROR "process-production-plist-present path=$LAUNCH_AGENTS_DIR/$process_label.plist"
    failures=$((failures + 1))
  fi
done
aggregate_loaded=0
if launchctl_label_query "$AGGREGATE_FALA_LABEL" >/dev/null; then
  aggregate_loaded=1
  log ERROR "aggregate-production-job-loaded label=$AGGREGATE_FALA_LABEL"
  failures=$((failures + 1))
else
  query_status=$?
  [[ "$query_status" -eq 2 ]] && failures=$((failures + 1))
fi
if [[ -f "$LAUNCH_AGENTS_DIR/$AGGREGATE_FALA_LABEL.plist" ]]; then
  log ERROR "aggregate-production-plist-present path=$LAUNCH_AGENTS_DIR/$AGGREGATE_FALA_LABEL.plist"
  failures=$((failures + 1))
fi
legacy_loaded=0
for legacy_label in com.mikolaj92.hermes.repo-issue-intake com.mikolaj92.hermes.repo-issue-to-pr-dispatch com.mikolaj92.hermes.repo-pr-triage com.mikolaj92.hermes.repo-agent-cleanup com.mikolaj92.hermes.repo-agent-fala-tick-all; do
  if launchctl_label_query "$legacy_label" >/dev/null; then
    legacy_loaded=$((legacy_loaded + 1))
  else
    [[ $? -ne 2 ]] || failures=$((failures + 1))
  fi
done
health_repair_loaded=0
health_plist_invalid=0
if launchctl_label_query "com.mikolaj92.hermes.repo-agent-health" >/dev/null; then
  health_plist="$HOME/Library/LaunchAgents/com.mikolaj92.hermes.repo-agent-health.plist"
  if [[ -f "$health_plist" ]] && grep -q -- '--repair' "$health_plist" 2>/dev/null; then
    health_repair_loaded=1
  elif [[ -f "$health_plist" ]]; then
    :
  else
    health_plist_invalid=1
  fi
else
  [[ $? -ne 2 ]] || failures=$((failures + 1))
fi
if [[ "$legacy_loaded" -gt 0 || "$health_repair_loaded" -eq 1 || "$health_plist_invalid" -eq 1 || "$aggregate_loaded" -eq 1 || "$process_loaded" -gt 0 ]]; then
  log ERROR "dual-mutator active legacy_loaded=$legacy_loaded health_repair_loaded=$health_repair_loaded health_plist_invalid=$health_plist_invalid aggregate_loaded=$aggregate_loaded process_loaded=$process_loaded supervisor_loaded=$supervisor_loaded"; failures=$((failures + 1))
else
  log OK "mutator-gate legacy_loaded=$legacy_loaded health_repair_loaded=$health_repair_loaded health_plist_invalid=$health_plist_invalid aggregate_loaded=$aggregate_loaded process_loaded=$process_loaded supervisor_loaded=$supervisor_loaded"
fi
current_target=""
if [[ -L "$DEPLOYMENT_ROOT/current" ]]; then
  current_target="$(realpath "$DEPLOYMENT_ROOT/current" 2>/dev/null || true)"
fi
if [[ -z "$current_target" || ! -f "$current_target/manifest.json" ]]; then
  log ERROR "fala-deployment invalid-current path=$DEPLOYMENT_ROOT/current"
  failures=$((failures + 1))
else
  fala_check=""
  if ! fala_check="$(validate_fala_current "$current_target" "$LAUNCH_AGENTS_DIR" "$FALA_REQUIRE_LIVE" "$DEPLOYMENT_ROOT")"; then
    log ERROR "fala-deployment candidate-invalid current=$current_target installed_root=$LAUNCH_AGENTS_DIR details=${fala_check:-unknown}"
    failures=$((failures + 1))
  else
    log OK "fala-deployment current=$current_target $fala_check installed_root=$LAUNCH_AGENTS_DIR"
  fi
fi
if [[ -f "$FALA_DB" ]]; then
  db_check=""
  if db_check="$(PYTHONPATH="$MANAGED_PYTHONPATH" "$MANAGED_PYTHON" - "$FALA_DB" "$FALA_MAX_RUN_AGE_SECONDS" "$FALA_REQUIRE_LIVE" <<'PY'
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
    log OK "fala-db path=$FALA_DB $db_check"
  else
    log ERROR "fala-db path=$FALA_DB ${db_check:-integrity=unknown}"
    failures=$((failures + 1))
  fi
else
  log ERROR "fala-db path=$FALA_DB presence=missing"
  failures=$((failures + 1))
fi

supervisor_status_dump=""
if [[ "$supervisor_loaded" -eq 1 ]]; then
  supervisor_status_dump="$(launchctl_label_query "$SUPERVISOR_LABEL" 2>/dev/null || true)"
fi
status_check=""
if status_check="$(validate_supervisor_status "$FALA_DB" "$SUPERVISOR_STATUS_MAX_AGE_SECONDS" "$DEPLOYMENT_ROOT" "${HERMES_LOKAY_CONFIG:-}" "$supervisor_status_dump")"; then
  log OK "supervisor-status $status_check"
else
  log ERROR "supervisor-status ${status_check:-validation-failed}"
  failures=$((failures + 1))
fi

for cmd in gh hermes git launchctl df find ps realpath; do
  require_cmd "$cmd" || failures=$((failures + 1))
done

if gh auth status >/dev/null 2>&1; then
  log OK "gh-auth account=$(gh api user --jq .login 2>/dev/null || echo unknown)"
else
  log ERROR "gh-auth bad"
  failures=$((failures + 1))
fi

hermes_version="$(hermes --version 2>&1 || true)"
if grep -Eiq 'update available|commits? behind|new version' <<<"$hermes_version"; then
  compact_version="$(printf '%s' "$hermes_version" | tr '\n' ' ' | sed 's/  */ /g')"
  log WARN "hermes-update-available details=$(printf '%q' "$compact_version")"
  warnings=$((warnings + 1))
else
  log OK "hermes-version $(printf '%s' "$hermes_version" | head -n 1 | sed 's/  */ /g')"
fi

if [[ -f "$HOME/.hermes/cron/jobs.json" ]]; then
  duplicate_cron="$(PYTHONPATH="$MANAGED_PYTHONPATH" "$MANAGED_PYTHON" - "$HOME/.hermes/cron/jobs.json" <<'PY'
import json, sys
path = sys.argv[1]
watched = {"repo-pr-triage", "repo-issue-to-pr-dispatch"}
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    sys.exit(0)
names = []
for job in data.get("jobs", []):
    if job.get("enabled") and job.get("name") in watched:
        names.append(f"{job.get('name')}:{job.get('id')}")
if names:
    print(",".join(names))
PY
)"
  if [[ -n "$duplicate_cron" ]]; then
    log WARN "duplicate-hermes-cron jobs=$duplicate_cron"
    warnings=$((warnings + 1))
  fi
fi

free_kb="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
free_gb=$((free_kb / 1024 / 1024))
if [[ "$free_gb" -lt "$MIN_FREE_GB" ]]; then
  log ERROR "disk-free-low home_gb=$free_gb min_gb=$MIN_FREE_GB"
  failures=$((failures + 1))
else
  now="$(date +%s)"
for item in "${jobs[@]}"; do
  IFS='|' read -r label plist runtime_log <<<"$item"
  launch_info=""
  if launch_info="$(launchctl_label_query "$label")"; then
    last_exit="$(printf '%s\n' "$launch_info" | awk -F '= ' '/last exit code =/ {gsub(/[^0-9-].*/, "", $2); print $2; exit}')"
    if [[ -z "$last_exit" || "$last_exit" != 0 ]]; then log ERROR "launchd-last-exit-invalid label=$label exit_code=${last_exit:-unknown}"; failures=$((failures + 1)); else log OK "launchd label=$label last_exit=$last_exit"; fi
  else
    # Supervisor is required; hermes-update may be absent without hard-failing the whole gate.
    if [[ "$label" == "$SUPERVISOR_LABEL" ]]; then
      log ERROR "launchd-query-failed label=$label details=$(printf '%q' "$launch_info")"; failures=$((failures + 1))
    else
      log WARN "launchd-not-loaded label=$label"
      warnings=$((warnings + 1))
    fi
  fi
  if [[ -z "$runtime_log" ]]; then
    log ERROR "launchd-log-path-missing label=$label plist=$plist"
    failures=$((failures + 1))
  elif [[ -f "$runtime_log" ]]; then
    mtime="$(stat -f %m "$runtime_log")"; age=$((now - mtime))
    if [[ "$age" -gt "$MAX_LOG_AGE_SECONDS" ]]; then log WARN "stale-log label=$label age_seconds=$age path=$runtime_log"; warnings=$((warnings + 1)); else log OK "recent-log label=$label age_seconds=$age path=$runtime_log"; fi
  else
    log WARN "missing-log label=$label path=$runtime_log"; warnings=$((warnings + 1))
  fi

done

while IFS= read -r lock; do
  [[ -n "$lock" ]] || continue
  log WARN "stale-lock path=$lock"; warnings=$((warnings + 1))
done < <(find /tmp "$WORKTREE_ROOT" -maxdepth 4 -type d \( -name 'hermes-repo-*.lock' -o -name '.agent.lock' \) -mmin "+$STALE_LOCK_MINUTES" 2>/dev/null)

active_worker_seen=0
while IFS= read -r pid_file; do
  [[ -n "$pid_file" ]] || continue
  lock="$(dirname "$pid_file")"; pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then log OK "active-worker-lock pid=$pid path=$lock"; active_worker_seen=1
  else
    log WARN "dead-worker-lock pid=${pid:-missing} path=$lock"; warnings=$((warnings + 1))
  fi
done < <(find "$WORKTREE_ROOT" -maxdepth 5 -type f -path '*/.agent.lock/pid' 2>/dev/null)

for entry in "${repos[@]}"; do
  IFS='|' read -r repo board clone_path repo_priority <<<"$entry"
  if ! open_prs="$(gh pr list --repo "$repo" --state open --json number --jq 'length' 2>&1)"; then log ERROR "queue repo=$repo prs-error=$(printf '%q' "$open_prs")"; failures=$((failures + 1)); continue; fi
  if ! open_issues="$(gh issue list --repo "$repo" --state open --json number --jq 'length' 2>&1)"; then log ERROR "queue repo=$repo issues-error=$(printf '%q' "$open_issues")"; failures=$((failures + 1)); continue; fi
  if ! board_counts="$(hermes kanban --board "$board" stats 2>&1)"; then log ERROR "queue repo=$repo kanban-error=$(printf '%q' "$board_counts")"; failures=$((failures + 1)); continue; fi
  log OK "queue repo=$repo open_prs=$open_prs open_issues=$open_issues board=$board stats=$(printf '%q' "$board_counts")"
done
fi

log OK "summary failures=$failures warnings=$warnings"
[[ "$failures" -eq 0 ]]
