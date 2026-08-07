#!/usr/bin/env python3
"""Fail-closed observational read of supervisor status.json for health/status scripts."""
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time

fala_db = pathlib.Path(sys.argv[1]).expanduser()
max_age = int(sys.argv[2])
deployment_root = pathlib.Path(sys.argv[3]).expanduser()
config_path = pathlib.Path(sys.argv[4]).expanduser() if sys.argv[4] else None
launchctl_dump = os.environ.get("HERMES_LOKAY_STATUS_LAUNCHCTL_DUMP", "")


def fail(reason: str) -> None:
    print(reason)
    raise SystemExit(1)


def state_root_for(db_path: pathlib.Path) -> pathlib.Path:
    configured = (
        os.environ.get("HERMES_LOKAY_SUPERVISOR_STATE_ROOT")
        or os.environ.get("LOKAY_SUPERVISOR_STATE_ROOT")
        or ""
    ).strip()
    if configured:
        return pathlib.Path(configured).expanduser()
    parent = db_path.resolve().parent
    if parent.name == "process-state":
        return parent.parent / "supervisor"
    return parent / "supervisor"


def is_candidate_id(value: object) -> bool:
    text = str(value or "").strip().lower()
    return bool(re.fullmatch(r"[0-9a-f]{64}", text)) and text != ("0" * 64)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def observed_start_identity(pid: int) -> str:
    try:
        boot = str(int(os.stat("/").st_ctime_ns))
    except OSError:
        boot = "0"
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return f"{pid}:{boot}:unverified"
    started = (result.stdout or "").strip()
    if result.returncode == 0 and started:
        return f"{pid}:{boot}:ps:{started}"
    return f"{pid}:{boot}:unverified"


def generation_value() -> str:
    configured = (
        os.environ.get("HERMES_LOKAY_GENERATION_PATH")
        or os.environ.get("LOKAY_GENERATION_PATH")
        or ""
    ).strip()
    path = pathlib.Path(configured).expanduser() if configured else pathlib.Path("~/.hermes/lokay/generation").expanduser()
    if path.is_symlink() or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def expected_candidate_id() -> str:
    current = deployment_root / "current"
    if current.is_symlink() or current.is_dir():
        try:
            resolved = current.resolve()
        except OSError:
            resolved = None
        if resolved is not None and is_candidate_id(resolved.name):
            return resolved.name.lower()
        manifest_path = (resolved / "manifest.json") if resolved is not None else None
        if manifest_path is not None and manifest_path.is_file() and not manifest_path.is_symlink():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                manifest = None
            if isinstance(manifest, dict) and is_candidate_id(manifest.get("candidate_id")):
                return str(manifest["candidate_id"]).lower()
    for key in ("FALA_CANDIDATE_ID", "HERMES_LOKAY_CANDIDATE_ID", "LOKAY_CANDIDATE_ID"):
        value = os.environ.get(key, "").strip().lower()
        if is_candidate_id(value):
            return value
    return ""


def expected_config_sha() -> str:
    if config_path is None:
        return ""
    if not config_path.is_file() or config_path.is_symlink():
        return ""
    try:
        return hashlib.sha256(config_path.read_bytes()).hexdigest()
    except OSError:
        return ""


status_path = state_root_for(fala_db) / "status.json"
if status_path.is_symlink():
    fail(f"supervisor-status-symlink path={status_path}")
if not status_path.is_file():
    fail(f"supervisor-status-missing path={status_path}")
try:
    raw = json.loads(status_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    fail(f"supervisor-status-malformed path={status_path} error={type(exc).__name__}")
if not isinstance(raw, dict):
    fail(f"supervisor-status-malformed path={status_path} error=not-object")

schema = raw.get("schema_version")
if schema != 1:
    fail(f"supervisor-status-schema-invalid path={status_path} schema_version={schema!r}")

lease_state = raw.get("lease_state")
if lease_state != "owned":
    fail(f"supervisor-status-lease-unowned path={status_path} lease_state={lease_state!r}")

loop_ts = raw.get("loop_timestamp")
if not isinstance(loop_ts, (int, float)) or isinstance(loop_ts, bool):
    fail(f"supervisor-status-loop-timestamp-invalid path={status_path} loop_timestamp={loop_ts!r}")
now = time.time()
if float(loop_ts) > now + 1.0:
    fail(f"supervisor-status-loop-timestamp-future path={status_path} loop_timestamp={loop_ts} now={now:.3f}")
age = max(0.0, now - float(loop_ts))
if age > max_age:
    fail(f"supervisor-status-stale path={status_path} age_seconds={age:.3f} max_age_seconds={max_age}")

pid_raw = raw.get("supervisor_pid")
if isinstance(pid_raw, bool) or not isinstance(pid_raw, (int, float)) or int(pid_raw) != pid_raw:
    fail(f"supervisor-status-pid-invalid path={status_path} supervisor_pid={pid_raw!r}")
pid = int(pid_raw)
if pid <= 0 or not pid_alive(pid):
    fail(f"supervisor-status-pid-dead path={status_path} supervisor_pid={pid}")

start_identity = raw.get("supervisor_start_identity")
if not isinstance(start_identity, str) or not start_identity.strip():
    fail(f"supervisor-status-start-identity-missing path={status_path}")

identity_check = "uncertain"
launchctl_pid = None
match = re.search(r"(?m)^\s*pid\s*=\s*([0-9]+)\s*$", launchctl_dump or "")
if match:
    launchctl_pid = int(match.group(1))
    if launchctl_pid != pid:
        fail(
            f"supervisor-status-pid-launchctl-mismatch path={status_path} "
            f"supervisor_pid={pid} launchctl_pid={launchctl_pid}"
        )
observed = observed_start_identity(pid)
if ":ps:" in start_identity and ":ps:" in observed:
    if observed != start_identity:
        fail(
            f"supervisor-status-start-identity-mismatch path={status_path} "
            f"expected={start_identity!r} observed={observed!r}"
        )
    identity_check = "matched"
elif launchctl_pid is not None:
    identity_check = "pid-matched-uncertain-start"

candidate_id = str(raw.get("candidate_id") or "").strip().lower()
generation = str(raw.get("generation") or "").strip().lower()
config_sha = str(raw.get("config_sha256") or "").strip().lower()
if not is_candidate_id(candidate_id):
    fail(f"supervisor-status-candidate-invalid path={status_path} candidate_id={candidate_id!r}")
if not is_candidate_id(generation):
    fail(f"supervisor-status-generation-invalid path={status_path} generation={generation!r}")
if not re.fullmatch(r"[0-9a-f]{64}", config_sha):
    fail(f"supervisor-status-config-sha-invalid path={status_path} config_sha256={config_sha!r}")

expected_candidate = expected_candidate_id()
if expected_candidate and expected_candidate != candidate_id:
    fail(
        f"supervisor-status-candidate-mismatch path={status_path} "
        f"status={candidate_id} expected={expected_candidate}"
    )
expected_generation = generation_value().lower()
if is_candidate_id(expected_generation) and expected_generation != generation:
    fail(
        f"supervisor-status-generation-mismatch path={status_path} "
        f"status={generation} expected={expected_generation}"
    )
expected_sha = expected_config_sha().lower()
if expected_sha and expected_sha != config_sha:
    fail(
        f"supervisor-status-config-mismatch path={status_path} "
        f"status={config_sha} expected={expected_sha}"
    )

slot_counts = raw.get("slot_counts")
dispatch_slots = raw.get("dispatch_slots")
if not isinstance(slot_counts, dict):
    fail(f"supervisor-status-slot-counts-invalid path={status_path}")
if not isinstance(dispatch_slots, list):
    fail(f"supervisor-status-dispatch-slots-invalid path={status_path}")

retry_exhausted = 0
orphan_slots = 0
recovery_required = 0
live_orphan = 0
for slot in dispatch_slots:
    if not isinstance(slot, dict):
        fail(f"supervisor-status-dispatch-slot-invalid path={status_path}")
    details = slot.get("details") if isinstance(slot.get("details"), dict) else {}
    status = str(slot.get("status") or "")
    if details.get("retry_exhausted") is True:
        retry_exhausted += 1
    if status == "orphaned":
        orphan_slots += 1
    if details.get("recovery_required") is True:
        recovery_required += 1
    if status == "orphaned" and (
        details.get("recovery_required") is True
        or details.get("fence_retained") is True
        or str(details.get("orphan_resolution") or "") in {"live", "unknown"}
    ):
        live_orphan += 1

if retry_exhausted:
    fail(
        f"supervisor-status-retry-exhausted path={status_path} "
        f"retry_exhausted={retry_exhausted} slot_counts={json.dumps(slot_counts, sort_keys=True)}"
    )
if recovery_required or live_orphan:
    fail(
        f"supervisor-status-orphan-recovery path={status_path} "
        f"recovery_required={recovery_required} live_orphan={live_orphan} orphan_slots={orphan_slots}"
    )

counts_text = ",".join(f"{key}={slot_counts[key]}" for key in sorted(slot_counts, key=str))
if not counts_text:
    counts_text = "idle=0"
print(
    f"path={status_path} schema_version=1 lease_state=owned supervisor_pid={pid} "
    f"identity_check={identity_check} loop_age_seconds={age:.3f} "
    f"candidate_id={candidate_id} generation={generation} "
    f"slot_counts={counts_text} dispatch_slots={len(dispatch_slots)} "
    f"retry_exhausted={retry_exhausted} orphan_slots={orphan_slots} recovery_required={recovery_required}"
)
