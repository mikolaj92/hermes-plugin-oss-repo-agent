"""Resident one-supervisor runtime for catalog-derived child dispatch.

Production LaunchAgent runs:

    python -m lokay.supervisor --config … --db … --live|--dry-run --json

This module owns only the supervisor singleton lease and dispatch-slot evidence.
Children own catalog leases, health, retries, receipts, and mutations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from lokay.config import ConfigError, load_config
from lokay.process_runtime import LeaseError, DEFAULT_GENERATION_PATH
from lokay.registry import PROCESS_IDS

SUPERVISOR_LABEL = "com.mikolaj92.lokay.supervisor"
SINGLETON_KEY = "supervisor/singleton"
SINGLETON_TTL_SECONDS = 90
SINGLETON_RENEW_SECONDS = 30
SINGLETON_STALE_AFTER_SECONDS = 180
MAX_WAKE_SECONDS = 30.0
DEFAULT_SHUTDOWN_DRAIN_SECONDS = 15.0
TIMEOUT_TERMINATE_GRACE_SECONDS = 0.5
TIMEOUT_KILL_GRACE_SECONDS = 0.5
POLL_INTERVAL_SECONDS = 0.05
SCHEMA_VERSION = 1
SUPERVISOR_STATUS_SCHEMA_VERSION = 1
SUPERVISOR_STATUS_FILENAME = "status.json"
DEFAULT_BACKOFF_SECONDS: tuple[int, ...] = (30, 60, 120, 300, 600)

Clock = Callable[[], float]
ProcessFactory = Callable[..., "ChildHandle"]


class SupervisorError(RuntimeError):
    """Fail-closed supervisor contract violation."""


@dataclass(frozen=True)
class SupervisorOwner:
    owner_token: str
    owner_pid: int
    start_identity: str
    candidate_id: str
    generation: str
    config_sha256: str


@dataclass(frozen=True)
class SingletonLeaseRecord:
    lease_key: str
    owner_token: str
    owner_pid: int
    start_identity: str
    candidate_id: str
    generation: str
    config_sha256: str
    acquired_at: str
    last_renewed_at: str
    expires_at: float
    stale_after: float
    ttl_seconds: int
    renew_seconds: int
    reclaimed: bool = False


@dataclass
class DispatchSlot:
    process_id: str
    dispatch_id: str
    command: tuple[str, ...]
    command_digest: str
    candidate_id: str
    generation: str
    config_sha256: str
    due_at: float
    status: str = "idle"
    pid: int | None = None
    start_identity: str | None = None
    started_at: float | None = None
    deadline_at: float | None = None
    exit_code: int | None = None
    stdout_path: str = ""
    stderr_path: str = ""
    stdout_digest: str | None = None
    stderr_digest: str | None = None
    reaped_at: str | None = None
    attempt: int = 0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChildHandle:
    """Minimal process handle used by the scheduler (real or injected)."""

    pid: int
    start_identity: str
    _exit_code: int | None = None
    _poll_calls: int = 0
    _poll_after: int = 1
    _terminate_called: bool = False

    def poll(self) -> int | None:
        if self._exit_code is not None:
            return self._exit_code
        self._poll_calls += 1
        if self._poll_calls >= self._poll_after:
            self._exit_code = 0
            return self._exit_code
        return None

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        code = self.poll()
        if code is None:
            self._exit_code = 0
            return 0
        return code

    def terminate(self) -> None:
        self._terminate_called = True
        if self._exit_code is None:
            self._exit_code = -15

    def kill(self) -> None:
        self._terminate_called = True
        self._exit_code = -9


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str | None:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError:
        return None


def command_digest(command: Sequence[str]) -> str:
    return _sha256_text(_canonical_json(list(command)))


def _ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise SupervisorError(f"directory must not be a symlink: {path}")
    mode = path.stat().st_mode
    if not stat.S_ISDIR(mode):
        raise SupervisorError(f"not a directory: {path}")
    os.chmod(path, 0o700)
    return path


def _fsync_dir(directory: Path) -> None:
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    _ensure_private_dir(path.parent)
    raw = (_canonical_json(dict(payload)) + "\n").encode("utf-8")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temp), str(path))
        _fsync_dir(path.parent)
    except Exception:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass
        raise


def _pid_alive(pid: int) -> bool:
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


def _connect(db_path: Path) -> sqlite3.Connection:
    _ensure_private_dir(db_path.parent)
    connection = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


def _start_identity(pid: int | None = None) -> str:
    """Return a stable boot/process-start identity when the host exposes one."""
    current = os.getpid() if pid is None else int(pid)
    try:
        boot = str(int(os.stat("/").st_ctime_ns))
    except OSError:
        boot = "0"
    try:
        result = subprocess.run(
            ["ps", "-p", str(current), "-o", "lstart="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        result = None
    started = (result.stdout or "").strip() if result is not None else ""
    if result is not None and result.returncode == 0 and started:
        return f"{current}:{boot}:ps:{started}"
    # This is deliberately distinguishable from a verified identity.  A
    # restart must not use an unverified value to clear a live orphan fence.
    return f"{current}:{boot}:unverified"


def _pid_start_identity_matches(pid: int, expected: str | None) -> bool | None:
    """Return match, mismatch, or unknown when process-start evidence is unavailable."""
    if pid <= 0 or not expected or ":ps:" not in expected:
        return None
    observed = _start_identity(pid)
    if ":ps:" not in observed:
        return None
    return observed == expected


def _orphan_resolution(slot: DispatchSlot) -> str:
    """Classify a persisted child fence without guessing from kill/absence."""
    if slot.pid is None or int(slot.pid) <= 0:
        return "dead"
    if not _pid_alive(int(slot.pid)):
        return "dead"
    if slot.start_identity and ":ps:" in slot.start_identity:
        identity_match = _pid_start_identity_matches(int(slot.pid), slot.start_identity)
        if identity_match is False:
            return "reused"
        # Unknown identity is deliberately live/uncertain, not reusable.
        return "live"
    return "live"


def _orphan_process_live(slot: DispatchSlot) -> bool:
    """Return true unless a fenced orphan is proven dead or PID-reused."""
    return _orphan_resolution(slot) == "live"


def supervisor_state_root(db_path: Path) -> Path:
    """Supervisor namespace lives beside the process-state/db parent."""
    configured = (
        os.environ.get("HERMES_LOKAY_SUPERVISOR_STATE_ROOT")
        or os.environ.get("LOKAY_SUPERVISOR_STATE_ROOT")
        or ""
    ).strip()
    if configured:
        return Path(configured).expanduser()
    parent = Path(db_path).expanduser().resolve().parent
    # Prefer task_receipts/supervisor when --db is process-state.sqlite3 under receipts.
    if parent.name == "process-state":
        return parent.parent / "supervisor"
    return parent / "supervisor"


def _generation_path() -> Path:
    configured = (
        os.environ.get("HERMES_LOKAY_GENERATION_PATH")
        or os.environ.get("LOKAY_GENERATION_PATH")
        or ""
    ).strip()
    if configured:
        return Path(configured).expanduser()
    return Path(DEFAULT_GENERATION_PATH).expanduser()


def _read_generation_value(path: Path, *, allow_env: bool = False) -> str:
    """Read the durable generation pointer; env fallback is test-only."""
    if not path.is_symlink() and path.is_file():
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    if allow_env:
        return (
            os.environ.get("HERMES_LOKAY_GENERATION")
            or os.environ.get("LOKAY_GENERATION")
            or ""
        ).strip()
    return ""


def _read_candidate_file(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        value = path.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return ""
    if len(value) == 64 and all(ch in "0123456789abcdef" for ch in value):
        return value
    return ""


def _candidate_identity(cfg: Any | None) -> str:
    if cfg is not None:
        raw = getattr(cfg, "raw", {})
        if isinstance(raw, dict):
            for key in ("candidate", "candidate_id"):
                value = str(raw.get(key) or "").strip().lower()
                if len(value) == 64 and all(ch in "0123456789abcdef" for ch in value):
                    return value
            for key in ("candidate_path", "candidate_id_path"):
                path_value = str(raw.get(key) or "").strip()
                if path_value:
                    loaded = _read_candidate_file(Path(path_value).expanduser())
                    if loaded:
                        return loaded
    for key in ("FALA_CANDIDATE_ID", "HERMES_LOKAY_CANDIDATE_ID", "LOKAY_CANDIDATE_ID"):
        value = os.environ.get(key, "").strip().lower()
        if len(value) == 64 and all(ch in "0123456789abcdef" for ch in value):
            return value
    for key in (
        "HERMES_LOKAY_CANDIDATE_PATH",
        "LOKAY_CANDIDATE_PATH",
        "FALA_CANDIDATE_PATH",
    ):
        path_value = os.environ.get(key, "").strip()
        if path_value:
            loaded = _read_candidate_file(Path(path_value).expanduser())
            if loaded:
                return loaded
    parts = Path(__file__).resolve().parts
    for index, part in enumerate(parts[:-1]):
        if part == "versions" and index + 1 < len(parts):
            value = parts[index + 1].lower()
            if (
                len(value) == 64
                and all(ch in "0123456789abcdef" for ch in value)
                and parts[index + 2 : index + 5] == ("source", "project", "src")
            ):
                return value
    return ""


def _is_candidate_id(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value)) and value != "0" * 64


def _immutable_candidate_identity() -> tuple[str, Path] | None:
    """Return the candidate directory identity when running from a sealed candidate."""
    module_path = Path(__file__).resolve()
    parts = module_path.parts
    for index, part in enumerate(parts[:-4]):
        if part != "versions" or index + 4 >= len(parts):
            continue
        candidate_id = parts[index + 1]
        if not _is_candidate_id(candidate_id):
            continue
        if parts[index + 2 : index + 5] != ("source", "project", "src"):
            continue
        candidate_root = Path(*parts[: index + 2])
        return candidate_id, candidate_root
    return None


def _verify_candidate_manifest(candidate_id: str, candidate_root: Path) -> None:
    manifest_path = candidate_root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SupervisorError(f"candidate manifest is unavailable or invalid: {manifest_path}") from exc
    manifest_id = manifest.get("candidate_id") if isinstance(manifest, dict) else None
    if manifest_id != candidate_id:
        raise SupervisorError("candidate manifest identity does not match immutable candidate path")


def _validate_candidate_pin(candidate_id: str | None) -> str:
    value = str(candidate_id or "").strip()
    if not _is_candidate_id(value):
        raise SupervisorError("candidate_id must be a non-zero lowercase sha256 hex")
    immutable = _immutable_candidate_identity()
    if immutable is not None:
        immutable_id, candidate_root = immutable
        if value != immutable_id:
            raise SupervisorError("candidate_id does not match immutable candidate path")
        _verify_candidate_manifest(immutable_id, candidate_root)
    return value


def _config_sha256(config_path: str | Path | None) -> str:
    if config_path is None:
        raise SupervisorError("config path is required")
    path = Path(config_path).expanduser()
    if not path.is_file():
        raise SupervisorError(f"config path missing: {path}")
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise SupervisorError(f"unable to read config bytes: {exc}") from exc


def build_child_command(
    *,
    process_id: str,
    python: Path | str,
    config_path: Path | str,
    db_path: Path | str,
    dry_run: bool,
) -> list[str]:
    mode = "--dry-run" if dry_run else "--live"
    return [
        str(python),
        "-m",
        "lokay.process",
        f"lokay-process-{process_id}",
        "--config",
        str(config_path),
        "--db",
        str(db_path),
        mode,
        "--json",
    ]


def build_dispatch_commands(
    *,
    processes: Sequence[Mapping[str, Any]],
    python: Path | str,
    config_path: Path | str,
    db_path: Path | str,
    dry_run: bool,
) -> list[dict[str, Any]]:
    """Ordered twelve-entry child inventory derived from PROCESS_IDS."""
    by_id = {str(row.get("id") or ""): dict(row) for row in processes}
    missing = [process_id for process_id in PROCESS_IDS if process_id not in by_id]
    if missing:
        raise SupervisorError(f"process catalog incomplete: missing {missing}")
    extra = sorted(set(by_id) - set(PROCESS_IDS))
    if extra:
        raise SupervisorError(f"process catalog has unknown ids: {extra}")
    inventory: list[dict[str, Any]] = []
    for process_id in PROCESS_IDS:
        row = by_id[process_id]
        command = build_child_command(
            process_id=process_id,
            python=python,
            config_path=config_path,
            db_path=db_path,
            dry_run=dry_run,
        )
        inventory.append(
            {
                "process_id": process_id,
                "enabled": bool(row.get("enabled", True)),
                "interval_seconds": int(row.get("interval_seconds") or 60),
                "max_attempts": int(row.get("max_attempts") or 5),
                "backoff_seconds": list(row.get("backoff_seconds") or DEFAULT_BACKOFF_SECONDS),
                "command": command,
                "command_digest": command_digest(command),
            }
        )
    return inventory


def initialize_supervisor_schema(db_path: Path) -> Path:
    connection = _connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY NOT NULL,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS singleton_lease (
                lease_key TEXT PRIMARY KEY NOT NULL,
                owner_token TEXT NOT NULL,
                owner_pid INTEGER NOT NULL,
                start_identity TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                generation TEXT NOT NULL,
                config_sha256 TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                last_renewed_at TEXT NOT NULL,
                expires_at REAL NOT NULL,
                stale_after REAL NOT NULL,
                ttl_seconds INTEGER NOT NULL,
                renew_seconds INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dispatch_slots (
                process_id TEXT PRIMARY KEY NOT NULL,
                dispatch_id TEXT NOT NULL,
                command_json TEXT NOT NULL,
                command_digest TEXT NOT NULL,
                pid INTEGER,
                start_identity TEXT,
                candidate_id TEXT NOT NULL,
                generation TEXT NOT NULL,
                config_sha256 TEXT NOT NULL,
                due_at REAL NOT NULL,
                started_at REAL,
                deadline_at REAL,
                status TEXT NOT NULL,
                exit_code INTEGER,
                stdout_path TEXT,
                stderr_path TEXT,
                stdout_digest TEXT,
                stderr_digest TEXT,
                reaped_at TEXT,
                attempt INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            """
        )
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)
                ON CONFLICT(key) DO NOTHING
                """,
                (str(SCHEMA_VERSION),),
            )
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None or str(row["value"]) != str(SCHEMA_VERSION):
                raise SupervisorError("unsupported or missing supervisor schema version")
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    finally:
        connection.close()
    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass
    return db_path


class SupervisorStore:
    """Durable singleton lease + dispatch-slot state for the supervisor namespace."""

    def __init__(self, root: Path, *, owner: SupervisorOwner, clock: Clock | None = None) -> None:
        self.root = Path(root).expanduser()
        self.owner = owner
        self.clock = clock or time.time
        self.db_path = self.root / "supervisor-state.sqlite3"
        self.status_path = self.root / SUPERVISOR_STATUS_FILENAME
        self.lease_path = self.root / "leases" / "singleton.json"
        self.slots_dir = self.root / "slots"
        self.logs_dir = self.root / "logs"
        self.lock_path = self.root / "leases" / "singleton.lock"
        for directory in (self.root, self.status_path.parent, self.lease_path.parent, self.slots_dir, self.logs_dir):
            _ensure_private_dir(directory)
        initialize_supervisor_schema(self.db_path)
        if self.status_path.parent != self.root:
            _ensure_private_dir(self.status_path.parent)

    def _connection(self) -> sqlite3.Connection:
        return _connect(self.db_path)

    def write_status(self, payload: Mapping[str, Any]) -> bool:
        """Atomically publish status only while this owner holds the lease."""
        document = dict(payload)
        document.setdefault("schema_version", SUPERVISOR_STATUS_SCHEMA_VERSION)
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM singleton_lease WHERE lease_key = ?",
                (SINGLETON_KEY,),
            ).fetchone()
            if row is None or not self._identity_matches(row, self.owner):
                connection.execute("ROLLBACK")
                return False
            _write_private_json(self.status_path, document)
            connection.execute("COMMIT")
            return True
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def read_status(self) -> dict[str, Any] | None:
        """Read the status snapshot; malformed data is never healthy."""
        if not self.status_path.is_file() or self.status_path.is_symlink():
            return None
        try:
            raw = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return dict(raw) if isinstance(raw, dict) else None

    def _row_to_record(self, row: sqlite3.Row, *, reclaimed: bool = False) -> SingletonLeaseRecord:
        return SingletonLeaseRecord(
            lease_key=str(row["lease_key"]),
            owner_token=str(row["owner_token"]),
            owner_pid=int(row["owner_pid"]),
            start_identity=str(row["start_identity"]),
            candidate_id=str(row["candidate_id"]),
            generation=str(row["generation"]),
            config_sha256=str(row["config_sha256"]),
            acquired_at=str(row["acquired_at"]),
            last_renewed_at=str(row["last_renewed_at"]),
            expires_at=float(row["expires_at"]),
            stale_after=float(row["stale_after"]),
            ttl_seconds=int(row["ttl_seconds"]),
            renew_seconds=int(row["renew_seconds"]),
            reclaimed=reclaimed,
        )

    def _mirror_singleton(self, record: SingletonLeaseRecord) -> None:
        _write_private_json(
            self.lease_path,
            {
                "lease_key": record.lease_key,
                "owner_token": record.owner_token,
                "owner_pid": record.owner_pid,
                "start_identity": record.start_identity,
                "candidate_id": record.candidate_id,
                "generation": record.generation,
                "config_sha256": record.config_sha256,
                "acquired_at": record.acquired_at,
                "last_renewed_at": record.last_renewed_at,
                "expires_at": record.expires_at,
                "stale_after": record.stale_after,
                "ttl_seconds": record.ttl_seconds,
                "renew_seconds": record.renew_seconds,
                "reclaimed": record.reclaimed,
            },
        )

    def _is_stale(self, row: Mapping[str, Any], *, now: float) -> bool:
        if now < float(row["expires_at"]):
            return False
        if now < float(row["stale_after"]):
            return False
        return not _pid_alive(int(row["owner_pid"]))

    def _identity_matches(self, row: Mapping[str, Any], owner: SupervisorOwner) -> bool:
        return (
            str(row["owner_token"]) == owner.owner_token
            and int(row["owner_pid"]) == owner.owner_pid
            and str(row["start_identity"]) == owner.start_identity
            and str(row["candidate_id"]) == owner.candidate_id
            and str(row["generation"]) == owner.generation
            and str(row["config_sha256"]) == owner.config_sha256
        )

    def acquire_singleton(
        self,
        *,
        ttl_seconds: int = SINGLETON_TTL_SECONDS,
        renew_seconds: int = SINGLETON_RENEW_SECONDS,
        stale_after_seconds: int = SINGLETON_STALE_AFTER_SECONDS,
    ) -> SingletonLeaseRecord:
        if ttl_seconds <= 0:
            raise LeaseError("ttl_seconds must be positive")
        if stale_after_seconds < 2 * ttl_seconds:
            raise LeaseError("stale_after_seconds must be >= 2 * ttl_seconds")
        if renew_seconds <= 0 or renew_seconds > ttl_seconds:
            raise LeaseError("renew_seconds must be positive and <= ttl_seconds")

        reclaimed = False
        connection = self._connection()
        try:
            # Serialize contenders with an exclusive SQLite transaction.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM singleton_lease WHERE lease_key = ?",
                (SINGLETON_KEY,),
            ).fetchone()
            now = float(self.clock())
            if row is not None:
                if self._identity_matches(row, self.owner):
                    # Same resident re-bind: advance expiry only.
                    pass
                elif self._is_stale(row, now=now):
                    reclaimed = True
                    connection.execute(
                        "DELETE FROM singleton_lease WHERE lease_key = ?",
                        (SINGLETON_KEY,),
                    )
                    row = None
                else:
                    connection.execute("ROLLBACK")
                    raise LeaseError(
                        "singleton held by "
                        f"token={row['owner_token']} pid={row['owner_pid']} "
                        f"candidate={row['candidate_id']}"
                    )

            acquired_at = _utc_now()
            expires_at = now + ttl_seconds
            stale_after = now + stale_after_seconds
            connection.execute(
                """
                INSERT INTO singleton_lease(
                    lease_key, owner_token, owner_pid, start_identity,
                    candidate_id, generation, config_sha256,
                    acquired_at, last_renewed_at, expires_at, stale_after,
                    ttl_seconds, renew_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(lease_key) DO UPDATE SET
                    owner_token = excluded.owner_token,
                    owner_pid = excluded.owner_pid,
                    start_identity = excluded.start_identity,
                    candidate_id = excluded.candidate_id,
                    generation = excluded.generation,
                    config_sha256 = excluded.config_sha256,
                    acquired_at = excluded.acquired_at,
                    last_renewed_at = excluded.last_renewed_at,
                    expires_at = excluded.expires_at,
                    stale_after = excluded.stale_after,
                    ttl_seconds = excluded.ttl_seconds,
                    renew_seconds = excluded.renew_seconds
                """,
                (
                    SINGLETON_KEY,
                    self.owner.owner_token,
                    self.owner.owner_pid,
                    self.owner.start_identity,
                    self.owner.candidate_id,
                    self.owner.generation,
                    self.owner.config_sha256,
                    acquired_at,
                    acquired_at,
                    expires_at,
                    stale_after,
                    ttl_seconds,
                    renew_seconds,
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            connection.close()
            raise
        connection.close()

        # Durable readback before any dispatch is allowed.
        record = self.read_singleton()
        if record is None:
            raise LeaseError("singleton acquire readback missing")
        if (
            record.owner_token != self.owner.owner_token
            or record.owner_pid != self.owner.owner_pid
            or record.start_identity != self.owner.start_identity
            or record.candidate_id != self.owner.candidate_id
            or record.generation != self.owner.generation
            or record.config_sha256 != self.owner.config_sha256
        ):
            raise LeaseError("singleton acquire readback identity mismatch")
        record = SingletonLeaseRecord(
            lease_key=record.lease_key,
            owner_token=record.owner_token,
            owner_pid=record.owner_pid,
            start_identity=record.start_identity,
            candidate_id=record.candidate_id,
            generation=record.generation,
            config_sha256=record.config_sha256,
            acquired_at=record.acquired_at,
            last_renewed_at=record.last_renewed_at,
            expires_at=record.expires_at,
            stale_after=record.stale_after,
            ttl_seconds=record.ttl_seconds,
            renew_seconds=record.renew_seconds,
            reclaimed=reclaimed,
        )
        self._mirror_singleton(record)
        return record

    def renew_singleton(
        self,
        *,
        ttl_seconds: int = SINGLETON_TTL_SECONDS,
        stale_after_seconds: int = SINGLETON_STALE_AFTER_SECONDS,
    ) -> SingletonLeaseRecord:
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = float(self.clock())
            renewed_at = _utc_now()
            expires_at = now + ttl_seconds
            stale_after = now + stale_after_seconds
            cursor = connection.execute(
                """
                UPDATE singleton_lease
                SET last_renewed_at = ?, expires_at = ?, stale_after = ?
                WHERE lease_key = ?
                  AND owner_token = ?
                  AND owner_pid = ?
                  AND start_identity = ?
                  AND candidate_id = ?
                  AND generation = ?
                  AND config_sha256 = ?
                """,
                (
                    renewed_at,
                    expires_at,
                    stale_after,
                    SINGLETON_KEY,
                    self.owner.owner_token,
                    self.owner.owner_pid,
                    self.owner.start_identity,
                    self.owner.candidate_id,
                    self.owner.generation,
                    self.owner.config_sha256,
                ),
            )
            if cursor.rowcount != 1:
                connection.execute("ROLLBACK")
                raise LeaseError("singleton renewal denied: ownership lost or fenced")
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            connection.close()
            raise
        connection.close()

        record = self.read_singleton()
        if record is None:
            raise LeaseError("singleton renew readback missing")
        if record.owner_token != self.owner.owner_token:
            raise LeaseError("singleton renew readback ownership lost")
        self._mirror_singleton(record)
        return record

    def read_singleton(self) -> SingletonLeaseRecord | None:
        connection = self._connection()
        try:
            row = connection.execute(
                "SELECT * FROM singleton_lease WHERE lease_key = ?",
                (SINGLETON_KEY,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return self._row_to_record(row)

    def release_singleton(self) -> None:
        """Release only while owner token still matches. Never call after lease loss."""
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                DELETE FROM singleton_lease
                WHERE lease_key = ?
                  AND owner_token = ?
                  AND owner_pid = ?
                  AND start_identity = ?
                  AND candidate_id = ?
                  AND generation = ?
                  AND config_sha256 = ?
                """,
                (
                    SINGLETON_KEY,
                    self.owner.owner_token,
                    self.owner.owner_pid,
                    self.owner.start_identity,
                    self.owner.candidate_id,
                    self.owner.generation,
                    self.owner.config_sha256,
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            connection.close()
            raise
        connection.close()
        if self.lease_path.exists():
            try:
                payload = json.loads(self.lease_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict) and payload.get("owner_token") == self.owner.owner_token:
                try:
                    self.lease_path.unlink()
                    _fsync_dir(self.lease_path.parent)
                except OSError:
                    pass

    def upsert_slot(self, slot: DispatchSlot) -> DispatchSlot:
        connection = self._connection()
        updated_at = _utc_now()
        details_json = _canonical_json(slot.details)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO dispatch_slots(
                    process_id, dispatch_id, command_json, command_digest,
                    pid, start_identity, candidate_id, generation, config_sha256,
                    due_at, started_at, deadline_at, status, exit_code,
                    stdout_path, stderr_path, stdout_digest, stderr_digest,
                    reaped_at, attempt, updated_at, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(process_id) DO UPDATE SET
                    dispatch_id = excluded.dispatch_id,
                    command_json = excluded.command_json,
                    command_digest = excluded.command_digest,
                    pid = excluded.pid,
                    start_identity = excluded.start_identity,
                    candidate_id = excluded.candidate_id,
                    generation = excluded.generation,
                    config_sha256 = excluded.config_sha256,
                    due_at = excluded.due_at,
                    started_at = excluded.started_at,
                    deadline_at = excluded.deadline_at,
                    status = excluded.status,
                    exit_code = excluded.exit_code,
                    stdout_path = excluded.stdout_path,
                    stderr_path = excluded.stderr_path,
                    stdout_digest = excluded.stdout_digest,
                    stderr_digest = excluded.stderr_digest,
                    reaped_at = excluded.reaped_at,
                    attempt = excluded.attempt,
                    updated_at = excluded.updated_at,
                    details_json = excluded.details_json
                """,
                (
                    slot.process_id,
                    slot.dispatch_id,
                    _canonical_json(list(slot.command)),
                    slot.command_digest,
                    slot.pid,
                    slot.start_identity,
                    slot.candidate_id,
                    slot.generation,
                    slot.config_sha256,
                    slot.due_at,
                    slot.started_at,
                    slot.deadline_at,
                    slot.status,
                    slot.exit_code,
                    slot.stdout_path,
                    slot.stderr_path,
                    slot.stdout_digest,
                    slot.stderr_digest,
                    slot.reaped_at,
                    slot.attempt,
                    updated_at,
                    details_json,
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            connection.close()
            raise
        connection.close()
        _write_private_json(
            self.slots_dir / f"{slot.process_id}.json",
            {
                "process_id": slot.process_id,
                "dispatch_id": slot.dispatch_id,
                "command": list(slot.command),
                "command_digest": slot.command_digest,
                "pid": slot.pid,
                "start_identity": slot.start_identity,
                "candidate_id": slot.candidate_id,
                "generation": slot.generation,
                "config_sha256": slot.config_sha256,
                "due_at": slot.due_at,
                "started_at": slot.started_at,
                "deadline_at": slot.deadline_at,
                "status": slot.status,
                "exit_code": slot.exit_code,
                "stdout_path": slot.stdout_path,
                "stderr_path": slot.stderr_path,
                "stdout_digest": slot.stdout_digest,
                "stderr_digest": slot.stderr_digest,
                "reaped_at": slot.reaped_at,
                "attempt": slot.attempt,
                "updated_at": updated_at,
                "details": slot.details,
            },
        )
        return slot

    def list_slots(self) -> list[DispatchSlot]:
        connection = self._connection()
        try:
            rows = connection.execute(
                "SELECT * FROM dispatch_slots ORDER BY process_id"
            ).fetchall()
        finally:
            connection.close()
        slots: list[DispatchSlot] = []
        for row in rows:
            command = tuple(json.loads(str(row["command_json"])))
            details_raw = str(row["details_json"] or "{}")
            try:
                details = json.loads(details_raw)
            except json.JSONDecodeError:
                details = {}
            if not isinstance(details, dict):
                details = {}
            slots.append(
                DispatchSlot(
                    process_id=str(row["process_id"]),
                    dispatch_id=str(row["dispatch_id"]),
                    command=command,
                    command_digest=str(row["command_digest"]),
                    candidate_id=str(row["candidate_id"]),
                    generation=str(row["generation"]),
                    config_sha256=str(row["config_sha256"]),
                    due_at=float(row["due_at"]),
                    status=str(row["status"]),
                    pid=None if row["pid"] is None else int(row["pid"]),
                    start_identity=None if row["start_identity"] is None else str(row["start_identity"]),
                    started_at=None if row["started_at"] is None else float(row["started_at"]),
                    deadline_at=None if row["deadline_at"] is None else float(row["deadline_at"]),
                    exit_code=None if row["exit_code"] is None else int(row["exit_code"]),
                    stdout_path=str(row["stdout_path"] or ""),
                    stderr_path=str(row["stderr_path"] or ""),
                    stdout_digest=None if row["stdout_digest"] is None else str(row["stdout_digest"]),
                    stderr_digest=None if row["stderr_digest"] is None else str(row["stderr_digest"]),
                    reaped_at=None if row["reaped_at"] is None else str(row["reaped_at"]),
                    attempt=int(row["attempt"] or 0),
                    details=details,
                )
            )
        return slots


def _subprocess_factory(
    command: Sequence[str],
    *,
    stdout_path: Path,
    stderr_path: Path,
    env: Mapping[str, str] | None = None,
) -> ChildHandle:
    _ensure_private_dir(stdout_path.parent)
    stdout = open(stdout_path, "wb")
    stderr = open(stderr_path, "wb")
    try:
        proc = subprocess.Popen(
            list(command),
            stdout=stdout,
            stderr=stderr,
            env=dict(env) if env is not None else None,
            start_new_session=True,
        )
    finally:
        stdout.close()
        stderr.close()

    class _RealChild(ChildHandle):
        def __init__(self) -> None:
            super().__init__(pid=int(proc.pid), start_identity=_start_identity(proc.pid))
            self._proc = proc

        def poll(self) -> int | None:
            return self._proc.poll()

        def wait(self, timeout: float | None = None) -> int:
            return int(self._proc.wait(timeout=timeout))

        def terminate(self) -> None:
            self._terminate_called = True
            try:
                os.killpg(int(self._proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass

        def kill(self) -> None:
            self._terminate_called = True
            try:
                os.killpg(int(self._proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass

    return _RealChild()

def _next_backoff(attempt: int, backoff_seconds: Sequence[int]) -> int:
    if not backoff_seconds:
        return 30
    index = max(0, min(attempt - 1, len(backoff_seconds) - 1))
    return int(backoff_seconds[index])


class Supervisor:
    """Resident scheduler: singleton lease, concurrent child dispatch, nonblocking reap."""

    def __init__(
        self,
        config_path: Path | str,
        db_path: Path | str,
        dry_run: bool,
        python: Path | str | None = None,
        state_root: Path | str | None = None,
        clock: Clock | None = None,
        process_factory: ProcessFactory | None = None,
        sleep: Callable[[float], None] | None = None,
        max_wake_seconds: float = MAX_WAKE_SECONDS,
        shutdown_drain_seconds: float = DEFAULT_SHUTDOWN_DRAIN_SECONDS,
        stop_event: threading.Event | None = None,
        once: bool = False,
        max_loops: int | None = None,
        owner_token: str | None = None,
        start_identity: str | None = None,
        candidate_id: str | None = None,
        generation: str | None = None,
        config_sha256: str | None = None,
    ) -> None:
        self.config_path = Path(config_path).expanduser()
        self.db_path = Path(db_path).expanduser()
        self.dry_run = bool(dry_run)
        if python is None:
            raise SupervisorError("supervisor python path is required")
        self.python = Path(python).expanduser()
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self.process_factory = process_factory or _subprocess_factory
        self.max_wake_seconds = float(max_wake_seconds)
        self.shutdown_drain_seconds = float(shutdown_drain_seconds)
        self.stop_event = stop_event or threading.Event()
        self.once = bool(once)
        self.max_loops = max_loops
        self._children: dict[str, ChildHandle] = {}
        self._draining = False
        self._acquired = False



        self._lease_lost = False
        self._should_release = True
        self._dispatches = 0
        self._loops = 0
        self._summary: dict[str, Any] = {}

        try:
            self.cfg = load_config(self.config_path)
        except ConfigError as exc:
            raise SupervisorError(str(exc)) from exc

        self.candidate_id = _validate_candidate_pin(candidate_id or _candidate_identity(self.cfg))
        if generation is not None:
            # Explicit generation injection is reserved for unit/integration tests.
            self.generation = str(generation).strip()
            if not self.generation:
                raise SupervisorError("injected generation must not be empty")
        else:
            generation_path = _generation_path()
            resolved_generation = _read_generation_value(generation_path)
            if not _is_candidate_id(resolved_generation):
                raise SupervisorError(
                    f"generation pointer is missing or invalid: {generation_path}"
                )
            if resolved_generation != self.candidate_id:
                raise SupervisorError("generation pointer does not match candidate_id")
            self.generation = resolved_generation
        self.config_sha256 = config_sha256 or _config_sha256(self.config_path)

        self.inventory = build_dispatch_commands(
            processes=list(getattr(self.cfg, "processes", ()) or ()),
            python=self.python,
            config_path=self.config_path,
            db_path=self.db_path,
            dry_run=self.dry_run,
        )
        self._inventory_by_id = {item["process_id"]: item for item in self.inventory}

        root = Path(state_root) if state_root is not None else supervisor_state_root(self.db_path)
        self.owner = SupervisorOwner(
            owner_token=owner_token or uuid.uuid4().hex,
            owner_pid=os.getpid(),
            start_identity=start_identity or _start_identity(),
            candidate_id=self.candidate_id,
            generation=self.generation,
            config_sha256=self.config_sha256,
        )
        self.store = SupervisorStore(root, owner=self.owner, clock=self.clock)
        self.slots: dict[str, DispatchSlot] = {}

    def _install_signal_handlers(self) -> Callable[[], None]:
        previous: dict[int, Any] = {}

        def _handler(signum: int, _frame: Any) -> None:
            del signum
            self.stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                previous[sig] = signal.signal(sig, _handler)
            except (ValueError, OSError):
                # Not in main thread / unsupported — tests inject stop_event.
                pass

        def restore() -> None:
            for sig, handler in previous.items():
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError):
                    pass

        return restore

    def _reconcile_slots(self) -> None:
        now = float(self.clock())
        active_statuses = {"starting", "running", "timeout_requested", "terminating", "kill_requested"}
        for slot in self.store.list_slots():
            self.slots[slot.process_id] = slot
            if slot.status == "starting":
                # The child may have been created after this reservation and before
                # its PID/start identity became durable. Never guess that it died.
                slot.status = "orphaned"
                slot.due_at = max(float(slot.due_at), now)
                slot.details = {
                    **slot.details,
                    "reason": "starting_reservation_unresolved",
                    "orphan_resolution": "unknown",
                    "fence_retained": True,
                    "recovery_required": True,
                }
                self._persist_slot(slot)
                continue
            if slot.status not in active_statuses:
                continue
            item = self._inventory_by_id.get(slot.process_id)
            if item is None:
                raise SupervisorError(f"unknown dispatch slot: {slot.process_id}")
            identity_ok = (
                slot.candidate_id == self.candidate_id
                and slot.generation == self.generation
                and slot.config_sha256 == self.config_sha256
                and slot.pid is not None
                and slot.start_identity is not None
            )
            resolution = _orphan_resolution(slot) if identity_ok else "identity_mismatch"
            slot.status = "orphaned"
            slot.details = {
                **slot.details,
                "reason": "restart_not_adopted" if identity_ok else "identity_or_liveness_mismatch",
                "alive": resolution == "live",
                "identity_ok": identity_ok,
                "orphan_resolution": resolution,
                "fence_retained": resolution == "live" or not identity_ok,
            }
            if resolution != "live" and slot.due_at < now:
                slot.due_at = now
            self._persist_slot(slot)

    def _persist_slot(self, slot: DispatchSlot) -> None:
        """Update the in-memory index and durably mirror one dispatch slot."""
        self.slots[slot.process_id] = slot
        self.store.upsert_slot(slot)

    def _status_snapshot(self, event: str) -> dict[str, Any]:
        lease = self.store.read_singleton()
        owned = lease is not None and self.store._identity_matches(lease.__dict__, self.owner)
        lease_state = "owned" if owned else ("present" if lease is not None else "absent")
        status_counts: dict[str, int] = {}
        slots: list[dict[str, Any]] = []
        for slot in sorted(self.slots.values(), key=lambda item: item.process_id):
            status_counts[slot.status] = status_counts.get(slot.status, 0) + 1
            slots.append(
                {
                    "process_id": slot.process_id,
                    "status": slot.status,
                    "dispatch_id": slot.dispatch_id,
                    "pid": slot.pid,
                    "start_identity": slot.start_identity,
                    "attempt": slot.attempt,
                    "due_at": slot.due_at,
                    "deadline_at": slot.deadline_at,
                    "exit_code": slot.exit_code,
                    "details": dict(slot.details),
                }
            )
        return {
            "schema_version": SUPERVISOR_STATUS_SCHEMA_VERSION,
            "event": event,
            "candidate_id": self.candidate_id,
            "generation": self.generation,
            "config_sha256": self.config_sha256,
            "supervisor_pid": self.owner.owner_pid,
            "supervisor_start_identity": self.owner.start_identity,
            "loop_timestamp": float(self.clock()),
            "loop_at": _utc_now(),
            "loops": self._loops,
            "dispatches": self._dispatches,
            "lease_state": lease_state,
            "lease": (
                None
                if lease is None
                else {
                    "owner_token": lease.owner_token,
                    "owner_pid": lease.owner_pid,
                    "start_identity": lease.start_identity,
                    "candidate_id": lease.candidate_id,
                    "generation": lease.generation,
                    "config_sha256": lease.config_sha256,
                    "expires_at": lease.expires_at,
                    "stale_after": lease.stale_after,
                    "last_renewed_at": lease.last_renewed_at,
                }
            ),
            "slot_counts": status_counts,
            "dispatch_slots": slots,
        }

    def _publish_status(self, event: str) -> None:
        if not self.store.write_status(self._status_snapshot(event)):
            self._lease_lost = True
            self._should_release = False
            raise LeaseError("supervisor status ownership lost")
        self._summary["status_path"] = str(self.store.status_path)

    def _ensure_slot(self, item: Mapping[str, Any], *, now: float) -> DispatchSlot:
        process_id = str(item["process_id"])
        existing = self.slots.get(process_id)
        if existing is not None:
            return existing
        slot = DispatchSlot(
            process_id=process_id,
            dispatch_id="",
            command=tuple(item["command"]),
            command_digest=str(item["command_digest"]),
            candidate_id=self.candidate_id,
            generation=self.generation,
            config_sha256=self.config_sha256,
            due_at=now,
            status="idle",
        )
        self._persist_slot(slot)
        return slot

    def _renew_or_fence(self) -> None:
        try:
            self.store.renew_singleton()
            if self._acquired:
                self._publish_status("renew")
        except LeaseError:
            self._lease_lost = True
            self._should_release = False
            raise

    def _clear_child_fence(self, slot: DispatchSlot) -> None:
        """Clear identity only after a terminal poll result."""
        slot.pid = None
        slot.start_identity = None
        slot.started_at = None
        slot.deadline_at = None

    def _reap_one(self, process_id: str, child: ChildHandle, slot: DispatchSlot) -> bool:
        item = self._inventory_by_id.get(process_id)
        if item is None:
            raise SupervisorError(f"unknown child process: {process_id}")
        now = float(self.clock())
        deadline_expired = slot.deadline_at is not None and now >= float(slot.deadline_at)
        code = child.poll()
        if code is None:
            if not self._draining and slot.status == "running" and deadline_expired:
                self._request_timeout(process_id, child, slot, now=now)
            elif slot.status == "terminating":
                sent_value = slot.details.get("terminate_sent_at")
                sent_at = now if sent_value is None else float(sent_value)
                if now >= sent_at + TIMEOUT_TERMINATE_GRACE_SECONDS:
                    self._request_kill(child, slot, now=now)
            elif slot.status == "kill_requested":
                sent_value = slot.details.get("kill_sent_at")
                sent_at = now if sent_value is None else float(sent_value)
                if now >= sent_at + TIMEOUT_KILL_GRACE_SECONDS:
                    slot.status = "orphaned"
                    slot.due_at = now
                    slot.details = {
                        **slot.details,
                        "reason": "timeout_kill_grace_expired",
                        "failure_class": "timeout",
                        "exit_confirmed": False,
                        "fence_retained": True,
                    }
                    self._persist_slot(slot)
            return False

        timed_out = slot.status in {"timeout_requested", "terminating", "kill_requested"}
        timed_out = timed_out or (
            slot.status == "orphaned" and slot.details.get("failure_class") == "timeout"
        )
        forced_stop = bool(slot.details.get("forced_stop")) or self._draining
        if deadline_expired and slot.status == "running" and not forced_stop:
            timed_out = True
        slot.exit_code = int(code)
        slot.reaped_at = _utc_now()
        if slot.stdout_path:
            slot.stdout_digest = _sha256_file(Path(slot.stdout_path))
        if slot.stderr_path:
            slot.stderr_digest = _sha256_file(Path(slot.stderr_path))

        if forced_stop:
            slot.status = "idle"
            slot.due_at = now
            slot.details = {**slot.details, "last_exit": int(code), "exit_confirmed": True}
        else:
            slot.status = "timed_out" if timed_out else ("reaped" if code == 0 else "failed")
            if code == 0 and not timed_out:
                slot.attempt = 0
                slot.due_at = now + float(item["interval_seconds"])
                slot.details = {**slot.details, "last_exit": int(code)}
            else:
                slot.attempt = int(slot.attempt) + 1
                max_attempts = max(1, int(item.get("max_attempts") or 1))
                backoff = _next_backoff(slot.attempt, item["backoff_seconds"])
                exhausted = slot.attempt >= max_attempts
                slot.due_at = now if exhausted else now + float(backoff)
                slot.details = {
                    **slot.details,
                    "last_exit": int(code),
                    "backoff_seconds": backoff,
                    "failure_class": "timeout" if timed_out else "retryable_child_exit",
                    "retry_exhausted": exhausted,
                }

        self._clear_child_fence(slot)
        self._persist_slot(slot)
        self._children.pop(process_id, None)
        return True

    def _reap_all(self) -> None:
        for process_id, child in list(self._children.items()):
            slot = self.slots.get(process_id)
            if slot is None:
                raise SupervisorError(f"child missing dispatch slot: {process_id}")
            self._reap_one(process_id, child, slot)
        if self._acquired:
            self._publish_status("reap")

    def _request_timeout(
        self,
        process_id: str,
        child: ChildHandle,
        slot: DispatchSlot,
        *,
        now: float,
    ) -> None:
        """Persist timeout_requested before TERM and retain the child handle."""
        del process_id
        if slot.status != "running":
            return
        slot.status = "timeout_requested"
        slot.details = {
            **slot.details,
            "reason": "deadline_exceeded",
            "failure_class": "timeout",
            "timeout_requested_at": now,
        }
        self._persist_slot(slot)
        try:
            child.terminate()
        except Exception as exc:
            slot.details = {**slot.details, "terminate_error": str(exc)}
        slot.status = "terminating"
        slot.details = {**slot.details, "terminate_sent_at": now}
        self._persist_slot(slot)

    def _request_kill(self, child: ChildHandle, slot: DispatchSlot, *, now: float) -> None:
        if slot.status != "terminating":
            return
        slot.status = "kill_requested"
        slot.details = {**slot.details, "kill_requested_at": now}
        self._persist_slot(slot)
        try:
            child.kill()
        except Exception as exc:
            slot.details = {**slot.details, "kill_error": str(exc)}
        slot.details = {**slot.details, "kill_sent_at": now}
        self._persist_slot(slot)

    def _dispatch_due(self) -> list[str]:
        if self._lease_lost or self.stop_event.is_set():
            return []
        now = float(self.clock())
        launched: list[str] = []
        for item in self.inventory:
            process_id = str(item["process_id"])
            if not item["enabled"] or process_id in self._children:
                continue
            slot = self._ensure_slot(item, now=now)
            if slot.status == "starting" or slot.details.get("recovery_required"):
                # A starting reservation has no trustworthy child identity. It is
                # a durable start fence, not permission to launch a duplicate.
                if slot.status == "starting":
                    slot.status = "orphaned"
                    slot.details = {
                        **slot.details,
                        "reason": "starting_reservation_unresolved",
                        "orphan_resolution": "unknown",
                        "fence_retained": True,
                        "recovery_required": True,
                    }
                    self._persist_slot(slot)
                continue
            if slot.status == "orphaned":
                resolution = _orphan_resolution(slot)
                if resolution == "live" or slot.details.get("recovery_required"):
                    slot.details = {
                        **slot.details,
                        "reason": "live_orphan_fence" if resolution == "live" else slot.details.get("reason"),
                        "alive": resolution == "live",
                        "orphan_resolution": resolution,
                    }
                    self._persist_slot(slot)
                    continue
                slot.status = "idle"
                slot.pid = None
                slot.start_identity = None
                slot.started_at = None
                slot.deadline_at = None
                slot.details = {
                    **slot.details,
                    "reason": "orphan_dead_redispatch",
                    "alive": False,
                    "orphan_resolution": resolution,
                }
                self._persist_slot(slot)
            if slot.details.get("retry_exhausted"):
                continue
            if float(slot.due_at) > now:
                continue
            dispatch_id = uuid.uuid4().hex
            log_dir = self.store.logs_dir / process_id
            _ensure_private_dir(log_dir)
            stdout_path = log_dir / f"{dispatch_id}.out.log"
            stderr_path = log_dir / f"{dispatch_id}.err.log"
            command = list(item["command"])
            slot.dispatch_id = dispatch_id
            slot.command = tuple(command)
            slot.command_digest = str(item["command_digest"])
            slot.pid = None
            slot.start_identity = None
            slot.started_at = None
            slot.deadline_at = None
            slot.status = "starting"
            slot.exit_code = None
            slot.stdout_path = str(stdout_path)
            slot.stderr_path = str(stderr_path)
            slot.stdout_digest = None
            slot.stderr_digest = None
            slot.reaped_at = None
            slot.candidate_id = self.candidate_id
            slot.generation = self.generation
            slot.config_sha256 = self.config_sha256
            shutdown_keys = {
                "forced_stop",
                "exit_confirmed",
                "fence_retained",
                "reason",
                "termination_requested_at",
                "terminate_sent_at",
                "terminate_error",
                "kill_requested_at",
                "kill_sent_at",
                "kill_error",
                "lease_lost",
                "timeout_requested_at",
                "recovery_required",
                "orphan_resolution",
                "alive",
                "start_reservation",
            }
            slot.details = {
                key: value
                for key, value in slot.details.items()
                if key not in {"retry_exhausted", "failure_class", "backoff_seconds"} | shutdown_keys
            }
            prior_details = {
                key: value
                for key, value in slot.details.items()
                if key not in {"retry_exhausted", "failure_class", "backoff_seconds"} | shutdown_keys
            }
            slot.details = {
                **prior_details,
                "dispatched_at": _utc_now(),
                "start_reservation": {
                    "dispatch_id": dispatch_id,
                    "command": list(command),
                    "command_digest": slot.command_digest,
                    "fencing_identity": {
                        "candidate_id": self.candidate_id,
                        "generation": self.generation,
                        "config_sha256": self.config_sha256,
                        "owner_token": self.owner.owner_token,
                        "supervisor_pid": self.owner.owner_pid,
                        "supervisor_start_identity": self.owner.start_identity,
                    },
                },
            }
            # This write is the child-start fence. A crash after factory() returns
            # but before running-state persistence leaves an unresolved reservation.
            self._persist_slot(slot)
            child = self.process_factory(command, stdout_path=stdout_path, stderr_path=stderr_path)
            slot.pid = int(child.pid)
            slot.start_identity = child.start_identity
            slot.started_at = now
            slot.deadline_at = now + max(float(item["interval_seconds"]) * 4.0, 120.0)
            slot.status = "running"
            slot.details = {key: value for key, value in slot.details.items() if key != "start_reservation"}
            self._persist_slot(slot)
            self._children[process_id] = child
            self._dispatches += 1
            launched.append(process_id)
        if launched and self._acquired:
            self._publish_status("dispatch")
        return launched
    def _next_sleep(self, *, now: float | None = None) -> float:
        """Return a bounded wake interval without spinning on live fences."""
        current = float(self.clock() if now is None else now)
        if self._children:
            return min(self.max_wake_seconds, POLL_INTERVAL_SECONDS)
        delays: list[float] = []
        for item in self.inventory:
            process_id = str(item["process_id"])
            if not item["enabled"]:
                continue
            slot = self.slots.get(process_id)
            if slot is None:
                delays.append(0.0)
                continue
            if slot.details.get("retry_exhausted"):
                continue
            if slot.status == "starting" or slot.details.get("recovery_required"):
                continue
            if slot.status == "orphaned" and _orphan_resolution(slot) == "live":
                # A live/uncertain orphan owns its fence; due_at must not cause
                # a redispatch loop while the identity remains unresolved.
                continue
            delays.append(max(0.0, float(slot.due_at) - current))
        if not delays:
            return self.max_wake_seconds
        return min(self.max_wake_seconds, max(0.0, min(delays)))

    def _drain(self) -> None:
        """Stop children within one absolute shutdown budget."""
        budget = max(0.0, self.shutdown_drain_seconds)
        started = time.monotonic()
        deadline = started + budget
        voluntary_deadline = started + budget * 0.25
        terminate_deadline = started + budget * 0.60
        self._draining = True
        try:
            def poll_children() -> None:
                for process_id, child in list(self._children.items()):
                    slot = self.slots.get(process_id)
                    if slot is None:
                        raise SupervisorError(f"child missing dispatch slot: {process_id}")
                    self._reap_one(process_id, child, slot)

            def poll_until(phase_deadline: float, phase_started: float) -> None:
                phase_budget = max(0.0, phase_deadline - phase_started)
                max_polls = max(1, int(phase_budget / POLL_INTERVAL_SECONDS) + 1)
                polls = 0
                while self._children and polls < max_polls:
                    remaining = phase_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self.sleep(min(POLL_INTERVAL_SECONDS, remaining))
                    polls += 1
                    poll_children()

            poll_children()
            if not self._children:
                return
            poll_until(voluntary_deadline, started)
            if not self._children:
                return

            termination_now = float(self.clock())
            for process_id, child in list(self._children.items()):
                slot = self.slots[process_id]
                slot.status = "terminating"
                slot.details = {
                    **slot.details,
                    "reason": "bounded_drain_stop",
                    "forced_stop": True,
                    "termination_requested_at": termination_now,
                    "terminate_sent_at": termination_now,
                    "lease_lost": self._lease_lost,
                }
                self._persist_slot(slot)
                try:
                    child.terminate()
                except Exception as exc:
                    slot.details = {**slot.details, "terminate_error": str(exc)}
                    self._persist_slot(slot)

            poll_until(terminate_deadline, time.monotonic())
            if not self._children:
                return

            kill_now = float(self.clock())
            for process_id, child in list(self._children.items()):
                slot = self.slots[process_id]
                if slot.status == "terminating":
                    self._request_kill(child, slot, now=kill_now)
                elif slot.status != "kill_requested":
                    slot.status = "kill_requested"
                    slot.details = {**slot.details, "kill_requested_at": kill_now}
                    self._persist_slot(slot)
                    try:
                        child.kill()
                    except Exception as exc:
                        slot.details = {**slot.details, "kill_error": str(exc)}
                    slot.details = {**slot.details, "kill_sent_at": kill_now}
                    self._persist_slot(slot)

            poll_until(deadline, time.monotonic())
            if not self._children:
                return

            now = float(self.clock())
            for process_id, child in list(self._children.items()):
                slot = self.slots[process_id]
                code = child.poll()
                if code is not None:
                    self._reap_one(process_id, child, slot)
                    continue
                slot.status = "orphaned"
                slot.due_at = now
                slot.details = {
                    **slot.details,
                    "reason": "bounded_drain_orphaned",
                    "forced_stop": True,
                    "exit_confirmed": False,
                    "fence_retained": True,
                }
                self._persist_slot(slot)
        finally:
            self._draining = False
    def run(self) -> dict[str, Any]:
        restore_signals = self._install_signal_handlers()
        status = "ok"
        reason = "completed"
        launched_total = 0
        acquired = False
        try:
            self.store.acquire_singleton()
            acquired = True
            self._acquired = True
            self._reconcile_slots()
            self._publish_status("acquire")
            while True:
                if self.stop_event.is_set():
                    reason = "signal"
                    break
                if self.max_loops is not None and self._loops >= self.max_loops:
                    reason = "max_loops"
                    break

                self._renew_or_fence()
                self._reap_all()
                launched_total += len(self._dispatch_due())
                self._loops += 1

                if self.once:
                    reason = "once"
                    break
                if self.max_loops is not None and self._loops >= self.max_loops:
                    reason = "max_loops"
                    break
                self.sleep(self._next_sleep())
        except LeaseError as exc:
            status = "failed"
            reason = "singleton_lease_lost" if self._lease_lost else "singleton_acquire_failed"
            self._summary["error"] = str(exc)
            self._should_release = acquired and not self._lease_lost
        except SupervisorError as exc:
            status = "failed"
            reason = "supervisor_error"
            self._summary["error"] = str(exc)
        finally:
            try:
                self._drain()
            finally:
                if self._should_release and not self._lease_lost:
                    try:
                        self.store.release_singleton()
                    except Exception:
                        pass
                restore_signals()

        payload = {
            "ok": status == "ok",
            "status": status,
            "reason": reason,
            "label": SUPERVISOR_LABEL,
            "singleton_key": SINGLETON_KEY,
            "dry_run": self.dry_run,
            "candidate_id": self.candidate_id,
            "generation": self.generation,
            "config_sha256": self.config_sha256,
            "owner_token": self.owner.owner_token,
            "dispatches": self._dispatches,
            "launched": launched_total,
            "loops": self._loops,
            "lease_lost": self._lease_lost,
            "dispatch_commands": [
                {
                    "process_id": item["process_id"],
                    "enabled": item["enabled"],
                    "interval_seconds": item["interval_seconds"],
                    "command": list(item["command"]),
                    "command_digest": item["command_digest"],
                }
                for item in self.inventory
            ],
            "slots": [
                {
                    "process_id": slot.process_id,
                    "status": slot.status,
                    "dispatch_id": slot.dispatch_id,
                    "pid": slot.pid,
                    "exit_code": slot.exit_code,
                    "due_at": slot.due_at,
                    "command_digest": slot.command_digest,
                    "stdout_path": slot.stdout_path,
                    "stderr_path": slot.stderr_path,
                }
                for slot in self.slots.values()
            ],
            "state_root": str(self.store.root),
        }
        payload.update(self._summary)
        return payload


def resolve_dry_run(args: argparse.Namespace) -> bool | int:
    if getattr(args, "dry_run", False) and getattr(args, "live", False):
        print("error: --dry-run and --live are mutually exclusive", file=sys.stderr)
        return 2
    if getattr(args, "live", False):
        return False
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lokay.supervisor",
        description=(
            "Resident catalog supervisor. Schedules isolated lokay.process children; "
            "does not own catalog mutations."
        ),
    )
    parser.add_argument("--config", required=True, help="Path to canonical config.toml")
    parser.add_argument("--db", required=True, help="Shared Fala/process db path passed to children")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Force dry-run children (default when --live is absent)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Allow live child mutations (requires config mode=live)",
    )
    parser.add_argument("--json", action="store_true", help="Print full JSON result")
    parser.add_argument(
        "--once",
        action="store_true",
        default=False,
        help="Run one scheduling pass then exit (tests/diagnostics)",
    )
    return parser


def run_supervisor(
    *,
    config_path: Path | str,
    db_path: Path | str,
    dry_run: bool,
    once: bool = False,
    python: Path | str | None = None,
    state_root: Path | str | None = None,
    clock: Clock | None = None,
    process_factory: ProcessFactory | None = None,
    sleep: Callable[[float], None] | None = None,
    stop_event: threading.Event | None = None,
    max_loops: int | None = None,
    shutdown_drain_seconds: float = DEFAULT_SHUTDOWN_DRAIN_SECONDS,
    owner_token: str | None = None,
    start_identity: str | None = None,
    candidate_id: str | None = None,
    generation: str | None = None,
    config_sha256: str | None = None,
) -> dict[str, Any]:
    supervisor = Supervisor(
        config_path=config_path,
        db_path=db_path,
        dry_run=dry_run,
        python=python,
        state_root=state_root,
        clock=clock,
        process_factory=process_factory,
        sleep=sleep,
        stop_event=stop_event,
        once=once,
        max_loops=max_loops,
        shutdown_drain_seconds=shutdown_drain_seconds,
        owner_token=owner_token,
        start_identity=start_identity,
        candidate_id=candidate_id,
        generation=generation,
        config_sha256=config_sha256,
    )
    return supervisor.run()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    dry = resolve_dry_run(args)
    if dry == 2:
        return 2
    try:
        result = run_supervisor(
            config_path=args.config,
            db_path=args.db,
            dry_run=bool(dry),
            once=bool(args.once),
            python=sys.executable,
        )
    except SupervisorError as exc:
        payload = {
            "ok": False,
            "status": "failed",
            "reason": "supervisor_error",
            "error": str(exc),
            "label": SUPERVISOR_LABEL,
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print(
            f"status={result.get('status')} reason={result.get('reason')} "
            f"dispatches={result.get('dispatches')} lease_lost={result.get('lease_lost')}"
        )
    if not result.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
