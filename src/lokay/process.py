"""Catalog-validated single-process dispatcher.

Each production job invokes this module with an exact catalog command identity:

    python -m lokay.process lokay-process-<id> --config … --db … --dry-run|--live --json

Validation against the complete loaded process catalog happens before any
flow/effector import or adapter side effects. Aggregate tick_all is never used.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from lokay.config import ConfigError, load_config
from lokay.process_runtime import (
    DEFAULT_GENERATION_PATH,
    FenceError,
    LeaseError,
    ProcessDisabledError,
    ProcessRuntime,
    ProcessRuntimeError,
    ReceiptConflictError,
    ReceiptRecord,
    payload_digest,
    subject_key,
)
from lokay.process_contracts import (
    FORBIDDEN_PATH_ALIASES,
    PROCESS_CONTRACTS,
    ProcessContract,
    contract_for,
)
from lokay.registry import PROCESS_GRAPH_CONTRACT, PROCESS_IDS

COMMAND_PREFIX = "lokay-process-"
_COMMAND_RE = re.compile(rf"^{re.escape(COMMAND_PREFIX)}([A-Za-z0-9_]+)\Z")

# Launch-affecting identity fields that must stay coherent with the catalog
# contract before any worker adapter runs.
_LAUNCH_IDENTITY_FIELDS = (
    "id",
    "enabled",
    "interval_seconds",
    "concurrency",
    "command",
    "candidate_fencing",
    "health_id",
    "lease_seconds",
    "lease_renew_seconds",
    "stale_owner_after_seconds",
    "lock_scope",
)


class ProcessError(ValueError):
    """Raised when process identity or activation is unsafe."""


_CANDIDATE_RE = re.compile(r"(?!0{64}\Z)[0-9a-f]{64}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_RUNTIME_CONTEXT: dict[str, Any] | None = None
_PACKAGE_PATH = Path(__file__).resolve().parents[2] / "fala-package.toml"
_TERMINAL_FAILURES = frozenset({"failed", "cancelled", "timed_out"})
_WAITING_STATUSES = frozenset(
    {
        "waiting",
        "retry_wait",
        "running",
        "pending",
        "ready",
        "created",
        "active",
        "cancel_requested",
    }
)


class _LiveAdapterFailure(Exception):
    """Carry a fail-closed adapter payload out of a fenced callback."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        super().__init__(str(payload.get("reason") or "failed"))

ProcessAdapter = Callable[..., dict[str, Any]]


def command_for_id(process_id: str) -> str:
    return f"{COMMAND_PREFIX}{process_id}"


def process_id_from_command(command: str) -> str | None:
    match = _COMMAND_RE.fullmatch(str(command or ""))
    return match.group(1) if match else None


def _error_payload(
    *,
    command: str,
    process_id: str,
    reason: str,
    dry_run: bool,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "failed",
        "reason": reason,
        "error": detail or reason,
        "command": command,
        "process_id": process_id,
        "dry_run": dry_run,
        "mutated": False,
        "ticks": 0,
        "stopped_reason": reason,
        "completed": [],
        "failed": [{"id": process_id or command, "status": "failed", "error": detail or reason}],
        "processes": [],
        "summary": {
            "run_status": "failed",
            "reason": reason,
            "command": command,
            "process_id": process_id,
            "mutated": False,
        },
        "path_id": process_id or command,
        "run_id": "",
        "fala_version": "0.7.15",
        "action": None,
        "follow_up": None,
    }


def _run_id(process_id: str) -> str:
    return (
        f"process-{process_id}-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
        f"{uuid.uuid4().hex[:8]}"
    )


def _planned_payload(
    *,
    process_id: str,
    command: str,
    record: Mapping[str, Any],
    dry_run: bool,
    db_path: Path,
    contract: ProcessContract | None = None,
) -> dict[str, Any]:
    run_id = _run_id(process_id)
    bound = contract or contract_for(process_id)
    if bound.path_id in FORBIDDEN_PATH_ALIASES or bound.path_id != process_id:
        raise ProcessError(
            f"adapter refused forbidden or mismatched path_id for {process_id}: "
            f"{bound.path_id!r}"
        )
    return {
        "ok": True,
        "status": "planned",
        "reason": "planned",
        "command": command,
        "process_id": process_id,
        "path_id": bound.path_id,
        "run_id": run_id,
        "dry_run": dry_run,
        "mutated": False,
        "ticks": 0,
        "stopped_reason": "planned",
        "db_path": str(db_path),
        "completed": [],
        "failed": [],
        "processes": [],
        "allowed_effectors": list(bound.allowed_effectors),
        "forbidden_sibling_effectors": list(bound.forbidden_sibling_effectors),
        "output_receipts": list(bound.output_receipts),
        "mutation_scopes": list(bound.mutation_scopes),
        "lock_scope": bound.lock_scope,
        "max_ticks": bound.max_ticks,
        "summary": {
            "run_status": "planned",
            "reason": "planned",
            "command": command,
            "process_id": process_id,
            "path_id": bound.path_id,
            "mutated": False,
            "interval_seconds": record.get("interval_seconds"),
            "adapter": process_id,
            "max_ticks": bound.max_ticks,
            "lock_scope": bound.lock_scope,
        },
        "fala_version": "0.7.15",
        "action": None,
        "follow_up": None,
        "catalog": {
            key: record.get(key)
            for key in _LAUNCH_IDENTITY_FIELDS
            if key in record
        },
        "contract": {
            "process_id": bound.process_id,
            "path_id": bound.path_id,
            "allowed_effectors": list(bound.allowed_effectors),
            "forbidden_sibling_effectors": list(bound.forbidden_sibling_effectors),
            "required_inputs": list(bound.required_inputs),
            "predecessor_groups": [list(group) for group in bound.predecessor_groups],
            "output_receipts": list(bound.output_receipts),
            "mutation_scopes": list(bound.mutation_scopes),
            "lock_scope": bound.lock_scope,
            "max_ticks": bound.max_ticks,
        },
    }


def _identity_error_payload(
    *,
    process_id: str,
    command: str,
    record: Mapping[str, Any],
    dry_run: bool,
    db_path: Path,
    reason: str,
    detail: str,
) -> dict[str, Any]:
    payload = _error_payload(
        command=command,
        process_id=process_id,
        reason=reason,
        dry_run=dry_run,
        detail=detail,
    )
    payload["db_path"] = str(db_path)
    payload["path_id"] = process_id
    payload["run_id"] = _run_id(process_id)
    payload["summary"]["adapter"] = process_id
    return payload


def _success_payload(
    *,
    process_id: str,
    command: str,
    record: Mapping[str, Any],
    dry_run: bool,
    db_path: Path,
    generation: str,
    candidate_id: str,
    config_sha256: str,
    state_root: Path,
    health_status: str,
    host: Any,
    package_path: Path,
    contract: ProcessContract,
    receipts: list[Mapping[str, Any]] | None = None,
    action: str | None = None,
) -> dict[str, Any]:
    processes = [
        {
            "id": process.id,
            "step_id": process.step_id,
            "status": process.status,
            "attempt": process.attempt,
            "max_attempts": process.max_attempts,
            "effector_id": process.effector_id,
            "correlation_path_id": process.correlation_path_id,
        }
        for process in host.processes
    ]
    completed = [item["id"] for item in processes if item["status"] == "succeeded"]
    return {
        "ok": True,
        "status": "ok",
        "reason": "completed",
        "command": command,
        "process_id": process_id,
        "path_id": host.path_id,
        "run_id": host.run_id,
        "dry_run": dry_run,
        "mutated": False,
        "ticks": host.ticks,
        "stopped_reason": host.run_status,
        "db_path": str(db_path),
        "package_path": str(package_path),
        "package_id": host.package_id,
        "package_version": host.package_version,
        "package_digest": host.package_digest,
        "correlation_path_digest": host.correlation_path_digest,
        "allowed_effectors": list(contract.allowed_effectors),
        "required_inputs": list(contract.required_inputs),
        "predecessor_groups": [list(group) for group in contract.predecessor_groups],
        "output_receipts": list(contract.output_receipts),
        "max_ticks": contract.max_ticks,
        "lock_scope": contract.lock_scope,
        "completed": completed,
        "failed": [],
        "processes": processes,
        "summary": {
            "run_status": host.run_status,
            "reason": "completed",
            "command": command,
            "process_id": process_id,
            "path_id": host.path_id,
            "mutated": False,
            "interval_seconds": record.get("interval_seconds"),
            "adapter": process_id,
            "generation": generation,
            "candidate_id": candidate_id,
            "config_sha256": config_sha256,
            "state_root": str(state_root),
            "health_status": health_status,
            "replayed": host.replayed,
            "package_id": host.package_id,
            "package_version": host.package_version,
            "package_digest": host.package_digest,
            "correlation_path_digest": host.correlation_path_digest,
            "max_ticks": contract.max_ticks,
        },
        "fala_version": "0.7.15",
        "action": action,
        "follow_up": None,
        "generation": generation,
        "candidate_id": candidate_id,
        "config_sha256": config_sha256,
        "state_root": str(state_root),
        "receipts": [dict(item) for item in (receipts or [])],
        "fala": {
            "run_id": host.run_id,
            "path_id": host.path_id,
            "run_status": host.run_status,
            "package_id": host.package_id,
            "package_version": host.package_version,
            "package_digest": host.package_digest,
            "correlation_path_digest": host.correlation_path_digest,
            "ticks": host.ticks,
            "replayed": host.replayed,
            "runtime_version": host.runtime_version,
            "backend_version": host.backend_version,
            "schema_version": host.schema_version,
        },
        "catalog": {
            key: record.get(key)
            for key in _LAUNCH_IDENTITY_FIELDS
            if key in record
        },
        "contract": {
            "process_id": contract.process_id,
            "path_id": contract.path_id,
            "allowed_effectors": list(contract.allowed_effectors),
            "forbidden_sibling_effectors": list(contract.forbidden_sibling_effectors),
            "required_inputs": list(contract.required_inputs),
            "predecessor_groups": [list(group) for group in contract.predecessor_groups],
            "output_receipts": list(contract.output_receipts),
            "mutation_scopes": list(contract.mutation_scopes),
            "lock_scope": contract.lock_scope,
            "max_ticks": contract.max_ticks,
        },
    }


def _resolve_package_path(cfg: Any | None = None) -> Path:
    """Resolve the repository Fala package path from env or package layout."""
    if cfg is not None:
        raw = getattr(cfg, "raw", {})
        if isinstance(raw, Mapping):
            for key in ("package_path", "fala_package_path"):
                value = str(raw.get(key) or "").strip()
                if value:
                    return Path(value).expanduser()
            paths = raw.get("paths")
            if isinstance(paths, Mapping):
                for key in ("package_path", "fala_package", "fala_package_path"):
                    value = str(paths.get(key) or "").strip()
                    if value:
                        return Path(value).expanduser()
    for key in (
        "HERMES_LOKAY_PACKAGE_PATH",
        "LOKAY_PACKAGE_PATH",
        "FALA_PACKAGE_PATH",
    ):
        value = os.environ.get(key, "").strip()
        if value:
            return Path(value).expanduser()
    return _PACKAGE_PATH


def _configured_repos(cfg: Any | None) -> list[dict[str, Any]]:
    """Return repository entries with absolute clone paths and triage overrides."""
    repos: list[dict[str, Any]] = []
    if cfg is None:
        return repos
    for repo in getattr(cfg, "repos", ()) or ():
        entry: dict[str, Any] = {
            "repo": getattr(repo, "repo", ""),
            "board": getattr(repo, "board", ""),
            "clone_path": getattr(repo, "clone_path", ""),
            "priority": getattr(repo, "priority", 50),
        }
        triage_goal = ""
        if hasattr(cfg, "effective_triage_goal"):
            try:
                triage_goal = str(cfg.effective_triage_goal(repo) or "")
            except Exception:
                triage_goal = str(getattr(repo, "triage_goal", "") or "")
        else:
            triage_goal = str(getattr(repo, "triage_goal", "") or "")
        if triage_goal:
            entry["triage_goal"] = triage_goal
        context_paths: tuple[str, ...] | list[str] = ()
        if hasattr(cfg, "effective_triage_context_paths"):
            try:
                context_paths = cfg.effective_triage_context_paths(repo) or ()
            except Exception:
                context_paths = getattr(repo, "triage_context_paths", ()) or ()
        else:
            context_paths = getattr(repo, "triage_context_paths", ()) or ()
        if context_paths:
            entry["triage_context_paths"] = list(context_paths)
        for name, effective in (
            ("auto_close_duplicates", "effective_auto_close_duplicates"),
            ("auto_close_out_of_scope", "effective_auto_close_out_of_scope"),
        ):
            value: Any = None
            if hasattr(cfg, effective):
                try:
                    value = getattr(cfg, effective)(repo)
                except Exception:
                    value = getattr(repo, name, None)
            else:
                value = getattr(repo, name, None)
            if value is not None:
                entry[name] = bool(value)
        repos.append(entry)
    return repos


def _default_issue_limit(cfg: Any | None) -> int:
    limit = 10
    if cfg is None:
        return limit
    raw = getattr(cfg, "raw", {})
    if isinstance(raw, Mapping):
        github = raw.get("github")
        if isinstance(github, Mapping):
            try:
                return int(github.get("default_limit") or limit)
            except (TypeError, ValueError):
                return limit
    return limit


def _automation_policy(cfg: Any) -> dict[str, Any]:
    automation = getattr(cfg, "automation", None)
    if automation is None:
        return {}
    return {
        "automerge": bool(getattr(automation, "automerge", False)),
        "require_human_approval": bool(getattr(automation, "require_human_approval", True)),
        "require_checks": bool(getattr(automation, "require_checks", True)),
        "require_test_evidence": bool(getattr(automation, "require_test_evidence", True)),
        "fixer_assignee": str(getattr(automation, "fixer_assignee", "") or ""),
        "merge_method": str(getattr(automation, "merge_method", "merge") or "merge"),
        "max_active_issues": int(getattr(automation, "max_active_issues", 1) or 1),
    }


def _label_map(cfg: Any) -> dict[str, Any]:
    labels = getattr(cfg, "labels", None)
    if labels is None:
        return {}
    mapping = {
        "ready": str(getattr(labels, "ready", "") or ""),
        "in_progress": str(getattr(labels, "in_progress", "") or ""),
        "blocked": str(getattr(labels, "blocked", "") or ""),
        "pr_opened": str(getattr(labels, "pr_opened", "") or ""),
        "generated": str(getattr(labels, "generated", "") or ""),
        "needs_feedback": str(getattr(labels, "needs_feedback", "") or ""),
        "duplicate": str(getattr(labels, "duplicate", "") or ""),
        "out_of_scope": str(getattr(labels, "out_of_scope", "") or ""),
        "frozen": str(getattr(labels, "frozen", "") or ""),
    }
    mapping.update(
        {
            "ready_label": mapping["ready"],
            "in_progress_label": mapping["in_progress"],
            "blocked_label": mapping["blocked"],
            "pr_opened_label": mapping["pr_opened"],
            "generated_label": mapping["generated"],
            "needs_feedback_label": mapping["needs_feedback"],
            "duplicate_label": mapping["duplicate"],
            "out_of_scope_label": mapping["out_of_scope"],
            "frozen_label": mapping["frozen"],
        }
    )
    return mapping


def _direction_map(cfg: Any) -> dict[str, Any]:
    direction = getattr(cfg, "direction", None)
    if direction is None:
        return {}
    return {
        "repo_goal": str(getattr(direction, "repo_goal", "") or ""),
        "require_keywords": list(getattr(direction, "require_keywords", ()) or ()),
        "deny_keywords": list(getattr(direction, "deny_keywords", ()) or ()),
        "reject_labels": list(getattr(direction, "reject_labels", ()) or ()),
        "min_goal_overlap": int(getattr(direction, "min_goal_overlap", 1) or 1),
    }


def _triage_map(cfg: Any) -> dict[str, Any]:
    triage = getattr(cfg, "triage", None)
    if triage is None:
        return {}
    return {
        "triage_enabled": bool(getattr(triage, "enabled", True)),
        "context_paths": list(getattr(triage, "context_paths", ()) or ()),
        "triage_context_paths": list(getattr(triage, "context_paths", ()) or ()),
        "context_max_bytes": int(getattr(triage, "context_max_bytes", 131_072) or 131_072),
        "triage_context_max_bytes": int(getattr(triage, "context_max_bytes", 131_072) or 131_072),
        "auto_close_duplicates": bool(getattr(triage, "auto_close_duplicates", False)),
        "auto_close_out_of_scope": bool(getattr(triage, "auto_close_out_of_scope", True)),
    }


def _executor_map(cfg: Any) -> dict[str, Any]:
    executor = getattr(cfg, "executor", None)
    if executor is None:
        return {}
    return {
        "executor_enabled": bool(getattr(executor, "enabled", False)),
        "executor_command": str(getattr(executor, "command", "") or ""),
        "executor_model": str(getattr(executor, "model", "") or ""),
        "command": str(getattr(executor, "command", "") or ""),
        "model": str(getattr(executor, "model", "") or ""),
        "thinking": str(getattr(executor, "thinking", "") or ""),
        "timeout_seconds": float(getattr(executor, "timeout_seconds", 7200.0) or 7200.0),
    }


def _path_map(cfg: Any) -> dict[str, Any]:
    paths = getattr(cfg, "paths", None)
    if paths is None:
        return {}
    worktree_root = str(getattr(paths, "worktree_root", "") or "")
    dispatch_receipts = str(getattr(paths, "dispatch_receipts", "") or "")
    task_receipts = str(getattr(paths, "task_receipts", "") or "")
    merge_receipts = str(getattr(paths, "merge_receipts", "") or "")
    active_issue = str(getattr(paths, "active_issue", "") or "")
    triage_receipts = str(getattr(paths, "triage_receipts", "") or "")
    cleanup_receipt_root = (
        str(Path(merge_receipts) / "cleanup-outcomes") if merge_receipts else ""
    )
    return {
        "worktree_root": worktree_root,
        "dispatch_receipts": dispatch_receipts,
        "task_receipts": task_receipts,
        "merge_receipts": merge_receipts,
        "active_issue": active_issue,
        "active_issue_path": active_issue,
        "claim_path": active_issue,
        "claim_root": active_issue,
        "triage_receipts": triage_receipts,
        "triage_receipt_root": triage_receipts,
        "receipt_root": triage_receipts,
        "task_receipt_root": task_receipts,
        "merge_receipt_root": merge_receipts,
        "cleanup_receipt_root": cleanup_receipt_root,
    }


def _base_effector_config(
    cfg: Any | None,
    *,
    contract: ProcessContract,
    process_id: str,
    run_id: str,
    db_path: Path,
    generation: str,
    candidate_id: str,
    config_sha256: str,
    command: str,
    is_dry: bool,
) -> dict[str, Any]:
    """Shared AgentConfig-derived step config for one process's allowed effectors."""
    config: dict[str, Any] = {
        "dry_run": bool(is_dry),
        "live": not bool(is_dry),
        "process_id": process_id,
        "path_id": contract.path_id,
        "run_id": run_id,
        "db_path": str(db_path),
        "command": command,
        "generation": generation,
        "candidate_id": candidate_id,
        "candidate": candidate_id,
        "config_sha256": config_sha256,
        "allowed_effectors": list(contract.allowed_effectors),
        "forbidden_sibling_effectors": list(contract.forbidden_sibling_effectors),
        "required_inputs": list(contract.required_inputs),
        "predecessor_groups": [list(group) for group in contract.predecessor_groups],
        "output_receipts": list(contract.output_receipts),
        "mutation_scopes": list(contract.mutation_scopes),
        "lock_scope": contract.lock_scope,
        "max_ticks": contract.max_ticks,
    }
    if cfg is None:
        return config

    config["assignee"] = str(getattr(cfg, "assignee", "") or "")
    config["kanban_intake_assignee"] = str(getattr(cfg, "kanban_intake_assignee", "") or "")
    config["gh_cli"] = str(getattr(cfg, "gh_cli", "gh") or "gh")
    config["branch_prefix"] = str(getattr(cfg, "branch_prefix", "ai/fix") or "ai/fix")
    config["base_branch"] = str(getattr(cfg, "base_branch", "main") or "main")
    config["mode"] = str(getattr(cfg, "mode", "dry-run") or "dry-run")

    policy = _automation_policy(cfg)
    config.update(policy)
    config["policy"] = dict(policy)

    labels = _label_map(cfg)
    config.update(labels)
    config["labels"] = {
        key: labels[key]
        for key in (
            "ready",
            "in_progress",
            "blocked",
            "pr_opened",
            "generated",
            "needs_feedback",
            "duplicate",
            "out_of_scope",
            "frozen",
        )
        if key in labels
    }

    config.update(_direction_map(cfg))
    config.update(_triage_map(cfg))
    config.update(_executor_map(cfg))
    config.update(_path_map(cfg))

    repos = _configured_repos(cfg)
    config["repos"] = repos

    if process_id in {
        "issue_triage",
        "issue_feedback",
        "issue_split",
        "issue_close",
        "issue_ready",
    }:
        if config.get("triage_receipts"):
            config.setdefault("receipt_root", config["triage_receipts"])
            config.setdefault("triage_receipt_root", config["triage_receipts"])
    if process_id in {"issue_to_pr", "pr_repair"}:
        if config.get("dispatch_receipts") and run_id:
            config.setdefault(
                "receipt_path",
                str(Path(str(config["dispatch_receipts"])) / f"dispatch-{run_id}.json"),
            )
    if process_id in {"pr_merge"}:
        if config.get("merge_receipts") and run_id:
            config.setdefault(
                "receipt_path",
                str(Path(str(config["merge_receipts"])) / f"merge-{run_id}.json"),
            )
    if process_id == "cleanup":
        if config.get("dispatch_receipts") and run_id:
            config.setdefault(
                "receipt_path",
                str(Path(str(config["dispatch_receipts"])) / f"cleanup-{run_id}.json"),
            )
    if process_id == "cleanup_reconcile":
        for src, dest in (
            ("active_issue", "claim_root"),
            ("task_receipts", "task_receipt_root"),
            ("merge_receipts", "merge_receipt_root"),
            ("worktree_root", "worktree_root"),
        ):
            if config.get(src):
                config.setdefault(dest, config[src])
        if config.get("merge_receipts"):
            config.setdefault(
                "cleanup_receipt_root",
                str(Path(str(config["merge_receipts"])) / "cleanup-outcomes"),
            )
    return config


def _merge_mapping(base: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(base, Mapping):
        return {}
    return {str(key): value for key, value in base.items()}


def _live_run_inputs(
    *,
    contract: ProcessContract,
    process_id: str,
    run_id: str,
    db_path: Path,
    cfg: Any | None,
    generation: str,
    candidate_id: str,
    config_sha256: str,
    command: str,
    subject: Mapping[str, Any] | None = None,
    predecessor_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build process-owned path-level inputs for a bounded Fala path invocation.

    ``subject`` and ``predecessor_evidence`` are optional seams for concurrent
    resolver wiring. When absent, predecessor required_inputs remain present as
    None so later cursor wiring can fill digests without sibling leakage.
    """
    repos = _configured_repos(cfg)
    limit = _default_issue_limit(cfg)
    inputs: dict[str, Any] = {
        "dry_run": False,
        "live": True,
        "process_id": process_id,
        "path_id": contract.path_id,
        "run_id": run_id,
        "db_path": str(db_path),
        "command": command,
        "generation": generation,
        "candidate_id": candidate_id,
        "candidate": candidate_id,
        "config_sha256": config_sha256,
        "required_inputs": list(contract.required_inputs),
        "predecessor_groups": [list(group) for group in contract.predecessor_groups],
        "predecessor_evidence": {
            "groups": [list(group) for group in contract.predecessor_groups],
            "required_inputs": list(contract.required_inputs),
        },
        "allowed_effectors": list(contract.allowed_effectors),
        "forbidden_sibling_effectors": list(contract.forbidden_sibling_effectors),
        "output_receipts": list(contract.output_receipts),
        "mutation_scopes": list(contract.mutation_scopes),
        "max_ticks": contract.max_ticks,
        "lock_scope": contract.lock_scope,
    }

    subject_map = _merge_mapping(subject)
    if subject_map:
        inputs["subject"] = dict(subject_map)
        for key, value in subject_map.items():
            if key in {"process_id", "scope"}:
                continue
            if value is not None:
                inputs.setdefault(key, value)

    pred_map = _merge_mapping(predecessor_evidence)
    if pred_map:
        envelope = dict(inputs["predecessor_evidence"])
        envelope.update(pred_map)
        inputs["predecessor_evidence"] = envelope
        for key, value in pred_map.items():
            if key in {"groups", "required_inputs"}:
                continue
            if value is not None:
                inputs[key] = value

    for key in contract.required_inputs:
        if key == "repos":
            inputs["repos"] = repos
            continue
        if key == "limit":
            inputs["limit"] = limit
            continue
        bound = pred_map.get(key)
        receipts = pred_map.get("receipts")
        if bound is None and isinstance(receipts, Mapping):
            item = receipts.get(key)
            if isinstance(item, Mapping):
                bound = item.get("digest")
            if bound is None:
                groups = pred_map.get("groups")
                if isinstance(groups, list):
                    for group in groups:
                        if not isinstance(group, list):
                            continue
                        matching = [kind for kind in group if str(kind) in key]
                        if len(matching) == 1:
                            item = receipts.get(matching[0])
                            if isinstance(item, Mapping):
                                bound = item.get("digest")
                            break
                    if bound is None and len(groups) == 1 and isinstance(groups[0], list) and len(groups[0]) == 1:
                        item = receipts.get(groups[0][0])
                        if isinstance(item, Mapping):
                            bound = item.get("digest")
        inputs[key] = bound

    if repos and "repos" not in inputs:
        inputs["repos"] = repos
    if "limit" not in inputs and process_id in {
        "repo_issue_poll",
        "pr_triage",
        "issue_triage",
    }:
        inputs["limit"] = limit
    return inputs


def build_effective_run(
    *,
    contract: ProcessContract,
    process_id: str,
    run_id: str,
    db_path: Path,
    cfg: Any | None,
    generation: str,
    candidate_id: str,
    config_sha256: str,
    command: str,
    subject: Mapping[str, Any] | None = None,
    predecessor_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build path-level inputs plus per-effector maps for one catalog process.

    Returns ``inputs``, ``effector_inputs``, and ``effector_configs``. Effector
    maps include only ``contract.allowed_effectors`` — never siblings or
    aggregate aliases. Optional ``subject`` / ``predecessor_evidence`` are
    merged concretely into path inputs and carried into step configs when set.
    """
    if contract.path_id != process_id or contract.path_id in FORBIDDEN_PATH_ALIASES:
        raise ProcessError(
            f"effective run refused forbidden or mismatched path_id for {process_id}: "
            f"{contract.path_id!r}"
        )
    if process_id not in PROCESS_CONTRACTS or process_id not in PROCESS_IDS:
        raise ProcessError(f"effective run refused unknown process id: {process_id}")

    inputs = _live_run_inputs(
        contract=contract,
        process_id=process_id,
        run_id=run_id,
        db_path=db_path,
        cfg=cfg,
        generation=generation,
        candidate_id=candidate_id,
        config_sha256=config_sha256,
        command=command,
        subject=subject,
        predecessor_evidence=predecessor_evidence,
    )
    step_config = _base_effector_config(
        cfg,
        contract=contract,
        process_id=process_id,
        run_id=run_id,
        db_path=db_path,
        generation=generation,
        candidate_id=candidate_id,
        config_sha256=config_sha256,
        command=command,
        is_dry=False,
    )

    for key in (
        "repo",
        "board",
        "clone_path",
        "issue",
        "number",
        "pr_number",
        "task_id",
        "branch",
        "head_oid",
        "verified_head",
        "receipt_path",
        "receipt_id",
        "worktree_path",
        "claim_path",
    ):
        if key in inputs and inputs[key] is not None:
            step_config.setdefault(key, inputs[key])

    common_input: dict[str, Any] = {
        "dry_run": False,
        "live": True,
        "process_id": process_id,
        "path_id": contract.path_id,
        "run_id": run_id,
        "db_path": str(db_path),
        "generation": generation,
        "candidate_id": candidate_id,
        "candidate": candidate_id,
        "config_sha256": config_sha256,
    }
    if "repos" in inputs:
        common_input["repos"] = inputs["repos"]
    if "limit" in inputs:
        common_input["limit"] = inputs["limit"]
    for key, value in inputs.items():
        if key in common_input or key in {
            "required_inputs",
            "predecessor_groups",
            "predecessor_evidence",
            "allowed_effectors",
            "forbidden_sibling_effectors",
            "output_receipts",
            "mutation_scopes",
            "max_ticks",
            "lock_scope",
            "command",
            "subject",
        }:
            continue
        if value is not None:
            common_input[key] = value
    if "subject" in inputs:
        common_input["subject"] = dict(inputs["subject"])
    if "predecessor_evidence" in inputs:
        common_input["predecessor_evidence"] = dict(inputs["predecessor_evidence"])

    effector_inputs: dict[str, dict[str, Any]] = {
        effector_id: dict(common_input) for effector_id in contract.allowed_effectors
    }
    effector_configs: dict[str, dict[str, Any]] = {
        effector_id: dict(step_config) for effector_id in contract.allowed_effectors
    }

    if process_id == "repo_issue_poll":
        for effector_id in ("read_open_issues", "normalize_issue_rows"):
            if effector_id in effector_inputs:
                effector_inputs[effector_id].setdefault(
                    "limit", inputs.get("limit", 10)
                )
                if "repos" in inputs:
                    effector_inputs[effector_id]["repos"] = inputs["repos"]
    elif process_id == "pr_triage":
        if "read_open_prs" in effector_inputs:
            effector_inputs["read_open_prs"].setdefault(
                "limit", inputs.get("limit", 30)
            )
        for effector_id, extra in (
            ("evaluate_checks", {"require_checks": step_config.get("require_checks", True)}),
            (
                "evaluate_test_evidence",
                {
                    "require_test_evidence": step_config.get(
                        "require_test_evidence", True
                    )
                },
            ),
            (
                "decide_triage_action",
                {
                    "automerge": step_config.get("automerge", False),
                    "branch_prefix": step_config.get("branch_prefix", "ai/fix"),
                    "base_branch": step_config.get("base_branch", "main"),
                    "require_human_approval": step_config.get(
                        "require_human_approval", True
                    ),
                },
            ),
        ):
            if effector_id in effector_inputs:
                effector_inputs[effector_id].update(extra)
    elif process_id == "issue_to_pr":
        for effector_id in (
            "create_local_branch",
            "verify_omp_postconditions",
            "create_pull_request",
            "build_dispatch_receipt",
            "publish_dispatch_receipt",
        ):
            if effector_id not in effector_inputs:
                continue
            if step_config.get("worktree_root"):
                effector_inputs[effector_id].setdefault(
                    "worktree_root", step_config["worktree_root"]
                )
            if step_config.get("base_branch"):
                effector_inputs[effector_id].setdefault(
                    "base_branch", step_config["base_branch"]
                )
            if step_config.get("receipt_path"):
                effector_inputs[effector_id].setdefault(
                    "receipt_path", step_config["receipt_path"]
                )
        if "complete_task" in effector_inputs:
            effector_inputs["complete_task"].setdefault(
                "result", "dispatched via issue_to_pr"
            )
    elif process_id == "cleanup":
        for effector_id in contract.allowed_effectors:
            effector_inputs[effector_id].setdefault("require_safe", True)
            if step_config.get("worktree_root"):
                effector_inputs[effector_id].setdefault(
                    "worktree_root", step_config["worktree_root"]
                )
            if step_config.get("active_issue"):
                effector_inputs[effector_id].setdefault(
                    "claim_path", step_config["active_issue"]
                )
                effector_inputs[effector_id].setdefault(
                    "active_issue_path", step_config["active_issue"]
                )
            if step_config.get("receipt_path"):
                effector_inputs[effector_id].setdefault(
                    "receipt_path", step_config["receipt_path"]
                )
    elif process_id == "cleanup_reconcile":
        for effector_id in contract.allowed_effectors:
            for key in (
                "worktree_root",
                "claim_root",
                "claim_path",
                "task_receipt_root",
                "merge_receipt_root",
                "cleanup_receipt_root",
                "db_path",
            ):
                if step_config.get(key):
                    effector_inputs[effector_id].setdefault(key, step_config[key])
                    effector_configs[effector_id].setdefault(key, step_config[key])

    allowed = set(contract.allowed_effectors)
    forbidden = set(contract.forbidden_sibling_effectors) | set(FORBIDDEN_PATH_ALIASES)
    leaked = (set(effector_inputs) | set(effector_configs)) - allowed
    if leaked or (set(effector_inputs) & forbidden) or (set(effector_configs) & forbidden):
        raise ProcessError(
            f"effective run leaked non-owned effectors for {process_id}: "
            f"{sorted(leaked | (set(effector_inputs) & forbidden) | (set(effector_configs) & forbidden))}"
        )
    if set(effector_inputs) != allowed or set(effector_configs) != allowed:
        raise ProcessError(
            f"effective run missing allowed effectors for {process_id}: "
            f"inputs={sorted(set(effector_inputs))} configs={sorted(set(effector_configs))} "
            f"allowed={sorted(allowed)}"
        )

    return {
        "inputs": inputs,
        "effector_inputs": effector_inputs,
        "effector_configs": effector_configs,
        "allowed_effectors": list(contract.allowed_effectors),
        "max_ticks": contract.max_ticks,
    }


def _host_failure_payload(
    *,
    process_id: str,
    command: str,
    record: Mapping[str, Any],
    db_path: Path,
    run_id: str,
    reason: str,
    detail: str,
    host: Any | None = None,
) -> dict[str, Any]:
    payload = _identity_error_payload(
        process_id=process_id,
        command=command,
        record=record,
        dry_run=False,
        db_path=db_path,
        reason=reason,
        detail=detail,
    )
    payload["run_id"] = run_id
    if host is not None:
        payload["path_id"] = host.path_id
        payload["ticks"] = host.ticks
        payload["stopped_reason"] = host.run_status
        payload["summary"]["run_status"] = host.run_status
        payload["fala"] = {
            "run_id": host.run_id,
            "path_id": host.path_id,
            "run_status": host.run_status,
            "package_id": host.package_id,
            "package_version": host.package_version,
            "package_digest": host.package_digest,
            "correlation_path_digest": host.correlation_path_digest,
            "ticks": host.ticks,
            "replayed": host.replayed,
        }
        payload["failed"] = [
            {
                "id": process.id,
                "step_id": process.step_id,
                "status": process.status,
                "error": process.error,
            }
            for process in host.failed
        ] or payload["failed"]
        payload["processes"] = [
            {
                "id": process.id,
                "step_id": process.step_id,
                "status": process.status,
                "effector_id": process.effector_id,
                "correlation_path_id": process.correlation_path_id,
            }
            for process in host.processes
        ]
    return payload
_OUTPUT_RECEIPT_EFFECTORS: dict[str, dict[str, str | tuple[str, ...]]] = {
    "repo_issue_poll": {"read_open_issues": "repo_poll", "normalize_issue_rows": "issue_snapshot"},
    "issue_triage": {"publish_triage_decision_receipt": "issue_decision"},
    "issue_feedback": {"publish_triage_feedback_receipt": "feedback", "publish_triage_mutation_verification": "feedback_verified"},
    "issue_split": {"split_mixed_triage_issue": ("split", "child_handoff", "split_verified")},
    "issue_close": {"publish_triage_close_authorization": "close_authorization", "publish_triage_close_verification": "close_verified"},
    "issue_ready": {"build_issue_claim_result": "claim", "create_intake_task": "task_handoff"},
    "issue_to_pr": {"build_dispatch_receipt": "implementation", "publish_dispatch_receipt": "pr_opened"},
    "pr_triage": {"decide_triage_action": "pr_decision"},
    "pr_repair": {"reserve_repair_attempt": "repair_reservation", "verify_repair_receipt": "repair_verified"},
    "pr_merge": {"build_merge_receipt": "merge_verified", "verify_merge_receipt": "finalization"},
    "cleanup": {"build_cleanup_receipt": "cleanup_verified"},
    "cleanup_reconcile": {"publish_reconcile_receipt": "cleanup_reconciliation"},
}


def _direct_subject(value: Mapping[str, Any]) -> dict[str, Any] | None:
    explicit = value.get("subject")
    if isinstance(explicit, Mapping):
        return dict(explicit)
    repo = value.get("repo") or value.get("repository")
    number = value.get("issue_number") or value.get("pr_number") or value.get("number")
    for key in ("issue", "pr"):
        child = value.get(key)
        if isinstance(child, Mapping):
            child_repo = child.get("repo") or child.get("repository")
            child_number = child.get("number") or child.get(f"{key}_number")
            if repo is None:
                repo = child_repo
            elif child_repo not in (None, "") and str(child_repo) != str(repo):
                return None
            if number is None:
                number = child_number
            elif child_number not in (None, "") and str(child_number) != str(number):
                return None
        elif isinstance(child, (int, str)) and number is None:
            number = child
    head = value.get("head_oid") or value.get("headSha") or value.get("verified_head") or value.get("oid") or value.get("sha")
    if repo is None and number is None and head is None:
        return None
    subject: dict[str, Any] = {}
    if repo is not None:
        subject["repo"] = str(repo)
    if number is not None:
        subject["number"] = int(number) if str(number).isdigit() else str(number)
    if head is not None:
        subject["head_oid"] = str(head)
    return subject


def _subject_from_payload(value: Any) -> dict[str, Any] | None:
    """Extract an unambiguous identity without choosing an arbitrary nested row."""
    if not isinstance(value, Mapping):
        return None
    candidates: list[dict[str, Any]] = []
    direct = _direct_subject(value)
    if direct is not None:
        candidates.append(direct)
    for key in ("payload", "entity", "provenance", "verified_provenance", "selected", "claim"):
        child = value.get(key)
        if isinstance(child, Mapping):
            nested = _direct_subject(child)
            if nested is not None:
                candidates.append(nested)
    unique: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        unique[subject_key(candidate)] = candidate
    return next(iter(unique.values())) if len(unique) == 1 else None


_RECEIPT_KIND_KEYS = ("receipt_kind", "output_receipt", "receipt_type")


def _concrete_subject(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProcessRuntimeError(f"{label} subject is not an object")
    subject = {str(key): item for key, item in value.items()}
    if not subject or subject.get("scope") == "lifecycle":
        raise ProcessRuntimeError(f"{label} subject is not concrete")
    if not any(key in subject for key in ("repo", "repository", "issue", "issue_number", "pr", "pr_number", "number", "head_oid", "oid", "sha")):
        raise ProcessRuntimeError(f"{label} subject lacks stable identity")
    return subject


def _receipt_identity_is_valid(
    runtime: ProcessRuntime,
    indexed: Mapping[str, Any],
    *,
    process_id: str,
    receipt_kind: str,
    subject: Mapping[str, Any],
    generation: str,
    candidate_id: str,
    config_sha256: str,
) -> dict[str, Any]:
    if indexed.get("process_id") != process_id or indexed.get("receipt_kind") != receipt_kind:
        raise ProcessRuntimeError("predecessor receipt identity mismatch")
    if indexed.get("status") not in {"written", "exists"}:
        raise ProcessRuntimeError("receipt is not durably written")
    body = indexed.get("payload")
    if not isinstance(body, Mapping) or body.get("verified_readback_state") != "verified":
        raise ProcessRuntimeError("receipt is not verified")
    if body.get("process_id") != process_id or body.get("generation") != generation:
        raise ProcessRuntimeError("receipt generation/process identity mismatch")
    if body.get("candidate_id") != candidate_id or body.get("config_sha256") != config_sha256:
        raise ProcessRuntimeError("receipt candidate/config identity mismatch")
    body_subject = _concrete_subject(body.get("subject"), label="receipt")
    if subject_key(body_subject) != str(indexed.get("subject")) or body_subject != dict(subject):
        raise ProcessRuntimeError("receipt subject identity mismatch")
    digest = str(body.get("content_digest") or "").lower()
    identity = {key: value for key, value in body.items() if key not in {"content_digest", "verified_readback_state"}}
    if len(digest) != 64 or payload_digest(identity) != digest or str(indexed.get("digest")) != digest:
        raise ProcessRuntimeError("receipt content digest mismatch")
    path = indexed.get("path")
    if not path:
        raise ProcessRuntimeError("receipt path missing")
    readback = runtime.read_receipt(path)
    if dict(readback) != dict(body):
        raise ProcessRuntimeError("receipt readback differs from indexed payload")
    return dict(body)


_EXTERNAL_PREDECESSOR_INPUTS = {
    "unresolved cleanup evidence": "unresolved_cleanup_evidence",
}
_CLEANUP_SELECTOR_CURSOR = "cleanup_reconcile/subjects"
_CLEANUP_CURSOR_KEY = _CLEANUP_SELECTOR_CURSOR.replace("/", "__")
_CLEANUP_EXTERNAL_REQUIRED_FIELDS = (
    "repo", "issue", "pr_number", "task_id", "branch", "clone_path",
    "worktree_path", "task_receipt_path", "claim_path", "merge_receipt_path",
    "receipt_path", "db_path", "base_sha", "head_oid", "merge_oid",
    "origin_main_sha", "remote_retention_authorized",
)


def _cleanup_subject_identity(subject: Mapping[str, Any]) -> tuple[str, str] | None:
    repo = subject.get("repo") or subject.get("repository")
    issue = subject.get("issue")
    if issue in (None, ""):
        issue = subject.get("issue_number") or subject.get("number")
    if repo in (None, "") or issue in (None, ""):
        return None
    return str(repo), str(issue)


def _cleanup_cursor_position(value: str | None) -> tuple[str, str] | None:
    if value in (None, ""):
        return None
    try:
        decoded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProcessRuntimeError("cleanup selector cursor is malformed") from exc
    if (
        not isinstance(decoded, list)
        or len(decoded) != 2
        or not all(isinstance(item, str) and item for item in decoded)
    ):
        raise ProcessRuntimeError("cleanup selector cursor is malformed")
    return str(decoded[0]), str(decoded[1])


def _catalog_cursor_key(record: Mapping[str, Any]) -> str:
    """Normalize one catalog cursor into the runtime's safe key component."""
    value = record.get("input_cursor")
    if not isinstance(value, str) or not value.strip():
        raise ProcessRuntimeError("catalog input cursor is missing")
    text = value.strip()
    if "\\" in text:
        raise ProcessRuntimeError("catalog input cursor is unsafe")
    key = text.replace("/", "__")
    if not key or key in {".", ".."}:
        raise ProcessRuntimeError("catalog input cursor is unsafe")
    return key


def _cleanup_cursor_value(row: Mapping[str, Any]) -> str:
    created_at = str(row.get("created_at") or "")
    subject = str(row.get("subject") or "")
    if not created_at or not subject:
        raise ProcessRuntimeError("cleanup selector row identity is malformed")
    return json.dumps([created_at, subject], separators=(",", ":"), ensure_ascii=False)


def _validate_external_input_row(
    runtime: ProcessRuntime,
    row: Mapping[str, Any],
    *,
    contract: ProcessContract,
    input_kind: str,
    generation: str,
    candidate_id: str,
    config_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if row.get("process_id") != contract.process_id or row.get("input_kind") != input_kind:
        raise ProcessRuntimeError("external input identity mismatch")
    if row.get("status") not in {"written", "exists"}:
        raise ProcessRuntimeError("external input is not durably written")
    body = row.get("payload")
    if not isinstance(body, Mapping):
        raise ProcessRuntimeError("external input payload is not an object")
    if body.get("process_id") != contract.process_id or body.get("input_kind") != input_kind:
        raise ProcessRuntimeError("external input identity mismatch")
    if body.get("generation") != generation:
        raise ProcessRuntimeError("external input generation mismatch")
    if body.get("candidate_id") != candidate_id or body.get("config_sha256") != config_sha256:
        raise ProcessRuntimeError("external input candidate/config identity mismatch")
    if body.get("verified_readback_state") != "verified":
        raise ProcessRuntimeError("external input is not verified")
    body_digest = str(body.get("content_digest") or "").lower()
    identity = {
        key: value
        for key, value in body.items()
        if key not in {"content_digest", "verified_readback_state"}
    }
    if _SHA256_RE.fullmatch(body_digest) is None or payload_digest(identity) != body_digest:
        raise ProcessRuntimeError("external input content digest mismatch")
    if str(row.get("digest") or "").lower() != body_digest:
        raise ProcessRuntimeError("external input index digest mismatch")
    path = row.get("path")
    if not path or runtime.read_external_input(path) != dict(body):
        raise ProcessRuntimeError("external input readback differs from index")
    subject = _concrete_subject(body.get("subject"), label="external input")
    if subject_key(subject) != str(row.get("subject")):
        raise ProcessRuntimeError("external input subject index mismatch")
    payload = body.get("payload")
    if not isinstance(payload, Mapping):
        raise ProcessRuntimeError("external input payload is not an object")
    missing = [
        field for field in _CLEANUP_EXTERNAL_REQUIRED_FIELDS
        if payload.get(field) in (None, "")
    ]
    if missing or payload.get("remote_retention_authorized") is not True:
        raise ProcessRuntimeError(
            f"external cleanup evidence missing fields: {missing or ['remote_retention_authorized']}"
        )
    top_level_repo = payload.get("repo") or payload.get("repository")
    top_level_issue = payload.get("issue")
    if top_level_issue in (None, ""):
        top_level_issue = payload.get("issue_number") or payload.get("number")
    external_identity = _cleanup_subject_identity(subject)
    if top_level_repo in (None, "") or top_level_issue in (None, "") or external_identity is None:
        raise ProcessRuntimeError("external cleanup evidence subject mismatch")
    identities = [(top_level_repo, top_level_issue)]
    payload_subject = payload.get("subject")
    if payload_subject is not None:
        if not isinstance(payload_subject, Mapping):
            raise ProcessRuntimeError("external cleanup evidence subject mismatch")
        nested_identity = _cleanup_subject_identity(payload_subject)
        if nested_identity is None:
            raise ProcessRuntimeError("external cleanup evidence subject mismatch")
        identities.append(nested_identity)
    for repo_value, issue_value in identities:
        if (str(repo_value), str(issue_value)) != external_identity:
            raise ProcessRuntimeError("external cleanup evidence subject mismatch")
    external = {
        "process_id": contract.process_id,
        "input_kind": input_kind,
        "digest": body_digest,
        "path": str(path),
        "payload": dict(payload),
        "body": dict(body),
        "created_at": str(row.get("created_at") or ""),
    }
    return subject, external


def _select_cleanup_external_input(
    runtime: ProcessRuntime,
    *,
    contract: ProcessContract,
    generation: str,
    candidate_id: str,
    config_sha256: str,
    cursor_key: str = _CLEANUP_CURSOR_KEY,
) -> dict[str, Any]:
    input_kind = _EXTERNAL_PREDECESSOR_INPUTS["unresolved cleanup evidence"]
    cursor = runtime.read_cursor(contract.process_id, cursor_key)
    cursor_position = _cleanup_cursor_position(cursor.value if cursor else None)
    candidates: list[tuple[tuple[str, str], Mapping[str, Any], dict[str, Any], dict[str, Any]]] = []
    for row in runtime.list_external_inputs(
        process_id=contract.process_id, input_kind=input_kind
    ):
        try:
            cursor_value = _cleanup_cursor_value(row)
            position = _cleanup_cursor_position(cursor_value)
            assert position is not None
            if cursor_position is not None and position <= cursor_position:
                continue
            subject, external = _validate_external_input_row(
                runtime,
                row,
                contract=contract,
                input_kind=input_kind,
                generation=generation,
                candidate_id=candidate_id,
                config_sha256=config_sha256,
            )
            if _cleanup_subject_identity(subject) is None:
                raise ProcessRuntimeError("external cleanup evidence subject mismatch")
            candidates.append((position, row, subject, external))
        except (AssertionError, ProcessRuntimeError, OSError, ValueError, TypeError):
            continue
    if not candidates:
        raise ProcessRuntimeError("external predecessor evidence is missing or invalid")
    selected = min(candidates, key=lambda item: item[0])
    selected_identity = _cleanup_subject_identity(selected[2])
    if selected_identity is None:
        raise ProcessRuntimeError("external cleanup evidence subject mismatch")
    if sum(_cleanup_subject_identity(item[2]) == selected_identity for item in candidates) > 1:
        raise ProcessRuntimeError("external predecessor evidence is ambiguous")
    return {
        "row": dict(selected[1]),
        "subject": selected[2],
        "external": selected[3],
        "cursor_key": cursor_key,
        "cursor_value": json.dumps(list(selected[0]), separators=(",", ":"), ensure_ascii=False),
    }


def _resolve_external_predecessor(
    runtime: ProcessRuntime,
    *,
    contract: ProcessContract,
    predecessor_kind: str,
    generation: str,
    candidate_id: str,
    config_sha256: str,
    selected: Mapping[str, Any] | None = None,
    cursor_key: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    input_kind = _EXTERNAL_PREDECESSOR_INPUTS.get(predecessor_kind)
    if input_kind is None:
        raise ProcessRuntimeError(f"unknown external predecessor {predecessor_kind!r}")
    selection = dict(selected) if selected is not None else _select_cleanup_external_input(
        runtime,
        contract=contract,
        generation=generation,
        candidate_id=candidate_id,
        config_sha256=config_sha256,
        cursor_key=cursor_key or _CLEANUP_CURSOR_KEY,
    )
    subject = selection.get("subject")
    external = selection.get("external")
    if not isinstance(subject, Mapping) or not isinstance(external, Mapping):
        raise ProcessRuntimeError("external predecessor evidence is malformed")
    evidence = {
        "groups": [[predecessor_kind]],
        "required_inputs": list(contract.required_inputs),
        "receipts": {},
        "unresolved_cleanup_evidence": dict(external["payload"]),
        "external_inputs": {input_kind: dict(external)},
        "cursor_key": str(selection.get("cursor_key") or _CLEANUP_CURSOR_KEY),
        "cursor_value": str(selection.get("cursor_value") or ""),
    }
    for field in _CLEANUP_EXTERNAL_REQUIRED_FIELDS:
        evidence[field] = external["payload"][field]
    return dict(subject), evidence


def _resolve_predecessor_evidence(
    runtime: ProcessRuntime,
    *,
    contract: ProcessContract,
    generation: str,
    candidate_id: str,
    config_sha256: str,
    cursor_key: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not contract.predecessor_groups:
        return {"process_id": contract.process_id, "scope": "poll"}, {}
    producers = {
        receipt: producer
        for producer, producer_contract in PROCESS_GRAPH_CONTRACT.items()
        for receipt in producer_contract["output_receipts"]
    }
    subject_matches: dict[str, list[tuple[tuple[str, ...], dict[str, Any]]]] = {}
    for group in contract.predecessor_groups:
        if len(group) == 1 and group[0] in _EXTERNAL_PREDECESSOR_INPUTS:
            external_subject, external_evidence = _resolve_external_predecessor(
                runtime,
                contract=contract,
                predecessor_kind=group[0],
                generation=generation,
                candidate_id=candidate_id,
                config_sha256=config_sha256,
                cursor_key=cursor_key,
            )
            return external_subject, external_evidence
        by_subject: dict[str, dict[str, Any]] = {}
        for kind in group:
            producer = producers.get(kind)
            if not producer:
                raise ProcessRuntimeError(f"no producer for predecessor receipt {kind!r}")
            for row in runtime.list_indexed_receipts(process_id=producer, receipt_kind=kind):
                body = row.get("payload")
                if not isinstance(body, Mapping):
                    continue
                try:
                    subject = _concrete_subject(body.get("subject"), label=f"predecessor {kind}")
                    valid = _receipt_identity_is_valid(
                        runtime, row, process_id=producer, receipt_kind=kind,
                        subject=subject, generation=generation,
                        candidate_id=candidate_id, config_sha256=config_sha256,
                    )
                except ProcessRuntimeError:
                    continue
                key = subject_key(subject)
                entry = by_subject.setdefault(key, {"subject": subject, "receipts": {}})
                entry["receipts"][kind] = {
                    "process_id": producer,
                    "receipt_kind": kind,
                    "digest": valid["content_digest"],
                    "payload": valid,
                }
        complete = [entry for entry in by_subject.values() if set(entry["receipts"]) == set(group)]
        if len(complete) > 1:
            raise ProcessRuntimeError("predecessor receipt handoff is ambiguous")
        if complete:
            entry = complete[0]
            subject_matches.setdefault(subject_key(entry["subject"]), []).append((tuple(group), entry))
    if len(subject_matches) != 1:
        raise ProcessRuntimeError("predecessor receipt handoff is missing, malformed, or ambiguous")
    alternatives = next(iter(subject_matches.values()))
    group, selected = alternatives[0]
    evidence = dict(selected)
    evidence["groups"] = [list(group)]
    evidence["required_inputs"] = list(contract.required_inputs)
    return dict(selected["subject"]), evidence


def _split_receipt_candidates(values: Mapping[str, Any], kinds: set[str]) -> list[tuple[str, Mapping[str, Any], Mapping[str, Any] | None, str | None]]:
    if values.get("status") != "split_verified" or values.get("action") != "split":
        return []
    parent = _subject_from_payload(values)
    if parent is None:
        return []
    children = values.get("children")
    if not isinstance(children, list) or not children:
        return []
    found: list[tuple[str, Mapping[str, Any], Mapping[str, Any] | None, str | None]] = []
    action = "split"
    if "split" in kinds:
        found.append(("split", dict(values), parent, action))
    if "split_verified" in kinds:
        found.append(("split_verified", dict(values), parent, action))
    if "child_handoff" in kinds:
        for child in children:
            if not isinstance(child, Mapping):
                return []
            child_payload = {
                "parent_subject": dict(parent),
                "child": dict(child),
                "repo": parent.get("repo"),
                "number": child.get("number"),
                "kind": child.get("kind"),
                "marker": child.get("marker"),
                "source": "split_mixed_triage_issue",
            }
            child_subject = _subject_from_payload(child_payload)
            if child_subject is None or child_subject == parent:
                return []
            found.append(("child_handoff", child_payload, child_subject, action))
    return found


def _receipt_candidates(
    value: Any,
    kinds: set[str],
    inherited_kind: str | None = None,
    mapped_kinds: tuple[str, ...] = (),
) -> list[tuple[str, Mapping[str, Any], Mapping[str, Any] | None, str | None]]:
    found: list[tuple[str, Mapping[str, Any], Mapping[str, Any] | None, str | None]] = []
    if not isinstance(value, Mapping):
        return found
    values = value.get("values")
    if isinstance(values, Mapping):
        if mapped_kinds and any(kind in {"split", "child_handoff", "split_verified"} for kind in mapped_kinds):
            return _split_receipt_candidates(values, kinds)
        if mapped_kinds == ("repo_poll",):
            results = values.get("repository_results")
            if not isinstance(results, list) or not results:
                found.append(("repo_poll", dict(values), None, None))
            else:
                for item in results:
                    if isinstance(item, Mapping):
                        found.append(("repo_poll", dict(item), _subject_from_payload(item), None))
                    else:
                        found.append(("repo_poll", dict(values), None, None))
            return found
        if mapped_kinds == ("issue_snapshot",):
            rows = values.get("rows")
            if not isinstance(rows, list) or not rows:
                found.append(("issue_snapshot", dict(values), None, None))
            else:
                for row in rows:
                    if isinstance(row, Mapping):
                        found.append(("issue_snapshot", dict(row), _subject_from_payload(row), None))
                    else:
                        found.append(("issue_snapshot", dict(values), None, None))
            return found
        for kind in mapped_kinds:
            if kind in kinds:
                found.append((kind, dict(values), _subject_from_payload(values), values.get("action") if isinstance(values.get("action"), str) else None))
        if mapped_kinds:
            return found
    kind = next((str(value[key]) for key in _RECEIPT_KIND_KEYS if isinstance(value.get(key), str) and value[key] in kinds), inherited_kind)
    payload = value.get("payload") if isinstance(value.get("payload"), Mapping) else value
    subject = value.get("subject") if isinstance(value.get("subject"), Mapping) else _subject_from_payload(payload)
    action = value.get("action") if isinstance(value.get("action"), str) else (payload.get("action") if isinstance(payload, Mapping) and isinstance(payload.get("action"), str) else None)
    if kind is not None and isinstance(payload, Mapping):
        found.append((kind, dict(payload), subject, action))
    for key, child in value.items():
        if key in kinds and isinstance(child, Mapping):
            found.extend(_receipt_candidates(child, kinds, str(key)))
        elif key in {"receipts", "output", "result", "payload"}:
            found.extend(_receipt_candidates(child, kinds))
    return found


def _publish_verified_outputs(
    runtime: ProcessRuntime,
    *,
    host: Any,
    contract: ProcessContract,
    process_id: str,
    leased_subject: Mapping[str, Any],
    generation: str,
    candidate_id: str,
    config_sha256: str,
    predecessor_digests: tuple[str, ...] = (),
    correlation_id: str | None = None,
) -> tuple[list[Mapping[str, Any]], str | None]:
    kinds = set(contract.output_receipts)
    candidates: list[tuple[str, Mapping[str, Any], Mapping[str, Any] | None, str | None]] = []
    for process in host.processes:
        if process.status == "succeeded":
            mapped = _OUTPUT_RECEIPT_EFFECTORS.get(process_id, {}).get(process.effector_id, ())
            candidates.extend(_receipt_candidates(process.output, kinds, mapped_kinds=(mapped,) if isinstance(mapped, str) else tuple(mapped)))
    selected: dict[str, dict[str, tuple[Mapping[str, Any], Mapping[str, Any] | None, str | None]]] = {}
    for kind, payload, output_subject, action in candidates:
        candidate_subject = output_subject or _subject_from_payload(payload)
        key = subject_key(candidate_subject) if isinstance(candidate_subject, Mapping) else f"missing:{len(selected.get(kind, {}))}"
        previous = selected.setdefault(kind, {}).get(key)
        item = (payload, output_subject, action)
        if previous is not None and previous != item:
            raise ProcessRuntimeError(f"conflicting output receipts for {kind}")
        selected[kind][key] = item
    if set(selected) != kinds:
        raise ProcessRuntimeError(f"missing durable output receipts: {sorted(kinds - set(selected))}")
    actions: set[str] = set()
    to_publish: list[tuple[str, Mapping[str, Any], Mapping[str, Any], str | None]] = []
    for kind in contract.output_receipts:
        for payload, output_subject, action in selected[kind].values():
            if payload.get("ok") is False:
                raise ProcessRuntimeError(f"output {kind} is not successful")
            subject_value = output_subject or _subject_from_payload(payload)
            subject = _concrete_subject(subject_value, label=f"output {kind}")
            if contract.predecessor_groups and kind != "child_handoff" and subject != dict(leased_subject):
                raise ProcessRuntimeError("output subject differs from leased predecessor subject")
            if action:
                actions.add(action)
            if action == "add_ready":
                raise ProcessRuntimeError("noncanonical action add_ready")
            to_publish.append((kind, payload, subject, action))
    if len(actions) > 1:
        raise ProcessRuntimeError("contradictory output actions")
    result: list[Mapping[str, Any]] = []
    for kind, payload, subject, _action in to_publish:
        record = runtime.publish_receipt(
            process_id=process_id,
            receipt_kind=kind,
            subject=subject,
            payload=payload,
            generation=generation,
            candidate_id=candidate_id,
            config_sha256=config_sha256,
            correlation_id=correlation_id or host.run_id,
            predecessor_digests=predecessor_digests,
            operation="publish",
            mutation_status=str(payload.get("mutation_status") or "mutated"),
        )
        indexed = runtime.get_indexed_receipt_record(
            process_id=process_id,
            receipt_kind=kind,
            subject=subject,
            digest=record.digest,
        )
        if indexed is None:
            raise ProcessRuntimeError(f"receipt index readback failed for {kind}")
        _receipt_identity_is_valid(
            runtime,
            indexed,
            process_id=process_id,
            receipt_kind=kind,
            subject=subject,
            generation=generation,
            candidate_id=candidate_id,
            config_sha256=config_sha256,
        )
        result.append({"process_id": record.process_id, "receipt_kind": record.receipt_kind, "subject": record.subject, "digest": record.digest, "path": str(record.path), "status": record.status, "verified_readback_state": record.payload.get("verified_readback_state")})
    return result, next(iter(actions), None)



def _candidate_identity(cfg: Any | None) -> str:
    """Return only an explicitly identified immutable deployment candidate."""
    if cfg is not None:
        raw = getattr(cfg, "raw", {})
        if isinstance(raw, dict):
            for key in ("candidate", "candidate_id"):
                value = str(raw.get(key) or "").strip().lower()
                if _CANDIDATE_RE.fullmatch(value):
                    return value
            for key in ("candidate_path", "candidate_id_path"):
                path_value = str(raw.get(key) or "").strip()
                if path_value:
                    loaded = _read_candidate_file(Path(path_value).expanduser())
                    if loaded:
                        return loaded
    for key in ("FALA_CANDIDATE_ID", "HERMES_LOKAY_CANDIDATE_ID", "LOKAY_CANDIDATE_ID"):
        value = os.environ.get(key, "").strip().lower()
        if _CANDIDATE_RE.fullmatch(value):
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
            if _CANDIDATE_RE.fullmatch(value) and parts[index + 2 : index + 5] == (
                "source",
                "project",
                "src",
            ):
                return value
    return ""


def _read_candidate_file(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        value = path.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return ""
    if _CANDIDATE_RE.fullmatch(value):
        return value
    return ""


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
    """Read the durable generation pointer; environment fallback is test-only."""
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


def _config_sha256(config_path: str | Path | None) -> tuple[str, str | None]:
    """Return (sha256, error) for exact --config bytes.

    error is None on success, "missing" when bytes are unavailable, or
    "invalid" when an env SHA is present but malformed/mismatched.
    """
    env_value = (
        os.environ.get("HERMES_LOKAY_CONFIG_SHA256")
        or os.environ.get("LOKAY_CONFIG_SHA256")
        or ""
    ).strip().lower()
    if env_value and not _SHA256_RE.fullmatch(env_value):
        return "", "invalid"

    if config_path is None:
        return "", "missing"
    path = Path(config_path).expanduser()
    if not path.is_file():
        return "", "missing"
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "", "missing"

    if env_value and env_value != digest:
        return "", "invalid"
    return digest, None


def _state_root(db_path: Path) -> Path:
    configured = (
        os.environ.get("HERMES_LOKAY_PROCESS_STATE_ROOT")
        or os.environ.get("LOKAY_PROCESS_STATE_ROOT")
        or ""
    ).strip()
    if configured:
        return Path(configured).expanduser()
    # Match launchd rendering when no explicit environment fence is present.
    return db_path.expanduser().resolve().parent / "process-state"



def _resolve_live_identity(
    *,
    process_id: str,
    record: Mapping[str, Any],
    db_path: Path,
    cfg: Any | None,
    config_path: str | Path | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (identity, error_payload). Exactly one is non-None."""
    command = str(record.get("command") or command_for_id(process_id))
    generation_path = _generation_path()
    generation = _read_generation_value(generation_path)
    candidate_id = _candidate_identity(cfg)
    config_sha, config_error = _config_sha256(config_path)
    state_root = _state_root(db_path)

    if config_error == "invalid":
        return None, _identity_error_payload(
            process_id=process_id,
            command=command,
            record=record,
            dry_run=False,
            db_path=db_path,
            reason="runtime_identity_invalid",
            detail="live adapter refused: config_sha256 is malformed or mismatched",
        )

    missing: list[str] = []
    if not generation:
        missing.append("generation")
    if record.get("candidate_fencing") is True and not candidate_id:
        missing.append("candidate_id")
    if not config_sha or config_error == "missing":
        missing.append("config_sha256")
    if missing:
        return None, _identity_error_payload(
            process_id=process_id,
            command=command,
            record=record,
            dry_run=False,
            db_path=db_path,
            reason="runtime_identity_missing",
            detail=(
                "live adapter refused: missing required runtime identity fields: "
                + ", ".join(missing)
            ),
        )

    if not _SHA256_RE.fullmatch(config_sha):
        return None, _identity_error_payload(
            process_id=process_id,
            command=command,
            record=record,
            dry_run=False,
            db_path=db_path,
            reason="runtime_identity_invalid",
            detail="live adapter refused: config_sha256 must be sha256 hex",
        )

    return (
        {
            "generation": generation,
            "generation_path": generation_path,
            "candidate_id": candidate_id,
            "config_sha256": config_sha,
            "state_root": state_root,
        },
        None,
    )


def _run_live_adapter(
    *,
    process_id: str,
    record: Mapping[str, Any],
    db_path: Path,
    cfg: Any | None,
    config_path: str | Path | None,
    contract: ProcessContract,
) -> dict[str, Any]:
    command = str(record.get("command") or command_for_id(process_id))
    identity, error = _resolve_live_identity(
        process_id=process_id,
        record=record,
        db_path=db_path,
        cfg=cfg,
        config_path=config_path,
    )
    if error is not None:
        return error
    assert identity is not None

    if contract.path_id != process_id or contract.path_id in FORBIDDEN_PATH_ALIASES:
        return _identity_error_payload(
            process_id=process_id,
            command=command,
            record=record,
            dry_run=False,
            db_path=db_path,
            reason="runtime_identity_invalid",
            detail=(
                "live adapter refused: contract path_id must equal process_id and "
                f"must not be an aggregate alias: {contract.path_id!r}"
            ),
        )

    package_path = _resolve_package_path(cfg)
    if not package_path.is_file():
        return _identity_error_payload(
            process_id=process_id,
            command=command,
            record=record,
            dry_run=False,
            db_path=db_path,
            reason="process_unavailable",
            detail=f"live adapter refused: package path missing: {package_path}",
        )

    lease_seconds = int(record.get("lease_seconds") or 120)
    lease_renew_seconds = int(record.get("lease_renew_seconds") or 0)
    stale_after = int(record.get("stale_owner_after_seconds") or max(240, 2 * lease_seconds))
    lock_scope = str(record.get("lock_scope") or contract.lock_scope or process_id)
    generation = str(identity["generation"])
    candidate_id = str(identity["candidate_id"])
    config_sha256 = str(identity["config_sha256"])
    state_root = Path(identity["state_root"])
    generation_path = Path(identity["generation_path"])
    run_id = _run_id(process_id)

    try:
        runtime = ProcessRuntime.open(
            state_root,
            dry_run=False,
            generation_path=generation_path,
            owner=f"lokay-process-{process_id}",
        )
    except ProcessRuntimeError as exc:
        return _identity_error_payload(
            process_id=process_id,
            command=command,
            record=record,
            dry_run=False,
            db_path=db_path,
            reason="process_unavailable",
            detail=f"live adapter refused: process runtime unavailable: {exc}",
        )

    try:
        cursor_key = _catalog_cursor_key(record) if process_id == "cleanup_reconcile" else None
        subject, predecessor_evidence = _resolve_predecessor_evidence(
            runtime,
            contract=contract,
            generation=generation,
            candidate_id=candidate_id,
            config_sha256=config_sha256,
            cursor_key=cursor_key,
        )
        predecessor_digests_list: list[str] = []
        groups = predecessor_evidence.get("groups")
        receipts = predecessor_evidence.get("receipts")
        external_inputs = predecessor_evidence.get("external_inputs")
        if groups:
            if not isinstance(groups, list) or not isinstance(receipts, Mapping):
                raise ProcessRuntimeError("resolved predecessor evidence is malformed")
            for group in groups:
                if not isinstance(group, list):
                    raise ProcessRuntimeError("resolved predecessor group is malformed")
                for kind in group:
                    item = receipts.get(kind)
                    if item is None and isinstance(external_inputs, Mapping):
                        external_kind = _EXTERNAL_PREDECESSOR_INPUTS.get(str(kind))
                        if external_kind is not None:
                            item = external_inputs.get(external_kind)
                    digest = item.get("digest") if isinstance(item, Mapping) else None
                    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
                        raise ProcessRuntimeError(f"resolved predecessor digest is invalid for {kind!r}")
                    predecessor_digests_list.append(digest)
        predecessor_digests = tuple(predecessor_digests_list)
    except (ProcessRuntimeError, ValueError, TypeError) as exc:
        return _identity_error_payload(
            process_id=process_id,
            command=command,
            record=record,
            dry_run=False,
            db_path=db_path,
            reason="predecessor_receipt_unavailable",
            detail=f"live adapter refused predecessor evidence: {exc}",
        )
    published_receipts: list[Mapping[str, Any]] = []
    selected_action: str | None = None

    def _invoke(_lease: Any) -> Any:
        nonlocal published_receipts, selected_action
        # Import only on the live path so catalog dry-run validation stays free of
        # flow/host side effects.
        from lokay.flows.runtime import RuntimeFacadeError, run_package_path_async

        effective = build_effective_run(
            contract=contract,
            process_id=process_id,
            run_id=run_id,
            db_path=db_path,
            cfg=cfg,
            generation=generation,
            candidate_id=candidate_id,
            config_sha256=config_sha256,
            command=command,
            subject=subject,
            predecessor_evidence=predecessor_evidence,
        )
        try:
            host = asyncio.run(
                run_package_path_async(
                    db_path=db_path,
                    package_path=package_path,
                    path_id=contract.path_id,
                    run_id=run_id,
                    inputs=effective["inputs"],
                    effector_inputs=effective["effector_inputs"],
                    effector_configs=effective["effector_configs"],
                    allowed_effectors=list(effective["allowed_effectors"]),
                    max_ticks=int(effective["max_ticks"]),
                    worker_id=f"lokay-process-{process_id}",
                    command_overrides=None,
                    run_metadata={
                        "mode": "live",
                        "process_id": process_id,
                        "path_id": contract.path_id,
                        "command": command,
                        "generation": generation,
                        "candidate_id": candidate_id,
                        "config_sha256": config_sha256,
                        "required_inputs": list(contract.required_inputs),
                        "predecessor_groups": [
                            list(group) for group in contract.predecessor_groups
                        ],
                        "output_receipts": list(contract.output_receipts),
                    },
                )
            )
        except RuntimeFacadeError as exc:
            raise _LiveAdapterFailure(
                _host_failure_payload(
                    process_id=process_id,
                    command=command,
                    record=record,
                    db_path=db_path,
                    run_id=run_id,
                    reason="process_unavailable",
                    detail=f"live adapter refused: Fala host/journal boundary: {exc}",
                )
            ) from exc
        except (OSError, ValueError, TypeError, ImportError, ModuleNotFoundError) as exc:
            raise _LiveAdapterFailure(
                _host_failure_payload(
                    process_id=process_id,
                    command=command,
                    record=record,
                    db_path=db_path,
                    run_id=run_id,
                    reason="process_unavailable",
                    detail=f"live adapter refused: Fala invocation failed: {exc}",
                )
            ) from exc

        if host.path_id != process_id or host.path_id != contract.path_id:
            raise _LiveAdapterFailure(
                _host_failure_payload(
                    process_id=process_id,
                    command=command,
                    record=record,
                    db_path=db_path,
                    run_id=run_id,
                    reason="process_path_mismatch",
                    detail=(
                        "live adapter refused: durable path_id "
                        f"{host.path_id!r} does not match process {process_id!r}"
                    ),
                    host=host,
                )
            )
        unexpected = [
            process
            for process in host.processes
            if process.correlation_path_id
            and process.correlation_path_id != process_id
        ]
        if unexpected:
            raise _LiveAdapterFailure(
                _host_failure_payload(
                    process_id=process_id,
                    command=command,
                    record=record,
                    db_path=db_path,
                    run_id=run_id,
                    reason="process_ownership_invalid",
                    detail=(
                        "live adapter refused: durable process rows include foreign "
                        f"path ownership: {[item.correlation_path_id for item in unexpected]!r}"
                    ),
                    host=host,
                )
            )
        foreign_effectors = [
            process.effector_id or process.step_id
            for process in host.processes
            if (process.effector_id or process.step_id)
            and (process.effector_id or process.step_id) not in contract.allowed_effectors
        ]
        if foreign_effectors:
            raise _LiveAdapterFailure(
                _host_failure_payload(
                    process_id=process_id,
                    command=command,
                    record=record,
                    db_path=db_path,
                    run_id=run_id,
                    reason="process_ownership_invalid",
                    detail=(
                        "live adapter refused: durable process rows include effectors "
                        f"outside contract.allowed_effectors: {foreign_effectors!r}"
                    ),
                    host=host,
                )
            )
        sibling_hits = [
            process.effector_id or process.step_id
            for process in host.processes
            if (process.effector_id or process.step_id)
            in contract.forbidden_sibling_effectors
        ]
        if sibling_hits:
            raise _LiveAdapterFailure(
                _host_failure_payload(
                    process_id=process_id,
                    command=command,
                    record=record,
                    db_path=db_path,
                    run_id=run_id,
                    reason="process_ownership_invalid",
                    detail=(
                        "live adapter refused: durable process rows include forbidden "
                        f"sibling effectors: {sibling_hits!r}"
                    ),
                    host=host,
                )
            )

        if host.run_status in _TERMINAL_FAILURES or host.failed:
            raise _LiveAdapterFailure(
                _host_failure_payload(
                    process_id=process_id,
                    command=command,
                    record=record,
                    db_path=db_path,
                    run_id=run_id,
                    reason="failed",
                    detail=(
                        "live adapter refused: durable Fala run status "
                        f"{host.run_status!r} is not successful"
                    ),
                    host=host,
                )
            )
        if host.run_status in _WAITING_STATUSES or host.waiting:
            raise _LiveAdapterFailure(
                _host_failure_payload(
                    process_id=process_id,
                    command=command,
                    record=record,
                    db_path=db_path,
                    run_id=run_id,
                    reason="waiting",
                    detail=(
                        "live adapter refused: durable Fala run is non-terminal "
                        f"({host.run_status!r})"
                    ),
                    host=host,
                )
            )
        if host.run_status != "completed":
            raise _LiveAdapterFailure(
                _host_failure_payload(
                    process_id=process_id,
                    command=command,
                    record=record,
                    db_path=db_path,
                    run_id=run_id,
                    reason="process_unavailable",
                    detail=(
                        "live adapter refused: unexpected durable Fala run status "
                        f"{host.run_status!r}"
                    ),
                    host=host,
                )
            )
        try:
            receipt_correlation_id = None
            if process_id == "cleanup_reconcile":
                receipt_correlation_id = payload_digest(
                    {
                        "process_id": process_id,
                        "subject": subject_key(subject),
                        "generation": generation,
                        "candidate_id": candidate_id,
                        "config_sha256": config_sha256,
                        "predecessor_digests": list(predecessor_digests),
                    }
                )
            published_receipts, selected_action = _publish_verified_outputs(
                runtime,
                host=host,
                contract=contract,
                process_id=process_id,
                leased_subject=subject,
                generation=generation,
                candidate_id=candidate_id,
                config_sha256=config_sha256,
                predecessor_digests=predecessor_digests,
                correlation_id=receipt_correlation_id,
            )
            if process_id == "cleanup_reconcile":
                cursor_key = predecessor_evidence.get("cursor_key")
                cursor_value = predecessor_evidence.get("cursor_value")
                if not isinstance(cursor_key, str) or not cursor_key:
                    raise ProcessRuntimeError("cleanup selector cursor key is missing")
                if not isinstance(cursor_value, str) or not cursor_value:
                    raise ProcessRuntimeError("cleanup selector cursor value is missing")
                cleanup_receipts = [
                    receipt
                    for receipt in published_receipts
                    if receipt.get("receipt_kind") == "cleanup_reconciliation"
                    and receipt.get("subject") == subject_key(subject)
                ]
                if len(cleanup_receipts) != 1:
                    raise ProcessRuntimeError(
                        "cleanup reconciliation receipt is missing or ambiguous"
                    )
                cleanup_receipt = cleanup_receipts[0]
                receipt_digest = cleanup_receipt.get("digest")
                receipt_path = cleanup_receipt.get("path")
                if not isinstance(receipt_digest, str) or not isinstance(receipt_path, str):
                    raise ProcessRuntimeError("cleanup reconciliation receipt identity is invalid")
                runtime.advance_cursor(
                    process_id=process_id,
                    cursor_key=cursor_key,
                    value=cursor_value,
                    receipt_digest=receipt_digest,
                    receipt_path=receipt_path,
                )
        except (ProcessRuntimeError, ReceiptConflictError, OSError, ValueError, TypeError) as exc:
            raise _LiveAdapterFailure(
                _host_failure_payload(
                    process_id=process_id,
                    command=command,
                    record=record,
                    db_path=db_path,
                    run_id=run_id,
                    reason="receipt_gate_failed",
                    detail=f"live adapter refused unverified output receipts: {exc}",
                    host=host,
                )
            ) from exc
        return host

    try:
        host = runtime.run_fenced(
            process_id=process_id,
            enabled=bool(record.get("enabled") is True),
            generation=generation,
            subject=subject,
            lease_seconds=lease_seconds,
            stale_owner_after_seconds=stale_after,
            lease_renew_seconds=lease_renew_seconds,
            lock_scope=lock_scope,
            candidate_id=candidate_id,
            config_sha256=config_sha256,
            callback=_invoke,
        )
    except _LiveAdapterFailure as exc:
        return exc.payload
    except ProcessDisabledError as exc:
        return _identity_error_payload(
            process_id=process_id,
            command=command,
            record=record,
            dry_run=False,
            db_path=db_path,
            reason="process_disabled",
            detail=str(exc),
        )
    except FenceError as exc:
        return _identity_error_payload(
            process_id=process_id,
            command=command,
            record=record,
            dry_run=False,
            db_path=db_path,
            reason="generation_fence_rejected",
            detail=str(exc),
        )
    except LeaseError as exc:
        return _identity_error_payload(
            process_id=process_id,
            command=command,
            record=record,
            dry_run=False,
            db_path=db_path,
            reason="process_lease_unavailable",
            detail=str(exc),
        )
    except (ProcessRuntimeError, OSError, ValueError, ImportError, ModuleNotFoundError) as exc:
        # Flow/callback/runtime boundary unavailable after identity/fence path.
        return _identity_error_payload(
            process_id=process_id,
            command=command,
            record=record,
            dry_run=False,
            db_path=db_path,
            reason="process_unavailable",
            detail=str(exc),
        )

    health = runtime.read_health(process_id)
    if health is None or str(health.status) != "ok":
        health_status = "missing" if health is None else str(health.status)
        return _identity_error_payload(
            process_id=process_id,
            command=command,
            record=record,
            dry_run=False,
            db_path=db_path,
            reason="process_health_invalid",
            detail=(
                "live adapter refused: durable health status is "
                f"{health_status!r} after fenced Fala invocation"
            ),
        )
    return _success_payload(
        process_id=process_id,
        command=command,
        record=record,
        dry_run=False,
        db_path=db_path,
        generation=generation,
        candidate_id=candidate_id,
        config_sha256=config_sha256,
        state_root=state_root,
        health_status=str(health.status),
        host=host,
        package_path=package_path,
        contract=contract,
        receipts=published_receipts,
        action=selected_action,
    )



def _bind_adapter(contract: ProcessContract) -> ProcessAdapter:
    """Return a named adapter bound to one explicit process contract."""

    process_id = contract.process_id
    if contract.path_id != process_id:
        raise ProcessError(
            f"contract path_id must equal process_id: {contract.path_id!r} != {process_id!r}"
        )
    if contract.path_id in FORBIDDEN_PATH_ALIASES:
        raise ProcessError(f"forbidden aggregate path alias: {contract.path_id}")

    def adapter(
        record: Mapping[str, Any],
        db_path: Path,
        dry_run: bool,
        *,
        cfg: Any | None = None,
        config_path: str | Path | None = None,
    ) -> dict[str, Any]:
        command = str(record.get("command") or command_for_id(process_id))
        if dry_run:
            return _planned_payload(
                process_id=process_id,
                command=command,
                record=record,
                dry_run=True,
                db_path=db_path,
                contract=contract,
            )
        context = _RUNTIME_CONTEXT or {}
        return _run_live_adapter(
            process_id=process_id,
            record=record,
            db_path=db_path,
            cfg=cfg if cfg is not None else context.get("cfg"),
            config_path=config_path if config_path is not None else context.get("config_path"),
            contract=contract,
        )

    adapter.__name__ = f"adapter_{process_id}"
    adapter.__qualname__ = f"adapter_{process_id}"
    adapter.contract = contract  # type: ignore[attr-defined]
    return adapter


# Explicit one-adapter-per-process registration. Values are independent callables
# bound to immutable contracts, never aliases to tick_all/auto_worker/issue_intake.
PROCESS_ADAPTERS: dict[str, ProcessAdapter] = {
    "repo_issue_poll": _bind_adapter(PROCESS_CONTRACTS["repo_issue_poll"]),
    "issue_triage": _bind_adapter(PROCESS_CONTRACTS["issue_triage"]),
    "issue_feedback": _bind_adapter(PROCESS_CONTRACTS["issue_feedback"]),
    "issue_split": _bind_adapter(PROCESS_CONTRACTS["issue_split"]),
    "issue_close": _bind_adapter(PROCESS_CONTRACTS["issue_close"]),
    "issue_ready": _bind_adapter(PROCESS_CONTRACTS["issue_ready"]),
    "issue_to_pr": _bind_adapter(PROCESS_CONTRACTS["issue_to_pr"]),
    "pr_triage": _bind_adapter(PROCESS_CONTRACTS["pr_triage"]),
    "pr_repair": _bind_adapter(PROCESS_CONTRACTS["pr_repair"]),
    "pr_merge": _bind_adapter(PROCESS_CONTRACTS["pr_merge"]),
    "cleanup": _bind_adapter(PROCESS_CONTRACTS["cleanup"]),
    "cleanup_reconcile": _bind_adapter(PROCESS_CONTRACTS["cleanup_reconcile"]),
}

if tuple(PROCESS_ADAPTERS) != PROCESS_IDS:
    raise RuntimeError(
        "PROCESS_ADAPTERS keys must match PROCESS_IDS order exactly: "
        f"got {tuple(PROCESS_ADAPTERS)!r}, expected {PROCESS_IDS!r}"
    )


def resolve_dry_run(args: argparse.Namespace) -> bool | int:
    """Return dry_run bool, or 2 if --dry-run and --live conflict."""
    if getattr(args, "dry_run", False) and getattr(args, "live", False):
        print("error: --dry-run and --live are mutually exclusive", file=sys.stderr)
        return 2
    if getattr(args, "live", False):
        return False
    return True


def validate_command_identity(command: str) -> str:
    """Return the process id encoded by an exact catalog command string."""
    process_id = process_id_from_command(command)
    if process_id is None:
        raise ProcessError(f"invalid process command identity: {command!r}")
    if process_id not in PROCESS_IDS:
        raise ProcessError(f"unknown process command identity: {command}")
    if command != command_for_id(process_id):
        raise ProcessError(f"mismatched process command identity: {command}")
    return process_id


def _coherent_with_defaults(record: Mapping[str, Any], process_id: str) -> None:
    """Fail closed when launch identity fields are stale or mismatched."""
    if process_id not in PROCESS_IDS:
        raise ProcessError(f"unknown process id: {process_id}")
    for key in _LAUNCH_IDENTITY_FIELDS:
        if key not in record:
            raise ProcessError(f"process {process_id} missing launch field {key}")
    if str(record["id"]) != process_id:
        raise ProcessError(f"process id mismatch: catalog has {record['id']!r}")
    expected_command = command_for_id(process_id)
    if str(record["command"]) != expected_command:
        raise ProcessError(
            f"process {process_id} command must be {expected_command!r}"
        )
    if "launchd_label" in record:
        raise ProcessError(
            f"process {process_id} must not declare launchd_label in production identity"
        )
    if record.get("candidate_fencing") is not True:
        raise ProcessError(f"process {process_id} candidate_fencing must be true")
    if str(record.get("health_id") or "") != process_id:
        raise ProcessError(f"process {process_id} health_id must equal process id")
    if record.get("concurrency") != 1:
        raise ProcessError(f"process {process_id} concurrency must be 1")
    interval = record.get("interval_seconds")
    if not isinstance(interval, int) or isinstance(interval, bool) or interval < 30:
        raise ProcessError(
            f"process {process_id} interval_seconds must be an integer >= 30"
        )


def resolve_catalog_process(cfg: Any, command: str) -> dict[str, Any]:
    """Map a command identity to exactly one enabled, coherent catalog record."""
    process_id = validate_command_identity(command)
    processes = list(getattr(cfg, "processes", ()) or ())
    matches = [
        dict(item)
        for item in processes
        if isinstance(item, Mapping) and str(item.get("command") or "") == command
    ]
    if not matches:
        # Fall back to id match so mismatched command fields fail with a clear reason.
        by_id = [
            dict(item)
            for item in processes
            if isinstance(item, Mapping) and str(item.get("id") or "") == process_id
        ]
        if not by_id:
            raise ProcessError(f"unknown process command: {command}")
        raise ProcessError(
            f"process {process_id} command identity is stale or mismatched"
        )
    if len(matches) != 1:
        raise ProcessError(f"duplicate process command identity: {command}")
    record = matches[0]
    if str(record.get("id") or "") != process_id:
        raise ProcessError(
            f"process command {command} maps to id {record.get('id')!r}, expected {process_id!r}"
        )
    if record.get("enabled") is not True:
        raise ProcessError(f"process {process_id} is disabled")
    _coherent_with_defaults(record, process_id)
    if process_id not in PROCESS_ADAPTERS:
        raise ProcessError(f"no adapter registered for process {process_id}")
    return record


def validate_live_mode(cfg: Any, *, dry_run: bool) -> None:
    """Fail closed when live activation is requested against a non-live config."""
    if dry_run:
        return
    mode = str(getattr(cfg, "mode", "") or "").strip().lower()
    if mode != "live":
        raise ProcessError(
            f"live activation refused: config mode is {mode!r}, expected 'live'"
        )


def run_process(
    *,
    command: str,
    config_path: str | Path | None,
    db_path: str | Path,
    dry_run: bool,
) -> dict[str, Any]:
    """Validate catalog identity then invoke the named process adapter."""
    # Config/catalog validation first — adapters (and any future flow imports)
    # run only after identity is proven coherent.
    global _RUNTIME_CONTEXT
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        raise ProcessError(str(exc)) from exc
    record = resolve_catalog_process(cfg, command)
    validate_live_mode(cfg, dry_run=dry_run)
    process_id = str(record["id"])
    adapter = PROCESS_ADAPTERS[process_id]
    db = Path(db_path).expanduser()
    # Do not create db parents here: validation must remain free of side effects
    # for fail-closed paths; adapters open process-state only after identity checks.
    previous = _RUNTIME_CONTEXT
    _RUNTIME_CONTEXT = {"cfg": cfg, "config_path": config_path, "db_path": db}
    try:
        return adapter(record, db, dry_run, cfg=cfg, config_path=config_path)
    finally:
        _RUNTIME_CONTEXT = previous


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lokay.process",
        description=(
            "Catalog-validated single-process dispatcher. "
            "Command identity must be exactly lokay-process-<id>."
        ),
    )
    parser.add_argument(
        "command",
        help="Exact catalog command identity (lokay-process-<id>)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to canonical config.toml (required for dispatch)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Fala SQLite path (required for dispatch)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Force dry-run (no mutations; default when --live is absent)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Allow mutations (requires config mode=live)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print full JSON result",
    )
    return parser


def _print_result(result: Mapping[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(dict(result), indent=2, sort_keys=True, default=str))
        return
    print(f"run_id={result.get('run_id', '')}")
    print(f"process_id={result.get('process_id', '')}")
    print(f"command={result.get('command', '')}")
    print(
        f"dry_run={result.get('dry_run')} status={result.get('status')} "
        f"stopped={result.get('stopped_reason')}"
    )
    print(f"summary={json.dumps(result.get('summary') or {}, default=str)}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    dry = resolve_dry_run(args)
    if dry == 2:
        return 2
    missing = [flag for flag, value in (("--config", args.config), ("--db", args.db)) if not value]
    if missing:
        parser.error(f"the following arguments are required: {', '.join(missing)}")
    try:
        result = run_process(
            command=str(args.command),
            config_path=args.config,
            db_path=args.db,
            dry_run=bool(dry),
        )
    except ProcessError as exc:
        payload = _error_payload(
            command=str(args.command),
            process_id=process_id_from_command(str(args.command)) or "",
            reason="process_identity_invalid",
            dry_run=bool(dry),
            detail=str(exc),
        )
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    _print_result(result, as_json=args.json)
    if result.get("ok") is False or result.get("status") in {"failed", "cancelled", "timed_out"}:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
