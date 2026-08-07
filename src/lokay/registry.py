from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib  # type: ignore


class ConfigError(ValueError):
    """Raised when a canonical Lokay configuration is unsafe or invalid."""


REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PROCESS_IDS = (
    "repo_issue_poll",
    "issue_triage",
    "issue_feedback",
    "issue_split",
    "issue_close",
    "issue_ready",
    "issue_to_pr",
    "pr_triage",
    "pr_repair",
    "pr_merge",
    "cleanup",
    "cleanup_reconcile",
)

# The TOML `predecessors` array is an ordered, flattened identity-bearing view
# of these groups. Groups are alternatives (OR); entries within one group are
# required together (AND). `unresolved cleanup evidence` is deliberately an
# external trigger/state, not a receipt emitted by another catalog process.
PROCESS_GRAPH_CONTRACT: dict[str, dict[str, Any]] = {
    "repo_issue_poll": {
        "output_receipts": ("repo_poll", "issue_snapshot"),
        "predecessor_groups": (),
        "successors": ("issue_triage",),
    },
    "issue_triage": {
        "output_receipts": ("issue_decision",),
        "predecessor_groups": (("issue_snapshot",), ("child_handoff",)),
        "successors": ("issue_feedback", "issue_split", "issue_close", "issue_ready"),
    },
    "issue_feedback": {
        "output_receipts": ("feedback", "feedback_verified"),
        "predecessor_groups": (("issue_decision",),),
        "successors": (),
    },
    "issue_split": {
        "output_receipts": ("split", "child_handoff", "split_verified"),
        "predecessor_groups": (("issue_decision",),),
        "successors": ("issue_triage", "issue_close"),
    },
    "issue_close": {
        "output_receipts": ("close_authorization", "close_verified"),
        "predecessor_groups": (("issue_decision",), ("split_verified",)),
        "successors": (),
    },
    "issue_ready": {
        "output_receipts": ("claim", "task_handoff"),
        "predecessor_groups": (("issue_decision",),),
        "successors": ("issue_to_pr",),
    },
    "issue_to_pr": {
        "output_receipts": ("implementation", "pr_opened"),
        "predecessor_groups": (("claim", "task_handoff"),),
        "successors": ("pr_triage",),
    },
    "pr_triage": {
        "output_receipts": ("pr_decision",),
        "predecessor_groups": (("pr_opened",), ("repair_verified",)),
        "successors": ("pr_repair", "pr_merge"),
    },
    "pr_repair": {
        "output_receipts": ("repair_reservation", "repair_verified"),
        "predecessor_groups": (("pr_decision",),),
        "successors": ("pr_triage",),
    },
    "pr_merge": {
        "output_receipts": ("merge_verified", "finalization"),
        "predecessor_groups": (("pr_decision",),),
        "successors": ("cleanup",),
    },
    "cleanup": {
        "output_receipts": ("cleanup_verified",),
        "predecessor_groups": (("finalization",),),
        "successors": (),
    },
    "cleanup_reconcile": {
        "output_receipts": ("cleanup_reconciliation",),
        "predecessor_groups": (("unresolved cleanup evidence",),),
        "successors": (),
    },
}

# Fixed home used for portable canonicalization regardless of runner HOME.
FIXED_HOME = Path("/Users/mini-m4-main")

# Exact live inventory enforced only by migrate_config / root-canonical checks.
LIVE_REPO_INVENTORY: tuple[tuple[str, str, str, int], ...] = (
    ("mikolaj92/Fala", "mikolaj92-fala", "~/Developer/hermes-repos/Fala-live", 100),
    ("mikolaj92/datasource-kit", "mikolaj92-datasource-kit", "~/Developer/hermes-repos/datasource-kit-live", 90),
    ("mikolaj92/reviewkit", "mikolaj92-reviewkit", "~/Developer/hermes-repos/reviewkit-live", 80),
    ("mikolaj92/msds-portal", "mikolaj92-msds-portal", "~/Developer/hermes-repos/msds-portal-live", 50),
    ("mikolaj92/app-factory", "mikolaj92-app-factory", "~/Developer/hermes-repos/app-factory-live", 45),
    ("mikolaj92/basecoat-factory", "mikolaj92-basecoat-factory", "~/Developer/hermes-repos/basecoat-factory-live", 40),
    ("mikolaj92/my-auth", "mikolaj92-my-auth", "~/Developer/hermes-repos/my-auth-live", 30),
    ("mikolaj92/my-usermanager", "mikolaj92-my-usermanager", "~/Developer/hermes-repos/my-usermanager-live", 30),
    ("mikolaj92/Posejdon", "mikolaj92-posejdon", "~/Developer/hermes-repos/Posejdon-live", 25),
    ("mikolaj92/lokay", "mikolaj92-lokay", "~/Developer/hermes-repos/lokay-live", 20),
    ("mikolaj92/influenzer", "mikolaj92-influenzer", "~/Developer/hermes-repos/influenzer-live", 20),
    ("mikolaj92/wolnyrolnik", "mikolaj92-wolnyrolnik", "~/Developer/hermes-repos/wolnyrolnik-live", 18),
    ("mikolaj92/Temida", "mikolaj92-temida", "~/Developer/hermes-repos/Temida-repo-agent-live", 15),
    ("mikolaj92/emitype", "mikolaj92-emitype", "~/Developer/hermes-repos/emitype-live", 15),
    ("mikolaj92/rnkstr", "mikolaj92-rnkstr", "~/Developer/hermes-repos/rnkstr-live", 15),
)

TOP_LEVEL_FIELDS = {
    "version",
    "mode",
    "branch_prefix",
    "base_branch",
    "github",
    "labels",
    "automation",
    "direction",
    "triage",
    "executor",
    "paths",
    "repos",
    "processes",
}
SECTION_FIELDS: dict[str, set[str]] = {
    "github": {"cli", "default_limit", "assignee"},
    "labels": {
        "ready",
        "in_progress",
        "blocked",
        "pr_opened",
        "generated",
        "needs_feedback",
        "duplicate",
        "out_of_scope",
        "frozen",
    },
    "automation": {
        "max_active_issues",
        "automerge",
        "require_human_approval",
        "require_checks",
        "require_test_evidence",
        "fixer_assignee",
        "merge_method",
    },
    "direction": {
        "repo_goal",
        "require_keywords",
        "deny_keywords",
        "reject_labels",
        "min_goal_overlap",
    },
    "triage": {
        "enabled",
        "context_paths",
        "context_max_bytes",
        "auto_close_duplicates",
        "auto_close_out_of_scope",
    },
    "executor": {
        "enabled",
        "command",
        "model",
        "thinking",
        "timeout_seconds",
        "max_attempts",
        "retry_backoff_seconds",
    },
    "paths": {
        "worktree_root",
        "dispatch_receipts",
        "task_receipts",
        "merge_receipts",
        "active_issue",
        "triage_receipts",
    },
}
REPO_FIELDS = {
    "repo",
    "board",
    "clone_path",
    "priority",
    "triage_goal",
    "triage_context_paths",
    "auto_close_duplicates",
    "auto_close_out_of_scope",
    "trusted_authors",
    "trusted_branch_prefixes",
    "allowed_base_branches",
    "external_pr_policy",
}
PROCESS_FIELDS = {
    "id",
    "enabled",
    "interval_seconds",
    "concurrency",
    "selector",
    "lease_seconds",
    "lease_renew_seconds",
    "stale_owner_after_seconds",
    "lock_scope",
    "retry_classes",
    "max_attempts",
    "backoff_seconds",
    "input_cursor",
    "output_receipts",
    "health_id",
    "candidate_fencing",
    "predecessors",
    "successors",
    "command",
}
RECOVERY_FIELDS = {
    "attempt_recovery": {
        "run_id",
        "process_id",
        "candidate",
        "path_id",
        "effector_id",
        "repo",
        "pr_number",
        "verified_head",
    },
    "repair_creation_recovery": {
        "run_id",
        "process_id",
        "candidate",
        "path_id",
        "effector_id",
    },
}
RETIRED_CONTENT_ENV = {
    "HERMES_LOKAY_REPOS_FILE",
    "HERMES_LOKAY_MODE",
    "HERMES_LOKAY_ASSIGNEE",
    "HERMES_LOKAY_KANBAN_INTAKE_ASSIGNEE",
    "HERMES_LOKAY_LABEL_READY",
    "HERMES_LOKAY_LABEL_IN_PROGRESS",
    "HERMES_LOKAY_LABEL_BLOCKED",
    "HERMES_LOKAY_LABEL_PR_OPENED",
    "HERMES_LOKAY_LABEL_GENERATED",
    "HERMES_LOKAY_GOAL",
    "HERMES_LOKAY_WORKTREE_ROOT",
    "HERMES_LOKAY_RECEIPT_DIR",
    "HERMES_LOKAY_TASK_RECEIPT_DIR",
    "HERMES_LOKAY_MERGE_RECEIPT_DIR",
    "HERMES_LOKAY_ACTIVE_ISSUE_DIR",
}

# Schema-safe generic defaults for incomplete documents / load-time fills.
# assignee is "" (optional empty string), never None.
DEFAULTS: dict[str, Any] = {
    "version": 1,
    "mode": "dry-run",
    "branch_prefix": "ai/fix",
    "base_branch": "main",
    "github": {"cli": "gh", "default_limit": 10, "assignee": ""},
    "labels": {
        "ready": "ai:ready",
        "in_progress": "ai:in-progress",
        "blocked": "ai:blocked",
        "pr_opened": "ai:pr-opened",
        "generated": "ai:generated",
        "needs_feedback": "ai:needs-feedback",
        "duplicate": "duplicate",
        "out_of_scope": "ai:out-of-scope",
        "frozen": "frozen",
    },
    "automation": {
        "max_active_issues": 1,
        "automerge": False,
        "require_human_approval": True,
        "require_checks": True,
        "require_test_evidence": True,
        "fixer_assignee": "lokay-fixer",
        "merge_method": "merge",
    },
    "direction": {
        "repo_goal": "",
        "require_keywords": [],
        "deny_keywords": [],
        "reject_labels": ["ai:out-of-scope", "wontfix", "invalid"],
        "min_goal_overlap": 1,
    },
    "triage": {
        "enabled": True,
        "context_paths": ["README.md"],
        "context_max_bytes": 131072,
        "auto_close_duplicates": False,
        "auto_close_out_of_scope": True,
    },
    "executor": {
        "enabled": False,
        "command": "omp",
        "model": "omniroute/omp/default",
        "thinking": "medium",
        "timeout_seconds": 7200,
        "max_attempts": 5,
        "retry_backoff_seconds": 60,
    },
    "paths": {
        "worktree_root": "~/.hermes/worktrees/lokay",
        "dispatch_receipts": "~/.hermes/state/lokay-dispatch",
        "task_receipts": "~/.hermes/state/lokay-receipts",
        "merge_receipts": "~/.hermes/state/lokay-merge",
        "active_issue": "~/.hermes/state/lokay-active",
        "triage_receipts": "~/.hermes/state/lokay-triage",
    },
}

# Plan-locked fills for migrate_config only (automatic live system).
MIGRATION_DEFAULTS: dict[str, Any] = {
    "version": 1,
    "mode": "live",
    "branch_prefix": "ai/fix",
    "base_branch": "main",
    "github": {"cli": "gh", "default_limit": 10, "assignee": ""},
    "labels": {
        "ready": "ai:ready",
        "in_progress": "ai:in-progress",
        "blocked": "ai:blocked",
        "pr_opened": "ai:pr-opened",
        "generated": "ai:generated",
        "needs_feedback": "ai:needs-feedback",
        "duplicate": "duplicate",
        "out_of_scope": "ai:out-of-scope",
        "frozen": "frozen",
    },
    "automation": {
        "max_active_issues": 1,
        "automerge": True,
        "require_human_approval": False,
        "require_checks": True,
        "require_test_evidence": True,
        "fixer_assignee": "lokay-fixer",
        "merge_method": "merge",
    },
    "direction": {
        "repo_goal": "",
        "require_keywords": [],
        "deny_keywords": [],
        "reject_labels": ["ai:out-of-scope", "wontfix", "invalid"],
        "min_goal_overlap": 1,
    },
    "triage": {
        "enabled": True,
        "context_paths": ["README.md"],
        "context_max_bytes": 131072,
        "auto_close_duplicates": True,
        "auto_close_out_of_scope": True,
    },
    "executor": {
        "enabled": True,
        "command": "omp",
        "model": "omniroute/omp/default",
        "thinking": "medium",
        "timeout_seconds": 7200,
        "max_attempts": 5,
        "retry_backoff_seconds": 60,
    },
    "paths": {
        "worktree_root": "~/.hermes/worktrees/lokay",
        "dispatch_receipts": "~/.hermes/state/lokay-dispatch-live",
        "task_receipts": "~/.hermes/state/lokay-receipts-live",
        "merge_receipts": "~/.hermes/state/lokay-merge-live",
        "active_issue": "~/.hermes/state/lokay-active-live",
        "triage_receipts": "~/.hermes/state/lokay-triage-live",
    },
}


def _fail(path: str, message: str) -> ConfigError:
    return ConfigError(f"{path}: {message}")


def _table(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(path, "must be a TOML table")
    return dict(value)


def _keys(value: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _fail(path, f"unknown key(s): {', '.join(unknown)}")


def _str(value: Any, path: str, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _fail(path, "must be a string")
    if not empty and not value.strip():
        raise _fail(path, "must not be empty")
    if value != value.strip():
        raise _fail(path, "must not have leading or trailing whitespace")
    return value


def _bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(path, "must be a boolean")
    return value


def _int(value: Any, path: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(path, "must be an integer")
    if minimum is not None and value < minimum:
        raise _fail(path, f"must be at least {minimum}")
    return value


def _number(
    value: Any,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(path, "must be a number")
    result = float(value)
    if minimum is not None and result <= minimum:
        raise _fail(path, f"must be greater than {minimum}")
    if maximum is not None and result > maximum:
        raise _fail(path, f"must be at most {maximum}")
    return result


def _strings(value: Any, path: str, *, empty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise _fail(path, "must be an array")
    result = [_str(item, f"{path}[{index}]", empty=empty) for index, item in enumerate(value)]
    if len(result) != len(set(result)):
        raise _fail(path, "must not contain duplicates")
    return result


def _reject_parent_components(text: str, path: str) -> None:
    if any(component == ".." for component in text.split("/")):
        raise _fail(path, "must not contain '..' path components")

def _path(value: Any, path: str, *, allow_absolute: bool = False) -> str:
    """Validate a portable path, with legacy absolutes restricted to migration."""
    text = _str(value, path)
    _reject_parent_components(text, path)
    if "\x00" in text:
        raise _fail(path, "must be rooted at ~")
    if text.startswith("~/"):
        expanded = Path(text).expanduser()
    elif allow_absolute and text.startswith("/"):
        expanded = Path(text)
    else:
        raise _fail(path, "must use a portable path rooted at ~")

    lexical = Path(os.path.abspath(expanded))
    home = Path.home()
    if home.is_symlink():
        raise _fail(path, "home directory must not be a symlink")
    home_lexical = Path(os.path.abspath(home))
    if allow_absolute:
        if text.startswith("~/"):
            root = home_lexical
            cursor = home.resolve()
        else:
            root = Path(lexical.anchor)
            cursor = root
        try:
            relative = lexical.relative_to(root)
        except ValueError as exc:
            raise _fail(path, "must remain rooted in the home directory") from exc
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                raise _fail(path, "must not traverse symlinked paths")
        return str(lexical)

    try:
        relative = lexical.relative_to(home_lexical)
    except ValueError as exc:
        raise _fail(path, "must remain rooted in the home directory") from exc
    cursor = home.resolve()
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise _fail(path, "must not traverse symlinked paths")
    return str(lexical)


def _portable_path(value: Any, path: str) -> str:
    """Validate and rewrite home-rooted absolute paths to portable ~/ form."""
    text = _str(value, path)
    _reject_parent_components(text, path)
    if "\x00" in text:
        raise _fail(path, "must be an absolute path or a path rooted at ~")
    if text.startswith("~/"):
        _path(text, path)
        return text
    if not text.startswith("/"):
        raise _fail(path, "must be an absolute path or a path rooted at ~")
    absolute = Path(os.path.abspath(text))
    if absolute.is_symlink():
        raise _fail(path, "must not be a symlink")
    for home in (FIXED_HOME, Path.home()):
        home = Path(os.path.abspath(home))
        try:
            relative = absolute.relative_to(home)
        except ValueError:
            continue
        cursor = home
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                raise _fail(path, "must not traverse symlinked paths")
        return f"~/{relative.as_posix()}"
    raise _fail(path, "absolute path must be rooted in the current home for migration")


def _validate_section(name: str, value: Any, *, allow_absolute_paths: bool = False) -> None:
    data = _table(value, name)
    _keys(data, SECTION_FIELDS[name], name)
    for key, item in data.items():
        path = f"{name}.{key}"
        if name == "github":
            if key in {"cli", "assignee"}:
                _str(item, path, empty=key == "assignee")
            else:
                _int(item, path, minimum=1)
        elif name == "labels":
            _str(item, path)
        elif name == "automation":
            if key == "max_active_issues":
                _int(item, path, minimum=1)
            elif key in {
                "automerge",
                "require_human_approval",
                "require_checks",
                "require_test_evidence",
            }:
                _bool(item, path)
            elif key == "merge_method":
                _str(item, path)
                if item not in {"merge", "squash", "rebase"}:
                    raise _fail(path, "must be merge, squash, or rebase")
            else:
                _str(item, path)
        elif name == "direction":
            if key == "repo_goal":
                _str(item, path, empty=True)
            elif key in {"require_keywords", "deny_keywords", "reject_labels"}:
                _strings(item, path)
            else:
                _int(item, path, minimum=1)
        elif name == "triage":
            if key in {"enabled", "auto_close_duplicates", "auto_close_out_of_scope"}:
                _bool(item, path)
            elif key == "context_paths":
                for index, relative in enumerate(_strings(item, path)):
                    if relative.startswith("/") or ".." in Path(relative).parts:
                        raise _fail(
                            f"{path}[{index}]",
                            "must be repository-relative and must not escape",
                        )
            else:
                _int(item, path, minimum=1)
        elif name == "executor":
            if key == "enabled":
                _bool(item, path)
            elif key == "timeout_seconds":
                _number(item, path, minimum=0, maximum=7200)
            elif key in {"max_attempts", "retry_backoff_seconds"}:
                _int(item, path, minimum=1)
            else:
                _str(item, path)
        elif name == "paths":
            _path(item, path, allow_absolute=allow_absolute_paths)


def _validate_repo(value: Any, index: int, *, allow_absolute_paths: bool = False) -> None:
    path = f"repos[{index}]"
    data = _table(value, path)
    _keys(data, REPO_FIELDS, path)
    for key in {"repo", "board", "clone_path", "priority"}:
        if key not in data:
            raise _fail(path, f"missing required key: {key}")
    repo = _str(data["repo"], f"{path}.repo")
    if not REPO_RE.fullmatch(repo):
        raise _fail(f"{path}.repo", "must use owner/repository form")
    _str(data["board"], f"{path}.board")
    _path(data["clone_path"], f"{path}.clone_path", allow_absolute=allow_absolute_paths)
    _int(data["priority"], f"{path}.priority")
    if "triage_goal" in data:
        _str(data["triage_goal"], f"{path}.triage_goal", empty=True)
    for key in {
        "triage_context_paths",
        "trusted_authors",
        "trusted_branch_prefixes",
        "allowed_base_branches",
    }:
        if key in data:
            _strings(data[key], f"{path}.{key}")
    for key in {"auto_close_duplicates", "auto_close_out_of_scope"}:
        if key in data:
            _bool(data[key], f"{path}.{key}")
    if "external_pr_policy" in data and data["external_pr_policy"] not in {
        "block",
        "report",
        "ignore",
    }:
        raise _fail(f"{path}.external_pr_policy", "must be block, report, or ignore")


def _validate_process(value: Any, index: int, *, enforce_graph: bool = False) -> None:
    path = f"processes[{index}]"
    data = _table(value, path)
    _keys(data, PROCESS_FIELDS, path)
    missing = sorted(PROCESS_FIELDS - set(data))
    if missing:
        raise _fail(path, f"missing required key(s): {', '.join(missing)}")
    process_id = _str(data["id"], f"{path}.id")
    _bool(data["enabled"], f"{path}.enabled")
    interval = _int(data["interval_seconds"], f"{path}.interval_seconds", minimum=30)
    _int(data["concurrency"], f"{path}.concurrency", minimum=1)
    if data["concurrency"] != 1:
        raise _fail(f"{path}.concurrency", "must be 1")
    _str(data["selector"], f"{path}.selector")
    lease = _int(data["lease_seconds"], f"{path}.lease_seconds", minimum=interval)
    _int(data["lease_renew_seconds"], f"{path}.lease_renew_seconds", minimum=1)
    stale = _int(
        data["stale_owner_after_seconds"],
        f"{path}.stale_owner_after_seconds",
        minimum=2 * lease,
    )
    if stale < 2 * lease:
        raise _fail(
            f"{path}.stale_owner_after_seconds",
            "must be at least twice lease_seconds",
        )
    _str(data["lock_scope"], f"{path}.lock_scope")
    if not _strings(data["retry_classes"], f"{path}.retry_classes"):
        raise _fail(f"{path}.retry_classes", "must not be empty")
    _int(data["max_attempts"], f"{path}.max_attempts", minimum=1)
    backoff = data["backoff_seconds"]
    if not isinstance(backoff, list) or not backoff:
        raise _fail(f"{path}.backoff_seconds", "must be a non-empty integer array")
    for number, item in enumerate(backoff):
        _int(item, f"{path}.backoff_seconds[{number}]", minimum=1)
    _str(data["input_cursor"], f"{path}.input_cursor")
    _strings(data["output_receipts"], f"{path}.output_receipts")
    _str(data["health_id"], f"{path}.health_id")
    if not _bool(data["candidate_fencing"], f"{path}.candidate_fencing"):
        raise _fail(f"{path}.candidate_fencing", "must be true")
    _strings(data["predecessors"], f"{path}.predecessors", empty=True)
    _strings(data["successors"], f"{path}.successors", empty=True)
    _str(data["command"], f"{path}.command")
    expected_command = f"lokay-process-{process_id}"
    if data["command"] != expected_command:
        raise _fail(
            f"{path}.command",
            f"must be {expected_command!r}",
        )
    if enforce_graph:
        contract = PROCESS_GRAPH_CONTRACT.get(process_id)
        if contract is None:
            raise _fail(f"{path}.id", f"unknown process {process_id}")
        expected_outputs = list(contract["output_receipts"])
        if data["output_receipts"] != expected_outputs:
            raise _fail(
                f"{path}.output_receipts",
                f"must equal {expected_outputs!r}",
            )
        expected_predecessors = [
            receipt
            for group in contract["predecessor_groups"]
            for receipt in group
        ]
        if data["predecessors"] != expected_predecessors:
            raise _fail(
                f"{path}.predecessors",
                f"must equal the locked predecessor receipt order {expected_predecessors!r}; alternatives are OR and group members are AND",
            )
        expected_successors = list(contract["successors"])
        if data["successors"] != expected_successors:
            raise _fail(
                f"{path}.successors",
                f"must equal {expected_successors!r}",
            )


def _validate_process_graph(processes: list[Any]) -> None:
    by_id = {str(item["id"]): item for item in processes}
    if set(by_id) != set(PROCESS_GRAPH_CONTRACT):
        return
    producers: dict[str, str] = {}
    for process_id, contract in PROCESS_GRAPH_CONTRACT.items():
        for receipt in contract["output_receipts"]:
            previous = producers.setdefault(receipt, process_id)
            if previous != process_id:
                raise _fail("processes", f"receipt {receipt} has multiple producers")
    external = {"unresolved cleanup evidence"}
    for process_id, contract in PROCESS_GRAPH_CONTRACT.items():
        successors = set(contract["successors"])
        for successor in successors:
            if successor not in by_id:
                raise _fail(f"processes[{process_id}].successors", f"unknown process {successor}")
            successor_predecessors = {
                receipt
                for group in PROCESS_GRAPH_CONTRACT[successor]["predecessor_groups"]
                for receipt in group
            }
            if not successor_predecessors.intersection(contract["output_receipts"]):
                raise _fail(
                    f"processes[{process_id}].successors",
                    f"edge to {successor} has no reciprocal predecessor receipt",
                )
        for group in contract["predecessor_groups"]:
            for receipt in group:
                producer = producers.get(receipt)
                if producer is None:
                    if receipt not in external:
                        raise _fail(
                            f"processes[{process_id}].predecessors",
                            f"unknown predecessor receipt {receipt}",
                        )
                    continue
                if process_id not in PROCESS_GRAPH_CONTRACT[producer]["successors"]:
                    raise _fail(
                        f"processes[{process_id}].predecessors",
                        f"receipt {receipt} from {producer} lacks reciprocal successor edge",
                    )


def validate_document(
    data: Mapping[str, Any],
    *,
    require_complete: bool = True,
    allow_recovery: bool = False,
    allow_absolute_paths: bool = False,
) -> dict[str, Any]:
    """Validate raw TOML before defaults, normalization, or activation.

    Generic validation accepts any non-empty repo list when complete. Exact
    15-row live inventory is enforced only by migrate_config / root checks.
    """
    if not isinstance(data, Mapping):
        raise ConfigError("config: root must be a TOML table")
    document = dict(data)
    recovery = sorted(set(document) & set(RECOVERY_FIELDS))
    if recovery and not allow_recovery:
        raise ConfigError(
            "config: recovery table(s) must be migrated before activation: "
            + ", ".join(recovery)
        )
    allowed = TOP_LEVEL_FIELDS | (set(RECOVERY_FIELDS) if allow_recovery else set())
    _keys(document, allowed, "config")
    if require_complete:
        missing = sorted(TOP_LEVEL_FIELDS - set(document))
        if missing:
            raise ConfigError(f"config: missing required key(s): {', '.join(missing)}")
    if "version" in document and _int(document["version"], "version") != 1:
        raise _fail("version", "must be 1")
    if "mode" in document and document["mode"] not in {"dry-run", "live"}:
        raise _fail("mode", "must be dry-run or live")
    if "branch_prefix" in document:
        _str(document["branch_prefix"], "branch_prefix")
    if "base_branch" in document:
        _str(document["base_branch"], "base_branch")
    for name in SECTION_FIELDS:
        if name in document:
            _validate_section(
                name,
                document[name],
                allow_absolute_paths=allow_absolute_paths,
            )
            if require_complete:
                section = _table(document[name], name)
                missing = sorted(SECTION_FIELDS[name] - set(section))
                if missing:
                    raise ConfigError(
                        f"{name}: missing required key(s): {', '.join(missing)}"
                    )
    if "repos" in document:
        if not isinstance(document["repos"], list):
            raise _fail("repos", "must be an array of tables")
        if require_complete and not document["repos"]:
            raise _fail("repos", "must not be empty")
        seen: set[str] = set()
        for index, item in enumerate(document["repos"]):
            _validate_repo(item, index, allow_absolute_paths=allow_absolute_paths)
            repo = str(item["repo"])
            if repo in seen:
                raise _fail(f"repos[{index}].repo", f"duplicate repository {repo}")
            seen.add(repo)
    if "processes" in document:
        if not isinstance(document["processes"], list):
            raise _fail("processes", "must be an array of tables")
        seen_processes: set[str] = set()
        for index, item in enumerate(document["processes"]):
            _validate_process(item, index, enforce_graph=require_complete)
            process_id = str(item["id"])
            if process_id in seen_processes:
                raise _fail(f"processes[{index}].id", f"duplicate process {process_id}")
            seen_processes.add(process_id)
        if require_complete and (
            len(document["processes"]) != len(PROCESS_IDS)
            or seen_processes != set(PROCESS_IDS)
        ):
            missing = sorted(set(PROCESS_IDS) - seen_processes)
            extra = sorted(seen_processes - set(PROCESS_IDS))
            detail = (
                f"missing {', '.join(missing)}"
                if missing
                else f"unknown {', '.join(extra)}"
            )
            raise _fail(
                "processes",
                f"catalog must contain exactly twelve process IDs ({detail})",
            )
        if require_complete:
            declared_order = [str(item["id"]) for item in document["processes"]]
            if declared_order != list(PROCESS_IDS):
                raise _fail(
                    "processes",
                    f"catalog must declare process IDs in canonical order: {list(PROCESS_IDS)!r}",
                )
        if require_complete:
            _validate_process_graph(document["processes"])
    if allow_recovery:
        for name in recovery:
            validate_recovery(name, document[name])
    return document


def validate_recovery(name: str, value: Any) -> dict[str, Any]:
    data = _table(value, name)
    _keys(data, RECOVERY_FIELDS[name], name)
    missing = sorted(RECOVERY_FIELDS[name] - set(data))
    if missing:
        raise _fail(name, f"missing required key(s): {', '.join(missing)}")
    for key, item in data.items():
        if key == "pr_number":
            _int(item, f"{name}.{key}", minimum=1)
        elif key == "repo":
            repo = _str(item, f"{name}.{key}")
            if not REPO_RE.fullmatch(repo):
                raise _fail(f"{name}.{key}", "must use owner/repository form")
        else:
            _str(item, f"{name}.{key}")
    return data


def validate_live_inventory(repos: list[Mapping[str, Any]]) -> None:
    """Fail closed unless repos match the exact live 15-row inventory/order."""
    if len(repos) != len(LIVE_REPO_INVENTORY):
        raise ConfigError(
            f"repos: live migration requires exactly {len(LIVE_REPO_INVENTORY)} "
            f"repositories, got {len(repos)}"
        )
    for index, (repo, board, clone_path, priority) in enumerate(LIVE_REPO_INVENTORY):
        item = repos[index]
        path = f"repos[{index}]"
        actual_repo = str(item.get("repo", ""))
        actual_board = str(item.get("board", ""))
        actual_clone = _portable_path(item.get("clone_path", ""), f"{path}.clone_path")
        try:
            actual_priority = int(item.get("priority"))  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise _fail(f"{path}.priority", "must be an integer") from exc
        if actual_repo != repo:
            raise _fail(f"{path}.repo", f"expected {repo}, got {actual_repo}")
        if actual_board != board:
            raise _fail(f"{path}.board", f"expected {board}, got {actual_board}")
        if actual_clone != clone_path:
            raise _fail(
                f"{path}.clone_path",
                f"expected {clone_path}, got {actual_clone}",
            )
        if actual_priority != priority:
            raise _fail(
                f"{path}.priority",
                f"expected {priority}, got {actual_priority}",
            )


def _deep_merge_defaults(
    data: Mapping[str, Any],
    defaults: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    base = defaults if defaults is not None else DEFAULTS
    result = json.loads(json.dumps(base))
    for key, value in data.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = {**result[key], **dict(value)}
        else:
            result[key] = value
    return result


def _read(path: Path) -> tuple[bytes, dict[str, Any]]:
    if path.suffix.lower() != ".toml":
        raise ConfigError(f"config must use .toml extension: {path}")
    if not path.exists():
        raise ConfigError(f"config not found: {path}")
    if path.is_symlink() or not path.is_file():
        raise ConfigError(f"config must be a regular non-symlink file: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"unable to read config {path}: {exc}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"invalid TOML config {path}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ConfigError("config: root must be a TOML table")
    return raw, dict(data)


def _check_env(env: Mapping[str, str] | None) -> None:
    values = os.environ if env is None else env
    retired = sorted(name for name in RETIRED_CONTENT_ENV if name in values)
    if retired:
        raise ConfigError(
            "retired configuration environment variable(s): " + ", ".join(retired)
        )


class RegistryDocument:
    def __init__(
        self,
        path: Path,
        raw_bytes: bytes,
        raw: dict[str, Any],
        data: dict[str, Any],
    ):
        self.path = path
        self.raw_bytes = raw_bytes
        self.raw = raw
        self.data = data

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw_bytes).hexdigest()

    @property
    def repos(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.data["repos"])

    @property
    def processes(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.data["processes"])

    def shell_rows(self) -> list[str]:
        return [
            f"{r['repo']}|{r['board']}|{Path(r['clone_path']).expanduser().absolute()}|{r['priority']}"
            for r in self.repos
        ]

    def process(self, process_id: str) -> dict[str, Any]:
        for item in self.processes:
            if item["id"] == process_id:
                return dict(item)
        raise ConfigError(f"unknown process id: {process_id}")


def load_registry(
    path: str | os.PathLike[str],
    *,
    env: Mapping[str, str] | None = None,
) -> RegistryDocument:
    _check_env(env)
    config_path = Path(path).expanduser()
    raw_bytes, raw = _read(config_path)
    # Activation consumes only a fully materialized canonical document. Do
    # not policy-fill a present-but-incomplete section with dry-run defaults.
    validated = validate_document(raw, require_complete=True)
    return RegistryDocument(config_path, raw_bytes, validated, validated)


def _stage_file(parent: Path, name: str, content: bytes, *, mode: int = 0o600) -> Path:
    """Write a durable staging file in its destination filesystem."""
    fd, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=str(parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        return Path(temporary)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _ensure_parents(parents: list[Path]) -> list[Path]:
    """Create destination parents after conflict checks and report new dirs."""
    missing: set[Path] = set()
    for parent in set(parents):
        cursor = parent
        pending: list[Path] = []
        while not cursor.exists() and not cursor.is_symlink():
            pending.append(cursor)
            cursor = cursor.parent
        if cursor.is_symlink() or (cursor.exists() and not cursor.is_dir()):
            raise ConfigError(f"destination parent is not a directory: {cursor}")
        missing.update(pending)
    for parent in sorted(missing, key=lambda item: len(item.parts)):
        parent.mkdir()
    return sorted(missing, key=lambda item: len(item.parts), reverse=True)


def _remove_empty_dirs(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.rmdir()
        except OSError:
            pass

def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ConfigError(f"cannot serialize TOML value of type {type(value).__name__}")


def canonical_toml(data: Mapping[str, Any]) -> bytes:
    """Serialize the normalized registry deterministically for migration output."""
    lines: list[str] = []
    scalar_order = ["version", "mode", "branch_prefix", "base_branch"]
    for key in scalar_order:
        if key in data:
            lines.append(f"{key} = {_toml_value(data[key])}")
    for section in (
        "github",
        "labels",
        "automation",
        "direction",
        "triage",
        "executor",
        "paths",
    ):
        lines.append("")
        lines.append(f"[{section}]")
        for key, value in data[section].items():
            lines.append(f"{key} = {_toml_value(value)}")
    for repo in data["repos"]:
        lines.append("")
        lines.append("[[repos]]")
        for key, value in repo.items():
            lines.append(f"{key} = {_toml_value(value)}")
    for process in data["processes"]:
        lines.append("")
        lines.append("[[processes]]")
        for key, value in process.items():
            lines.append(f"{key} = {_toml_value(value)}")
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _normalize_portable_document(data: Mapping[str, Any]) -> dict[str, Any]:
    """Copy document with home-rooted paths rewritten to portable ~/ form."""
    result = json.loads(json.dumps(data))
    paths = result.get("paths")
    if isinstance(paths, dict):
        for key, value in list(paths.items()):
            paths[key] = _portable_path(value, f"paths.{key}")
    repos = result.get("repos")
    if isinstance(repos, list):
        for index, item in enumerate(repos):
            if isinstance(item, dict) and "clone_path" in item:
                item["clone_path"] = _portable_path(
                    item["clone_path"],
                    f"repos[{index}].clone_path",
                )
    return result


def _strip_legacy_process_identity_fields(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Drop pre-supervisor launchd labels before validating migration input."""
    document = json.loads(json.dumps(raw))
    processes = document.get("processes")
    if isinstance(processes, list):
        for process in processes:
            if isinstance(process, dict):
                process.pop("launchd_label", None)
    return document


def _prepare_migration_document(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate raw live TOML, fill only absent fields, enforce live inventory."""
    raw = _strip_legacy_process_identity_fields(raw)
    validate_document(
        raw,
        require_complete=False,
        allow_recovery=True,
        allow_absolute_paths=True,
    )
    if "repos" not in raw:
        raise ConfigError("repos: live migration requires a top-level repos array")
    if not isinstance(raw["repos"], list) or not raw["repos"]:
        raise ConfigError("repos: live migration requires a non-empty repos array")

    recovery_names = [name for name in RECOVERY_FIELDS if name in raw]
    records = {name: validate_recovery(name, raw[name]) for name in recovery_names}
    if recovery_names and set(recovery_names) != set(RECOVERY_FIELDS):
        missing = sorted(set(RECOVERY_FIELDS) - set(recovery_names))
        raise ConfigError("recovery state is incomplete; missing: " + ", ".join(missing))
    paths = raw.get("paths")
    task_receipts = paths.get("task_receipts") if isinstance(paths, Mapping) else None
    recovery_dir = Path(str(task_receipts)).expanduser() / "recovery" if task_receipts else None
    discovered: dict[str, dict[str, Any]] = {}
    if recovery_dir is not None:
        if os.path.lexists(recovery_dir) and (recovery_dir.is_symlink() or not recovery_dir.is_dir()):
            raise ConfigError(f"recovery directory must be a non-symlink directory: {recovery_dir}")
        recovery_paths = {name: recovery_dir / f"{name}.json" for name in RECOVERY_FIELDS}
        present = [path for path in recovery_paths.values() if os.path.lexists(path)]
        if present and len(present) != len(recovery_paths):
            missing = sorted(name for name, path in recovery_paths.items() if not os.path.lexists(path))
            raise ConfigError("recovery state is incomplete; missing: " + ", ".join(missing))
        for name, fields in RECOVERY_FIELDS.items():
            path = recovery_paths[name]
            if not os.path.lexists(path):
                continue
            if path.is_symlink() or not path.is_file():
                raise ConfigError(f"recovery state must be a regular non-symlink file: {path}")
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ConfigError(f"invalid recovery state {path}: {exc}") from exc
            if not isinstance(payload, Mapping):
                raise ConfigError(f"recovery state must be a JSON object: {path}")
            _keys(payload, set(fields) | {"schema_version", "kind"}, str(path))
            schema_version = payload.get("schema_version")
            if isinstance(schema_version, bool) or schema_version != 1 or payload.get("kind") != name:
                raise ConfigError(f"invalid recovery state identity: {path}")
            discovered[name] = validate_recovery(name, {key: payload.get(key) for key in fields})
    if discovered:
        if recovery_names:
            for name in RECOVERY_FIELDS:
                if records[name] != discovered[name]:
                    raise ConfigError(f"embedded and external recovery state conflict: {name}")
        else:
            recovery_names = list(discovered)
            records = discovered
    base = dict(raw)
    for name in RECOVERY_FIELDS:
        base.pop(name, None)
    # Presence-sensitive merge: only absent keys receive plan-locked defaults.
    normalized = _deep_merge_defaults(base, MIGRATION_DEFAULTS)
    normalized["repos"] = [dict(item) for item in base["repos"]]
    normalized["processes"] = process_defaults()
    normalized = _normalize_portable_document(normalized)
    validate_live_inventory(normalized["repos"])
    validate_document(normalized)
    return {
        "normalized": normalized,
        "recovery_names": recovery_names,
        "records": records,
    }


def _commit_replacements(planned: list[tuple[Path, Path]]) -> None:
    """Atomically promote staged files with all-or-nothing rollback.

    Each pair is (staged_source, destination). Destinations that already exist
    with identical bytes are left untouched. Any failure after the first new
    create rolls back every destination created by this commit.
    """
    created: list[Path] = []
    try:
        for staged, destination in planned:
            staged_bytes = staged.read_bytes()
            if destination.exists():
                if destination.is_symlink() or destination.read_bytes() != staged_bytes:
                    raise ConfigError(f"destination conflict: {destination}")
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, destination)
            created.append(destination)
    except Exception:
        for path in reversed(created):
            try:
                path.unlink()
            except OSError:
                pass
        raise


def migrate_config(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    recovery_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Stage a legacy live TOML, recovery records, and canonical root together.

    The source is never changed. All raw input, recovery values, and merged
    defaults are validated before any destination or recovery file is created.
    Config and recovery promotions share one commit with rollback on failure.
    """
    source_path = Path(source).expanduser()
    destination_path = Path(destination).expanduser()
    raw_bytes, raw = _read(source_path)
    prepared = _prepare_migration_document(raw)
    normalized = prepared["normalized"]
    recovery_names: list[str] = prepared["recovery_names"]
    records: dict[str, dict[str, Any]] = prepared["records"]
    canonical = canonical_toml(normalized)

    if recovery_root is not None:
        target_recovery = Path(recovery_root).expanduser().absolute()
    else:
        target_recovery = (
            Path(str(normalized["paths"]["task_receipts"])).expanduser() / "recovery"
        ).absolute()

    recovery_payloads = {
        name: (
            json.dumps(
                {"schema_version": 1, "kind": name, **record},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        for name, record in records.items()
    }

    # Conflict checks must complete before creating destination parents.
    def present(path: Path) -> bool:
        return path.exists() or path.is_symlink()

    if present(destination_path) and (
        destination_path.is_symlink()
        or not destination_path.is_file()
        or destination_path.read_bytes() != canonical
    ):
        raise ConfigError(f"canonical config conflict: {destination_path}")
    for name, payload in recovery_payloads.items():
        target = target_recovery / f"{name}.json"
        if present(target) and (
            target.is_symlink() or not target.is_file() or target.read_bytes() != payload
        ):
            raise ConfigError(f"recovery state conflict: {target}")

    output_paths = [destination_path] + [
        target_recovery / f"{name}.json" for name in recovery_payloads
    ]
    created_dirs = _ensure_parents([path.parent for path in output_paths])
    staged: list[Path] = []
    try:
        planned: list[tuple[Path, Path]] = []
        if not present(destination_path):
            staged_config = _stage_file(destination_path.parent, destination_path.name, canonical)
            staged.append(staged_config)
            planned.append((staged_config, destination_path))
        for name, payload in recovery_payloads.items():
            target = target_recovery / f"{name}.json"
            if not present(target):
                staged_recovery = _stage_file(target.parent, target.name, payload)
                staged.append(staged_recovery)
                planned.append((staged_recovery, target))
        _commit_replacements(planned)
    finally:
        for temporary in staged:
            try:
                temporary.unlink()
            except OSError:
                pass
        _remove_empty_dirs(created_dirs)

    return {
        "config": str(destination_path),
        "config_sha256": hashlib.sha256(canonical).hexdigest(),
        "recovery_root": str(target_recovery),
        "migrated": recovery_names,
        "source_sha256": hashlib.sha256(raw_bytes).hexdigest(),
    }


def process_defaults() -> list[dict[str, Any]]:
    rows = [
        (
            "repo_issue_poll",
            60,
            120,
            30,
            240,
            "poll/repo",
            "poll/repo/{repo}",
            ["repo_poll", "issue_snapshot"],
            [],
            ["issue_triage"],
        ),
        (
            "issue_triage",
            60,
            180,
            30,
            360,
            "issue/repo/number",
            "triage/decision",
            ["issue_decision"],
            ["issue_snapshot", "child_handoff"],
            ["issue_feedback", "issue_split", "issue_close", "issue_ready"],
        ),
        (
            "issue_feedback",
            60,
            180,
            30,
            360,
            "issue/repo/number",
            "feedback/decision",
            ["feedback", "feedback_verified"],
            ["issue_decision"],
            [],
        ),
        (
            "issue_split",
            60,
            300,
            30,
            600,
            "split/repo/number",
            "split/decision",
            ["split", "child_handoff", "split_verified"],
            ["issue_decision"],
            ["issue_triage", "issue_close"],
        ),
        (
            "issue_close",
            60,
            180,
            30,
            360,
            "issue/repo/number",
            "close/decision",
            ["close_authorization", "close_verified"],
            ["issue_decision", "split_verified"],
            [],
        ),
        (
            "issue_ready",
            60,
            180,
            30,
            360,
            "issue/repo/number",
            "ready/decision",
            ["claim", "task_handoff"],
            ["issue_decision"],
            ["issue_to_pr"],
        ),
        (
            "issue_to_pr",
            60,
            7800,
            60,
            15600,
            "task/board/id",
            "dispatch/claim",
            ["implementation", "pr_opened"],
            ["claim", "task_handoff"],
            ["pr_triage"],
        ),
        (
            "pr_triage",
            60,
            180,
            30,
            360,
            "pr/repo/number",
            "pr/decision",
            ["pr_decision"],
            ["pr_opened", "repair_verified"],
            ["pr_repair", "pr_merge"],
        ),
        (
            "pr_repair",
            60,
            7800,
            60,
            15600,
            "repair/repo/number/head",
            "repair/decision",
            ["repair_reservation", "repair_verified"],
            ["pr_decision"],
            ["pr_triage"],
        ),
        (
            "pr_merge",
            60,
            300,
            30,
            600,
            "merge/repo/number/head",
            "merge/decision",
            ["merge_verified", "finalization"],
            ["pr_decision"],
            ["cleanup"],
        ),
        (
            "cleanup",
            60,
            300,
            30,
            600,
            "cleanup/repo/number/head",
            "cleanup/finalization",
            ["cleanup_verified"],
            ["finalization"],
            [],
        ),
        (
            "cleanup_reconcile",
            300,
            300,
            30,
            600,
            "cleanup_reconcile/subject",
            "cleanup/unresolved",
            ["cleanup_reconciliation"],
            ["unresolved cleanup evidence"],
            [],
        ),
    ]
    result = []
    for (
        process_id,
        interval,
        lease,
        renew,
        stale,
        lock,
        cursor,
        receipts,
        predecessors,
        successors,
    ) in rows:
        result.append(
            {
                "id": process_id,
                "enabled": True,
                "interval_seconds": interval,
                "concurrency": 1,
                "selector": "all_repos" if process_id == "repo_issue_poll" else "eligible",
                "lease_seconds": lease,
                "lease_renew_seconds": renew,
                "stale_owner_after_seconds": stale,
                "lock_scope": lock,
                "retry_classes": ["retryable_read", "reconcile_then_retry"],
                "max_attempts": 5,
                "backoff_seconds": [30, 60, 120, 300, 600],
                "input_cursor": cursor,
                "output_receipts": receipts,
                "health_id": process_id,
                "candidate_fencing": True,
                "predecessors": predecessors,
                "successors": successors,
                "command": f"lokay-process-{process_id}",
            }
        )
    return result


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and project canonical Lokay TOML")
    parser.add_argument("--config", required=True)
    parser.add_argument("--format", choices=("json", "shell"), default="json")
    args = parser.parse_args(argv)
    try:
        document = load_registry(args.config)
    except ConfigError as exc:
        print(f"registry-error: {exc}", file=sys.stderr)
        return 2
    if args.format == "shell":
        print("\n".join(document.shell_rows()))
    else:
        print(
            json.dumps(
                {
                    "ok": True,
                    "config": str(document.path),
                    "sha256": document.sha256,
                    "repos": list(document.repos),
                    "processes": list(document.processes),
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
