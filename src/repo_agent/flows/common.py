"""Shared result models for Fala package path ticks."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from repo_agent.flows.runtime import JournalProcess


def process_summary(process: JournalProcess) -> dict[str, Any]:
    return {
        "id": process.id,
        "step_id": process.step_id,
        "status": process.status,
        "attempt": process.attempt,
        "max_attempts": process.max_attempts,
        "output": process.output,
        "error": process.error,
    }


def process_values(summary: dict[str, Any]) -> dict[str, Any]:
    """Return domain values while preserving raw journal output in evidence."""
    output = summary.get("output")
    if not isinstance(output, dict):
        return {}
    values = output.get("values")
    return values if isinstance(values, dict) else output


@dataclass
class PathRunResult:
    """Normalized result for any repo-agent package path tick."""

    run_id: str
    path_id: str
    dry_run: bool
    ticks: int
    stopped_reason: str
    completed: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    processes: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    fala_version: str = "0.7.9"
    status: str = ""
    action: str | None = None
    follow_up: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def path_result_to_dict(result: PathRunResult) -> dict[str, Any]:
    return result.to_dict()
