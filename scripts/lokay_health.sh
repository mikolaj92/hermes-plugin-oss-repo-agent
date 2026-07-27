#!/usr/bin/env bash
set -euo pipefail

# Health/watchdog for the Hermes lokay launchd pipeline.

export HOME="${HOME:-/Users/mini-m4-main}"
export PATH="${PATH:-/Users/mini-m4-main/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}"

LOG_FILE="${HERMES_LOKAY_HEALTH_LOG:-$HOME/.hermes/logs/lokay-health.log}"
STALE_LOCK_MINUTES="${HERMES_LOKAY_STALE_LOCK_MINUTES:-180}"
MAX_LOG_AGE_SECONDS="${HERMES_LOKAY_MAX_LOG_AGE_SECONDS:-1800}"
WORKER_TIMEOUT_SECONDS="${HERMES_LOKAY_WORKER_TIMEOUT_SECONDS:-7200}"
MIN_FREE_GB="${HERMES_LOKAY_MIN_FREE_GB:-5}"
WORKTREE_ROOT="${HERMES_LOKAY_WORKTREE_ROOT:-$HOME/.hermes/worktrees}"
FALA_LABEL="${HERMES_LOKAY_FALA_LABEL:-com.mikolaj92.lokay.fala-tick-all}"
DEPLOYMENT_ROOT="${HERMES_LOKAY_DEPLOYMENT_ROOT:-$HOME/.hermes/lokay/deployment}"
FALA_DB="${HERMES_LOKAY_FALA_DB:-$HOME/.hermes/lokay/fala/state.sqlite}"
FALA_PLIST="${HERMES_LOKAY_FALA_PLIST:-$HOME/Library/LaunchAgents/$FALA_LABEL.plist}"
FALA_MAX_RUN_AGE_SECONDS="${HERMES_LOKAY_FALA_MAX_RUN_AGE_SECONDS:-1800}"
FALA_REQUIRE_LIVE="${HERMES_LOKAY_FALA_REQUIRE_LIVE:-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

valid_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }
for _env in STALE_LOCK_MINUTES MAX_LOG_AGE_SECONDS WORKER_TIMEOUT_SECONDS MIN_FREE_GB FALA_MAX_RUN_AGE_SECONDS; do
  _value="${!_env}"
  if ! valid_uint "$_value"; then printf 'invalid-env name=%s value=%s\n' "$_env" "$_value" >&2; exit 2; fi
done
if [[ "$FALA_REQUIRE_LIVE" != 0 && "$FALA_REQUIRE_LIVE" != 1 ]]; then
  printf 'invalid-env name=HERMES_LOKAY_FALA_REQUIRE_LIVE value=%s\n' "$FALA_REQUIRE_LIVE" >&2; exit 2
fi

validate_fala_current() {
  local candidate_id managed_python
  candidate_id="$(basename "$1")"
  managed_python="$DEPLOYMENT_ROOT/runtime/$candidate_id/.venv/bin/python"
  if [[ "$managed_python" != /* || ! -x "$managed_python" ]] || ! "$managed_python" -c 'import sys,tomllib; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; then
    printf 'toml-parser-unavailable interpreter=%s\n' "$managed_python"
    return 1
  fi
  "$managed_python" - "$1" "$2" "$3" "$4" <<'PY'
import hashlib, json, pathlib, plistlib, sys, tomllib
candidate = pathlib.Path(sys.argv[1]).resolve()
installed = pathlib.Path(sys.argv[2]).expanduser()
require_live = sys.argv[3] == "1"
deployment_root = pathlib.Path(sys.argv[4]).expanduser().resolve()
errors = []
plist_relative = "launchd/com.mikolaj92.lokay.fala-tick-all.plist"
pinned_commit = "b5f9a6d500a442a1c79060a862fe4b9da87bc98f"

def sha256(data):
    return hashlib.sha256(data).hexdigest()

def artifact_path(relative):
    if not isinstance(relative, str) or not relative or "\x00" in relative or pathlib.Path(relative).is_absolute() or ".." in pathlib.Path(relative).parts:
        errors.append(f"artifact-path-invalid:{relative!r}")
        return None
    path = (candidate / relative).resolve()
    try:
        path.relative_to(candidate)
    except ValueError:
        errors.append(f"artifact-path-escapes:{relative}")
        return None
    return path


def policy_from_toml_text(text):
    root = tomllib.loads(text)
    automation = root.get("automation") or {}
    executor = root.get("executor") or {}
    if not isinstance(automation, dict) or not isinstance(executor, dict):
        raise ValueError("policy-table-invalid")
    return {
        "automerge": root.get("automerge", automation.get("automerge", False)),
        "require_human_approval": root.get("require_human_approval", automation.get("require_human_approval", True)),
        "require_checks": root.get("require_checks", automation.get("require_checks", True)),
        "require_test_evidence": root.get("require_test_evidence", automation.get("require_test_evidence", True)),
        "executor_enabled": executor.get("enabled", False),
    }
try:
    if candidate.parent != (deployment_root / "versions").resolve():
        errors.append("current-outside-versions")
    manifest = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
        errors.append("manifest-schema-invalid")
    if not isinstance(manifest, dict):
        raise ValueError("manifest-not-object")
    candidate_id = str(manifest.get("candidate_id") or "")
    identity = manifest.get("identity")
    if not isinstance(identity, dict):
        errors.append("manifest-identity-invalid")
        identity = {}
    expected_id = sha256((json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n").encode())
    if candidate_id != candidate.name:
        errors.append("candidate-id-path-mismatch")
    if candidate_id != expected_id:
        errors.append("manifest-hash-mismatch")
    stable_keys = {"schema", "mode", "plugin_commit", "fala_tag", "fala_commit", "lock_hash", "config_path", "config_hash", "db_path", "metadata_path", "lock_path", "config_artifact_path", "revision_path", "policy"}
    expected_manifest_keys = stable_keys | {"candidate_id", "identity", "created_at", "program_arguments", "artifacts", "runtime_identity"}
    if set(manifest) != expected_manifest_keys:
        errors.append("manifest-key-set-mismatch")
    if set(identity) != stable_keys:
        errors.append("manifest-identity-key-set-mismatch")
    for key, expected in identity.items():
        if key in stable_keys and manifest.get(key) != expected:
            errors.append(f"identity-mismatch:{key}")
    policy = manifest.get("policy")
    policy_keys = {"automerge", "require_human_approval", "require_checks", "require_test_evidence", "executor_enabled"}
    safe_policy = {
        "automerge": False,
        "require_human_approval": True,
        "require_checks": True,
        "require_test_evidence": True,
        "executor_enabled": False,
    }
    if not isinstance(policy, dict) or set(policy) != policy_keys or any(not isinstance(policy.get(key), bool) for key in policy_keys):
        errors.append("identity-policy-invalid")
        policy = policy if isinstance(policy, dict) else {}
    elif policy != safe_policy:
        errors.append("identity-policy-unsafe")
    try:
        expected_policy = policy_from_toml_text((candidate / "source" / "config.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, tomllib.TOMLDecodeError):
        errors.append("identity-policy-config-unreadable")
        expected_policy = None
    if expected_policy is not None and isinstance(policy, dict) and set(policy) == policy_keys and all(isinstance(policy.get(key), bool) for key in policy_keys) and policy != expected_policy:
        errors.append("identity-policy-config-mismatch")
    active_config = pathlib.Path(str(manifest.get("config_path") or "")).expanduser()
    if active_config.is_file() and not active_config.is_symlink():
        try:
            active_policy = policy_from_toml_text(active_config.read_text(encoding="utf-8"))
            if isinstance(policy, dict) and set(policy) == policy_keys and all(isinstance(policy.get(key), bool) for key in policy_keys) and policy != active_policy:
                errors.append("identity-policy-active-config-mismatch")
        except (OSError, UnicodeError, ValueError, tomllib.TOMLDecodeError):
            errors.append("identity-policy-active-config-unreadable")
    mode = manifest.get("mode")
    manifest_args = manifest.get("program_arguments")
    if not isinstance(manifest_args, list) or any(not isinstance(value, str) for value in manifest_args):
        errors.append("identity-program-arguments-mismatch")
        manifest_args = []
    frozen = ["--frozen"] if "--frozen" in manifest_args else []
    expected_args = [
        manifest_args[0] if manifest_args else "",
        "run", *frozen, "--project", str(candidate / "source" / "project"),
        "lokay-tick-all", "--config", str(candidate / "source" / "config.toml"),
        "--db", str(manifest.get("db_path") or ""),
        f"--{mode}", "--json",
    ]
    if not manifest_args or not pathlib.Path(manifest_args[0]).is_absolute() or manifest_args != expected_args:
        errors.append("identity-program-arguments-mismatch")
    if mode not in {"dry-run", "live"}:
        errors.append("mode-invalid")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        errors.append("manifest-artifacts-invalid")
        artifacts = {}
    config_artifact = artifact_path(manifest.get("config_artifact_path"))
    config_hash = manifest.get("config_hash")
    if config_artifact is None or not config_artifact.is_file() or not isinstance(config_hash, str) or sha256(config_artifact.read_bytes()) != config_hash:
        errors.append("identity-config-hash-mismatch")
    active_config = pathlib.Path(str(manifest.get("config_path") or "")).expanduser()
    if active_config.is_file() and not active_config.is_symlink() and (not isinstance(config_hash, str) or sha256(active_config.read_bytes()) != config_hash):
        errors.append("identity-active-config-hash-mismatch")
    plist_relative = "launchd/com.mikolaj92.lokay.fala-tick-all.plist"
    if plist_relative not in artifacts:
        errors.append("identity-artifact-key-set-mismatch")
    for relative, expected in artifacts.items():
        if (not isinstance(relative, str) or not relative or not isinstance(expected, dict) or set(expected) != {"sha256", "bytes"} or not isinstance(expected.get("sha256"), str) or len(expected["sha256"]) != 64 or not isinstance(expected.get("bytes"), int) or expected["bytes"] < 0):
            errors.append(f"identity-artifact-mismatch:{relative}")
    if manifest.get("fala_tag") != "0.7.15" or manifest.get("fala_commit") != pinned_commit:
        errors.append("fala-provenance-invalid")
    candidate_plist = candidate / plist_relative
    candidate_bytes = candidate_plist.read_bytes()
    candidate_doc = plistlib.loads(candidate_bytes)
    installed_bytes = installed.read_bytes()
    plist_hash = sha256(candidate_bytes)
    runtime = manifest.get("runtime_identity")
    runtime_keys = {"program_arguments", "working_directory", "standard_out_path", "standard_error_path", "environment_variables", "start_interval", "run_at_load", "process_type", "limit_load_to_session_type", "plist_sha256"}
    if not isinstance(runtime, dict) or set(runtime) != runtime_keys:
        errors.append("runtime-identity-key-set-mismatch")
        runtime = runtime if isinstance(runtime, dict) else {}
    if runtime.get("program_arguments") != manifest_args or runtime.get("program_arguments") != candidate_doc.get("ProgramArguments"):
        errors.append("runtime-identity-program-arguments-mismatch")
    if runtime.get("working_directory") != candidate_doc.get("WorkingDirectory"):
        errors.append("runtime-identity-working-directory-mismatch")
    for runtime_key, plist_key in (("standard_out_path", "StandardOutPath"), ("standard_error_path", "StandardErrorPath"), ("start_interval", "StartInterval"), ("run_at_load", "RunAtLoad"), ("process_type", "ProcessType"), ("limit_load_to_session_type", "LimitLoadToSessionType")):
        if runtime.get(runtime_key) != candidate_doc.get(plist_key):
            errors.append(f"runtime-identity-{runtime_key}-mismatch")
    if runtime.get("environment_variables") != candidate_doc.get("EnvironmentVariables"):
        errors.append("runtime-identity-environment-mismatch")
    if runtime.get("plist_sha256") != plist_hash:
        errors.append("runtime-identity-plist-hash-mismatch")
    if runtime.get("working_directory") != str(candidate / "source" / "project"):
        errors.append("runtime-identity-working-directory-invalid")
    runtime_env = runtime.get("environment_variables")
    required_env = {"HOME", "UV_PROJECT_ENVIRONMENT", "UV_CACHE_DIR"}
    if not isinstance(runtime_env, dict) or not required_env.issubset(runtime_env) or any(not isinstance(runtime_env.get(key), str) or not pathlib.Path(runtime_env[key]).is_absolute() for key in required_env):
        errors.append("runtime-identity-environment-invalid")
    for key in ("standard_out_path", "standard_error_path"):
        value = runtime.get(key)
        if not isinstance(value, str) or not pathlib.Path(value).is_absolute() or "~" in value:
            errors.append(f"runtime-identity-{key}-invalid")
    if runtime.get("start_interval") != 600 or runtime.get("run_at_load") is not False or runtime.get("process_type") != "Background" or runtime.get("limit_load_to_session_type") not in (None, "Background"):
        errors.append("runtime-identity-schedule-invalid")
    for required_relative in ("fala-package.toml", "src/lokay/effector.py"):
        required_file = candidate / "source" / "project" / required_relative
        if not required_file.is_file() or required_file.is_symlink():
            errors.append(f"required-package-artifact-missing:{required_relative}")
    plistlib.loads(installed_bytes)
    declared_plist = artifacts.get(plist_relative, {})
    expected_plist_hash = declared_plist.get("sha256") if isinstance(declared_plist, dict) else declared_plist
    if expected_plist_hash != plist_hash:
        errors.append("candidate-plist-hash-mismatch")
    if isinstance(declared_plist, dict) and declared_plist.get("bytes") != len(candidate_bytes):
        errors.append("candidate-plist-size-mismatch")
    if sha256(installed_bytes) != plist_hash:
        errors.append("installed-plist-not-current")
    args = candidate_doc.get("ProgramArguments")
    if not isinstance(args, list) or args != manifest_args:
        errors.append("program-arguments-mismatch")
    mode_flags = {"dry-run": "--dry-run", "live": "--live"}
    if mode not in mode_flags or not isinstance(args, list) or args.count("--dry-run") + args.count("--live") != 1 or mode_flags.get(mode) not in args:
        errors.append("mode-arguments-invalid")
    if not isinstance(args, list) or "lokay-tick-all" not in args or "--config" not in args or "--db" not in args or "--json" not in args:
        errors.append("program-arguments-incomplete")
    if require_live and "--live" not in args:
        errors.append("production-gate-requires-live")
    if not pathlib.Path(str(manifest.get("config_path") or "")).is_absolute() or not pathlib.Path(str(manifest.get("db_path") or "")).is_absolute():
        errors.append("runtime-path-not-absolute")
    if candidate_doc.get("StartInterval") != 600 or candidate_doc.get("ProcessType") != "Background" or candidate_doc.get("RunAtLoad") is not False:
        errors.append("plist-schedule-invalid")
    home = (candidate_doc.get("EnvironmentVariables") or {}).get("HOME")
    if not isinstance(home, str) or not home.startswith("/"):
        errors.append("plist-home-invalid")
    for runtime_path, label in (
        (args[args.index("--project") + 1] if isinstance(args, list) and "--project" in args and args.index("--project") + 1 < len(args) else "", "project"),
        (args[args.index("--config") + 1] if isinstance(args, list) and "--config" in args and args.index("--config") + 1 < len(args) else "", "config"),
        (candidate_doc.get("WorkingDirectory", ""), "working-directory"),
    ):
        if not runtime_path:
            errors.append(f"{label}-path-missing")
            continue
        try:
            pathlib.Path(str(runtime_path)).expanduser().resolve().relative_to(candidate)
        except ValueError:
            errors.append(f"{label}-path-escapes")
    declared_paths = set(artifacts)
    for path in candidate.rglob("*"):
        if not path.is_file() or path == candidate / "manifest.json":
            continue
        try:
            relative = str(path.resolve().relative_to(candidate))
        except ValueError:
            errors.append(f"artifact-path-escapes:{path}")
            continue
        if relative not in declared_paths:
            errors.append(f"unmanifested-artifact:{relative}")
    for relative, declared in artifacts.items():
        artifact = artifact_path(relative)
        if artifact is None:
            continue
        if not artifact.is_file():
            errors.append(f"artifact-missing:{relative}")
            continue
        if artifact.stat().st_mode & 0o222:
            errors.append(f"artifact-writable:{relative}")
        expected_hash = declared.get("sha256") if isinstance(declared, dict) else declared
        if sha256(artifact.read_bytes()) != expected_hash:
            errors.append(f"artifact-mismatch:{relative}")
        if isinstance(declared, dict) and declared.get("bytes") != artifact.stat().st_size:
            errors.append(f"artifact-size-mismatch:{relative}")
    if errors:
        print(";".join(errors))
        raise SystemExit(1)
    print(f"candidate_id={candidate_id} plist_sha256={plist_hash} mode={mode}")
except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError, plistlib.InvalidFileException) as exc:
    print(f"candidate-validation-error={type(exc).__name__}")
    raise SystemExit(1)
PY
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

FALA_RUNTIME_LOG="$(python3 - "$FALA_PLIST" <<'PY' 2>/dev/null || true
import plistlib, sys
try:
    with open(sys.argv[1], "rb") as stream:
        value = plistlib.load(stream).get("StandardOutPath", "")
    if isinstance(value, str):
        print(value)
except (OSError, plistlib.InvalidFileException):
    pass
PY
)"

uid="$(id -u)"
jobs=(
  "com.mikolaj92.lokay.fala-tick-all|$FALA_PLIST|$FALA_RUNTIME_LOG"
  "com.mikolaj92.lokay.hermes-update|$HOME/Library/LaunchAgents/com.mikolaj92.lokay.hermes-update.plist|$HOME/.hermes/logs/lokay-hermes-update.log"
)
repo_data=""
if ! repo_data="$(lokay_repos)"; then
  printf 'registry-error unavailable\n' >&2; exit 1
fi
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
  if parity_output="$(python3 "$SCRIPT_DIR/../tools/deployment_parity.py" "${parity_args[@]}" 2>&1)"; then
    log OK "deployment-parity source=$parity_source_root active=$parity_active_root active_plist=$parity_active_plist_root config=$parity_config_root"
  else
    log ERROR "deployment-parity mismatch details=${parity_output:-unknown}"
    failures=$((failures + 1))
  fi
fi
fala_loaded=0
if launchctl_label_query "$FALA_LABEL" >/dev/null; then
  fala_loaded=1
else
  query_status=$?
  failures=$((failures + 1))
  [[ "$query_status" -eq 1 ]] && log ERROR "fala-not-loaded label=$FALA_LABEL"
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
  if health_repair="$(python3 - "$health_plist" <<'PY'
import plistlib, sys
try:
    with open(sys.argv[1], "rb") as stream:
        arguments = plistlib.load(stream).get("ProgramArguments")
    if not isinstance(arguments, list) or any(not isinstance(value, str) for value in arguments):
        raise ValueError("invalid ProgramArguments")
    print("1" if "--repair" in arguments else "0")
except (OSError, ValueError, plistlib.InvalidFileException):
    raise SystemExit(1)
PY
  )"; then
    [[ "$health_repair" != 1 ]] || health_repair_loaded=1
  else
    health_plist_invalid=1
  fi
else
  [[ $? -ne 2 ]] || failures=$((failures + 1))
fi
if [[ "$legacy_loaded" -gt 0 || "$health_repair_loaded" -eq 1 || "$health_plist_invalid" -eq 1 ]]; then
  log ERROR "dual-mutator active legacy_loaded=$legacy_loaded health_repair_loaded=$health_repair_loaded health_plist_invalid=$health_plist_invalid fala_loaded=$fala_loaded"; failures=$((failures + 1))
else
  log OK "mutator-gate legacy_loaded=$legacy_loaded health_repair_loaded=$health_repair_loaded health_plist_invalid=$health_plist_invalid fala_loaded=$fala_loaded"
fi
current_target=""
if [[ -L "$DEPLOYMENT_ROOT/current" ]]; then
  current_target="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$DEPLOYMENT_ROOT/current" 2>/dev/null || true)"
fi
if [[ -z "$current_target" || ! -f "$current_target/manifest.json" ]]; then
  log ERROR "fala-deployment invalid-current path=$DEPLOYMENT_ROOT/current"
  failures=$((failures + 1))
else
  fala_check=""
  if ! fala_check="$(validate_fala_current "$current_target" "$FALA_PLIST" "$FALA_REQUIRE_LIVE" "$DEPLOYMENT_ROOT")"; then
    log ERROR "fala-deployment candidate-invalid current=$current_target installed_plist=$FALA_PLIST details=${fala_check:-unknown}"
    failures=$((failures + 1))
  else
    log OK "fala-deployment current=$current_target $fala_check installed_plist=$FALA_PLIST"
  fi
fi
if [[ -f "$FALA_DB" ]]; then
  db_check=""
  if db_check="$(python3 - "$FALA_DB" "$FALA_MAX_RUN_AGE_SECONDS" "$FALA_REQUIRE_LIVE" <<'PY'
import json, sqlite3, sys
from datetime import datetime, timezone
path, max_age_text, require_live_text = sys.argv[1:]
max_age = int(max_age_text)
require_live = require_live_text == "1"
try:
    with sqlite3.connect(path) as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"runs", "processes", "schema_migrations"}
        version = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] if "schema_migrations" in tables else None
        runs = db.execute("SELECT id,status,updated_at,metadata FROM runs ORDER BY updated_at DESC").fetchall() if "runs" in tables else []
        if not runs: raise ValueError("runs-missing")
        safe_terminal_statuses = {"completed"}
        unsafe_statuses = {"created", "active", "waiting", "retry_wait", "cancel_requested", "failed", "cancelled", "timed_out"}
        latest = runs[0]
        unresolved = []
        for row in runs:
            if len(row) != 4 or not row[0] or not row[1] or not row[2]: raise ValueError("run-row-invalid")
            stamp = datetime.fromisoformat(str(row[2]).replace("Z", "+00:00"))
            if stamp.tzinfo is None: stamp = stamp.replace(tzinfo=timezone.utc)
            age = max(0, int((datetime.now(timezone.utc) - stamp).total_seconds()))
            if str(row[1]) in unsafe_statuses: unresolved.append((str(row[0]), str(row[1]), age))
        if integrity != "ok" or version != 6 or not required.issubset(tables): raise ValueError("schema-or-integrity-invalid")
        if latest is None or not latest[0] or not latest[1] or not latest[2]: raise ValueError("latest-run-missing-or-invalid")
        stamp = datetime.fromisoformat(str(latest[2]).replace("Z", "+00:00"))
        if stamp.tzinfo is None: stamp = stamp.replace(tzinfo=timezone.utc)
        age = max(0, int((datetime.now(timezone.utc) - stamp).total_seconds()))
        if age > max_age: raise ValueError(f"latest-run-stale:{age}")
        status = str(latest[1])
        if status not in safe_terminal_statuses: raise ValueError(f"latest-run-not-completed:{status}")
        delta = (datetime.now(timezone.utc) - stamp).total_seconds()
        if delta < 0: raise ValueError("future-run-timestamp")
        age = int(delta)
        try: metadata = json.loads(latest[3] or "{}")
        except (TypeError, json.JSONDecodeError): raise ValueError("latest-run-metadata-invalid")
        mode = metadata.get("mode")
        if mode not in {"live", "dry-run"}: mode = "dry-run" if metadata.get("dry_run") is True else ("live" if metadata.get("dry_run") is False else None)
        if mode is None: raise ValueError("latest-run-mode-missing")
        if require_live and mode != "live": raise ValueError(f"latest-run-not-live:{mode}")
        rows = db.execute("SELECT status,COUNT(*) FROM processes WHERE run_id=? GROUP BY status", (latest[0],)).fetchall()
        counts = {str(row[0]): int(row[1]) for row in rows}
        failed_count = sum(counts.get(key, 0) for key in ("failed", "cancelled", "timed_out"))
        waiting_count = sum(counts.get(key, 0) for key in ("waiting", "retry_wait"))
        if failed_count: raise ValueError(f"failed-processes:{failed_count}")
        if waiting_count: raise ValueError(f"waiting-processes:{waiting_count}")
    print(f"integrity={integrity} schema={version} run_id={latest[0]} run_status={status} run_mode={mode} run_age_seconds={age} unresolved_runs={len(unresolved)} failed_processes={failed_count} waiting_processes={waiting_count}")
except Exception as exc:
    print(f"integrity=unknown error={type(exc).__name__}:{exc}")
    raise SystemExit(1)
PY
  )"; then
    log OK "fala-db path=$FALA_DB $db_check"
  else
    log ERROR "fala-db path=$FALA_DB ${db_check:-integrity=unknown}"
    failures=$((failures + 1))
  fi
else
  log ERROR "fala-db missing path=$FALA_DB"
  failures=$((failures + 1))
fi

for cmd in gh hermes git python3 launchctl df find ps; do
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
  duplicate_cron="$(python3 - "$HOME/.hermes/cron/jobs.json" <<'PY'
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
    log ERROR "launchd-query-failed label=$label details=$(printf '%q' "$launch_info")"; failures=$((failures + 1))
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
