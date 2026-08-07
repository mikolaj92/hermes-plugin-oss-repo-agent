"""SQLite-backed durable process runtime foundation for canonical Lokay processes.

Non-mutating orchestration only: adapters own external mutations. This module
owns durable process-state schema, immutable receipts, cursor advance after
verified receipt, process/subject leases and locks, generation fencing,
retry/backoff decisions, and health rows.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from lokay.registry import PROCESS_IDS

SCHEMA_VERSION = 1
DEFAULT_BACKOFF_SECONDS: tuple[int, ...] = (30, 60, 120, 300, 600)
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_GENERATION_PATH = Path("~/.hermes/lokay/generation")

_RECEIPT_ROOT_BY_KIND: dict[str, str] = {
    "repo_poll": "triage",
    "issue_snapshot": "triage",
    "issue_decision": "triage",
    "feedback": "triage",
    "feedback_verified": "triage",
    "split": "triage",
    "child_handoff": "triage",
    "split_verified": "triage",
    "close_authorization": "triage",
    "close_verified": "triage",
    "claim": "dispatch",
    "task_handoff": "dispatch",
    "implementation": "dispatch",
    "pr_opened": "dispatch",
    "pr_decision": "merge",
    "repair_reservation": "merge",
    "repair_verified": "merge",
    "merge_verified": "merge",
    "finalization": "merge",
    "cleanup_verified": "cleanup",
    "cleanup_reconciliation": "reconciliation",
}
_EXTERNAL_INPUT_PATH_BY_KIND: dict[str, tuple[str, ...]] = {
    "unresolved_cleanup_evidence": ("cleanup", "unresolved"),
}

 
@dataclass(frozen=True)
class ExternalInputRecord:
    process_id: str
    input_kind: str
    subject: str
    digest: str
    path: Path
    payload: dict[str, Any]
    status: str  # written | exists | planned



class ProcessRuntimeError(RuntimeError):
    """Fail-closed process runtime contract violation."""


class ReceiptConflictError(ProcessRuntimeError):
    """An existing receipt diverges from the candidate payload."""


class FenceError(ProcessRuntimeError):
    """Candidate/config generation fence rejected the operation."""


class LeaseError(ProcessRuntimeError):
    """Process or subject lease could not be acquired or renewed."""


class CursorError(ProcessRuntimeError):
    """Cursor advance was refused because the receipt is unverified."""


class ProcessDisabledError(ProcessRuntimeError):
    """Process is disabled and must not mutate or run callbacks."""


@dataclass(frozen=True)
class RuntimePaths:
    """Durable roots for process-state, receipts, locks, and fence pointer."""

    state_root: Path
    db_path: Path
    triage_receipts: Path
    dispatch_receipts: Path
    merge_receipts: Path
    generation_path: Path

    @classmethod
    def from_state_root(
        cls,
        state_root: str | Path,
        *,
        generation_path: str | Path | None = None,
    ) -> RuntimePaths:
        root = Path(state_root).expanduser()
        return cls(
            state_root=root,
            db_path=root / "process-state.sqlite3",
            triage_receipts=root / "triage_receipts",
            dispatch_receipts=root / "dispatch_receipts",
            merge_receipts=root / "merge_receipts",
            generation_path=Path(generation_path or DEFAULT_GENERATION_PATH).expanduser(),
        )


@dataclass(frozen=True)
class LeaseRecord:
    process_id: str
    subject: str
    owner: str
    owner_pid: int
    acquired_at: str
    expires_at: float
    stale_after: float
    generation: str
    reclaimed: bool = False


@dataclass(frozen=True)
class ReceiptRecord:
    process_id: str
    receipt_kind: str
    subject: str
    digest: str
    path: Path
    payload: dict[str, Any]
    status: str  # written | exists | planned


@dataclass(frozen=True)
class CursorRecord:
    process_id: str
    cursor_key: str
    value: str
    receipt_digest: str
    advanced_at: str


@dataclass(frozen=True)
class RetryDecision:
    process_id: str
    subject: str
    attempt: int
    max_attempts: int
    failure_class: str
    should_retry: bool
    backoff_seconds: int
    next_attempt_at: float | None
    exhausted: bool


@dataclass(frozen=True)
class HealthRecord:
    process_id: str
    status: str
    owner: str | None
    lease_expires_at: float | None
    last_exit: int | None
    last_error: str | None
    attempt: int
    generation: str | None
    updated_at: str
    details: dict[str, Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def payload_digest(payload: Mapping[str, Any]) -> str:
    return _digest_bytes(_canonical_json(dict(payload)).encode("utf-8"))


def subject_key(subject: str | Mapping[str, Any]) -> str:
    if isinstance(subject, str):
        text = subject.strip()
        if not text:
            raise ProcessRuntimeError("subject must not be empty")
        return text
    return payload_digest(dict(subject))


def _safe_component(value: str, *, field: str) -> str:
    text = value.strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text:
        raise ProcessRuntimeError(f"unsafe_{field}")
    return text


def _process_id(process_id: str) -> str:
    text = _safe_component(process_id, field="process_id")
    if text not in PROCESS_IDS:
        raise ProcessRuntimeError(f"unknown process id: {text}")
    return text


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ProcessRuntimeError(f"directory must not be a symlink: {path}")
    mode = path.stat().st_mode
    if not stat.S_ISDIR(mode):
        raise ProcessRuntimeError(f"not a directory: {path}")
    os.chmod(path, 0o700)
    return path


def _fsync_dir(directory: Path) -> None:
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _private_stat(path: Path) -> os.stat_result | None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_nlink != 1:
        raise ProcessRuntimeError("receipt_not_private_regular_single_link")
    return metadata


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
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


def initialize_schema(db_path: str | Path) -> Path:
    """Create durable process-state schema. Idempotent and fail-closed."""
    path = Path(db_path).expanduser()
    connection = _connect(path)
    try:
        # executescript issues its own commits; do not wrap it in BEGIN/COMMIT.
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY NOT NULL,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS receipts (
                process_id TEXT NOT NULL,
                receipt_kind TEXT NOT NULL,
                subject TEXT NOT NULL,
                digest TEXT NOT NULL,
                path TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (process_id, receipt_kind, subject)
            );
            CREATE TABLE IF NOT EXISTS external_inputs (
                process_id TEXT NOT NULL,
                input_kind TEXT NOT NULL,
                subject TEXT NOT NULL,
                digest TEXT NOT NULL,
                path TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (process_id, input_kind, subject)
            );
            CREATE TABLE IF NOT EXISTS cursors (
                process_id TEXT NOT NULL,
                cursor_key TEXT NOT NULL,
                value TEXT NOT NULL,
                receipt_digest TEXT NOT NULL,
                advanced_at TEXT NOT NULL,
                PRIMARY KEY (process_id, cursor_key)
            );
            CREATE TABLE IF NOT EXISTS leases (
                process_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                owner TEXT NOT NULL,
                owner_pid INTEGER NOT NULL,
                acquired_at TEXT NOT NULL,
                expires_at REAL NOT NULL,
                stale_after REAL NOT NULL,
                generation TEXT NOT NULL,
                PRIMARY KEY (process_id, subject)
            );
            CREATE TABLE IF NOT EXISTS retries (
                process_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                max_attempts INTEGER NOT NULL,
                failure_class TEXT NOT NULL,
                next_attempt_at REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (process_id, subject)
            );
            CREATE TABLE IF NOT EXISTS health (
                process_id TEXT PRIMARY KEY NOT NULL,
                status TEXT NOT NULL,
                owner TEXT,
                lease_expires_at REAL,
                last_exit INTEGER,
                last_error TEXT,
                attempt INTEGER NOT NULL DEFAULT 0,
                generation TEXT,
                updated_at TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            """
        )
        # DDL above is intentionally outside the transaction because
        # sqlite3.executescript commits it. Serialize the version marker
        # separately so concurrent openers cannot observe or partially write
        # an unverified schema version.
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
                raise ProcessRuntimeError(
                    "unsupported or missing process-state schema version"
                )
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
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


class ProcessRuntime:
    """Durable process runtime state APIs for one state root."""

    def __init__(
        self,
        paths: RuntimePaths,
        *,
        dry_run: bool = True,
        owner: str | None = None,
    ) -> None:
        self.paths = paths
        self.dry_run = bool(dry_run)
        self.owner = owner or f"pid:{os.getpid()}"
        self._owner_pid = os.getpid()
        initialize_schema(self.paths.db_path)
        for directory in (
            self.paths.state_root,
            self.paths.triage_receipts,
            self.paths.dispatch_receipts,
            self.paths.merge_receipts,
            self.paths.state_root / "leases",
            self.paths.state_root / "retries",
            self.paths.state_root / "health",
            self.paths.state_root / "locks",
            self.paths.state_root / "cursors",
            self.paths.state_root / "cleanup",
            self.paths.state_root / "reconciliation",
        ):
            _ensure_private_dir(directory)

    @classmethod
    def open(
        cls,
        state_root: str | Path,
        *,
        dry_run: bool = True,
        generation_path: str | Path | None = None,
        owner: str | None = None,
    ) -> ProcessRuntime:
        return cls(
            RuntimePaths.from_state_root(state_root, generation_path=generation_path),
            dry_run=dry_run,
            owner=owner,
        )

    def _connection(self) -> sqlite3.Connection:
        return _connect(self.paths.db_path)

    def _receipt_root(self, receipt_kind: str) -> Path:
        family = _RECEIPT_ROOT_BY_KIND.get(receipt_kind)
        if family is None:
            raise ProcessRuntimeError(f"unknown receipt kind: {receipt_kind}")
        if family == "triage":
            return self.paths.triage_receipts
        if family == "dispatch":
            return self.paths.dispatch_receipts
        if family == "merge":
            return self.paths.merge_receipts
        if family == "cleanup":
            return self.paths.state_root / "cleanup"
        return self.paths.state_root / "reconciliation"

    def _subject_dir(
        self,
        *,
        process_id: str,
        receipt_kind: str,
        subject_text: str,
    ) -> Path:
        return (
            self._receipt_root(receipt_kind)
            / process_id
            / receipt_kind
            / _sha256_hex(subject_text)[:32]
        )

    def receipt_path(
        self,
        *,
        process_id: str,
        receipt_kind: str,
        subject: str | Mapping[str, Any],
        digest: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> Path:
        process = _process_id(process_id)
        kind = _safe_component(receipt_kind, field="receipt_kind")
        subject_text = subject_key(subject)
        if digest is None:
            if payload is None:
                raise ProcessRuntimeError("digest or payload is required")
            digest = payload_digest(payload)
        digest = digest.lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ProcessRuntimeError("digest must be sha256 hex")
        # Content-addressed: sha256(subject|stage|digest).json
        name = _sha256_hex(f"{subject_text}|{kind}|{digest}") + ".json"
        return self._subject_dir(
            process_id=process, receipt_kind=kind, subject_text=subject_text
        ) / name

    def read_generation(self) -> str:
        path = self.paths.generation_path
        if not path.exists():
            return ""
        _private_stat(path)
        return path.read_text(encoding="utf-8").strip()

    def write_generation(self, generation: str) -> str:
        value = generation.strip()
        if not value:
            raise ProcessRuntimeError("generation must not be empty")
        path = self.paths.generation_path
        _ensure_private_dir(path.parent)
        fd, name = tempfile.mkstemp(prefix=".generation.", suffix=".tmp", dir=str(path.parent))
        temp = Path(name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(value + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(str(temp), str(path))
            _fsync_dir(path.parent)
            _private_stat(path)
        except Exception:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass
            raise
        return value

    def check_generation_fence(
        self,
        expected_generation: str,
        *,
        config_sha256: str | None = None,
        candidate_id: str | None = None,
    ) -> str:
        """Fail closed when generation or optional candidate/config identity mismatches."""
        current = self.read_generation()
        expected = (expected_generation or "").strip()
        if not expected:
            raise FenceError("generation fence requires expected_generation")
        if current != expected:
            raise FenceError(
                f"generation fence mismatch: current={current!r} expected={expected!r}"
            )
        if candidate_id is not None and not str(candidate_id).strip():
            raise FenceError("candidate_id must not be empty when provided")
        if config_sha256 is not None:
            digest = str(config_sha256).strip().lower()
            if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                raise FenceError("config_sha256 must be sha256 hex")
        return current

    def _build_receipt_body(
        self,
        *,
        process: str,
        kind: str,
        subject: str | Mapping[str, Any],
        subject_text: str,
        payload: Mapping[str, Any],
        generation: str,
        candidate_id: str,
        config_sha256: str,
        correlation_id: str,
        predecessor_digests: Sequence[str],
        operation: str,
        mutation_status: str,
        is_dry: bool,
    ) -> tuple[str, dict[str, Any]]:
        # Deterministic identity body (no wall-clock) so re-publish is idempotent.
        identity = {
            "schema_version": SCHEMA_VERSION,
            "process_id": process,
            "candidate_id": candidate_id,
            "config_sha256": config_sha256,
            "generation": generation,
            "correlation_id": correlation_id
            or payload_digest(
                {"process_id": process, "kind": kind, "subject": subject_text}
            ),
            "subject": dict(subject) if isinstance(subject, Mapping) else {"id": subject_text},
            "predecessor_digests": list(predecessor_digests),
            "operation": operation,
            "source": "lokay.process_runtime",
            "mutation_status": "planned" if is_dry else mutation_status,
            "payload": dict(payload),
        }
        digest = payload_digest(identity)
        body = dict(identity)
        body["content_digest"] = digest
        body["verified_readback_state"] = "not_applicable" if is_dry else "verified"
        return digest, body

    def _identity_equal(self, left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
        keys = (
            "schema_version",
            "process_id",
            "candidate_id",
            "config_sha256",
            "generation",
            "correlation_id",
            "subject",
            "predecessor_digests",
            "operation",
            "source",
            "mutation_status",
            "payload",
            "content_digest",
        )
        return all(left.get(key) == right.get(key) for key in keys)

    def _lookup_subject_receipt(
        self,
        *,
        process_id: str,
        receipt_kind: str,
        subject: str,
    ) -> sqlite3.Row | None:
        connection = self._connection()
        try:
            return connection.execute(
                """
                SELECT process_id, receipt_kind, subject, digest, path,
                       payload_json, status, created_at
                FROM receipts
                WHERE process_id = ? AND receipt_kind = ? AND subject = ?
                """,
                (process_id, receipt_kind, subject),
            ).fetchone()
        finally:
            connection.close()

    def publish_receipt(
        self,
        *,
        process_id: str,
        receipt_kind: str,
        subject: str | Mapping[str, Any],
        payload: Mapping[str, Any],
        generation: str,
        candidate_id: str = "",
        config_sha256: str = "",
        correlation_id: str = "",
        predecessor_digests: Sequence[str] = (),
        operation: str = "publish",
        mutation_status: str = "mutated",
        dry_run: bool | None = None,
    ) -> ReceiptRecord:
        """Publish an immutable private receipt with fsync + readback.

        Identical payload is idempotent (`exists`). Divergent payload conflicts.
        """
        is_dry = self.dry_run if dry_run is None else bool(dry_run)
        process = _process_id(process_id)
        kind = _safe_component(receipt_kind, field="receipt_kind")
        subject_text = subject_key(subject)
        self.check_generation_fence(
            generation,
            config_sha256=config_sha256 or None,
            candidate_id=candidate_id or None,
        )
        digest, body = self._build_receipt_body(
            process=process,
            kind=kind,
            subject=subject,
            subject_text=subject_text,
            payload=payload,
            generation=generation,
            candidate_id=candidate_id,
            config_sha256=config_sha256,
            correlation_id=correlation_id,
            predecessor_digests=predecessor_digests,
            operation=operation,
            mutation_status=mutation_status,
            is_dry=is_dry,
        )
        path = self.receipt_path(
            process_id=process,
            receipt_kind=kind,
            subject=subject_text,
            digest=digest,
        )
        if is_dry:
            return ReceiptRecord(
                process_id=process,
                receipt_kind=kind,
                subject=subject_text,
                digest=digest,
                path=path,
                payload=body,
                status="planned",
            )

        subject_dir = self._subject_dir(
            process_id=process, receipt_kind=kind, subject_text=subject_text
        )
        _ensure_private_dir(subject_dir)
        with _file_lock(subject_dir / ".receipt.lock"):
            indexed = self._lookup_subject_receipt(
                process_id=process, receipt_kind=kind, subject=subject_text
            )
            if indexed is not None:
                if str(indexed["digest"]) != digest:
                    raise ReceiptConflictError(
                        f"receipt conflict for {process}/{kind}/{subject_text}: "
                        f"existing digest={indexed['digest']}"
                    )
                existing_path = Path(str(indexed["path"]))
                existing_body = self.read_receipt(existing_path)
                if not self._identity_equal(existing_body, body):
                    raise ReceiptConflictError(
                        f"receipt conflict at {existing_path}: payload diverges"
                    )
                self._index_receipt(
                    process_id=process,
                    receipt_kind=kind,
                    subject=subject_text,
                    digest=digest,
                    path=existing_path,
                    payload=existing_body,
                    status="exists",
                )
                return ReceiptRecord(
                    process_id=process,
                    receipt_kind=kind,
                    subject=subject_text,
                    digest=digest,
                    path=existing_path,
                    payload=existing_body,
                    status="exists",
                )

            # Filesystem subject-directory scan: one logical receipt per subject.
            for candidate in sorted(subject_dir.glob("*.json")):
                if candidate.name.startswith("."):
                    continue
                existing_body = self.read_receipt(candidate)
                if self._identity_equal(existing_body, body):
                    self._index_receipt(
                        process_id=process,
                        receipt_kind=kind,
                        subject=subject_text,
                        digest=digest,
                        path=candidate,
                        payload=existing_body,
                        status="exists",
                    )
                    return ReceiptRecord(
                        process_id=process,
                        receipt_kind=kind,
                        subject=subject_text,
                        digest=digest,
                        path=candidate,
                        payload=existing_body,
                        status="exists",
                    )
                raise ReceiptConflictError(
                    f"receipt conflict at {candidate}: divergent payload for subject"
                )

            raw = (_canonical_json(body) + "\n").encode("utf-8")
            temp: Path | None = None
            published_identity: tuple[int, int] | None = None
            try:
                fd, name = tempfile.mkstemp(
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    dir=str(path.parent),
                )
                temp = Path(name)
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                    meta = os.fstat(stream.fileno())
                    published_identity = (meta.st_dev, meta.st_ino)
                try:
                    os.link(str(temp), str(path))
                except FileExistsError:
                    existing_body = self.read_receipt(path)
                    if self._identity_equal(existing_body, body):
                        self._index_receipt(
                            process_id=process,
                            receipt_kind=kind,
                            subject=subject_text,
                            digest=digest,
                            path=path,
                            payload=existing_body,
                            status="exists",
                        )
                        return ReceiptRecord(
                            process_id=process,
                            receipt_kind=kind,
                            subject=subject_text,
                            digest=digest,
                            path=path,
                            payload=existing_body,
                            status="exists",
                        )
                    raise ReceiptConflictError(
                        f"receipt conflict at {path}: existing digest differs"
                    )
                os.unlink(str(temp))
                temp = None
                _fsync_dir(path.parent)
                readback = self.read_receipt(path)
                if not self._identity_equal(readback, body):
                    raise ProcessRuntimeError("receipt_readback_mismatch")
            except Exception as exc:
                if published_identity is not None and path.exists():
                    try:
                        current = _private_stat(path)
                        if (
                            current is not None
                            and (current.st_dev, current.st_ino) == published_identity
                        ):
                            path.unlink()
                            _fsync_dir(path.parent)
                    except OSError:
                        pass
                if temp is not None and temp.exists():
                    try:
                        temp.unlink()
                    except OSError:
                        pass
                if isinstance(exc, (ProcessRuntimeError, ReceiptConflictError)):
                    raise
                raise ProcessRuntimeError(f"receipt_write_failed: {exc}") from exc

            self._index_receipt(
                process_id=process,
                receipt_kind=kind,
                subject=subject_text,
                digest=digest,
                path=path,
                payload=body,
                status="written",
            )
            return ReceiptRecord(
                process_id=process,
                receipt_kind=kind,
                subject=subject_text,
                digest=digest,
                path=path,
                payload=body,
                status="written",
            )

    def _external_input_root(self, input_kind: str) -> Path:
        parts = _EXTERNAL_INPUT_PATH_BY_KIND.get(input_kind)
        if parts is None:
            raise ProcessRuntimeError(f"unknown external input kind: {input_kind}")
        root = self.paths.state_root
        for part in parts:
            root /= part
        return root

    def external_input_path(
        self,
        *,
        process_id: str,
        input_kind: str,
        subject: str | Mapping[str, Any],
        digest: str,
    ) -> Path:
        process = _process_id(process_id)
        kind = _safe_component(input_kind, field="input_kind")
        subject_text = subject_key(subject)
        digest = str(digest).lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ProcessRuntimeError("digest must be sha256 hex")
        root = self._external_input_root(kind) / process / kind / _sha256_hex(subject_text)[:32]
        return root / (_sha256_hex(f"{subject_text}|{kind}|{digest}") + ".json")

    def _index_external_input(
        self,
        *,
        process_id: str,
        input_kind: str,
        subject: str,
        digest: str,
        path: Path,
        payload: Mapping[str, Any],
        status: str,
    ) -> None:
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO external_inputs(
                    process_id, input_kind, subject, digest, path,
                    payload_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(process_id, input_kind, subject) DO UPDATE SET
                    digest = excluded.digest,
                    path = excluded.path,
                    payload_json = excluded.payload_json,
                    status = excluded.status
                """,
                (
                    process_id, input_kind, subject, digest, str(path),
                    _canonical_json(dict(payload)), status, _utc_now(),
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def publish_external_input(
        self,
        *,
        process_id: str,
        input_kind: str,
        subject: str | Mapping[str, Any],
        payload: Mapping[str, Any],
        generation: str,
        candidate_id: str,
        config_sha256: str,
    ) -> ExternalInputRecord:
        """Persist an identity-bound external trigger/input with readback."""
        process = _process_id(process_id)
        kind = _safe_component(input_kind, field="input_kind")
        if kind not in _EXTERNAL_INPUT_PATH_BY_KIND:
            raise ProcessRuntimeError(f"unknown external input kind: {kind}")
        if not isinstance(payload, Mapping) or not payload:
            raise ProcessRuntimeError("external input payload must be a non-empty object")
        self.check_generation_fence(
            generation,
            candidate_id=candidate_id,
            config_sha256=config_sha256,
        )
        subject_text = subject_key(subject)
        identity = {
            "schema_version": SCHEMA_VERSION,
            "process_id": process,
            "input_kind": kind,
            "candidate_id": str(candidate_id),
            "config_sha256": str(config_sha256).lower(),
            "generation": str(generation),
            "subject": dict(subject) if isinstance(subject, Mapping) else {"id": subject_text},
            "payload": dict(payload),
            "source": "lokay.process_runtime.external_input",
        }
        digest = payload_digest(identity)
        body = dict(identity)
        body["content_digest"] = digest
        body["verified_readback_state"] = "not_applicable" if self.dry_run else "verified"
        path = self.external_input_path(
            process_id=process, input_kind=kind, subject=subject_text, digest=digest
        )
        if self.dry_run:
            return ExternalInputRecord(process, kind, subject_text, digest, path, body, "planned")
        subject_dir = path.parent
        with _file_lock(subject_dir / ".external-input.lock"):
            indexed = self._lookup_external_input(
                process_id=process, input_kind=kind, subject=subject_text
            )
            if indexed is not None:
                if str(indexed["digest"]).lower() != digest:
                    raise ProcessRuntimeError("external input conflict for subject")
                existing_path = Path(str(indexed["path"]))
                existing = self.read_external_input(existing_path)
                if existing != body:
                    raise ProcessRuntimeError("external input conflict for subject")
                self._index_external_input(
                    process_id=process, input_kind=kind, subject=subject_text,
                    digest=digest, path=existing_path, payload=existing, status="exists",
                )
                return ExternalInputRecord(process, kind, subject_text, digest, existing_path, existing, "exists")
            raw = (_canonical_json(body) + "\n").encode("utf-8")
            _ensure_private_dir(subject_dir)
            temp: Path | None = None
            try:
                fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(subject_dir))
                temp = Path(name)
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(str(temp), str(path))
                os.unlink(str(temp))
                temp = None
                _fsync_dir(subject_dir)
                if self.read_external_input(path) != body:
                    raise ProcessRuntimeError("external input readback mismatch")
            except FileExistsError:
                existing = self.read_external_input(path)
                if existing != body:
                    raise ProcessRuntimeError("external input conflict for subject")
                if temp is not None and temp.exists():
                    temp.unlink()
                self._index_external_input(
                    process_id=process, input_kind=kind, subject=subject_text,
                    digest=digest, path=path, payload=existing, status="exists",
                )
                return ExternalInputRecord(process, kind, subject_text, digest, path, existing, "exists")
            except Exception:
                if temp is not None and temp.exists():
                    temp.unlink()
                raise
            self._index_external_input(
                process_id=process, input_kind=kind, subject=subject_text,
                digest=digest, path=path, payload=body, status="written",
            )
        return ExternalInputRecord(process, kind, subject_text, digest, path, body, "written")

    def _lookup_external_input(
        self, *, process_id: str, input_kind: str, subject: str
    ) -> sqlite3.Row | None:
        connection = self._connection()
        try:
            return connection.execute(
                "SELECT process_id,input_kind,subject,digest,path,payload_json,status,created_at "
                "FROM external_inputs WHERE process_id=? AND input_kind=? AND subject=?",
                (process_id, input_kind, subject),
            ).fetchone()
        finally:
            connection.close()

    def read_external_input(self, path: str | Path) -> dict[str, Any]:
        value = self.read_receipt(path)
        if value.get("verified_readback_state") != "verified":
            raise ProcessRuntimeError("external input is not verified")
        return value

    def list_external_inputs(
        self, *, process_id: str, input_kind: str
    ) -> list[dict[str, Any]]:
        process = _process_id(process_id)
        kind = _safe_component(input_kind, field="input_kind")
        if kind not in _EXTERNAL_INPUT_PATH_BY_KIND:
            raise ProcessRuntimeError(f"unknown external input kind: {kind}")
        connection = self._connection()
        try:
            rows = connection.execute(
                "SELECT process_id,input_kind,subject,digest,path,payload_json,status,created_at "
                "FROM external_inputs WHERE process_id=? AND input_kind=? ORDER BY created_at,subject",
                (process, kind),
            ).fetchall()
        finally:
            connection.close()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ProcessRuntimeError("external_input_index_payload_invalid") from exc
            if not isinstance(payload, dict):
                raise ProcessRuntimeError("external_input_index_payload_invalid")
            result.append({
                "process_id": str(row["process_id"]),
                "input_kind": str(row["input_kind"]),
                "subject": str(row["subject"]),
                "digest": str(row["digest"]).lower(),
                "path": str(row["path"]),
                "payload": payload,
                "status": str(row["status"]),
                "created_at": str(row["created_at"]),
            })
        return result

    def read_receipt(self, path: str | Path) -> dict[str, Any]:
        receipt = Path(path)
        if _private_stat(receipt) is None:
            raise ProcessRuntimeError(f"receipt missing: {receipt}")
        try:
            value = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            raise ProcessRuntimeError(f"receipt unreadable: {exc}") from exc
        if not isinstance(value, dict):
            raise ProcessRuntimeError("receipt_not_object")
        return value

    def _index_receipt(
        self,
        *,
        process_id: str,
        receipt_kind: str,
        subject: str,
        digest: str,
        path: Path,
        payload: Mapping[str, Any],
        status: str,
    ) -> None:
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO receipts(
                    process_id, receipt_kind, subject, digest, path,
                    payload_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(process_id, receipt_kind, subject) DO UPDATE SET
                    digest = excluded.digest,
                    path = excluded.path,
                    payload_json = excluded.payload_json,
                    status = excluded.status
                """,
                (
                    process_id,
                    receipt_kind,
                    subject,
                    digest,
                    str(path),
                    _canonical_json(dict(payload)),
                    status,
                    _utc_now(),
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def get_indexed_receipt_record(
        self,
        *,
        process_id: str,
        receipt_kind: str,
        subject: str | Mapping[str, Any],
        digest: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the durable index row, including path and verification metadata."""
        row = self._lookup_subject_receipt(
            process_id=_process_id(process_id),
            receipt_kind=_safe_component(receipt_kind, field="receipt_kind"),
            subject=subject_key(subject),
        )
        if row is None:
            return None
        row_digest = str(row["digest"] or "").lower()
        if digest is not None and row_digest != digest.lower():
            return None
        try:
            indexed_payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProcessRuntimeError("indexed_receipt_payload_invalid") from exc
        if not isinstance(indexed_payload, dict):
            raise ProcessRuntimeError("indexed_receipt_payload_invalid")
        return {
            "process_id": str(row["process_id"]),
            "receipt_kind": str(row["receipt_kind"]),
            "subject": str(row["subject"]),
            "digest": row_digest,
            "path": str(row["path"]),
            "payload": indexed_payload,
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
        }

    def get_indexed_receipt(
        self,
        *,
        process_id: str,
        receipt_kind: str,
        subject: str | Mapping[str, Any],
        digest: str | None = None,
    ) -> dict[str, Any] | None:
        record = self.get_indexed_receipt_record(
            process_id=process_id,
            receipt_kind=receipt_kind,
            subject=subject,
            digest=digest,
        )
        return None if record is None else dict(record["payload"])

    def list_indexed_receipts(
        self,
        *,
        process_id: str | None = None,
        receipt_kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return durable index rows, optionally filtered by process and kind."""
        clauses: list[str] = []
        params: list[str] = []
        if process_id is not None:
            clauses.append("process_id = ?")
            params.append(_process_id(process_id))
        if receipt_kind is not None:
            clauses.append("receipt_kind = ?")
            params.append(_safe_component(receipt_kind, field="receipt_kind"))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        connection = self._connection()
        try:
            rows = connection.execute(
                f"""
                SELECT process_id, receipt_kind, subject, digest, path,
                       payload_json, status, created_at
                FROM receipts
                {where}
                ORDER BY process_id, receipt_kind, subject
                """,
                tuple(params),
            ).fetchall()
        finally:
            connection.close()
        records: list[dict[str, Any]] = []
        for row in rows:
            try:
                indexed_payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ProcessRuntimeError("indexed_receipt_payload_invalid") from exc
            if not isinstance(indexed_payload, dict):
                raise ProcessRuntimeError("indexed_receipt_payload_invalid")
            records.append(
                {
                    "process_id": str(row["process_id"]),
                    "receipt_kind": str(row["receipt_kind"]),
                    "subject": str(row["subject"]),
                    "digest": str(row["digest"] or "").lower(),
                    "path": str(row["path"]),
                    "payload": indexed_payload,
                    "status": str(row["status"]),
                    "created_at": str(row["created_at"]),
                }
            )
        return records



    def advance_cursor(
        self,
        *,
        process_id: str,
        cursor_key: str,
        value: str,
        receipt_digest: str,
        receipt_path: str | Path | None = None,
        require_indexed: bool = True,
    ) -> CursorRecord:
        """Advance a cursor only after the named receipt is verified/indexed."""
        process = _process_id(process_id)
        key = _safe_component(cursor_key, field="cursor_key")
        digest = receipt_digest.lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise CursorError("receipt_digest must be sha256 hex")

        if receipt_path is not None:
            payload = self.read_receipt(receipt_path)
            stamped = str(payload.get("content_digest") or "")
            if stamped != digest:
                raise CursorError("cursor receipt digest mismatch")
            if payload.get("verified_readback_state") != "verified":
                raise CursorError("cursor receipt is not verified")

        if require_indexed:
            connection = self._connection()
            try:
                row = connection.execute(
                    """
                    SELECT status, payload_json FROM receipts
                    WHERE process_id = ? AND digest = ?
                    LIMIT 1
                    """,
                    (process, digest),
                ).fetchone()
            finally:
                connection.close()
            if row is None:
                raise CursorError(
                    f"cursor advance refused: receipt {digest} is not indexed"
                )
            payload = json.loads(row["payload_json"])
            if payload.get("verified_readback_state") != "verified":
                raise CursorError("cursor advance refused: receipt not verified")

        advanced_at = _utc_now()
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO cursors(process_id, cursor_key, value, receipt_digest, advanced_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(process_id, cursor_key) DO UPDATE SET
                    value = excluded.value,
                    receipt_digest = excluded.receipt_digest,
                    advanced_at = excluded.advanced_at
                """,
                (process, key, str(value), digest, advanced_at),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

        cursor_dir = _ensure_private_dir(self.paths.state_root / "cursors" / process)
        cursor_path = cursor_dir / f"{key}.json"
        body = {
            "process_id": process,
            "cursor_key": key,
            "value": str(value),
            "receipt_digest": digest,
            "advanced_at": advanced_at,
        }
        self._write_private_json(cursor_path, body)
        return CursorRecord(
            process_id=process,
            cursor_key=key,
            value=str(value),
            receipt_digest=digest,
            advanced_at=advanced_at,
        )

    def read_cursor(self, process_id: str, cursor_key: str) -> CursorRecord | None:
        process = _process_id(process_id)
        key = _safe_component(cursor_key, field="cursor_key")
        connection = self._connection()
        try:
            row = connection.execute(
                """
                SELECT process_id, cursor_key, value, receipt_digest, advanced_at
                FROM cursors WHERE process_id = ? AND cursor_key = ?
                """,
                (process, key),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return CursorRecord(
            process_id=row["process_id"],
            cursor_key=row["cursor_key"],
            value=row["value"],
            receipt_digest=row["receipt_digest"],
            advanced_at=row["advanced_at"],
        )

    def _lease_paths(self, process_id: str, subject: str) -> tuple[Path, Path]:
        process = _process_id(process_id)
        subject_text = subject_key(subject)
        digest = _sha256_hex(subject_text)
        directory = _ensure_private_dir(self.paths.state_root / "leases" / process)
        return directory / f"{digest}.json", directory / f"{digest}.lock"

    def _read_lease_row(
        self, connection: sqlite3.Connection, process_id: str, subject: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT process_id, subject, owner, owner_pid, acquired_at,
                   expires_at, stale_after, generation
            FROM leases WHERE process_id = ? AND subject = ?
            """,
            (process_id, subject),
        ).fetchone()

    def _lease_is_stale(self, row: Mapping[str, Any], *, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        expires_at = float(row["expires_at"])
        stale_after = float(row["stale_after"])
        owner_pid = int(row["owner_pid"])
        if current < expires_at:
            return False
        if current < stale_after:
            return False
        return not _pid_alive(owner_pid)

    @contextmanager
    def process_lease(
        self,
        *,
        process_id: str,
        subject: str | Mapping[str, Any] = "process",
        lease_seconds: int,
        stale_owner_after_seconds: int,
        generation: str,
        dry_run: bool | None = None,
    ) -> Iterator[LeaseRecord]:
        """Acquire an OS-visible process lease; reclaim only stale owners."""
        is_dry = self.dry_run if dry_run is None else bool(dry_run)
        process = _process_id(process_id)
        subject_text = subject_key(subject)
        if lease_seconds <= 0:
            raise LeaseError("lease_seconds must be positive")
        if stale_owner_after_seconds < 2 * lease_seconds:
            raise LeaseError("stale_owner_after_seconds must be >= 2 * lease_seconds")
        self.check_generation_fence(generation)

        if is_dry:
            now = time.time()
            yield LeaseRecord(
                process_id=process,
                subject=subject_text,
                owner=self.owner,
                owner_pid=self._owner_pid,
                acquired_at=_utc_now(),
                expires_at=now + lease_seconds,
                stale_after=now + stale_owner_after_seconds,
                generation=generation,
                reclaimed=False,
            )
            return

        lease_path, lock_path = self._lease_paths(process, subject_text)
        reclaimed = False
        with _file_lock(lock_path):
            connection = self._connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = self._read_lease_row(connection, process, subject_text)
                now = time.time()
                if row is not None:
                    same_owner = (
                        str(row["owner"]) == self.owner
                        and int(row["owner_pid"]) == self._owner_pid
                    )
                    if not same_owner:
                        if not self._lease_is_stale(row, now=now):
                            raise LeaseError(
                                f"lease held by {row['owner']} pid={row['owner_pid']}"
                            )
                        reclaimed = True
                        connection.execute(
                            "DELETE FROM leases WHERE process_id = ? AND subject = ?",
                            (process, subject_text),
                        )
                acquired_at = _utc_now()
                expires_at = now + lease_seconds
                stale_after = now + stale_owner_after_seconds
                connection.execute(
                    """
                    INSERT INTO leases(
                        process_id, subject, owner, owner_pid, acquired_at,
                        expires_at, stale_after, generation
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(process_id, subject) DO UPDATE SET
                        owner = excluded.owner,
                        owner_pid = excluded.owner_pid,
                        acquired_at = excluded.acquired_at,
                        expires_at = excluded.expires_at,
                        stale_after = excluded.stale_after,
                        generation = excluded.generation
                    """,
                    (
                        process,
                        subject_text,
                        self.owner,
                        self._owner_pid,
                        acquired_at,
                        expires_at,
                        stale_after,
                        generation,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                connection.close()
                raise
            connection.close()

            record = LeaseRecord(
                process_id=process,
                subject=subject_text,
                owner=self.owner,
                owner_pid=self._owner_pid,
                acquired_at=acquired_at,
                expires_at=expires_at,
                stale_after=stale_after,
                generation=generation,
                reclaimed=reclaimed,
            )
            self._write_private_json(
                lease_path,
                {
                    "process_id": record.process_id,
                    "subject": record.subject,
                    "owner": record.owner,
                    "owner_pid": record.owner_pid,
                    "acquired_at": record.acquired_at,
                    "expires_at": record.expires_at,
                    "stale_after": record.stale_after,
                    "generation": record.generation,
                    "reclaimed": record.reclaimed,
                },
            )
            if reclaimed:
                self.write_health(
                    process_id=process,
                    status="stale_reclaimed",
                    owner=self.owner,
                    lease_expires_at=expires_at,
                    last_exit=0,
                    last_error=None,
                    attempt=0,
                    generation=generation,
                    details={"subject": subject_text, "reclaimed": True},
                )

        # Hold the OS lock only for acquire mutation; release_lease re-locks.
        try:
            yield record
        finally:
            self.release_lease(process_id=process, subject=subject_text)

    def release_lease(
        self,
        *,
        process_id: str,
        subject: str | Mapping[str, Any] = "process",
    ) -> None:
        process = _process_id(process_id)
        subject_text = subject_key(subject)
        lease_path, lock_path = self._lease_paths(process, subject_text)
        with _file_lock(lock_path):
            connection = self._connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = self._read_lease_row(connection, process, subject_text)
                if row is not None and (
                    str(row["owner"]) == self.owner
                    and int(row["owner_pid"]) == self._owner_pid
                ):
                    connection.execute(
                        "DELETE FROM leases WHERE process_id = ? AND subject = ?",
                        (process, subject_text),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
            if lease_path.exists():
                try:
                    payload = json.loads(lease_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    payload = None
                if isinstance(payload, dict) and payload.get("owner") == self.owner:
                    try:
                        lease_path.unlink()
                        _fsync_dir(lease_path.parent)
                    except OSError:
                        pass

    def renew_lease(
        self,
        *,
        process_id: str,
        subject: str | Mapping[str, Any] = "process",
        lease_seconds: int,
        stale_owner_after_seconds: int,
        generation: str,
    ) -> LeaseRecord:
        process = _process_id(process_id)
        subject_text = subject_key(subject)
        self.check_generation_fence(generation)
        lease_path, lock_path = self._lease_paths(process, subject_text)
        with _file_lock(lock_path):
            connection = self._connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = self._read_lease_row(connection, process, subject_text)
                if row is None:
                    raise LeaseError("lease missing")
                if str(row["owner"]) != self.owner or int(row["owner_pid"]) != self._owner_pid:
                    raise LeaseError("lease owned by another process")
                now = time.time()
                expires_at = now + lease_seconds
                stale_after = now + stale_owner_after_seconds
                connection.execute(
                    """
                    UPDATE leases
                    SET expires_at = ?, stale_after = ?, generation = ?
                    WHERE process_id = ? AND subject = ?
                    """,
                    (expires_at, stale_after, generation, process, subject_text),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                connection.close()
                raise
            connection.close()
            record = LeaseRecord(
                process_id=process,
                subject=subject_text,
                owner=self.owner,
                owner_pid=self._owner_pid,
                acquired_at=str(row["acquired_at"]),
                expires_at=expires_at,
                stale_after=stale_after,
                generation=generation,
                reclaimed=False,
            )
            self._write_private_json(
                lease_path,
                {
                    "process_id": record.process_id,
                    "subject": record.subject,
                    "owner": record.owner,
                    "owner_pid": record.owner_pid,
                    "acquired_at": record.acquired_at,
                    "expires_at": record.expires_at,
                    "stale_after": record.stale_after,
                    "generation": record.generation,
                    "reclaimed": False,
                },
            )
            return record

    def read_lease(
        self,
        *,
        process_id: str,
        subject: str | Mapping[str, Any] = "process",
    ) -> LeaseRecord | None:
        process = _process_id(process_id)
        subject_text = subject_key(subject)
        connection = self._connection()
        try:
            row = self._read_lease_row(connection, process, subject_text)
        finally:
            connection.close()
        if row is None:
            return None
        return LeaseRecord(
            process_id=row["process_id"],
            subject=row["subject"],
            owner=row["owner"],
            owner_pid=int(row["owner_pid"]),
            acquired_at=row["acquired_at"],
            expires_at=float(row["expires_at"]),
            stale_after=float(row["stale_after"]),
            generation=row["generation"],
            reclaimed=False,
        )

    @contextmanager
    def subject_lock(
        self,
        *,
        lock_scope: str,
        subject: str | Mapping[str, Any],
    ) -> Iterator[Path]:
        """OS-visible exclusive subject lock under locks/<scope>/<hash>.lock."""
        scope = _safe_component(lock_scope.replace("/", "__"), field="lock_scope")
        subject_text = subject_key(subject)
        digest = _sha256_hex(f"{lock_scope}|{subject_text}")
        directory = _ensure_private_dir(self.paths.state_root / "locks" / scope)
        path = directory / f"{digest}.lock"
        with _file_lock(path):
            yield path

    def decide_retry(
        self,
        *,
        process_id: str,
        subject: str | Mapping[str, Any],
        failure_class: str,
        attempt: int | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: Sequence[int] = DEFAULT_BACKOFF_SECONDS,
        retry_classes: Sequence[str] = ("retryable_read", "reconcile_then_retry"),
        now: float | None = None,
        dry_run: bool | None = None,
    ) -> RetryDecision:
        """Bound retry/backoff decision and optionally persist retry state."""
        process = _process_id(process_id)
        subject_text = subject_key(subject)
        is_dry = self.dry_run if dry_run is None else bool(dry_run)
        if max_attempts <= 0:
            raise ProcessRuntimeError("max_attempts must be positive")
        schedule = [int(item) for item in backoff_seconds]
        if not schedule or any(item < 0 for item in schedule):
            raise ProcessRuntimeError("backoff_seconds must be non-empty non-negative")

        current_attempt = 1 if attempt is None else int(attempt)
        if current_attempt <= 0:
            raise ProcessRuntimeError("attempt must be positive")

        allowed = set(retry_classes)
        retryable = failure_class in allowed
        exhausted = current_attempt >= max_attempts
        should_retry = retryable and not exhausted
        if should_retry:
            delay = schedule[min(current_attempt - 1, len(schedule) - 1)]
            next_at = (time.time() if now is None else now) + delay
        else:
            delay = 0
            next_at = None

        decision = RetryDecision(
            process_id=process,
            subject=subject_text,
            attempt=current_attempt,
            max_attempts=max_attempts,
            failure_class=failure_class,
            should_retry=should_retry,
            backoff_seconds=delay,
            next_attempt_at=next_at,
            exhausted=exhausted or not retryable,
        )
        if is_dry:
            return decision

        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO retries(
                    process_id, subject, attempt, max_attempts,
                    failure_class, next_attempt_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(process_id, subject) DO UPDATE SET
                    attempt = excluded.attempt,
                    max_attempts = excluded.max_attempts,
                    failure_class = excluded.failure_class,
                    next_attempt_at = excluded.next_attempt_at,
                    updated_at = excluded.updated_at
                """,
                (
                    process,
                    subject_text,
                    decision.attempt,
                    decision.max_attempts,
                    decision.failure_class,
                    decision.next_attempt_at,
                    _utc_now(),
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

        retry_dir = _ensure_private_dir(self.paths.state_root / "retries" / process)
        digest = _sha256_hex(subject_text)
        self._write_private_json(
            retry_dir / f"{digest}.json",
            {
                "process_id": decision.process_id,
                "subject": decision.subject,
                "attempt": decision.attempt,
                "max_attempts": decision.max_attempts,
                "failure_class": decision.failure_class,
                "should_retry": decision.should_retry,
                "backoff_seconds": decision.backoff_seconds,
                "next_attempt_at": decision.next_attempt_at,
                "exhausted": decision.exhausted,
            },
        )
        return decision

    def write_health(
        self,
        *,
        process_id: str,
        status: str,
        owner: str | None = None,
        lease_expires_at: float | None = None,
        last_exit: int | None = None,
        last_error: str | None = None,
        attempt: int = 0,
        generation: str | None = None,
        details: Mapping[str, Any] | None = None,
        dry_run: bool | None = None,
    ) -> HealthRecord:
        process = _process_id(process_id)
        is_dry = self.dry_run if dry_run is None else bool(dry_run)
        record = HealthRecord(
            process_id=process,
            status=str(status),
            owner=owner,
            lease_expires_at=lease_expires_at,
            last_exit=last_exit,
            last_error=last_error,
            attempt=int(attempt),
            generation=generation,
            updated_at=_utc_now(),
            details=dict(details or {}),
        )
        if is_dry:
            return record

        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO health(
                    process_id, status, owner, lease_expires_at, last_exit,
                    last_error, attempt, generation, updated_at, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(process_id) DO UPDATE SET
                    status = excluded.status,
                    owner = excluded.owner,
                    lease_expires_at = excluded.lease_expires_at,
                    last_exit = excluded.last_exit,
                    last_error = excluded.last_error,
                    attempt = excluded.attempt,
                    generation = excluded.generation,
                    updated_at = excluded.updated_at,
                    details_json = excluded.details_json
                """,
                (
                    record.process_id,
                    record.status,
                    record.owner,
                    record.lease_expires_at,
                    record.last_exit,
                    record.last_error,
                    record.attempt,
                    record.generation,
                    record.updated_at,
                    _canonical_json(record.details),
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

        health_path = _ensure_private_dir(self.paths.state_root / "health") / f"{process}.json"
        self._write_private_json(
            health_path,
            {
                "process_id": record.process_id,
                "status": record.status,
                "owner": record.owner,
                "lease_expires_at": record.lease_expires_at,
                "last_exit": record.last_exit,
                "last_error": record.last_error,
                "attempt": record.attempt,
                "generation": record.generation,
                "updated_at": record.updated_at,
                "details": record.details,
            },
        )
        return record

    def read_health(self, process_id: str) -> HealthRecord | None:
        process = _process_id(process_id)
        connection = self._connection()
        try:
            row = connection.execute(
                """
                SELECT process_id, status, owner, lease_expires_at, last_exit,
                       last_error, attempt, generation, updated_at, details_json
                FROM health WHERE process_id = ?
                """,
                (process,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return HealthRecord(
            process_id=row["process_id"],
            status=row["status"],
            owner=row["owner"],
            lease_expires_at=row["lease_expires_at"],
            last_exit=row["last_exit"],
            last_error=row["last_error"],
            attempt=int(row["attempt"]),
            generation=row["generation"],
            updated_at=row["updated_at"],
            details=json.loads(row["details_json"]),
        )

    def run_fenced(
        self,
        *,
        process_id: str,
        enabled: bool,
        generation: str,
        subject: str | Mapping[str, Any] = "process",
        lease_seconds: int = 120,
        stale_owner_after_seconds: int = 240,
        lease_renew_seconds: int | None = None,
        lock_scope: str | None = None,
        candidate_id: str = "",
        config_sha256: str = "",
        callback: Callable[[LeaseRecord], Any],
        dry_run: bool | None = None,
    ) -> Any:
        """Run callback only when process is enabled and generation fence holds.

        Lease/lock/fence failures raise before callback execution. During non-dry
        execution a concurrent heartbeat renews the process lease at the configured
        interval; renewal/fence failures fail closed and discard the callback result.
        """
        is_dry = self.dry_run if dry_run is None else bool(dry_run)
        process = _process_id(process_id)
        if not enabled:
            self.write_health(
                process_id=process,
                status="disabled",
                owner=self.owner,
                last_exit=0,
                generation=generation,
                details={"reason": "process_disabled"},
                dry_run=is_dry,
            )
            raise ProcessDisabledError(f"process disabled: {process}")

        renew_interval: float | None = None
        if lease_renew_seconds is not None:
            renew_interval = float(lease_renew_seconds)
            if renew_interval <= 0:
                raise LeaseError("lease_renew_seconds must be positive")

        self.check_generation_fence(
            generation,
            config_sha256=config_sha256 or None,
            candidate_id=candidate_id or None,
        )

        def _check_fence() -> None:
            self.check_generation_fence(
                generation,
                config_sha256=config_sha256 or None,
                candidate_id=candidate_id or None,
            )

        def _invoke(lease: LeaseRecord) -> Any:
            # Re-check fence immediately before callback.
            _check_fence()
            return callback(lease)

        with self.process_lease(
            process_id=process,
            subject=subject,
            lease_seconds=lease_seconds,
            stale_owner_after_seconds=stale_owner_after_seconds,
            generation=generation,
            dry_run=is_dry,
        ) as lease:
            stop_heartbeat = threading.Event()
            heartbeat_error: list[BaseException] = []
            heartbeat_thread: threading.Thread | None = None

            def _heartbeat() -> None:
                while not stop_heartbeat.wait(timeout=renew_interval):
                    try:
                        _check_fence()
                        self.renew_lease(
                            process_id=process,
                            subject=subject,
                            lease_seconds=lease_seconds,
                            stale_owner_after_seconds=stale_owner_after_seconds,
                            generation=generation,
                        )
                    except BaseException as exc:  # fail closed; surface after stop
                        heartbeat_error.append(exc)
                        return

            def _stop_heartbeat() -> None:
                if heartbeat_thread is None:
                    return
                stop_heartbeat.set()
                heartbeat_thread.join()

            callback_error: BaseException | None = None
            result: Any = None
            try:
                if not is_dry and renew_interval is not None:
                    heartbeat_thread = threading.Thread(
                        target=_heartbeat,
                        name=f"lokay-lease-heartbeat-{process}",
                        daemon=True,
                    )
                    heartbeat_thread.start()

                try:
                    if lock_scope and not is_dry:
                        with self.subject_lock(lock_scope=lock_scope, subject=subject):
                            result = _invoke(lease)
                    else:
                        result = _invoke(lease)
                    # Re-check fence after callback before accepting the result.
                    _check_fence()
                except BaseException as exc:
                    callback_error = exc
            finally:
                # Always stop/join before process_lease releases the lease.
                _stop_heartbeat()

            if heartbeat_error:
                raise heartbeat_error[0]
            if callback_error is not None:
                raise callback_error

            self.write_health(
                process_id=process,
                status="ok" if not is_dry else "planned",
                owner=self.owner,
                lease_expires_at=lease.expires_at,
                last_exit=0,
                generation=generation,
                details={"subject": lease.subject},
                dry_run=is_dry,
            )
            return result

    def _write_private_json(self, path: Path, payload: Mapping[str, Any]) -> None:
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
            _private_stat(path)
        except Exception:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass
            raise


@contextmanager
def _file_lock(path: Path) -> Iterator[Path]:
    _ensure_private_dir(path.parent)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.fchmod(fd, 0o600)
        yield path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


__all__ = [
    "CursorError",
    "CursorRecord",
    "DEFAULT_BACKOFF_SECONDS",
    "DEFAULT_GENERATION_PATH",
    "DEFAULT_MAX_ATTEMPTS",
    "ExternalInputRecord",
    "FenceError",
    "HealthRecord",
    "LeaseError",
    "LeaseRecord",
    "ProcessDisabledError",
    "ProcessRuntime",
    "ProcessRuntimeError",
    "ReceiptConflictError",
    "ReceiptRecord",
    "RetryDecision",
    "RuntimePaths",
    "SCHEMA_VERSION",
    "initialize_schema",
    "payload_digest",
    "subject_key",
]
