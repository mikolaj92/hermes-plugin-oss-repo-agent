"""Thin Fala package host and durable journal facade."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
import tomllib

import threading
from contextlib import closing
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fala.host import host_run_package
_HOST_RUN_LOCK = threading.Lock()

_PROCESS_COLUMNS = (
    "id",
    "status",
    "attempt",
    "max_attempts",
    "output_json",
    "error_json",
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

    @property
    def step_id(self) -> str:
        return self.id.rsplit(":", 1)[-1]


@dataclass(frozen=True)
class HostPathRunResult:
    run_id: str
    path_id: str
    run_status: str
    replayed: bool
    ticks: int
    processes: tuple[JournalProcess, ...]

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


def _json_object(raw: Any, *, process_id: str, column: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise RuntimeFacadeError(f"journal {column} for {process_id!r} is not text")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeFacadeError(f"journal {column} for {process_id!r} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeFacadeError(f"journal {column} for {process_id!r} must decode to an object")
    return _redact(value)


def read_journal_processes(db_path: str | Path, run_id: str) -> tuple[JournalProcess, ...]:
    """Read exact process evidence for one run, failing closed on schema drift."""
    if not isinstance(run_id, str) or not run_id:
        raise RuntimeFacadeError("run_id must be a non-empty string")
    try:
        with closing(sqlite3.connect(Path(db_path).expanduser().resolve())) as connection:
            cursor = connection.execute(
                "SELECT id,status,attempt,max_attempts,output_json,error_json "
                "FROM processes WHERE run_id=? ORDER BY id",
                (run_id,),
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
        process_id, status, attempt, max_attempts, output_json, error_json = row
        if not isinstance(process_id, str) or not process_id:
            raise RuntimeFacadeError("journal process id must be a non-empty string")
        if not isinstance(status, str) or status not in _PROCESS_STATUSES:
            raise RuntimeFacadeError(f"journal process {process_id!r} has invalid status")
        if type(attempt) is not int or attempt < 0:
            raise RuntimeFacadeError(f"journal process {process_id!r} has invalid attempt")
        if type(max_attempts) is not int or max_attempts < 1:
            raise RuntimeFacadeError(f"journal process {process_id!r} has invalid max_attempts")
        processes.append(
            JournalProcess(
                id=process_id,
                status=status,
                attempt=attempt,
                max_attempts=max_attempts,
                output=_json_object(output_json, process_id=process_id, column="output_json"),
                error=_json_object(error_json, process_id=process_id, column="error_json"),
            )
        )
    return tuple(processes)


def _normalize_host_result(
    raw: Any,
    *,
    db_path: str | Path,
    path_id: str,
    expected_run_id: str,
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

    processes = read_journal_processes(db_path, run_id)
    journal_processes = {process.id: process.status for process in processes}
    if host_processes != journal_processes:
        raise RuntimeFacadeError("Fala host process summaries disagree with the durable journal")
    return HostPathRunResult(
        run_id=run_id,
        path_id=path_id,
        run_status=run_status,
        replayed=replayed,
        ticks=ticks,
        processes=processes,
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
                if any(existing.get(key) != value for key, value in requested.items()):
                    raise RuntimeFacadeError("Fala replay metadata disagrees with the durable journal")
                return
            existing.update(requested)
            encoded = json.dumps(existing, sort_keys=True, separators=(",", ":"))
            connection.execute("UPDATE runs SET metadata=? WHERE id=?", (encoded, run_id))
    except RuntimeFacadeError:
        raise
    except (json.JSONDecodeError, sqlite3.Error, TypeError, ValueError) as exc:
        raise RuntimeFacadeError(f"unable to persist Fala run metadata: {_redact(str(exc))}") from exc


def _host_python_overrides(package_path: str | Path) -> dict[str, tuple[str, ...]]:
    """Run repo-agent Python effectors with the interpreter hosting Fala."""
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
                and command[:3] == ["python3", "-m", "repo_agent.effector"]
                and all(isinstance(part, str) for part in command)
            ):
                overrides[effector["id"]] = (sys.executable, *command[1:])
    return overrides


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
    max_ticks: int = 32,
    worker_id: str = "repo-agent",
) -> HostPathRunResult:
    """Run one package path and normalize evidence from its SQLite journal."""
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
    )


async def run_package_path_async(**kwargs: Any) -> HostPathRunResult:
    """Run the blocking Mojo host without blocking an async tick caller."""
    return await asyncio.to_thread(run_package_path, **kwargs)
