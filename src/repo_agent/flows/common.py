"""Shared helpers for Fala 0.2.x correlation path composition."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from fala.models import CorrelationPathSpec, EffectorAdapterSpec, EffectorSpec
from fala.runtime_backend import Process


def effector(
    effector_id: str,
    ref: str,
    *,
    conduction: list[str] | None = None,
) -> EffectorSpec:
    """Build a python_function EffectorSpec for path composition."""
    return EffectorSpec(
        id=effector_id,
        capability=effector_id,
        adapter=EffectorAdapterSpec(kind="python_function", ref=ref),
        conduction=conduction or [],
    )


def process_summary(process: Process) -> dict[str, Any]:
    marker = (process.metadata or {}).get("correlation_path") or {}
    status = process.status.value if hasattr(process.status, "value") else str(process.status)
    available_at = getattr(process, "available_at", None)
    if hasattr(available_at, "isoformat"):
        available_at = available_at.isoformat()
    return {
        "id": process.id,
        "step_id": marker.get("effector_id"),
        "status": status,
        "attempt": process.attempt,
        "max_attempts": process.max_attempts,
        "available_at": available_at,
        "output": process.output or {},
        "error": process.error or {},
    }


@dataclass
class PathRunResult:
    """Normalized result for any repo-agent correlation path tick."""

    run_id: str
    path_id: str
    dry_run: bool
    ticks: int
    stopped_reason: str
    completed: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    processes: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    fala_version: str = "0.2.1"
    status: str = ""
    action: str | None = None  # triage router follow-up
    follow_up: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

def path_result_to_dict(result: PathRunResult) -> dict[str, Any]:
    """Serialize a completed path result for tick JSON output."""
    return result.to_dict()


def path_ids(spec: CorrelationPathSpec) -> list[str]:
    return [e.id for e in spec.effectors]


def path_conduction_graph(spec: CorrelationPathSpec) -> dict[str, list[str]]:
    return {e.id: list(e.conduction) for e in spec.effectors}
