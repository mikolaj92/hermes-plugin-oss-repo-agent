#!/usr/bin/env bash
set -euo pipefail

# One-screen operational status for the Hermes repo-agent pipeline.

export HOME="${HOME:-/Users/mini-m4-main}"
export PATH="${PATH:-/Users/mini-m4-main/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}"

LOG_DIR="${HERMES_REPO_AGENT_LOG_DIR:-$HOME/.hermes/logs}"
WORKTREE_ROOT="${HERMES_WORKTREE_ROOT:-$HOME/.hermes/worktrees/repo-fixer}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/repo_agent_repos.sh"

valid_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }
FALA_MAX_RUN_AGE_SECONDS="${HERMES_REPO_AGENT_FALA_MAX_RUN_AGE_SECONDS:-1800}"
FALA_REQUIRE_LIVE="${HERMES_REPO_AGENT_FALA_REQUIRE_LIVE:-1}"
if ! valid_uint "$FALA_MAX_RUN_AGE_SECONDS" || [[ "$FALA_REQUIRE_LIVE" != 0 && "$FALA_REQUIRE_LIVE" != 1 ]]; then
  printf 'invalid-env FALA_MAX_RUN_AGE_SECONDS=%s FALA_REQUIRE_LIVE=%s\n' "$FALA_MAX_RUN_AGE_SECONDS" "$FALA_REQUIRE_LIVE" >&2; exit 2
fi

usage() {
  cat <<'USAGE'
Usage: repo_agent_status.sh

Prints launchd state, worker locks, repo queue counts, and recent repo-agent
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
  command -v "$1" >/dev/null 2>&1 || { printf 'missing-command name=%s\n' "$1"; status_failures=$((status_failures + 1)); return 1; }
}
launchctl_query() {
  local output
  if ! output="$(launchctl print "$1" 2>&1)"; then
    [[ "$output" == *"could not find service"* || "$output" == *"No such process"* || "$output" == *"Could not find service"* ]] && return 1
    printf 'launchctl-error target=%s error=%s\n' "$1" "$output" >&2
    return 2
  fi
  printf '%s\n' "$output"
}
FALA_LABEL="${HERMES_REPO_AGENT_FALA_LABEL:-com.mikolaj92.hermes.repo-agent-fala-tick-all}"
DEPLOYMENT_ROOT="${HERMES_REPO_AGENT_DEPLOYMENT_ROOT:-$HOME/.hermes/oss-repo-agent/deployment}"
FALA_DB="${HERMES_REPO_AGENT_FALA_DB:-$HOME/.hermes/oss-repo-agent/fala/state.sqlite}"
FALA_PLIST="${HERMES_REPO_AGENT_FALA_PLIST:-$HOME/Library/LaunchAgents/$FALA_LABEL.plist}"
FALA_MAX_RUN_AGE_SECONDS="${HERMES_REPO_AGENT_FALA_MAX_RUN_AGE_SECONDS:-1800}"
FALA_REQUIRE_LIVE="${HERMES_REPO_AGENT_FALA_REQUIRE_LIVE:-1}"
status_failures=0

fala_loaded=0
query_status=0
if launchctl_query "gui/$(id -u)/$FALA_LABEL" >/dev/null; then fala_loaded=1; else query_status=$?; fi
[[ "$query_status" -ne 2 ]] || status_failures=$((status_failures + 1))
legacy_loaded_labels=()
for legacy_label in \
  com.mikolaj92.hermes.repo-issue-intake \
  com.mikolaj92.hermes.repo-issue-to-pr-dispatch \
  com.mikolaj92.hermes.repo-pr-triage \
  com.mikolaj92.hermes.repo-agent-cleanup; do
  query_status=0
  if launchctl_query "gui/$(id -u)/$legacy_label" >/dev/null; then legacy_loaded_labels+=("$legacy_label"); else query_status=$?; fi
  [[ "$query_status" -ne 2 ]] || status_failures=$((status_failures + 1))
done
health_repair_loaded=0
query_status=0
if launchctl_query "gui/$(id -u)/com.mikolaj92.hermes.repo-agent-health" >/dev/null; then
  if grep -q -- '--repair' "$HOME/Library/LaunchAgents/com.mikolaj92.hermes.repo-agent-health.plist" 2>/dev/null; then health_repair_loaded=1; fi
else query_status=$?; fi
[[ "$query_status" -ne 2 ]] || status_failures=$((status_failures + 1))

printf '\nFala gate\n'
if [[ "$fala_loaded" != 1 ]]; then
  printf '  ERROR fala-not-loaded\n'
  status_failures=$((status_failures + 1))
fi
printf '  label configured=%s loaded=%s plist=%s\n' "$FALA_LABEL" "$([[ $fala_loaded == 1 ]] && echo yes || echo no)" "$FALA_PLIST"
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
  if fala_check="$(python3 - "$current_target" "$FALA_PLIST" "$FALA_REQUIRE_LIVE" "$DEPLOYMENT_ROOT" <<'PY'
import hashlib, json, pathlib, plistlib, sys
cand=pathlib.Path(sys.argv[1]).resolve(); installed=pathlib.Path(sys.argv[2]).expanduser(); require_live=sys.argv[3]=="1"; root=pathlib.Path(sys.argv[4]).expanduser().resolve(); errors=[]; plist_relative="launchd/com.mikolaj92.hermes.repo-agent-fala-tick-all.plist"; pinned="b5f8085f418010a9290613b86671d435551411a9"
sha=lambda data: hashlib.sha256(data).hexdigest()
def artifact_path(relative):
    if not isinstance(relative,str) or not relative or "\x00" in relative or pathlib.Path(relative).is_absolute() or ".." in pathlib.Path(relative).parts:
        errors.append(f"artifact-path-invalid:{relative!r}"); return None
    path=(cand/relative).resolve()
    try: path.relative_to(cand)
    except ValueError: errors.append(f"artifact-path-escapes:{relative}"); return None
    return path
try:
    if cand.parent != (root/"versions").resolve(): errors.append("current-outside-versions")
    manifest=json.loads((cand/"manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest,dict) or manifest.get("schema") != 1: errors.append("manifest-schema-invalid")
    if not isinstance(manifest,dict): raise ValueError("manifest-not-object")
    cid=str(manifest.get("candidate_id") or ""); identity=manifest.get("identity")
    if not isinstance(identity,dict): errors.append("manifest-identity-invalid"); identity={}
    if cid != cand.name: errors.append("candidate-id-path-mismatch")
    if sha((json.dumps(identity,sort_keys=True,separators=(",",":"))+"\n").encode()) != cid: errors.append("manifest-hash-mismatch")
    stable_keys={"schema","mode","plugin_commit","fala_tag","fala_commit","lock_hash","config_path","config_hash","db_path","metadata_path","lock_path","config_artifact_path","revision_path","policy"}
    expected_manifest_keys=stable_keys|{"candidate_id","identity","created_at","program_arguments","artifacts","runtime_identity"}
    if set(manifest)!=expected_manifest_keys: errors.append("manifest-key-set-mismatch")
    if set(identity)!=stable_keys: errors.append("manifest-identity-key-set-mismatch")
    for key,expected in identity.items():
        if key in stable_keys and manifest.get(key)!=expected: errors.append(f"identity-mismatch:{key}")
    mode=manifest.get("mode")
    manifest_args=manifest.get("program_arguments")
    if not isinstance(manifest_args,list) or any(not isinstance(value,str) for value in manifest_args):
        errors.append("identity-program-arguments-mismatch"); manifest_args=[]
    expected_args=[manifest_args[0] if manifest_args else "","run","--project",str(cand/"source"/"project"),"repo-agent-tick-all","--config",str(cand/"source"/"config.toml"),"--db",str(manifest.get("db_path") or ""),f"--{mode}","--json"]
    if not manifest_args or not pathlib.Path(manifest_args[0]).is_absolute() or manifest_args != expected_args: errors.append("identity-program-arguments-mismatch")
    if mode not in {"dry-run","live"}: errors.append("mode-invalid")
    artifacts=manifest.get("artifacts")
    if not isinstance(artifacts,dict) or not artifacts:
        errors.append("manifest-artifacts-invalid"); artifacts={}
    plist_relative="launchd/com.mikolaj92.hermes.repo-agent-fala-tick-all.plist"
    if plist_relative not in artifacts: errors.append("identity-artifact-key-set-mismatch")
    for relative,expected in artifacts.items():
        if (not isinstance(relative,str) or not relative or not isinstance(expected,dict) or set(expected)!={"sha256","bytes"} or not isinstance(expected.get("sha256"),str) or len(expected["sha256"])!=64 or not isinstance(expected.get("bytes"),int) or expected["bytes"]<0): errors.append(f"identity-artifact-mismatch:{relative}")
    if manifest.get("fala_tag") != "0.2.1" or manifest.get("fala_commit") != pinned: errors.append("fala-provenance-invalid")
    cp=cand/plist_relative; cb=cp.read_bytes(); doc=plistlib.loads(cb); ib=installed.read_bytes(); plistlib.loads(ib); ph=sha(cb)
    runtime=manifest.get("runtime_identity")
    runtime_keys={"program_arguments","working_directory","standard_out_path","standard_error_path","environment_variables","start_interval","run_at_load","process_type","limit_load_to_session_type","plist_sha256"}
    if not isinstance(runtime,dict) or set(runtime)!=runtime_keys:
        errors.append("runtime-identity-key-set-mismatch"); runtime=runtime if isinstance(runtime,dict) else {}
    if runtime.get("program_arguments") != manifest_args or runtime.get("program_arguments") != doc.get("ProgramArguments"): errors.append("runtime-identity-program-arguments-mismatch")
    if runtime.get("working_directory") != doc.get("WorkingDirectory"): errors.append("runtime-identity-working-directory-mismatch")
    for runtime_key,plist_key in (("standard_out_path","StandardOutPath"),("standard_error_path","StandardErrorPath"),("start_interval","StartInterval"),("run_at_load","RunAtLoad"),("process_type","ProcessType"),("limit_load_to_session_type","LimitLoadToSessionType")):
        if runtime.get(runtime_key) != doc.get(plist_key): errors.append(f"runtime-identity-{runtime_key}-mismatch")
    if runtime.get("environment_variables") != doc.get("EnvironmentVariables"): errors.append("runtime-identity-environment-mismatch")
    if runtime.get("plist_sha256") != ph: errors.append("runtime-identity-plist-hash-mismatch")
    if runtime.get("working_directory") != str(cand/"source"/"project"): errors.append("runtime-identity-working-directory-invalid")
    runtime_env=runtime.get("environment_variables")
    if not isinstance(runtime_env,dict) or set(runtime_env)!={"HOME"} or not isinstance(runtime_env.get("HOME"),str) or not pathlib.Path(runtime_env["HOME"]).is_absolute(): errors.append("runtime-identity-environment-invalid")
    for key in ("standard_out_path","standard_error_path"):
        value=runtime.get(key)
        if not isinstance(value,str) or not pathlib.Path(value).is_absolute() or "~" in value: errors.append(f"runtime-identity-{key}-invalid")
    if runtime.get("start_interval") != 600 or runtime.get("run_at_load") is not False or runtime.get("process_type") != "Background" or runtime.get("limit_load_to_session_type") not in (None,"Background"): errors.append("runtime-identity-schedule-invalid")
    declared=artifacts.get(plist_relative,{}); expected=declared.get("sha256") if isinstance(declared,dict) else declared
    if expected != ph: errors.append("candidate-plist-hash-mismatch")
    if isinstance(declared,dict) and declared.get("bytes") != len(cb): errors.append("candidate-plist-size-mismatch")
    if sha(ib) != ph: errors.append("installed-plist-not-current")
    args=doc.get("ProgramArguments")
    if not isinstance(args,list) or args != manifest_args: errors.append("program-arguments-mismatch")
    mode_args={"dry-run":"--dry-run","live":"--live"}
    if mode not in mode_args or not isinstance(args,list) or args.count("--dry-run")+args.count("--live") != 1 or mode_args.get(mode) not in args: errors.append("mode-arguments-invalid")
    if not isinstance(args,list) or any(required not in args for required in ("repo-agent-tick-all","--config","--db","--json")): errors.append("program-arguments-incomplete")
    if require_live and "--live" not in args: errors.append("production-gate-requires-live")
    if not pathlib.Path(str(manifest.get("config_path") or "")).is_absolute() or not pathlib.Path(str(manifest.get("db_path") or "")).is_absolute(): errors.append("runtime-path-not-absolute")
    if doc.get("StartInterval") != 600 or doc.get("ProcessType") != "Background" or doc.get("RunAtLoad") is not False: errors.append("plist-schedule-invalid")
    home=(doc.get("EnvironmentVariables") or {}).get("HOME")
    if not isinstance(home,str) or not home.startswith("/"): errors.append("plist-home-invalid")
    declared_paths=set(artifacts)
    for path in cand.rglob("*"):
        if path.is_file() and path != cand/"manifest.json" and str(path.relative_to(cand)) not in declared_paths: errors.append(f"unmanifested-artifact:{path.relative_to(cand)}")
    for relative,declared_artifact in artifacts.items():
        artifact=artifact_path(relative)
        if artifact is None: continue
        if not artifact.is_file(): errors.append(f"artifact-missing:{relative}"); continue
        if artifact.stat().st_mode & 0o222: errors.append(f"artifact-writable:{relative}")
        declared_hash=declared_artifact.get("sha256") if isinstance(declared_artifact,dict) else declared_artifact
        if sha(artifact.read_bytes()) != declared_hash: errors.append(f"artifact-mismatch:{relative}")
        if isinstance(declared_artifact,dict) and declared_artifact.get("bytes") != artifact.stat().st_size: errors.append(f"artifact-size-mismatch:{relative}")
    if errors: print(";".join(errors)); raise SystemExit(1)
    print(f"candidate_id={cid} plist_sha256={ph} mode={mode}")
except (OSError,UnicodeError,TypeError,ValueError,json.JSONDecodeError,plistlib.InvalidFileException) as exc:
    print(f"validation-error={type(exc).__name__}"); raise SystemExit(1)
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
    status=str(latest[1]); valid_statuses={"created","active","waiting","retry_wait","completed","failed","cancel_requested","cancelled","timed_out"}
    if status not in valid_statuses: raise ValueError(f"latest-run-status-invalid:{status}")
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
legacy_count="${#legacy_loaded_labels[@]}"
mutator_gate="single-or-none"
if [[ "$legacy_count" -gt 0 ]]; then
  mutator_gate=FAIL
  status_failures=$((status_failures + 1))
  printf '  ERROR legacy-mutator-unexpected-loaded labels=%s\n' "${legacy_loaded_labels[*]}"
fi
if [[ "$fala_loaded" == 1 && "$health_repair_loaded" == 1 ]]; then
  mutator_gate=FAIL
  status_failures=$((status_failures + 1))
fi
printf '  mutators legacy_loaded=%s health_repair_loaded=%s fala_loaded=%s gate=%s\n' "${legacy_loaded_labels[*]:-none}" "$health_repair_loaded" "$([[ $fala_loaded == 1 ]] && echo yes || echo no)" "$mutator_gate"
if [[ "$mutator_gate" == FAIL && "$fala_loaded" == 1 && ("$legacy_count" -gt 0 || "$health_repair_loaded" == 1) ]]; then
  printf '  ERROR dual-mutator legacy-health-repair-and-fala-active\n'
fi
for cmd in gh hermes launchctl find tail date python3; do
  require_cmd "$cmd"
done
uid="$(id -u)"
jobs=(
  "fala|$FALA_LABEL"
  "update|com.mikolaj92.hermes.repo-agent-hermes-update"
  "health|com.mikolaj92.hermes.repo-agent-health"
)

printf 'repo-agent status %s\n\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

printf 'Launchd\n'
for item in "${jobs[@]}"; do
  IFS='|' read -r name label <<<"$item"
  if info="$(launchctl_query "gui/$uid/$label" 2>/dev/null)"; then
    state="$(printf '%s\n' "$info" | awk -F '= ' '/state =/ {print $2; exit}')"
    runs="$(printf '%s\n' "$info" | awk -F '= ' '/runs =/ {gsub(/[^0-9].*/, "", $2); print $2; exit}')"
    last="$(printf '%s\n' "$info" | awk -F '= ' '/last exit code =/ {gsub(/[^0-9-].*/, "", $2); print $2; exit}')"
    printf '  %-9s state=%s runs=%s last_exit=%s\n' "$name" "${state:-unknown}" "${runs:-0}" "${last:-unknown}"
    if [[ "$name" == fala && ( -z "$last" || "$last" != 0) ]]; then
      printf '  ERROR fala-last-exit-invalid exit_code=%s\n' "${last:-unknown}"
      status_failures=$((status_failures + 1))
    elif [[ "$name" == health && -z "$last" ]]; then
      status_failures=$((status_failures + 1))
    fi
  else
    printf '  %-9s missing label=%s\n' "$name" "$label"
    :
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
if ! repo_data="$(repo_agent_repos)"; then
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

printf '\nRecent Decisions\n'
RECENT_SIGNAL_PATTERN='DECISION|CLAUDE_|WORKTREE_|LOCAL_BRANCH_|DONE|WARN|ERROR|ASSIGN_FAILED|PR_ASSIGNED|FIX_TASK_CREATED|FIX_TASK_FAILED|LOCK_HELD|KANBAN_LIST_FAILED|PR_LIST_FAILED|MERGE_FAILED|watchdog-worker-'
for log in "$LOG_DIR/repo-issue-to-pr-dispatch.log" "$LOG_DIR/repo-pr-triage.log" "$LOG_DIR/repo-agent-cleanup.log" "$LOG_DIR/repo-agent-hermes-update.log" "$LOG_DIR/repo-agent-health.log"; do
  [[ -f "$log" ]] || continue
  printf '  %s\n' "$(basename "$log")"
  recent="$(tail -n 80 "$log" | grep -E "$RECENT_SIGNAL_PATTERN" | tail -n 8 || true)"
  if [[ -n "$recent" ]]; then printf '%s\n' "$recent" | sed 's/^/    /'; else printf '    no recent decisions\n'; fi
done
printf '\nGate summary failures=%s\n' "$status_failures"
if [[ "$status_failures" -ne 0 || "$mutator_gate" == FAIL ]]; then
  exit 1
fi
