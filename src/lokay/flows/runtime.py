"""Thin Fala package host and durable journal facade."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
import tomllib
from importlib import metadata

import threading
from collections.abc import Collection, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fala.host import host_run_package

_FALA_VERSION = "0.7.15"
_FALA_SCHEMA_VERSION = 6

_HOST_RUN_LOCK = threading.Lock()

_PROCESS_COLUMNS = (
    "id",
    "status",
    "attempt",
    "max_attempts",
    "output_json",
    "error_json",
    "metadata",
)
_RUN_COLUMNS = (
    "id",
    "status",
    "package_id",
    "package_version",
    "package_digest",
    "correlation_path_id",
    "correlation_path_digest",
    "runtime_version",
    "backend_version",
    "schema_version",
    "metadata",
)
_RUN_STATUSES = {
    "created",
    "active",
    "waiting",
    "completed",
    "failed",
    "cancel_requested",
    "cancelled",
    "timed_out",
}
_PROCESS_STATUSES = {
    "pending",
    "ready",
    "running",
    "waiting",
    "retry_wait",
    "succeeded",
    "failed",
    "cancel_requested",
    "cancelled",
    "timed_out",
}
_TERMINAL_FAILURES = {"failed", "cancelled", "timed_out"}
_SECRET_KEY = re.compile(r"token|password|secret|api[_-]?key|authorization", re.IGNORECASE)
_AUTH_VALUE = re.compile(r"(?i)(authorization\s*[:=]\s*)[^\r\n,;]+")
_SECRET_VALUE = re.compile(
    r"(?i)((?:token|password|secret|api[_-]?key)\s*[:=]\s*)[^\s,;]+"
)


class RuntimeFacadeError(RuntimeError):
    """The Fala host or durable journal violated its public contract."""


@dataclass(frozen=True)
class JournalProcess:
    id: str
    status: str
    attempt: int
    max_attempts: int
    output: dict[str, Any]
    error: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    correlation_path_id: str = ""
    effector_id: str = ""

    @property
    def step_id(self) -> str:
        if self.effector_id:
            return self.effector_id
        return self.id.rsplit(":", 1)[-1]


@dataclass(frozen=True)
class JournalRun:
    id: str
    status: str
    package_id: str
    package_version: str
    package_digest: str
    correlation_path_id: str
    correlation_path_digest: str
    runtime_version: str
    backend_version: str
    schema_version: int
    metadata: dict[str, Any]


@dataclass(frozen=True)
class HostPathRunResult:
    run_id: str
    path_id: str
    run_status: str
    replayed: bool
    ticks: int
    processes: tuple[JournalProcess, ...]
    package_id: str = ""
    package_version: str = ""
    package_digest: str = ""
    correlation_path_digest: str = ""
    runtime_version: str = ""
    backend_version: str = ""
    schema_version: int = 0
    run_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def completed(self) -> tuple[JournalProcess, ...]:
        return tuple(process for process in self.processes if process.status == "succeeded")

    @property
    def failed(self) -> tuple[JournalProcess, ...]:
        return tuple(process for process in self.processes if process.status in _TERMINAL_FAILURES)

    @property
    def waiting(self) -> tuple[JournalProcess, ...]:
        return tuple(
            process
            for process in self.processes
            if process.status not in _TERMINAL_FAILURES | {"succeeded"}
        )


def _redact(value: Any, *, key: str = "") -> Any:
    if _SECRET_KEY.search(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item, key=key) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub(r"\1<redacted>", _AUTH_VALUE.sub(r"\1<redacted>", value))[:2000]
    return value


def _require_nonempty_str(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeFacadeError(f"{label} must be a non-empty string")
    return value


def _json_object(raw: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise RuntimeFacadeError(f"{label} is not text")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeFacadeError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeFacadeError(f"{label} must decode to an object")
    return _redact(value)


def _json_object_for_process(raw: Any, *, process_id: str, column: str) -> dict[str, Any]:
    return _json_object(raw, label=f"journal {column} for {process_id!r}")


def _process_id_parts(process_id: str, *, run_id: str, path_id: str) -> str:
    prefix = f"{run_id}:{path_id}:"
    if not process_id.startswith(prefix):
        raise RuntimeFacadeError(
            f"journal process {process_id!r} must match run_id:path_id:effector_id"
        )
    effector_id = process_id[len(prefix) :]
    if not effector_id or ":" in effector_id:
        raise RuntimeFacadeError(
            f"journal process {process_id!r} must match run_id:path_id:effector_id"
        )
    return effector_id


def read_journal_run(db_path: str | Path, run_id: str) -> JournalRun:
    """Read exact durable run identity, failing closed on missing or malformed rows."""
    expected_run_id = _require_nonempty_str(run_id, label="run_id")
    try:
        with closing(sqlite3.connect(Path(db_path).expanduser().resolve())) as connection:
            cursor = connection.execute(
                "SELECT id,status,package_id,package_version,package_digest,"
                "correlation_path_id,correlation_path_digest,runtime_version,"
                "backend_version,schema_version,metadata "
                "FROM runs WHERE id=?",
                (expected_run_id,),
            )
            columns = tuple(item[0] for item in cursor.description or ())
            if columns != _RUN_COLUMNS:
                raise RuntimeFacadeError(f"unexpected run query columns: {columns!r}")
            row = cursor.fetchone()
    except RuntimeFacadeError:
        raise
    except sqlite3.Error as exc:
        raise RuntimeFacadeError(f"unable to read Fala run journal: {_redact(str(exc))}") from exc

    if row is None:
        raise RuntimeFacadeError(f"durable Fala run {expected_run_id!r} is missing")
    if not isinstance(row, Sequence) or len(row) != len(_RUN_COLUMNS):
        raise RuntimeFacadeError("journal run row has unexpected shape")

    (
        observed_id,
        status,
        package_id,
        package_version,
        package_digest,
        correlation_path_id,
        correlation_path_digest,
        runtime_version,
        backend_version,
        schema_version,
        metadata_raw,
    ) = row
    if observed_id != expected_run_id:
        raise RuntimeFacadeError("durable Fala run id disagrees with requested run_id")
    if not isinstance(status, str) or status not in _RUN_STATUSES:
        raise RuntimeFacadeError(f"durable Fala run {expected_run_id!r} has invalid status")
    package_id = _require_nonempty_str(package_id, label="runs.package_id")
    package_version = _require_nonempty_str(package_version, label="runs.package_version")
    package_digest = _require_nonempty_str(package_digest, label="runs.package_digest")
    correlation_path_id = _require_nonempty_str(
        correlation_path_id, label="runs.correlation_path_id"
    )
    correlation_path_digest = _require_nonempty_str(
        correlation_path_digest, label="runs.correlation_path_digest"
    )
    runtime_version = _require_nonempty_str(runtime_version, label="runs.runtime_version")
    backend_version = _require_nonempty_str(backend_version, label="runs.backend_version")
    if type(schema_version) is not int or schema_version < 1:
        raise RuntimeFacadeError(f"durable Fala run {expected_run_id!r} has invalid schema_version")
    metadata = _json_object(metadata_raw, label=f"runs.metadata for {expected_run_id!r}")
    return JournalRun(
        id=observed_id,
        status=status,
        package_id=package_id,
        package_version=package_version,
        package_digest=package_digest,
        correlation_path_id=correlation_path_id,
        correlation_path_digest=correlation_path_digest,
        runtime_version=runtime_version,
        backend_version=backend_version,
        schema_version=schema_version,
        metadata=metadata,
    )


def read_journal_processes(
    db_path: str | Path,
    run_id: str,
    *,
    expected_path_id: str | None = None,
    allowed_effectors: Collection[str] | None = None,
) -> tuple[JournalProcess, ...]:
    """Read exact process evidence for one run, failing closed on schema drift."""
    expected_run_id = _require_nonempty_str(run_id, label="run_id")
    durable_run = read_journal_run(db_path, expected_run_id)
    path_id = expected_path_id if expected_path_id is not None else durable_run.correlation_path_id
    path_id = _require_nonempty_str(path_id, label="expected_path_id")
    if path_id != durable_run.correlation_path_id:
        raise RuntimeFacadeError(
            "requested path_id disagrees with durable runs.correlation_path_id"
        )
    allowed: frozenset[str] | None
    if allowed_effectors is None:
        allowed = None
    else:
        if any(not isinstance(item, str) or not item for item in allowed_effectors):
            raise RuntimeFacadeError("allowed_effectors must contain non-empty strings")
        allowed = frozenset(allowed_effectors)

    try:
        with closing(sqlite3.connect(Path(db_path).expanduser().resolve())) as connection:
            cursor = connection.execute(
                "SELECT id,status,attempt,max_attempts,output_json,error_json,metadata "
                "FROM processes WHERE run_id=? ORDER BY id",
                (expected_run_id,),
            )
            columns = tuple(item[0] for item in cursor.description or ())
            if columns != _PROCESS_COLUMNS:
                raise RuntimeFacadeError(f"unexpected process query columns: {columns!r}")
            rows = cursor.fetchall()
    except RuntimeFacadeError:
        raise
    except sqlite3.Error as exc:
        raise RuntimeFacadeError(f"unable to read Fala process journal: {_redact(str(exc))}") from exc

    processes: list[JournalProcess] = []
    for row in rows:
        if not isinstance(row, Sequence) or len(row) != len(_PROCESS_COLUMNS):
            raise RuntimeFacadeError("journal process row has unexpected shape")
        process_id, status, attempt, max_attempts, output_json, error_json, metadata_raw = row
        process_id = _require_nonempty_str(process_id, label="journal process id")
        if not isinstance(status, str) or status not in _PROCESS_STATUSES:
            raise RuntimeFacadeError(f"journal process {process_id!r} has invalid status")
        if type(attempt) is not int or attempt < 0:
            raise RuntimeFacadeError(f"journal process {process_id!r} has invalid attempt")
        if type(max_attempts) is not int or max_attempts < 1:
            raise RuntimeFacadeError(f"journal process {process_id!r} has invalid max_attempts")

        metadata = _json_object_for_process(metadata_raw, process_id=process_id, column="metadata")
        correlation_path_id = metadata.get("correlation_path_id")
        effector_id = metadata.get("effector_id")
        if not isinstance(correlation_path_id, str) or not correlation_path_id:
            raise RuntimeFacadeError(
                f"journal process {process_id!r} metadata.correlation_path_id is required"
            )
        if not isinstance(effector_id, str) or not effector_id:
            raise RuntimeFacadeError(
                f"journal process {process_id!r} metadata.effector_id is required"
            )
        if correlation_path_id != path_id:
            raise RuntimeFacadeError(
                f"journal process {process_id!r} has foreign correlation_path_id"
            )
        id_effector = _process_id_parts(process_id, run_id=expected_run_id, path_id=path_id)
        if effector_id != id_effector:
            raise RuntimeFacadeError(
                f"journal process {process_id!r} metadata.effector_id disagrees with process id"
            )
        if allowed is not None and effector_id not in allowed:
            raise RuntimeFacadeError(
                f"journal process {process_id!r} effector is not in the allowed set"
            )

        processes.append(
            JournalProcess(
                id=process_id,
                status=status,
                attempt=attempt,
                max_attempts=max_attempts,
                output=_json_object_for_process(
                    output_json, process_id=process_id, column="output_json"
                ),
                error=_json_object_for_process(
                    error_json, process_id=process_id, column="error_json"
                ),
                metadata=metadata,
                correlation_path_id=correlation_path_id,
                effector_id=effector_id,
            )
        )
    return tuple(processes)


def _host_identity_str(raw: Mapping[str, Any], key: str) -> str:
    return _require_nonempty_str(raw.get(key), label=f"Fala host {key}")


def _require_identity_match(
    *,
    durable_value: str,
    expected_value: str,
    label: str,
) -> None:
    if durable_value != expected_value:
        raise RuntimeFacadeError(f"durable {label} disagrees with host identity")


def _require_run_metadata_match(
    durable_metadata: Mapping[str, Any],
    expected_metadata: Mapping[str, Any],
) -> None:
    for key, expected in expected_metadata.items():
        if durable_metadata.get(key) != expected:
            raise RuntimeFacadeError(
                f"durable run metadata {key!r} disagrees with requested run_metadata"
            )


def _normalize_host_result(
    raw: Any,
    *,
    db_path: str | Path,
    path_id: str,
    expected_run_id: str,
    allowed_effectors: Collection[str] | None = None,
    expected_run_metadata: Mapping[str, Any] | None = None,
) -> HostPathRunResult:
    if not isinstance(raw, Mapping):
        raise RuntimeFacadeError("Fala host result must be an object")
    if raw.get("ok") is not True:
        raise RuntimeFacadeError("Fala host did not report success")
    run_id = raw.get("run_id")
    run_status = raw.get("run_status")
    replayed = raw.get("replayed")
    ticks = raw.get("ticks")
    summaries = raw.get("processes")
    if run_id != expected_run_id:
        raise RuntimeFacadeError("Fala host returned an unexpected run_id")
    if not isinstance(run_status, str) or run_status not in _RUN_STATUSES:
        raise RuntimeFacadeError("Fala host returned an invalid run_status")
    if type(replayed) is not bool:
        raise RuntimeFacadeError("Fala host returned an invalid replayed flag")
    if type(ticks) is not int or ticks < 0:
        raise RuntimeFacadeError("Fala host returned an invalid tick count")
    if not isinstance(summaries, list):
        raise RuntimeFacadeError("Fala host returned invalid process summaries")
    requested_path_id = _require_nonempty_str(path_id, label="path_id")
    expected_package_id = _host_identity_str(raw, "package_id")
    expected_package_version = _host_identity_str(raw, "package_version")
    expected_package_digest = _host_identity_str(raw, "package_digest")
    expected_correlation_path_id = _host_identity_str(raw, "correlation_path_id")
    expected_correlation_path_digest = _host_identity_str(raw, "correlation_path_digest")
    expected_runtime_version = _host_identity_str(raw, "runtime_version")
    expected_backend_version = _host_identity_str(raw, "backend_version")
    if "schema_version" in raw:
        expected_schema_version = raw.get("schema_version")
        if type(expected_schema_version) is not int or expected_schema_version < 1:
            raise RuntimeFacadeError("Fala host returned an invalid schema_version")
    else:
        expected_schema_version = _FALA_SCHEMA_VERSION
    if expected_correlation_path_id != requested_path_id:
        raise RuntimeFacadeError(
            "Fala host correlation_path_id disagrees with requested path_id"
        )
    if allowed_effectors is None:
        # Production normalization requires an explicit ownership set. Callers that
        # only need diagnostic readback should use read_journal_processes directly.
        raise RuntimeFacadeError("allowed effectors are required for durable host normalization")

    durable_run = read_journal_run(db_path, expected_run_id)
    if durable_run.status != run_status:
        raise RuntimeFacadeError("Fala host run_status disagrees with the durable journal")
    if durable_run.correlation_path_id != requested_path_id:
        raise RuntimeFacadeError(
            "requested path_id disagrees with durable runs.correlation_path_id"
        )
    _require_identity_match(
        durable_value=durable_run.package_id,
        expected_value=expected_package_id,
        label="package_id",
    )
    _require_identity_match(
        durable_value=durable_run.package_version,
        expected_value=expected_package_version,
        label="package_version",
    )
    _require_identity_match(
        durable_value=durable_run.package_digest,
        expected_value=expected_package_digest,
        label="package_digest",
    )
    _require_identity_match(
        durable_value=durable_run.correlation_path_id,
        expected_value=expected_correlation_path_id,
        label="correlation_path_id",
    )
    _require_identity_match(
        durable_value=durable_run.correlation_path_digest,
        expected_value=expected_correlation_path_digest,
        label="correlation_path_digest",
    )
    _require_identity_match(
        durable_value=durable_run.runtime_version,
        expected_value=expected_runtime_version,
        label="runtime_version",
    )
    _require_identity_match(
        durable_value=durable_run.backend_version,
        expected_value=expected_backend_version,
        label="backend_version",
    )
    if durable_run.schema_version != expected_schema_version:
        raise RuntimeFacadeError("durable schema_version disagrees with host identity")
    if expected_run_metadata is not None and not replayed:
        # Replay returns an immutable prior invocation; only non-replay writes are
        # expected to match the caller's requested metadata.
        _require_run_metadata_match(durable_run.metadata, expected_run_metadata)

    host_processes: dict[str, str] = {}
    for item in summaries:
        if not isinstance(item, Mapping):
            raise RuntimeFacadeError("Fala host process summary must be an object")
        process_id = item.get("id")
        status = item.get("status")
        if not isinstance(process_id, str) or not process_id or process_id in host_processes:
            raise RuntimeFacadeError("Fala host returned an invalid process id")
        if not isinstance(status, str) or status not in _PROCESS_STATUSES:
            raise RuntimeFacadeError(f"Fala host process {process_id!r} has invalid status")
        host_processes[process_id] = status

    # Durable SQLite identity is authoritative. Host summaries are only a
    # fail-closed cross-check and never supply path/effector evidence.
    processes = read_journal_processes(
        db_path,
        expected_run_id,
        expected_path_id=durable_run.correlation_path_id,
        allowed_effectors=allowed_effectors,
    )
    journal_processes = {process.id: process.status for process in processes}
    if host_processes != journal_processes:
        raise RuntimeFacadeError("Fala host process summaries disagree with the durable journal")
    return HostPathRunResult(
        run_id=durable_run.id,
        path_id=durable_run.correlation_path_id,
        run_status=durable_run.status,
        replayed=replayed,
        ticks=ticks,
        processes=processes,
        package_id=durable_run.package_id,
        package_version=durable_run.package_version,
        package_digest=durable_run.package_digest,
        correlation_path_digest=durable_run.correlation_path_digest,
        runtime_version=durable_run.runtime_version,
        backend_version=durable_run.backend_version,
        schema_version=durable_run.schema_version,
        run_metadata=durable_run.metadata,
    )



def _write_run_metadata(
    db_path: str | Path,
    run_id: str,
    metadata: Mapping[str, Any],
    *,
    replayed: bool,
) -> None:
    try:
        requested = dict(metadata)
        with sqlite3.connect(Path(db_path).expanduser().resolve()) as connection:
            row = connection.execute("SELECT metadata FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise RuntimeFacadeError("Fala run metadata target is missing")
            existing = json.loads(row[0] or "{}")
            if not isinstance(existing, dict):
                raise RuntimeFacadeError("Fala run metadata must decode to an object")
            if replayed:
                # A replay returns the durable run; caller metadata may come from a
                # different invocation (for example dry-run versus live). Never
                # validate or rewrite immutable run metadata on that path.
                return
            existing.update(requested)
            encoded = json.dumps(existing, sort_keys=True, separators=(",", ":"))
            connection.execute("UPDATE runs SET metadata=? WHERE id=?", (encoded, run_id))
    except RuntimeFacadeError:
        raise
    except (json.JSONDecodeError, sqlite3.Error, TypeError, ValueError) as exc:
        raise RuntimeFacadeError(f"unable to persist Fala run metadata: {_redact(str(exc))}") from exc


def _host_python_overrides(package_path: str | Path) -> dict[str, tuple[str, ...]]:
    """Run lokay Python effectors with the interpreter hosting Fala."""
    path = Path(package_path)
    if not path.is_file():
        return {}
    try:
        package = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    paths = package.get("correlation_paths")
    if not isinstance(paths, list):
        return {}
    overrides: dict[str, tuple[str, ...]] = {}
    for path_spec in paths:
        if not isinstance(path_spec, dict):
            continue
        effectors = path_spec.get("effectors")
        if not isinstance(effectors, list):
            continue
        for effector in effectors:
            if not isinstance(effector, dict) or not isinstance(effector.get("id"), str):
                continue
            adapter = effector.get("adapter")
            command = adapter.get("command") if isinstance(adapter, dict) else None
            if (
                isinstance(command, list)
                and command[:3] == ["python3", "-m", "lokay.effector"]
                and all(isinstance(part, str) for part in command)
            ):
                overrides[effector["id"]] = (sys.executable, *command[1:])
    return overrides


def _verify_fala_runtime() -> None:
    module = sys.modules.get(host_run_package.__module__)
    loaded_path = Path(getattr(module, "__file__", "") or "").resolve()
    try:
        observed = metadata.version("fala")
    except metadata.PackageNotFoundError as exc:
        raise RuntimeFacadeError(
            f"Fala runtime metadata is unavailable; loaded host: {loaded_path}"
        ) from exc
    if observed != _FALA_VERSION:
        raise RuntimeFacadeError(
            f"Fala runtime version mismatch: expected {_FALA_VERSION}, observed {observed}; loaded host: {loaded_path}"
        )


def run_package_path(
    *,
    db_path: str | Path,
    package_path: str | Path,
    path_id: str,
    run_id: str,
    inputs: Mapping[str, Any] | None = None,
    effector_inputs: Mapping[str, Mapping[str, Any]] | None = None,
    effector_configs: Mapping[str, Mapping[str, Any] | str] | None = None,
    command_overrides: Mapping[str, Sequence[str]] | None = None,
    run_metadata: Mapping[str, Any] | None = None,
    allowed_effectors: Collection[str] | None = None,
    max_ticks: int = 32,
    worker_id: str = "lokay",
) -> HostPathRunResult:
    """Run one package path and normalize evidence from its SQLite journal."""
    _verify_fala_runtime()
    # Fala's in-process Mojo bridge temporarily changes the process-wide cwd.
    # Serialize host calls so concurrent async tick callers cannot race it.
    resolved_overrides = _host_python_overrides(package_path)
    if command_overrides:
        resolved_overrides.update(command_overrides)
    with _HOST_RUN_LOCK:
        raw = host_run_package(
            db_path=db_path,
            package_path=package_path,
            path_id=path_id,
            run_id=run_id,
            inputs=inputs,
            effector_inputs=effector_inputs,
            effector_configs=effector_configs,
            command_overrides=resolved_overrides or None,
            max_ticks=max_ticks,
            worker_id=worker_id,
        )
        if run_metadata is not None:
            _write_run_metadata(db_path, run_id, run_metadata, replayed=bool(raw.get("replayed")))
    return _normalize_host_result(
        raw,
        db_path=db_path,
        path_id=path_id,
        expected_run_id=run_id,
        allowed_effectors=allowed_effectors,
        expected_run_metadata=run_metadata,
    )


async def run_package_path_async(**kwargs: Any) -> HostPathRunResult:
    """Run the blocking Mojo host without blocking an async tick caller."""
    return await asyncio.to_thread(run_package_path, **kwargs)
