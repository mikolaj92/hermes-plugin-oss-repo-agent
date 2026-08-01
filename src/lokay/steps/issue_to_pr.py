"""Mega-atomic effectors: issue → PR domain."""

from __future__ import annotations

import json
import fcntl
import hashlib
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from dataclasses import dataclass

from lokay.envelope import Request, Result

from lokay.adapters_cli import CommandError, hermes_kanban_json, run_cmd
from lokay.adapters_git import (
    branch_config_get,
    branch_config_set,
    branch_exists,
    git,
    is_dirty,
    local_branch_head,
    parse_worktree_porcelain,
    push_branch as git_push_branch,
    remote_ref,
    remote_url,
    rev_parse,
    status_porcelain,
    worktree_add,
    worktree_list,
    worktree_remove,
)
from lokay.adapters_omp import run_omp
from lokay.envelope import (
    cfg_of,
    cond_blob,
    cond_get,
    dry_run_flag,
    fail,
    input_of,
    noop,
    ok,
    planned,
    upstream_noop,
)
_PROVENANCE_ALIASES = (
    "triage_build_repair_prompt",
    "triage_decide_triage_action",
    "triage_load_pr_fields",
    "build_repair_prompt",
    "decide_triage_action",
    "load_pr_fields",
    "dispatch_parse_issue_ref",
    "parse_issue_ref_from_task",
    "select_dispatch_task",
    "read_dispatch_tasks",
    "read_fix_tasks",
    "read_clone_preconditions",
    "read_base_ref",
    "read_worktree_inventory",
    "read_branch_provenance",
)

def _conduction_blobs(request: Request, aliases: tuple[str, ...]) -> list[dict[str, Any]]:
    conduction = input_of(request).get("conduction")
    if not isinstance(conduction, dict):
        return []
    blobs: list[dict[str, Any]] = []
    for alias in aliases:
        for name, value in conduction.items():
            if (name == alias or name.endswith(f"_{alias}")) and isinstance(value, dict) and value:
                if value not in blobs:
                    blobs.append(dict(value))
    return blobs






















def parse_issue_ref_from_task(request: Request) -> Result:
    """Pure: extract owner/repo#N and preferred branch from task title/body."""
    data = input_of(request)
    upstream = upstream_noop(request, "select_dispatch_task")
    if upstream:
        return noop(str(upstream.get("reason") or "no_ready_task"))
    task = data.get("task") or cond_get(request, "task", "select_dispatch_task")
    if not task:
        return fail("missing_task", failure_class="terminal", retry_safe=False)
    title = str(task.get("title") or "")
    body = str(task.get("body") or task.get("description") or "")
    m = re.search(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)\b", title)
    repo = issue = None
    if m:
        repo, issue = m.group(1), int(m.group(2))
    else:
        br = re.search(r"^Repository:\s*(\S+)\s*$", body, re.M)
        bi = re.search(r"^Issue:\s*#([0-9]+)\s*$", body, re.M)
        if br and bi:
            repo, issue = br.group(1), int(bi.group(1))
    if not repo or not issue:
        return fail("unparseable_issue_ref", failure_class="terminal", retry_safe=False, title=title)
    loaded = cond_blob(request, "select_dispatch_task")
    configured = _repo_context_for_repo(request, str(repo))
    context = {
        key: loaded.get(key) or configured.get(key)
        for key in ("board", "clone_path", "priority")
        if loaded.get(key) not in (None, "") or configured.get(key) not in (None, "")
    }
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", title.lower())[:40].strip("-")
    branch_prefix = str(cfg_of(request).get("branch_prefix") or "ai/fix")
    branch = f"{branch_prefix}/{issue}-{slug or 'task'}"
    return ok(status="parsed", repo=repo, issue=issue, branch=branch, task_id=task.get("id") or task.get("task_id"), task_title=title, **context)









def _identity_values(
    request: Request,
    keys: tuple[str, ...],
    aliases: tuple[str, ...] = _PROVENANCE_ALIASES,
) -> list[str]:
    """Collect every non-empty identity value available before this effector."""
    values: list[str] = []

    def add(source: Any) -> None:
        if not isinstance(source, dict):
            return
        for key in keys:
            value = source.get(key)
            if key in {"task_id", "task"} and isinstance(value, dict):
                value = value.get("id") or value.get("task_id")
            if value is None or not str(value).strip():
                continue
            normalized = str(value).strip()
            if normalized not in values:
                values.append(normalized)

    add(input_of(request))
    add(cfg_of(request))
    for source in _conduction_blobs(request, aliases):
        add(source)
    return values


def _worktree_provenance(request: Request, branch: str) -> dict[str, str]:
    """Resolve one complete ownership tuple from this path's conduction."""
    task_values = _identity_values(request, ("task_id", "task"))
    issue_values = _identity_values(request, ("issue",))
    receipt_values = _identity_values(request, ("receipt_id", "receipt_path"))
    repo_values = _identity_values(request, ("repo",))
    return {
        "task_id": task_values[0] if len(task_values) == 1 else "",
        "issue": issue_values[0] if len(issue_values) == 1 else "",
        "receipt": receipt_values[0] if len(receipt_values) == 1 else "",
        "repo": repo_values[0] if len(repo_values) == 1 else "",
        "branch": branch,
    }


def _worktree_provenance_error(request: Request, provenance: dict[str, str]) -> dict[str, Any]:
    fields = {
        "task_id": ("task_id", "task"),
        "issue": ("issue",),
        "receipt": ("receipt_id", "receipt_path"),
        "repo": ("repo",),
    }
    missing = [key for key, aliases in fields.items() if not _identity_values(request, aliases)]
    conflicts = {
        key: values
        for key, aliases in fields.items()
        if len(values := _identity_values(request, aliases)) > 1
    }
    if not provenance.get("branch"):
        missing.append("branch")
    return {"missing": missing, "conflicts": conflicts}


def _worktree_branch(request: Request) -> tuple[str, list[str]]:
    """Resolve branch from current input/config and upstream conduction."""
    aliases = (
        "triage_build_repair_prompt", "build_repair_prompt",
        "read_worktree_inventory", "read_branch_provenance",
        "dispatch_parse_issue_ref", "parse_issue_ref", "parse_issue_ref_from_task",
        "load_pr_fields", "triage_load_pr_fields",
    )
    values = _identity_values(request, ("branch",), aliases=aliases)
    for blob in _conduction_blobs(request, aliases):
        pr = blob.get("pr")
        candidates = [pr.get("headRefName")] if isinstance(pr, dict) else []
        candidates.append(blob.get("headRefName"))
        for candidate in candidates:
            value = str(candidate or "").strip()
            if value and value not in values:
                values.append(value)
    return (values[0] if len(values) == 1 else ""), values


def _branch_provenance(clone_path: str, branch: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for key in ("task", "issue", "receipt", "repo", "base", "local-oid"):
        try:
            values[key] = branch_config_get(clone_path, branch, f"lokay-{key}").strip()
        except CommandError:
            values[key] = ""
    return values
def _provenance_matches(expected: dict[str, str], actual: dict[str, str]) -> bool:
    """Accept reuse only when every recorded ownership field matches exactly."""
    return all(
        expected.get(expected_key, "")
        and expected.get(expected_key, "") == actual.get(actual_key, "")
        for expected_key, actual_key in (
            ("task_id", "task"),
            ("issue", "issue"),
            ("receipt", "receipt"),
            ("repo", "repo"),
        )
    )


def _update_receipt_if_needed(clone_path: str, branch: str, expected_receipt: str, actual_receipt: str) -> None:
    if expected_receipt and expected_receipt != actual_receipt:
        branch_config_set(clone_path, branch, "lokay-receipt", expected_receipt)





def _omp_diff_paths(worktree_path: str) -> list[str]:
    """Return changed paths reported by git, including untracked files."""
    status = git(
        ["status", "--porcelain=v1", "--untracked-files=all"], cwd=worktree_path
    )
    paths: list[str] = []
    for line in status.splitlines():
        value = line[3:] if len(line) >= 3 else line
        if " -> " in value:
            value = value.rsplit(" -> ", 1)[-1]
        if value:
            paths.append(value)
    for args in (["diff", "--name-only", "HEAD"], ["diff", "--cached", "--name-only"]):
        paths.extend(p for p in git(args, cwd=worktree_path).splitlines() if p)
    return paths


def _escaped_omp_paths(worktree_path: str, paths: list[str]) -> list[str]:
    root = Path(worktree_path).resolve()
    escaped: list[str] = []
    for value in paths:
        path = Path(value)
        candidate = path.resolve() if path.is_absolute() else (root / path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            escaped.append(value)
    return escaped









_UPSTREAM_TERMINAL = {"failed", "cancelled", "timed_out"}


def _receipt_metadata(request: Request, payload: dict[str, Any], *, entity: dict[str, Any]) -> dict[str, Any]:
    data = input_of(request)
    cfg = cfg_of(request)
    def first(key: str, default: Any = "") -> Any:
        value = data.get(key)
        if value in (None, ""):
            value = cfg.get(key)
        if value in (None, ""):
            value = request.get(key)
        if value in (None, ""):
            value = payload.get(key, default)
        return value
    run_id = first("run_id")
    path_id = first("path_id")
    process_id = first("process_id")
    candidate = first("candidate")
    timestamp = first("timestamp", payload.get("timestamp", "unspecified"))
    if not any((run_id, path_id, process_id, candidate, cfg, entity)):
        return {}
    return {
        "run_id": str(run_id),
        "path_id": str(path_id),
        "process_id": str(process_id),
        "candidate": candidate,
        "config": dict(cfg),
        "entity": dict(entity),
        "timestamp": str(timestamp),
    }






def check_worktree_dirty(request: Request) -> Result:
    """Atomic read: is worktree dirty (status --porcelain non-empty)?"""
    data = input_of(request)
    worktree_path = str(data.get("worktree_path") or "")
    if not worktree_path:
        return fail("missing_worktree_path", failure_class="terminal", retry_safe=False, mutated=False)
    if not Path(worktree_path).exists():
        return fail("worktree_missing", failure_class="terminal", retry_safe=False, worktree_path=worktree_path, mutated=False)
    dirty = is_dirty(worktree_path)
    return ok(status="checked", worktree_path=worktree_path, dirty=dirty)


def list_controlled_worktrees(request: Request) -> Result:
    """List git worktrees under clone; optionally filter by worktree_root prefix."""
    data = input_of(request)
    cfg = cfg_of(request)
    clone_path = str(data.get("clone_path") or "")
    worktree_root = str(data.get("worktree_root") or cfg.get("worktree_root") or "")
    if not clone_path:
        return fail("missing_clone_path", failure_class="terminal", retry_safe=False, mutated=False)
    try:
        text = worktree_list(clone_path)
    except CommandError as exc:
        return fail("worktree_list_failed", failure_class="terminal", retry_safe=False, error=str(exc), mutated=False)
    rows = parse_worktree_porcelain(text)
    if worktree_root:
        root = str(Path(worktree_root).resolve()) if Path(worktree_root).exists() else worktree_root
        filtered = []
        for row in rows:
            p = row.get("path") or ""
            if p == clone_path:
                continue
            if p.startswith(worktree_root) or p.startswith(root):
                filtered.append(row)
        rows = filtered
    return ok(status="listed", clone_path=clone_path, count=len(rows), worktrees=rows)





# Fala 0.7.15 atomic dispatch operations: one external read, mutation, or
# pure transform per process.
from lokay.envelope import terminal_upstream


def _atomic_terminal(request: Request, operation: str, *peers: str) -> Result | None:
    return terminal_upstream(request, operation, *peers)


_DISPATCH_TAIL_ANCESTRY = (
    "select_dispatch_task", "verify_dispatch_receipt", "publish_dispatch_receipt", "build_dispatch_receipt",
    "aggregate_issue_label_results", "issue_to_pr_add_issue_label", "aggregate_pr_label_results", "add_pr_label",
    "normalize_pr_labels", "reconcile_pull_request", "create_pull_request", "decide_existing_pr",
    "read_open_pr_for_branch", "verify_updated_branch_local_oid", "update_branch_local_oid", "verify_push_oid", "read_pushed_ref", "push_branch", "read_push_head",
    "decide_branch_has_commits", "read_base_head", "read_worktree_head", "verify_omp_postconditions", "invoke_omp",
    "read_omp_preconditions", "verify_worktree_head", "add_worktree", "write_branch_provenance", "create_local_branch",
    "read_branch_provenance", "read_worktree_inventory", "read_base_ref", "fetch_clone_origin", "read_clone_preconditions",
    "reconcile_fix_task", "create_fix_task", "find_fix_task_marker", "read_fix_tasks", "read_dispatch_tasks",
    "intake_reconcile_intake_task", "intake_create_intake_task", "intake_build_issue_claim_result",
)
def _atomic_board(request: Request) -> str:
    data, cfg = input_of(request), cfg_of(request)
    selected = data.get("selected") if isinstance(data.get("selected"), Mapping) else {}
    if not isinstance(selected, dict):
        selected = {}
    board = str(
        data.get("board")
        or selected.get("board")
        or cond_get(
            request,
            "board",
            "read_dispatch_tasks",
            "read_fix_tasks",
            "reconcile_intake_task",
            "create_intake_task",
            "read_intake_tasks",
            "find_intake_marker",
            "build_issue_claim_result",
            "select_issue_candidate",
            "reserve_claim_file",
            "select_dispatch_task",
        )
        or cfg.get("board")
        or ""
    )
    if board:
        return board
    for effector_id in (
        "reconcile_intake_task",
        "create_intake_task",
        "read_intake_tasks",
        "find_intake_marker",
        "build_issue_claim_result",
        "select_issue_candidate",
        "reserve_claim_file",
        "select_dispatch_task",
    ):
        blob = cond_blob(request, effector_id)
        nested = blob.get("selected") if isinstance(blob.get("selected"), Mapping) else {}
        candidate = str(blob.get("board") or (nested.get("board") if isinstance(nested, Mapping) else "") or "")
        if candidate:
            return candidate
    return ""


def _atomic_repo(request: Request) -> str:
    data = input_of(request)
    return str(data.get("repo") or cond_get(request, "repo", "parse_issue_ref_from_task", "select_dispatch_task") or "")


def _atomic_rows(request: Request, *ids: str) -> list[dict[str, Any]] | None:
    blob = cond_blob(request, *ids)
    rows = next((blob[key] for key in ("tasks", "rows", "items") if key in blob), None)
    return rows if isinstance(rows, list) and all(isinstance(row, dict) for row in rows) else None


def read_dispatch_tasks(request: Request) -> Result:
    """Read the dispatch board; policy selection is a separate process.

    Dispatch is intentionally independent of intake claim capacity when an
    active claim already holds work. Idle intake still yields a clean noop.
    """
    held_board = _held_claim_board(request)
    if not held_board:
        idle = upstream_noop(
            request,
            "intake_reconcile_intake_task",
            "intake_create_intake_task",
            "intake_build_issue_claim_result",
        )
        if idle:
            return noop(str(idle.get("reason") or "no_selected_issue"), operation="read_dispatch_tasks")
    board = held_board or _atomic_board(request)
    if not board:
        board = _dispatch_board_fallback(request)
    if not board:
        return fail("missing_board", failure_class="terminal", retry_safe=False, operation="read_dispatch_tasks")
    try:
        rows = hermes_kanban_json(["--board", board, "list", "--json", "--sort", "created-desc"])
    except CommandError as exc:
        return fail("kanban_list_failed", failure_class="retryable_read", retry_safe=True, operation="read_dispatch_tasks", board=board, error=str(exc))
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return fail("invalid_kanban_json", failure_class="terminal", retry_safe=False, operation="read_dispatch_tasks", board=board)
    return ok(status="read", operation="read_dispatch_tasks", board=board, tasks=rows)


def _dispatch_board_fallback(request: Request) -> str:
    """Resolve board from active claim identity, then failed selection identity.

    When intake is claim_busy, the held claim is the work that should continue;
    never invent a board from repos[0].
    """
    data, cfg = input_of(request), cfg_of(request)
    # 1) durable active claim identity — held work owns capacity
    held = _held_claim_board(request)
    if held:
        return held
    # 2) nested selected.board / repo on claim or selection blobs
    for effector_id in (
        "reserve_claim_file",
        "select_issue_candidate",
        "build_issue_claim_result",
        "reconcile_intake_task",
        "create_intake_task",
        "find_intake_marker",
    ):
        blob = cond_blob(request, effector_id)
        nested = blob.get("selected") if isinstance(blob.get("selected"), Mapping) else {}
        candidate = str(blob.get("board") or (nested.get("board") if isinstance(nested, Mapping) else "") or "").strip()
        if candidate:
            return candidate
        selected_repo = str((nested.get("repo") if isinstance(nested, Mapping) else "") or blob.get("repo") or "").strip()
        if selected_repo:
            matched = _board_for_repo(request, selected_repo)
            if matched:
                return matched
    # 3) explicit selected/repo/board input only — never repos[0]
    selected = data.get("selected") if isinstance(data.get("selected"), Mapping) else {}
    candidate = str(data.get("board") or selected.get("board") or cfg.get("board") or "").strip()
    if candidate:
        return candidate
    selected_repo = str(selected.get("repo") or data.get("repo") or "").strip()
    if selected_repo:
        return _board_for_repo(request, selected_repo)
    return ""


def _held_claim_identity(request: Request) -> dict[str, Any] | None:
    """Return the single unambiguous active claim payload, if any."""
    from lokay.steps.claim import _claim_file, _claims_in_directory, _read_claim

    data, cfg = input_of(request), cfg_of(request)
    path_value = data.get("active_issue_path") or cfg.get("active_issue_path") or (cfg.get("paths") or {}).get("active_issue")
    if not path_value:
        return None
    configured = Path(str(path_value)).expanduser()
    path = _claim_file(str(path_value))
    if path is None:
        return None
    is_directory = (configured.exists() and configured.is_dir()) or configured.suffix.lower() != ".json"
    if is_directory:
        claims, error = _claims_in_directory(path.parent if path.name.endswith(".json") else configured)
        if error or len(claims) != 1:
            return None
        return dict(claims[0][1])
    payload, error = _read_claim(path)
    return None if error or not payload else dict(payload)


def _held_claim_board(request: Request) -> str:
    """Return board from the single active claim when capacity is held."""
    claim = _held_claim_identity(request)
    return str((claim or {}).get("board") or "").strip()


def _board_for_repo(request: Request, repo: str) -> str:
    data, cfg = input_of(request), cfg_of(request)
    repos = data.get("repos") if isinstance(data.get("repos"), list) else cfg.get("repos")
    if not isinstance(repos, list):
        return ""
    wanted = repo.strip().casefold()
    for entry in repos:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("repo") or "").strip().casefold() != wanted:
            continue
        board = str(entry.get("board") or "").strip()
        if board:
            return board


def _repo_context_for_repo(request: Request, repo: str) -> dict[str, Any]:
    data, cfg = input_of(request), cfg_of(request)
    repos = data.get("repos") if isinstance(data.get("repos"), list) else cfg.get("repos")
    if not isinstance(repos, list):
        return {}
    wanted = repo.strip().casefold()
    matches = [dict(entry) for entry in repos if isinstance(entry, Mapping) and str(entry.get("repo") or "").strip().casefold() == wanted]
    return matches[0] if len(matches) == 1 else {}


def _fix_identity(request: Request) -> tuple[str, int | str, str, dict[str, Any]]:
    data = input_of(request)
    held = _held_claim_identity(request) or {}
    sources = (
        held,
        data,
        cond_blob(request, "reconcile_fix_task"),
        cond_blob(request, "create_fix_task"),
        cond_blob(request, "find_fix_task_marker"),
        cond_blob(request, "read_fix_tasks"),
        cond_blob(request, "parse_issue_ref_from_task"),
        cond_blob(request, "select_dispatch_task"),
        cond_blob(request, "read_worktree_inventory"),
        cond_blob(request, "read_branch_provenance"),
        cond_blob(request, "create_local_branch"),
        cond_blob(request, "write_branch_provenance"),
        cond_blob(request, "read_clone_preconditions"),
        cond_blob(request, "fetch_clone_origin"),
        cond_blob(request, "read_base_ref"),
    )
    repo = str(next((source.get("repo") for source in sources if source.get("repo") not in (None, "")), "")).strip()
    issue = next((source.get("issue") or source.get("number") for source in sources if source.get("issue") not in (None, "") or source.get("number") not in (None, "")), "")
    board = str(next((source.get("board") for source in sources if source.get("board") not in (None, "")), "")).strip()
    branch = str(next((source.get("branch") for source in sources if source.get("branch") not in (None, "")), "")).strip()
    task_id = ""
    for source in sources:
        if source.get("task_id") not in (None, ""):
            task_id = str(source.get("task_id")).strip()
            break
        task = source.get("task")
        if isinstance(task, Mapping) and task.get("id") not in (None, ""):
            task_id = str(task.get("id")).strip()
            break
        if not isinstance(task, Mapping) and task not in (None, ""):
            task_id = str(task).strip()
            break
    context = _repo_context_for_repo(request, repo) if repo else {}
    if branch:
        context = {**context, "branch": branch}
    if task_id:
        context = {**context, "task_id": task_id}
    if not context.get("clone_path"):
        clone = str(next((source.get("clone_path") for source in sources if source.get("clone_path") not in (None, "")), "")).strip()
        if clone:
            context = {**context, "clone_path": clone}
    return repo, issue, board or str(context.get("board") or "").strip(), context


def _dispatch_worktree_path(request: Request, *, branch: str) -> str:
    data, cfg = input_of(request), cfg_of(request)
    explicit = str(data.get("worktree_path") or cfg.get("worktree_path") or "").strip()
    if explicit:
        return str(Path(explicit).expanduser())
    root = str(data.get("worktree_root") or cfg.get("worktree_root") or "").strip()
    if not root or not branch:
        return ""
    safe = re.sub(r"[^a-zA-Z0-9._/-]+", "-", branch).strip("-")
    return str((Path(root).expanduser() / safe)) if safe else ""


def _task_matches_issue(row: Mapping[str, Any], *, repo: str, issue: int | str) -> bool:
    marker = f"github-issue:{repo}:{issue}"
    body = str(row.get("body") or row.get("description") or "")
    title = str(row.get("title") or "")
    needle = f"{repo}#{issue}"
    return marker in body or needle in title or needle in body


def _fix_marker_for_issue_task(row: Mapping[str, Any]) -> str:
    title = str(row.get("title") or "")
    if not title.startswith("[issue]"):
        return ""
    body = str(row.get("body") or row.get("description") or "")
    match = re.search(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)\b", title)
    if match:
        return f"fix-pr:{match.group(1)}:{match.group(2)}"
    repo = re.search(r"^Repository:\s*(\S+)\s*$", body, re.M)
    issue = re.search(r"^Issue:\s*#?([0-9]+)\s*$", body, re.M)
    if repo and issue:
        return f"fix-pr:{repo.group(1)}:{issue.group(1)}"
    return ""


def _task_has_marker(row: Mapping[str, Any], marker: str) -> bool:
    body = str(row.get("body") or row.get("description") or "")
    return bool(marker) and re.search(rf"^Idempotency-Key:\s*{re.escape(marker)}\s*$", body, re.M) is not None

def _task_has_prefixed_marker(row: Mapping[str, Any], prefix: str, marker: str) -> bool:
    title = str(row.get("title") or "")
    if prefix == "fix-pr":
        if not title.startswith("[fix-pr]") or title.startswith("[fix-pr-review]"):
            return False
    elif not title.startswith(f"[{prefix}]"):
        return False
    return _task_has_marker(row, marker)


def _has_fix_task(rows: list[dict[str, Any]], marker: str) -> bool:
    return any(_task_has_prefixed_marker(row, "fix-pr", marker) for row in rows)


def _held_claim_has_merged_receipt(request: Request, *, repo: str, issue: int | str) -> bool:
    """True when a durable MERGED receipt matches the held claim identity."""
    data, cfg = input_of(request), cfg_of(request)
    receipt_value = (
        data.get("merge_receipts")
        or cfg.get("merge_receipts")
        or (cfg.get("paths") if isinstance(cfg.get("paths"), dict) else {}).get("merge_receipts")
    )
    if not receipt_value:
        return False
    receipt_root = Path(str(receipt_value)).expanduser()
    if not receipt_root.is_dir():
        return False
    try:
        issue_num = int(issue)
    except (TypeError, ValueError):
        return False
    wanted = str(repo or "").strip()
    if not wanted or issue_num <= 0:
        return False
    try:
        paths = sorted(receipt_root.glob("*.json"))
    except OSError:
        return False
    if len(paths) > 256:
        return False
    for path in paths:
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(receipt, dict) or receipt.get("phase") != "MERGED":
            continue
        provenance = receipt.get("verified_provenance")
        if not isinstance(provenance, dict):
            continue
        if provenance.get("source") != "github_pr_readback" or provenance.get("state") != "MERGED":
            continue
        receipt_repo = str(receipt.get("repo") or provenance.get("repo") or "").strip()
        if receipt_repo != wanted:
            continue
        branch = str(provenance.get("head_ref") or "").strip()
        branch_match = re.fullmatch(r"ai/fix/([1-9][0-9]*)(?:-[A-Za-z0-9._-]+)?", branch)
        receipt_issue = int(branch_match.group(1)) if branch_match else 0
        if receipt_issue != issue_num:
            continue
        number = provenance.get("number")
        if receipt.get("pr") != number:
            continue
        head = str(provenance.get("head_oid") or "").strip()
        if not head or receipt.get("headSha") != head:
            continue
        return True
    return False



def select_dispatch_task(request: Request) -> Result:
    """Select ready dispatch work, constrained to an active claim when held."""
    terminal = _atomic_terminal(request, "select_dispatch_task", "read_dispatch_tasks")
    if terminal:
        return terminal
    idle = upstream_noop(request, "read_dispatch_tasks", "intake_reconcile_intake_task")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="select_dispatch_task")
    data = input_of(request)
    rows = data.get("tasks") if isinstance(data.get("tasks"), list) else _atomic_rows(request, "read_dispatch_tasks")
    if rows is None:
        return fail("missing_dispatch_rows", failure_class="terminal", retry_safe=False, operation="select_dispatch_task")
    requested = str(data.get("task_id") or "")
    held = _held_claim_identity(request) or {}
    held_repo = str(held.get("repo") or "").strip()
    held_issue = held.get("issue")
    ready: list[dict[str, Any]] = []
    for row in rows:
        tid = str(row.get("id") or row.get("task_id") or "")
        state = str(row.get("status") or row.get("state") or "").lower()
        title = str(row.get("title") or "")
        marker = _fix_marker_for_issue_task(row)
        superseded = _has_fix_task(rows, marker)
        if requested and tid != requested:
            continue
        if requested and superseded:
            return noop("fix_task_handoff", operation="select_dispatch_task", task_id=tid, marker=marker)
        if superseded:
            continue
        if state in {"done", "completed", "archived", "blocked"}:
            continue
        if not (requested or title.startswith("[issue]") or (title.startswith("[fix-pr]") and not title.startswith("[fix-pr-review]"))):
            continue
        if held_repo and held_issue not in (None, "") and not _task_matches_issue(row, repo=held_repo, issue=held_issue):
            continue
        ready.append(row)
    if not ready:
        reason = "held_claim_task_unavailable" if held_repo and held_issue not in (None, "") else "no_ready_task"
        return noop(reason, operation="select_dispatch_task", repo=held_repo or None, issue=held_issue)
    if held_repo and held_issue not in (None, "") and _held_claim_has_merged_receipt(
        request, repo=held_repo, issue=held_issue
    ):
        return noop(
            "held_claim_task_unavailable",
            operation="select_dispatch_task",
            repo=held_repo,
            issue=held_issue,
        )
    row = ready[0]
    tid = str(row.get("id") or row.get("task_id") or "")
    context = {k: row[k] for k in ("repo", "clone_path", "priority", "board") if row.get(k) not in (None, "")}
    if held_repo:
        context.setdefault("repo", held_repo)
    if held.get("board"):
        context.setdefault("board", held.get("board"))
    return ok(status="selected", operation="select_dispatch_task", task=row, task_id=tid, **context)


def read_fix_tasks(request: Request) -> Result:
    terminal = _atomic_terminal(request, "read_fix_tasks", "select_dispatch_task", "read_dispatch_tasks")
    if terminal:
        return terminal
    idle = upstream_noop(request, "select_dispatch_task", "read_dispatch_tasks")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="read_fix_tasks")
    board = _atomic_board(request)
    if not board:
        return fail("missing_board", failure_class="terminal", retry_safe=False, operation="read_fix_tasks")
    try:
        rows = hermes_kanban_json(["--board", board, "list", "--json", "--sort", "created-desc"])
    except CommandError as exc:
        return fail("kanban_list_failed", failure_class="retryable_read", retry_safe=True, operation="read_fix_tasks", board=board, error=str(exc))
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return fail("invalid_kanban_json", failure_class="terminal", retry_safe=False, operation="read_fix_tasks", board=board)
    repo, issue, board, context = _fix_identity(request)
    return ok(status="read", operation="read_fix_tasks", board=board, repo=repo, issue=issue, branch=context.get("branch"), task_id=context.get("task_id"), clone_path=context.get("clone_path"), tasks=rows)


def find_fix_task_marker(request: Request) -> Result:
    terminal = _atomic_terminal(request, "find_fix_task_marker", "read_fix_tasks")
    if terminal:
        return terminal
    data = input_of(request)
    rows = data.get("tasks") if isinstance(data.get("tasks"), list) else _atomic_rows(request, "read_fix_tasks")
    repo, issue, board, context = _fix_identity(request)
    marker = str(data.get("idempotency_key") or f"fix-pr:{repo}:{issue}")
    idle = upstream_noop(request, "read_fix_tasks", "select_dispatch_task")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="find_fix_task_marker")
    if not repo or issue in (None, ""):
        return fail("missing_repo_or_issue", failure_class="terminal", retry_safe=False, operation="find_fix_task_marker", idempotency_key=marker)
    if rows is None:
        return fail("missing_fix_rows", failure_class="terminal", retry_safe=False, operation="find_fix_task_marker", idempotency_key=marker)
    matches = [r for r in rows if _task_has_prefixed_marker(r, "fix-pr", marker)]
    if len(matches) > 1:
        return fail("ambiguous_fix_task", failure_class="terminal", retry_safe=False, operation="find_fix_task_marker", idempotency_key=marker, matches=matches)
    return ok(
        status="found" if matches else "absent",
        operation="find_fix_task_marker",
        marker=marker,
        repo=repo,
        issue=issue,
        board=board,
        branch=context.get("branch"),
        task=matches[0] if matches else None,
        task_id=(matches[0].get("id") or matches[0].get("task_id")) if matches else None,
        source_task_id=context.get("task_id"),
    )


def create_fix_task(request: Request) -> Result:
    terminal = _atomic_terminal(request, "create_fix_task", "find_fix_task_marker")
    if terminal:
        return terminal
    data, cfg = input_of(request), cfg_of(request)
    idle = upstream_noop(request, "find_fix_task_marker", "select_dispatch_task")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="create_fix_task")
    found = cond_blob(request, "find_fix_task_marker")
    repo, issue, board, context = _fix_identity(request)
    title = str(data.get("title") or f"[fix-pr] {repo}#{issue}: Fix {repo}#{issue}")
    marker = str(data.get("idempotency_key") or found.get("marker") or f"fix-pr:{repo}:{issue}")
    if not board or not repo or issue in (None, ""):
        return fail("missing_board_repo_issue", failure_class="terminal", retry_safe=False, operation="create_fix_task", idempotency_key=marker)
    if found.get("task_id") not in (None, ""):
        return ok(status="fix_task_exists", operation="create_fix_task", board=board, repo=repo, issue=issue, branch=context.get("branch"), clone_path=context.get("clone_path"), task_id=found.get("task_id"), task=found.get("task"), idempotency_key=marker, mutated=False, reused=True)
    body = str(data.get("body") or f"Repository: {repo}\nIssue: #{issue}\nIdempotency-Key: {marker}\n")
    if dry_run_flag(request):
        return planned(operation="create_fix_task", board=board, title=title, body=body, idempotency_key=marker, repo=repo, issue=issue, branch=context.get("branch"), clone_path=context.get("clone_path"), task_id=context.get("task_id"))
    try:
        proc = run_cmd(["hermes", "kanban", "--board", board, "create", "--body", body, "--assignee", str(cfg.get("fixer_assignee") or "lokay-fixer"), "--idempotency-key", marker, title], timeout=90)
    except CommandError as exc:
        return fail("kanban_create_failed", failure_class="reconcile_then_retry", retry_safe=False, operation="create_fix_task", board=board, idempotency_key=marker, error=str(exc), mutated=True)
    return ok(status="created", operation="create_fix_task", board=board, title=title, idempotency_key=marker, repo=repo, issue=issue, branch=context.get("branch"), clone_path=context.get("clone_path"), task_id=context.get("task_id"), stdout=(proc.stdout or "")[-400:], mutated=True)


def reconcile_fix_task(request: Request) -> Result:
    """Reconcile fix-task state and stop intake tasks at the handoff boundary."""
    reconciled = _reconcile_kanban_marker(request, "reconcile_fix_task", "create_fix_task", "fix-pr")
    if reconciled.get("ok") is not True or reconciled.get("status") in {"noop", "planned"}:
        return reconciled
    selected = cond_blob(request, "select_dispatch_task").get("task")
    title = str(selected.get("title") or "") if isinstance(selected, Mapping) else ""
    if title.startswith("[issue]"):
        return noop(
            "fix_task_handoff",
            operation="reconcile_fix_task",
            board=reconciled.get("board"),
            repo=reconciled.get("repo"),
            issue=reconciled.get("issue"),
            task=reconciled.get("task"),
            task_id=reconciled.get("task_id"),
            source_task_id=cond_get(request, "task_id", "select_dispatch_task"),
            marker=reconciled.get("marker"),
        )
    return reconciled


def _reconcile_kanban_marker(request: Request, operation: str, peer: str, prefix: str) -> Result:
    terminal = _atomic_terminal(request, operation, peer)
    if terminal:
        return terminal
    idle = upstream_noop(request, "create_fix_task", "find_fix_task_marker", "select_dispatch_task")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation=operation)
    data = input_of(request); created = cond_blob(request, peer); board = str(data.get("board") or created.get("board") or _atomic_board(request)); marker = str(data.get("idempotency_key") or created.get("idempotency_key") or created.get("marker") or "")
    if not board or not marker:
        return fail("missing_board_or_marker", failure_class="terminal", retry_safe=False, operation=operation)
    try: rows = hermes_kanban_json(["--board", board, "list", "--json", "--sort", "created-desc"])
    except CommandError as exc: return fail("reconcile_read_failed", failure_class="retryable_read", retry_safe=True, operation=operation, error=str(exc), board=board)
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows): return fail("invalid_reconcile_read", failure_class="terminal", retry_safe=False, operation=operation)
    matches = [r for r in rows if _task_has_prefixed_marker(r, prefix, marker)]
    if len(matches) != 1: return fail("reconcile_conflict" if len(matches) > 1 else "created_task_unresolved", failure_class="terminal", retry_safe=False, operation=operation, matches=matches, mutated=False)
    row = matches[0]; tid = row.get("id") or row.get("task_id")
    if not tid: return fail("invalid_kanban_task_id", failure_class="terminal", retry_safe=False, operation=operation, mutated=False)
    context = {key: created[key] for key in ("repo", "issue", "clone_path", "branch") if created.get(key) not in (None, "")}
    return ok(status="reconciled", operation=operation, task=row, task_id=tid, board=board, marker=marker, mutated=False, **context)

def read_task_for_completion(request: Request) -> Result:
    terminal = _atomic_terminal(request, "read_task_for_completion", "select_dispatch_task", "verify_dispatch_receipt", "invoke_omp", "verify_omp_postconditions")
    if terminal: return terminal
    idle = upstream_noop(request, "select_dispatch_task", "verify_dispatch_receipt", "publish_dispatch_receipt", "build_dispatch_receipt", "aggregate_issue_label_results", "issue_to_pr_add_issue_label", "aggregate_pr_label_results", "add_pr_label", "normalize_pr_labels", "reconcile_pull_request", "create_pull_request", "decide_existing_pr", "read_open_pr_for_branch", "verify_updated_branch_local_oid", "update_branch_local_oid", "verify_push_oid", "read_pushed_ref", "push_branch", "read_push_head", "decide_branch_has_commits", "read_base_head", "read_worktree_head", "verify_omp_postconditions", "invoke_omp", "read_omp_preconditions", "verify_worktree_head", "add_worktree", "write_branch_provenance", "create_local_branch", "read_branch_provenance", "read_worktree_inventory", "read_base_ref", "fetch_clone_origin", "read_clone_preconditions", "reconcile_fix_task", "create_fix_task", "find_fix_task_marker", "read_fix_tasks", "read_dispatch_tasks", "intake_reconcile_intake_task", "intake_create_intake_task", "intake_build_issue_claim_result")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="read_task_for_completion")
    board, task_id = _atomic_board(request), str(input_of(request).get("task_id") or cond_get(request, "task_id", "select_dispatch_task") or "")
    if not board or not task_id: return fail("missing_board_or_task_id", failure_class="terminal", retry_safe=False, operation="read_task_for_completion")
    try: rows = hermes_kanban_json(["--board", board, "list", "--json", "--sort", "created-desc"])
    except CommandError as exc: return fail("kanban_list_failed", failure_class="retryable_read", retry_safe=True, operation="read_task_for_completion", error=str(exc), board=board, task_id=task_id)
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows): return fail("invalid_kanban_json", failure_class="terminal", retry_safe=False, operation="read_task_for_completion")
    found = [r for r in rows if str(r.get("id") or r.get("task_id") or "") == task_id]
    if len(found) != 1: return fail("task_not_found" if not found else "ambiguous_task", failure_class="terminal", retry_safe=False, operation="read_task_for_completion", task_id=task_id)
    return ok(status="read", operation="read_task_for_completion", board=board, task=found[0], task_id=task_id)


def decide_task_completion(request: Request) -> Result:
    terminal = _atomic_terminal(request, "decide_task_completion", "read_task_for_completion")
    if terminal: return terminal
    task = input_of(request).get("task") or cond_get(request, "task", "read_task_for_completion") or {}
    state = str(task.get("status") or task.get("state") or "").lower() if isinstance(task, dict) else ""
    if state in {"done", "completed", "archived"}: return ok(status="already_completed", operation="decide_task_completion", should_complete=False)
    return ok(status="should_complete", operation="decide_task_completion", should_complete=True)


def complete_task(request: Request) -> Result:
    terminal = _atomic_terminal(request, "complete_task", "decide_task_completion", "read_task_for_completion", "select_dispatch_task", "invoke_omp", "verify_omp_postconditions")
    if terminal: return terminal
    idle = upstream_noop(request, "decide_task_completion", "read_task_for_completion", "select_dispatch_task", "verify_dispatch_receipt", "publish_dispatch_receipt", "build_dispatch_receipt", "aggregate_issue_label_results", "issue_to_pr_add_issue_label", "aggregate_pr_label_results", "add_pr_label", "normalize_pr_labels", "reconcile_pull_request", "create_pull_request", "decide_existing_pr", "read_open_pr_for_branch", "verify_updated_branch_local_oid", "update_branch_local_oid", "verify_push_oid", "read_pushed_ref", "push_branch", "read_push_head", "decide_branch_has_commits", "read_base_head", "read_worktree_head", "verify_omp_postconditions", "invoke_omp", "read_omp_preconditions", "verify_worktree_head", "add_worktree", "write_branch_provenance", "create_local_branch", "read_branch_provenance", "read_worktree_inventory", "read_base_ref", "fetch_clone_origin", "read_clone_preconditions", "reconcile_fix_task", "create_fix_task", "find_fix_task_marker", "read_fix_tasks", "read_dispatch_tasks", "intake_reconcile_intake_task", "intake_create_intake_task", "intake_build_issue_claim_result")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="complete_task")
    data, cfg = input_of(request), cfg_of(request); board = _atomic_board(request); tid = str(data.get("task_id") or cond_get(request, "task_id", "read_task_for_completion", "select_dispatch_task") or ""); text = str(data.get("result") or "completed")
    decision = cond_blob(request, "decide_task_completion")
    if decision.get("should_complete") is False: return ok(status="already_completed", operation="complete_task", board=board, task_id=tid, mutated=False)
    if not board or not tid: return fail("missing_board_or_task_id", failure_class="terminal", retry_safe=False, operation="complete_task")
    if dry_run_flag(request): return planned(operation="complete_task", board=board, task_id=tid, result=text)
    try: run_cmd(["hermes", "kanban", "--board", board, "complete", tid, "--result", text, "--summary", text], timeout=60)
    except CommandError as exc: return fail("complete_failed", failure_class="reconcile_then_retry", retry_safe=False, operation="complete_task", error=str(exc), board=board, task_id=tid, mutated=True)
    return ok(status="completed", operation="complete_task", board=board, task_id=tid, result=text, mutated=True)


def verify_task_completed(request: Request) -> Result:
    terminal = _atomic_terminal(request, "verify_task_completed", "complete_task")
    if terminal: return terminal
    idle = upstream_noop(request, "complete_task")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="verify_task_completed")
    completed = cond_blob(request, "complete_task")
    board = str(completed.get("board") or _atomic_board(request) or "")
    task_id = str(completed.get("task_id") or cond_get(request, "task_id", "complete_task") or "")
    if not board or not task_id: return fail("missing_board_or_task_id", failure_class="terminal", retry_safe=False, operation="verify_task_completed")
    try: rows = hermes_kanban_json(["--board", board, "list", "--json", "--sort", "created-desc"])
    except CommandError as exc: return fail("kanban_list_failed", failure_class="retryable_read", retry_safe=True, operation="verify_task_completed", error=str(exc), board=board, task_id=task_id)
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows): return fail("invalid_kanban_json", failure_class="terminal", retry_safe=False, operation="verify_task_completed")
    found = [r for r in rows if str(r.get("id") or r.get("task_id") or "") == task_id]
    if len(found) != 1: return fail("task_not_found" if not found else "ambiguous_task", failure_class="terminal", retry_safe=False, operation="verify_task_completed", task_id=task_id)
    state = str(found[0].get("status") or found[0].get("state") or "").lower()
    if state not in {"done", "completed", "archived"}:
        return fail("task_not_completed", failure_class="reconcile_then_retry", retry_safe=False, operation="verify_task_completed", task_id=task_id, state=state)
    return ok(status="verified", operation="verify_task_completed", task_id=task_id)


def read_clone_preconditions(request: Request) -> Result:
    data, cfg = input_of(request), cfg_of(request)
    repo, issue, board, context = _fix_identity(request)
    clone_path = str(data.get("clone_path") or cfg.get("clone_path") or context.get("clone_path") or "")
    base_branch = str(data.get("base_branch") or cfg.get("base_branch") or "main")
    idle = upstream_noop(request, "reconcile_fix_task", "select_dispatch_task")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="read_clone_preconditions")
    if not clone_path:
        return fail("missing_clone_path", failure_class="terminal", retry_safe=False, operation="read_clone_preconditions", repo=repo)
    clone = Path(clone_path)
    if not (clone / ".git").exists():
        return fail("clone_missing", failure_class="terminal", retry_safe=False, operation="read_clone_preconditions", clone_path=clone_path, repo=repo)
    try:
        status, origin = status_porcelain(clone), remote_url(clone)
    except CommandError as exc:
        return fail("clone_precondition_read_failed", failure_class="retryable_read", retry_safe=True, operation="read_clone_preconditions", error=str(exc), clone_path=clone_path, repo=repo)
    if status.strip():
        return fail("clone_dirty", failure_class="terminal", retry_safe=False, operation="read_clone_preconditions", clone_path=clone_path, clone_status=status, repo=repo)
    if not origin.strip():
        return fail("origin_missing", failure_class="terminal", retry_safe=False, operation="read_clone_preconditions", clone_path=clone_path, repo=repo)
    return ok(status="ready", operation="read_clone_preconditions", clone_path=clone_path, base_branch=base_branch, origin=origin, repo=repo, issue=issue, board=board, branch=context.get("branch"), task_id=context.get("task_id"))


def fetch_clone_origin(request: Request) -> Result:
    terminal = _atomic_terminal(request, "fetch_clone_origin", "read_clone_preconditions")
    if terminal: return terminal
    idle = upstream_noop(request, "read_clone_preconditions")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="fetch_clone_origin")
    clone_path = str(input_of(request).get("clone_path") or cond_get(request, "clone_path", "read_clone_preconditions") or "")
    repo, issue, _, context = _fix_identity(request)
    if dry_run_flag(request): return planned(operation="fetch_clone_origin", clone_path=clone_path, repo=repo, issue=issue, branch=context.get("branch"))
    try: git(["fetch", "origin", "--prune"], cwd=clone_path)
    except CommandError as exc: return fail("fetch_failed", failure_class="retryable", retry_safe=True, operation="fetch_clone_origin", clone_path=clone_path, error=str(exc), mutated=True)
    return ok(status="fetched", operation="fetch_clone_origin", clone_path=clone_path, repo=repo, issue=issue, branch=context.get("branch"), task_id=context.get("task_id"), mutated=True)


def read_base_ref(request: Request) -> Result:
    terminal = _atomic_terminal(request, "read_base_ref", "fetch_clone_origin", "read_clone_preconditions")
    if terminal: return terminal
    idle = upstream_noop(request, "fetch_clone_origin", "read_clone_preconditions")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="read_base_ref")
    data, cfg = input_of(request), cfg_of(request)
    clone = str(data.get("clone_path") or cond_get(request, "clone_path", "fetch_clone_origin", "read_clone_preconditions") or "")
    base_branch = str(data.get("base_branch") or cfg.get("base_branch") or "main")
    repo, issue, _, context = _fix_identity(request)
    try:
        head = remote_ref(clone, "origin", base_branch)
    except CommandError as exc:
        return fail("base_ref_read_failed", failure_class="retryable_read", retry_safe=True, operation="read_base_ref", error=str(exc), clone_path=clone, base_branch=base_branch)
    return ok(
        status="read",
        operation="read_base_ref",
        clone_path=clone,
        base_branch=base_branch,
        base_head=head,
        repo=repo,
        issue=issue,
        branch=context.get("branch"),
        task_id=context.get("task_id"),
    )


def read_worktree_inventory(request: Request) -> Result:
    terminal = _atomic_terminal(request, "read_worktree_inventory", "read_base_ref", "fetch_clone_origin")
    if terminal: return terminal
    idle = upstream_noop(request, "read_clone_preconditions")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="read_worktree_inventory")
    repo, issue, board, context = _fix_identity(request)
    clone = str(
        input_of(request).get("clone_path")
        or cond_get(request, "clone_path", "read_base_ref", "fetch_clone_origin", "read_clone_preconditions")
        or context.get("clone_path")
        or ""
    )
    try:
        text = worktree_list(clone)
        rows = parse_worktree_porcelain(text)
    except (CommandError, ValueError) as exc:
        return fail("worktree_list_failed", failure_class="retryable_read", retry_safe=True, operation="read_worktree_inventory", error=str(exc))
    return ok(
        status="read",
        operation="read_worktree_inventory",
        clone_path=clone,
        worktrees=rows,
        repo=repo,
        issue=issue,
        board=board,
        branch=context.get("branch"),
        task_id=context.get("task_id"),
    )


def read_branch_provenance(request: Request) -> Result:
    idle = upstream_noop(request, "read_worktree_inventory")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="read_branch_provenance")
    data = input_of(request)
    repo, issue, board, context = _fix_identity(request)
    inventory = cond_blob(request, "read_worktree_inventory")
    clone = str(data.get("clone_path") or inventory.get("clone_path") or context.get("clone_path") or "")
    branch = str(data.get("branch") or inventory.get("branch") or context.get("branch") or "")
    task_id = str(data.get("task_id") or inventory.get("task_id") or context.get("task_id") or "")
    receipt = str(data.get("receipt_path") or data.get("receipt_id") or inventory.get("receipt_path") or inventory.get("receipt") or "")
    if not clone or not branch:
        return fail("missing_clone_or_branch", failure_class="terminal", retry_safe=False, operation="read_branch_provenance", repo=repo, issue=issue, mutated=False)
    provenance = _branch_provenance(clone, branch)
    expected = _worktree_provenance(request, branch)
    if not expected.get("task_id") and task_id:
        expected = {**expected, "task_id": task_id}
    if not expected.get("issue") and issue not in (None, ""):
        expected = {**expected, "issue": str(issue)}
    if not expected.get("repo") and repo:
        expected = {**expected, "repo": repo}
    if not expected.get("receipt") and receipt:
        expected = {**expected, "receipt": receipt}
    if not expected.get("branch"):
        expected = {**expected, "branch": branch}
    error = _worktree_provenance_error(request, expected)
    if error["missing"] or error["conflicts"]:
        return fail(
            "conflicting_worktree_provenance" if error["conflicts"] else "missing_worktree_provenance",
            failure_class="terminal",
            retry_safe=False,
            operation="read_branch_provenance",
            mutated=False,
            **error,
        )
    if branch_exists(clone, branch) and not _provenance_matches(expected, provenance):
        stable_expected = {**expected, "receipt": provenance.get("receipt", "")} if provenance.get("receipt") else expected
        if not _provenance_matches(stable_expected, provenance):
            return fail("foreign_branch_ownership", failure_class="terminal", retry_safe=False, operation="read_branch_provenance", provenance=provenance, expected=expected, mutated=False)
        if dry_run_flag(request):
            return planned(operation="read_branch_provenance", provenance=provenance, expected=expected, clone_path=clone, branch=branch, repo=expected.get("repo") or repo, issue=expected.get("issue") or issue, board=board, task_id=expected.get("task_id"), receipt=expected.get("receipt"), worktree_path=_dispatch_worktree_path(request, branch=branch) or None, worktrees=inventory.get("worktrees") if isinstance(inventory.get("worktrees"), list) else [])
        try:
            _update_receipt_if_needed(clone, branch, expected.get("receipt", ""), provenance.get("receipt", ""))
        except CommandError as exc:
            return fail("branch_provenance_write_failed", failure_class="retryable", retry_safe=True, operation="read_branch_provenance", error=str(exc), provenance=provenance, expected=expected, mutated=False)
        provenance = {**provenance, "receipt": expected.get("receipt", "")}
    worktree_path = _dispatch_worktree_path(request, branch=branch)
    return ok(
        status="read",
        operation="read_branch_provenance",
        provenance=provenance,
        expected=expected,
        clone_path=clone,
        branch=branch,
        repo=expected.get("repo") or repo,
        issue=expected.get("issue") or issue,
        board=board,
        task_id=expected.get("task_id"),
        receipt=expected.get("receipt"),
        worktree_path=worktree_path or None,
        worktrees=inventory.get("worktrees") if isinstance(inventory.get("worktrees"), list) else [],
    )


def create_local_branch(request: Request) -> Result:
    terminal = _atomic_terminal(request, "create_local_branch", "read_base_ref", "read_branch_provenance")
    if terminal: return terminal
    idle = upstream_noop(request, "read_branch_provenance", "read_clone_preconditions", "reconcile_fix_task")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="create_local_branch")
    data = input_of(request)
    repo, issue, board, context = _fix_identity(request)
    proven = cond_blob(request, "read_branch_provenance")
    clone = str(data.get("clone_path") or proven.get("clone_path") or cond_get(request, "clone_path", "read_base_ref", "read_clone_preconditions") or context.get("clone_path") or "")
    branch = str(data.get("branch") or proven.get("branch") or context.get("branch") or "")
    base = str(data.get("base_head") or cond_get(request, "base_head", "read_base_ref") or "")
    path_value = str(data.get("worktree_path") or proven.get("worktree_path") or _dispatch_worktree_path(request, branch=branch) or "").strip()
    if not clone or not branch or not base or not path_value:
        return fail("missing_branch_inputs", failure_class="terminal", retry_safe=False, operation="create_local_branch", repo=repo, issue=issue, branch=branch, clone_path=clone, worktree_path=path_value or None)
    path = Path(path_value).expanduser().resolve()
    worktrees = proven.get("worktrees") if isinstance(proven.get("worktrees"), list) else []
    if branch_exists(clone, branch):
        actual = proven.get("provenance") or _branch_provenance(clone, branch)
        expected = proven.get("expected") if isinstance(proven.get("expected"), dict) else {
            "task_id": str(proven.get("task_id") or context.get("task_id") or ""),
            "issue": str(issue or ""),
            "receipt": str(data.get("receipt_path") or data.get("receipt_id") or proven.get("receipt") or ""),
            "repo": repo,
            "branch": branch,
        }
        if not _provenance_matches(expected, actual):
            return fail("foreign_branch_ownership", failure_class="terminal", retry_safe=False, operation="create_local_branch", mutated=False)
        try:
            head = local_branch_head(clone, branch)
        except CommandError as exc:
            return fail("branch_head_read_failed", failure_class="retryable_read", retry_safe=True, operation="create_local_branch", error=str(exc), mutated=False)
        matching = [row for row in worktrees if isinstance(row, dict) and Path(str(row.get("path") or "")).expanduser().resolve() == path]
        if any(str(row.get("branch") or "") == branch for row in matching):
            return ok(status="reused", operation="create_local_branch", clone_path=clone, branch=branch, head=head, repo=repo, issue=issue, board=board, task_id=context.get("task_id"), worktree_path=str(path), worktrees=worktrees, mutated=False)
        if head != base:
            return fail("branch_create_failed", failure_class="terminal", retry_safe=False, operation="create_local_branch", head=head, base_head=base, mutated=False)
        if path.exists() or matching:
            return fail("worktree_path_collision", failure_class="terminal", retry_safe=False, operation="create_local_branch", worktree_path=str(path))
        return ok(status="reused", operation="create_local_branch", clone_path=clone, branch=branch, repo=repo, issue=issue, board=board, task_id=context.get("task_id"), worktree_path=str(path), worktrees=worktrees, mutated=False)
    if path.exists() or any(Path(str(row.get("path") or "")).expanduser().resolve() == path for row in worktrees if isinstance(row, dict)):
        return fail("worktree_path_collision", failure_class="terminal", retry_safe=False, operation="create_local_branch", worktree_path=str(path))
    if dry_run_flag(request):
        return planned(operation="create_local_branch", branch=branch, base_head=base, clone_path=clone, repo=repo, issue=issue, worktree_path=str(path))
    try:
        git(["branch", branch, base], cwd=clone)
    except CommandError as exc:
        return fail("branch_create_failed", failure_class="reconcile_then_retry", retry_safe=False, operation="create_local_branch", error=str(exc), mutated=False)
    return ok(status="created", operation="create_local_branch", clone_path=clone, branch=branch, repo=repo, issue=issue, board=board, task_id=context.get("task_id"), worktree_path=str(path), mutated=True)


def write_branch_provenance(request: Request) -> Result:
    terminal = _atomic_terminal(request, "write_branch_provenance", "create_local_branch", "read_branch_provenance")
    if terminal: return terminal
    idle = upstream_noop(request, "create_local_branch", "read_branch_provenance")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="write_branch_provenance")
    data = input_of(request)
    created = cond_blob(request, "create_local_branch")
    proven = cond_blob(request, "read_branch_provenance")
    repo, issue, board, context = _fix_identity(request)
    clone = str(data.get("clone_path") or created.get("clone_path") or proven.get("clone_path") or context.get("clone_path") or "")
    branch = str(data.get("branch") or created.get("branch") or proven.get("branch") or context.get("branch") or "")
    values = dict(data["provenance"]) if isinstance(data.get("provenance"), dict) else {
        "task": str(data.get("task_id") or proven.get("task_id") or context.get("task_id") or ""),
        "issue": str(issue or ""),
        "receipt": str(data.get("receipt_path") or data.get("receipt_id") or ""),
        "repo": repo,
        "base": str(data.get("base_head") or cond_get(request, "base_head", "read_base_ref") or ""),
    }
    if not clone or not branch:
        return fail("missing_branch_provenance", failure_class="terminal", retry_safe=False, operation="write_branch_provenance")
    if dry_run_flag(request):
        return planned(operation="write_branch_provenance", branch=branch, clone_path=clone, repo=repo, issue=issue)
    try:
        local_oid = local_branch_head(clone, branch)
    except CommandError as exc:
        return fail("branch_head_read_failed", failure_class="retryable_read", retry_safe=True, operation="write_branch_provenance", error=str(exc), mutated=created.get("status") != "reused")
    if not local_oid:
        return fail("branch_head_read_failed", failure_class="terminal", retry_safe=False, operation="write_branch_provenance", mutated=created.get("status") != "reused")
    values["local-oid"] = local_oid
    if created.get("status") == "reused":
        actual = proven.get("provenance") or _branch_provenance(clone, branch)
        expected = proven.get("expected") if isinstance(proven.get("expected"), dict) else {
            "task_id": str(proven.get("task_id") or context.get("task_id") or ""),
            "issue": str(issue or ""),
            "receipt": str(data.get("receipt_path") or data.get("receipt_id") or proven.get("receipt") or ""),
            "repo": repo,
            "branch": branch,
        }
        if not _provenance_matches(expected, actual):
            return fail("foreign_branch_ownership", failure_class="terminal", retry_safe=False, operation="write_branch_provenance", mutated=False)
    try:
        for key, value in values.items():
            if value:
                branch_config_set(clone, branch, f"lokay-{key}", str(value))
    except CommandError as exc:
        return fail("branch_provenance_write_failed", failure_class="retryable", retry_safe=True, operation="write_branch_provenance", error=str(exc), mutated=True)
    return ok(status="written", operation="write_branch_provenance", branch=branch, clone_path=clone, repo=repo, issue=issue, board=board, task_id=context.get("task_id"), worktree_path=created.get("worktree_path"), mutated=True)


def add_worktree(request: Request) -> Result:
    terminal = _atomic_terminal(request, "add_worktree", "create_local_branch", "write_branch_provenance")
    if terminal: return terminal
    idle = upstream_noop(request, "create_local_branch", "write_branch_provenance")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="add_worktree")
    data = input_of(request)
    created = cond_blob(request, "create_local_branch")
    written = cond_blob(request, "write_branch_provenance")
    repo, issue, board, context = _fix_identity(request)
    clone = str(data.get("clone_path") or created.get("clone_path") or written.get("clone_path") or context.get("clone_path") or "")
    branch = str(data.get("branch") or created.get("branch") or written.get("branch") or context.get("branch") or "")
    path = str(data.get("worktree_path") or created.get("worktree_path") or written.get("worktree_path") or _dispatch_worktree_path(request, branch=branch) or "")
    root_value = str(data.get("worktree_root") or cfg_of(request).get("worktree_root") or "")
    if not clone or not path or not branch or not root_value:
        return fail("missing_worktree_inputs", failure_class="terminal", retry_safe=False, operation="add_worktree", clone_path=clone, branch=branch, worktree_path=path or None)
    root = Path(root_value).expanduser().resolve()
    resolved = Path(path).expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return fail("worktree_path_escape", failure_class="terminal", retry_safe=False, operation="add_worktree", worktree_path=path)
    worktrees = created.get("worktrees") if isinstance(created.get("worktrees"), list) else []
    if not worktrees:
        proven = cond_blob(request, "read_branch_provenance")
        worktrees = proven.get("worktrees") if isinstance(proven.get("worktrees"), list) else []
    matching = [row for row in worktrees if isinstance(row, dict) and Path(str(row.get("path") or "")).expanduser().resolve() == resolved]
    if matching and any(str(row.get("branch") or "") == branch for row in matching):
        return ok(status="reused", operation="add_worktree", worktree_path=str(resolved), branch=branch, head=created.get("head"), clone_path=clone, repo=repo, issue=issue, board=board, task_id=context.get("task_id"), mutated=False)
    if dry_run_flag(request):
        return planned(operation="add_worktree", worktree_path=str(resolved), branch=branch, clone_path=clone, repo=repo, issue=issue)
    try:
        worktree_add(clone, str(resolved), branch, create_branch=False)
    except CommandError as exc:
        return fail("worktree_add_failed", failure_class="reconcile_then_retry", retry_safe=False, operation="add_worktree", error=str(exc), mutated=False)
    return ok(status="added", operation="add_worktree", worktree_path=str(resolved), branch=branch, clone_path=clone, repo=repo, issue=issue, board=board, task_id=context.get("task_id"), mutated=True)


def verify_worktree_head(request: Request) -> Result:
    terminal = _atomic_terminal(request, "verify_worktree_head", "add_worktree")
    if terminal: return terminal
    idle = upstream_noop(request, "add_worktree")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="verify_worktree_head")
    data = input_of(request)
    added = cond_blob(request, "add_worktree")
    path = str(data.get("worktree_path") or added.get("worktree_path") or "")
    branch = str(data.get("branch") or added.get("branch") or "")
    expected = str(added.get("head") or data.get("base_head") or cond_get(request, "base_head", "read_base_ref") or "")
    base = str(data.get("base_head") or cond_get(request, "base_head", "read_base_ref") or expected)
    if not path or not branch:
        return fail("missing_worktree_or_branch", failure_class="terminal", retry_safe=False, operation="verify_worktree_head")
    try: head = rev_parse(path)
    except CommandError as exc: return fail("worktree_head_read_failed", failure_class="retryable_read", retry_safe=True, operation="verify_worktree_head", error=str(exc))
    if expected and head != expected: return fail("worktree_base_mismatch", failure_class="terminal", retry_safe=False, operation="verify_worktree_head", head=head, base_head=expected)
    repo, issue, board, context = _fix_identity(request)
    return ok(status="verified", operation="verify_worktree_head", head=head, base_head=base, worktree_path=path, branch=branch, repo=repo, issue=issue, board=board, task_id=added.get("task_id") or context.get("task_id"))


def read_omp_preconditions(request: Request) -> Result:
    terminal = _atomic_terminal(request, "read_omp_preconditions", "verify_worktree_head")
    if terminal: return terminal
    idle = upstream_noop(request, "verify_worktree_head")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="read_omp_preconditions")
    data = input_of(request)
    verified = cond_blob(request, "verify_worktree_head")
    path = str(data.get("worktree_path") or verified.get("worktree_path") or "")
    branch = str(data.get("branch") or verified.get("branch") or "")
    if not path or not branch: return fail("missing_worktree_or_branch", failure_class="terminal", retry_safe=False, operation="read_omp_preconditions")
    try: top, actual = git(["rev-parse", "--show-toplevel"], cwd=path), git(["branch", "--show-current"], cwd=path)
    except CommandError as exc: return fail("omp_precondition_failed", failure_class="terminal", retry_safe=False, operation="read_omp_preconditions", error=str(exc))
    if Path(top).resolve() != Path(path).resolve(): return fail("omp_worktree_confinement", failure_class="terminal", retry_safe=False, operation="read_omp_preconditions", top_level=top)
    if actual != branch: return fail("omp_branch_mismatch", failure_class="terminal", retry_safe=False, operation="read_omp_preconditions", expected_branch=branch, actual_branch=actual)
    try: head = rev_parse(path)
    except CommandError as exc: return fail("omp_head_read_failed", failure_class="retryable_read", retry_safe=True, operation="read_omp_preconditions", error=str(exc))
    repo, issue, board, context = _fix_identity(request)
    return ok(status="ready", operation="read_omp_preconditions", pre_head=head, base_head=verified.get("base_head"), branch=actual, worktree_path=path, repo=repo, issue=issue, board=board, task_id=verified.get("task_id") or context.get("task_id"))


def invoke_omp(request: Request) -> Result:
    terminal = _atomic_terminal(request, "invoke_omp", "read_omp_preconditions")
    if terminal: return terminal
    idle = upstream_noop(request, "read_omp_preconditions", "add_worktree")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="invoke_omp")
    data, cfg = input_of(request), cfg_of(request)
    pre = cond_blob(request, "read_omp_preconditions")
    path = str(data.get("worktree_path") or pre.get("worktree_path") or "")
    repo, issue, board, context = _fix_identity(request)
    prompt = str(data.get("prompt") or (
        f"Fix GitHub issue {repo}#{issue}. Work only in the current isolated worktree on branch {pre.get('branch')}. "
        "Use gh to inspect the issue and repository context. Reproduce the problem, make the smallest safe fix, "
        "run relevant tests, and commit the result. Do not push, open or merge a pull request, or modify other worktrees."
        if repo and issue not in (None, "") and pre.get("branch") else ""
    ))
    if not path or not prompt: return fail("missing_worktree_or_prompt", failure_class="terminal", retry_safe=False, operation="invoke_omp")
    values = dict(operation="invoke_omp", worktree_path=path, branch=pre.get("branch"), pre_head=pre.get("pre_head"), base_head=pre.get("base_head"), repo=repo, issue=issue, board=board, task_id=pre.get("task_id") or context.get("task_id"))
    if dry_run_flag(request): return planned(**values)
    try:
        dirty_paths = _omp_diff_paths(path)
    except CommandError as exc:
        return fail("omp_precondition_failed", failure_class="terminal", retry_safe=False, operation="invoke_omp", error=str(exc))
    if dirty_paths:
        return fail("omp_worktree_dirty", failure_class="terminal", retry_safe=False, operation="invoke_omp", paths=dirty_paths)
    if pre.get("pre_head") and pre.get("base_head") and pre["pre_head"] != pre["base_head"]:
        try:
            ancestry = run_cmd(["git", "merge-base", "--is-ancestor", str(pre["base_head"]), str(pre["pre_head"])], cwd=path, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return fail("omp_branch_ancestry_read_failed", failure_class="retryable_read", retry_safe=True, operation="invoke_omp", error=str(exc), base_head=pre.get("base_head"), pre_head=pre.get("pre_head"))
        if ancestry.returncode == 1:
            return fail("omp_branch_diverged", failure_class="terminal", retry_safe=False, operation="invoke_omp", base_head=pre.get("base_head"), pre_head=pre.get("pre_head"))
        if ancestry.returncode != 0:
            return fail("omp_branch_ancestry_read_failed", failure_class="retryable_read", retry_safe=True, operation="invoke_omp", error=ancestry.stderr.strip(), base_head=pre.get("base_head"), pre_head=pre.get("pre_head"))
        return ok(status="reused", operation="invoke_omp", worktree_path=path, branch=pre.get("branch"), pre_head=pre.get("pre_head"), base_head=pre.get("base_head"), repo=repo, issue=issue, board=board, task_id=pre.get("task_id") or context.get("task_id"), mutated=False)
    try:
        out = run_omp(
            prompt=prompt,
            cwd=path,
            command=str(data.get("command") or cfg.get("executor_command") or "omp"),
            model=str(data.get("model") or cfg.get("model") or "omniroute/omp/default"),
            thinking=str(data.get("thinking") or cfg.get("thinking") or "medium"),
            timeout=float(data.get("timeout_seconds") or cfg.get("timeout_seconds") or 1800),
            dry_run=dry_run_flag(request),
        )
    except (CommandError, subprocess.TimeoutExpired) as exc:
        return fail(
            "omp_failed",
            failure_class="terminal",
            retry_safe=False,
            operation="invoke_omp",
            error=str(exc),
            timed_out=isinstance(exc, subprocess.TimeoutExpired),
            mutated=True,
        )
    return ok(status="invoked", mutated=True, omp=out, **values)


def verify_omp_postconditions(request: Request) -> Result:
    terminal = _atomic_terminal(request, "verify_omp_postconditions", "invoke_omp")
    if terminal: return terminal
    idle = upstream_noop(request, "invoke_omp", "read_omp_preconditions")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="verify_omp_postconditions")
    data = input_of(request)
    invoked = cond_blob(request, "invoke_omp")
    pre = cond_blob(request, "read_omp_preconditions")
    path = str(data.get("worktree_path") or invoked.get("worktree_path") or pre.get("worktree_path") or "")
    before = str(data.get("pre_head") or invoked.get("pre_head") or pre.get("pre_head") or "")
    try: head = rev_parse(path); dirty_paths = _omp_diff_paths(path); escaped_paths = _escaped_omp_paths(path, dirty_paths)
    except (CommandError, OSError, ValueError) as exc: return fail("omp_postcondition_failed", failure_class="terminal", retry_safe=False, operation="verify_omp_postconditions", error=str(exc))
    if escaped_paths: return fail("omp_diff_path_escape", failure_class="terminal", retry_safe=False, operation="verify_omp_postconditions", paths=escaped_paths)
    if dirty_paths: return fail("omp_worktree_dirty", failure_class="terminal", retry_safe=False, operation="verify_omp_postconditions", paths=dirty_paths)
    if before and head == before and invoked.get("status") != "reused": return fail("omp_head_unchanged", failure_class="terminal", retry_safe=False, operation="verify_omp_postconditions", head=head, pre_head=before)
    return ok(status="verified", operation="verify_omp_postconditions", head=head, base_head=invoked.get("base_head") or pre.get("base_head"), worktree_path=path, branch=invoked.get("branch") or pre.get("branch"), repo=invoked.get("repo") or pre.get("repo"), issue=invoked.get("issue") or pre.get("issue"), board=invoked.get("board") or pre.get("board"), task_id=invoked.get("task_id") or pre.get("task_id"))


def read_worktree_head(request: Request) -> Result:
    terminal = _atomic_terminal(request, "read_worktree_head", "verify_omp_postconditions")
    if terminal: return terminal
    idle = upstream_noop(request, "verify_omp_postconditions")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="read_worktree_head")
    data = input_of(request)
    verified = cond_blob(request, "verify_omp_postconditions")
    path = str(data.get("worktree_path") or verified.get("worktree_path") or "")
    try: head = rev_parse(path)
    except CommandError as exc: return fail("worktree_head_read_failed", failure_class="retryable_read", retry_safe=True, operation="read_worktree_head", error=str(exc))
    return ok(status="read", operation="read_worktree_head", head=head, base_head=verified.get("base_head"), worktree_path=path, branch=verified.get("branch"), repo=verified.get("repo"), issue=verified.get("issue"), board=verified.get("board"), task_id=verified.get("task_id"))


def read_base_head(request: Request) -> Result:
    terminal = _atomic_terminal(request, "read_base_head", "read_worktree_head")
    if terminal: return terminal
    idle = upstream_noop(request, "read_worktree_head")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="read_base_head")
    read = cond_blob(request, "read_worktree_head")
    base = str(input_of(request).get("base_head") or read.get("base_head") or "")
    if not base: return fail("missing_base_head", failure_class="terminal", retry_safe=False, operation="read_base_head")
    return ok(status="read", operation="read_base_head", base_head=base, worktree_path=read.get("worktree_path"), branch=read.get("branch"), repo=read.get("repo"), issue=read.get("issue"), board=read.get("board"), task_id=read.get("task_id"))


def decide_branch_has_commits(request: Request) -> Result:
    terminal = _atomic_terminal(request, "decide_branch_has_commits", "read_worktree_head", "read_base_head", "read_base_ref")
    if terminal: return terminal
    idle = upstream_noop(request, "read_worktree_head", "read_base_head", "read_base_ref", "verify_omp_postconditions", "invoke_omp", "read_omp_preconditions", "verify_worktree_head", "add_worktree", "write_branch_provenance", "create_local_branch", "read_branch_provenance", "read_worktree_inventory", "fetch_clone_origin", "read_clone_preconditions", "reconcile_fix_task", "create_fix_task", "find_fix_task_marker", "read_fix_tasks", "select_dispatch_task", "read_dispatch_tasks", "intake_reconcile_intake_task", "intake_create_intake_task", "intake_build_issue_claim_result")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="decide_branch_has_commits")
    head = cond_get(request, "head", "read_worktree_head"); base = cond_get(request, "base_head", "read_base_head", "read_base_ref") or cond_get(request, "base", "read_base_head")
    if not head or not base: return fail("missing_head_or_base", failure_class="terminal", retry_safe=False, operation="decide_branch_has_commits")
    if head == base: return fail("no_new_commits", failure_class="terminal", retry_safe=False, operation="decide_branch_has_commits", head=head, base=base)
    return ok(status="has_commits", operation="decide_branch_has_commits", head=head, base=base)


def read_push_head(request: Request) -> Result:
    terminal = _atomic_terminal(request, "read_push_head", "decide_branch_has_commits", "verify_omp_postconditions")
    if terminal: return terminal
    idle = upstream_noop(request, "decide_branch_has_commits", "verify_omp_postconditions")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="read_push_head")
    data = input_of(request); verified = cond_blob(request, "verify_omp_postconditions"); path = str(data.get("worktree_path") or verified.get("worktree_path") or ""); branch = str(data.get("branch") or verified.get("branch") or "")
    if not path or not branch: return fail("missing_worktree_or_branch", failure_class="terminal", retry_safe=False, operation="read_push_head")
    try: head = rev_parse(path)
    except CommandError as exc: return fail("push_head_read_failed", failure_class="retryable_read", retry_safe=True, operation="read_push_head", error=str(exc))
    return ok(status="read", operation="read_push_head", worktree_path=path, branch=branch, local_oid=head, repo=verified.get("repo"), issue=verified.get("issue"), board=verified.get("board"), task_id=verified.get("task_id"))


def push_branch(request: Request) -> Result:
    terminal = _atomic_terminal(request, "push_branch", "read_push_head")
    if terminal: return terminal
    idle = upstream_noop(request, "read_push_head")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="push_branch")
    data = input_of(request); read = cond_blob(request, "read_push_head"); path = str(data.get("worktree_path") or read.get("worktree_path") or ""); branch = str(data.get("branch") or read.get("branch") or "")
    if not path or not branch: return fail("missing_worktree_or_branch", failure_class="terminal", retry_safe=False, operation="push_branch")
    if dry_run_flag(request): return planned(operation="push_branch", branch=branch, remote="origin", worktree_path=path, repo=read.get("repo"), issue=read.get("issue"))
    try: out = git_push_branch(path, branch, set_upstream=True)
    except CommandError as exc: return fail("push_failed", failure_class="reconcile_then_retry", retry_safe=False, operation="push_branch", error=str(exc), mutated=True)
    return ok(status="pushed", operation="push_branch", branch=branch, worktree_path=path, local_oid=read.get("local_oid"), repo=read.get("repo"), issue=read.get("issue"), board=read.get("board"), task_id=read.get("task_id"), stdout_tail=(out or "")[-400:], mutated=True)


def read_pushed_ref(request: Request) -> Result:
    terminal = _atomic_terminal(request, "read_pushed_ref", "push_branch", "read_push_head")
    if terminal: return terminal
    idle = upstream_noop(request, "push_branch", "read_push_head")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="read_pushed_ref")
    data = input_of(request); pushed = cond_blob(request, "push_branch"); read = cond_blob(request, "read_push_head"); path = str(data.get("worktree_path") or pushed.get("worktree_path") or read.get("worktree_path") or ""); branch = str(data.get("branch") or pushed.get("branch") or read.get("branch") or "")
    try: line = git(["ls-remote", "origin", f"refs/heads/{branch}"], cwd=path); oid = line.split()[0] if line.split() else ""
    except CommandError as exc: return fail("push_readback_failed", failure_class="retryable_read", retry_safe=True, operation="read_pushed_ref", error=str(exc))
    return ok(status="read", operation="read_pushed_ref", remote_oid=oid, local_oid=read.get("local_oid"), branch=branch, repo=read.get("repo"), issue=read.get("issue"), board=read.get("board"), task_id=read.get("task_id"))


def verify_push_oid(request: Request) -> Result:
    terminal = _atomic_terminal(request, "verify_push_oid", "read_pushed_ref", "read_push_head")
    if terminal: return terminal
    idle = upstream_noop(request, "read_pushed_ref", "read_push_head", "push_branch", "decide_branch_has_commits", "verify_omp_postconditions", "read_worktree_head", "read_base_head", "read_base_ref", "invoke_omp", "read_omp_preconditions", "verify_worktree_head", "add_worktree", "write_branch_provenance", "create_local_branch", "read_branch_provenance", "read_worktree_inventory", "fetch_clone_origin", "read_clone_preconditions", "reconcile_fix_task", "create_fix_task", "find_fix_task_marker", "read_fix_tasks", "select_dispatch_task", "read_dispatch_tasks", "intake_reconcile_intake_task", "intake_create_intake_task", "intake_build_issue_claim_result")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="verify_push_oid")
    pushed = cond_blob(request, "read_pushed_ref"); read = cond_blob(request, "read_push_head"); local = pushed.get("local_oid") or read.get("local_oid") or input_of(request).get("local_oid"); remote = pushed.get("remote_oid") or input_of(request).get("remote_oid")
    if not local or not remote: return fail("missing_push_oids", failure_class="terminal", retry_safe=False, operation="verify_push_oid")
    if local != remote: return fail("push_readback_mismatch", failure_class="terminal", retry_safe=False, operation="verify_push_oid", local_oid=local, remote_oid=remote, mutated=True)
    return ok(status="verified", operation="verify_push_oid", local_oid=local, remote_oid=remote, repo=pushed.get("repo") or read.get("repo"), issue=pushed.get("issue") or read.get("issue"), board=pushed.get("board") or read.get("board"), task_id=pushed.get("task_id") or read.get("task_id"), branch=pushed.get("branch") or read.get("branch"))


def update_branch_local_oid(request: Request) -> Result:
    """Re-authorize lokay-local-oid to the verified post-push tip for cleanup."""
    terminal = _atomic_terminal(request, "update_branch_local_oid", "verify_push_oid")
    if terminal:
        return terminal
    idle = upstream_noop(request, "verify_push_oid")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="update_branch_local_oid")
    data = input_of(request)
    verified = cond_blob(request, "verify_push_oid")
    written = cond_blob(request, "write_branch_provenance")
    created = cond_blob(request, "create_local_branch")
    repo, issue, board, context = _fix_identity(request)
    clone = str(
        data.get("clone_path")
        or written.get("clone_path")
        or created.get("clone_path")
        or context.get("clone_path")
        or ""
    ).strip()
    branch = str(
        data.get("branch")
        or verified.get("branch")
        or written.get("branch")
        or created.get("branch")
        or context.get("branch")
        or ""
    ).strip()
    local_oid = str(verified.get("local_oid") or verified.get("remote_oid") or data.get("local_oid") or "").strip()
    if not clone or not branch or not local_oid:
        return fail(
            "missing_branch_local_oid_context",
            failure_class="terminal",
            retry_safe=False,
            operation="update_branch_local_oid",
            clone_path=clone,
            branch=branch,
            local_oid=local_oid,
        )
    values = {
        "operation": "update_branch_local_oid",
        "clone_path": clone,
        "branch": branch,
        "local_oid": local_oid,
        "repo": repo or verified.get("repo"),
        "issue": issue if issue not in (None, "") else verified.get("issue"),
        "board": board or verified.get("board"),
        "task_id": verified.get("task_id") or context.get("task_id"),
    }
    if dry_run_flag(request):
        return planned(**values)
    try:
        branch_config_set(clone, branch, "lokay-local-oid", local_oid)
    except CommandError as exc:
        return fail(
            "branch_local_oid_update_failed",
            failure_class="retryable",
            retry_safe=True,
            operation="update_branch_local_oid",
            error=str(exc),
            mutated=True,
            **values,
        )
    return ok(status="updated", mutated=True, **values)


def verify_updated_branch_local_oid(request: Request) -> Result:
    """Verify lokay-local-oid read-back matches the verified post-push tip."""
    terminal = _atomic_terminal(request, "verify_updated_branch_local_oid", "update_branch_local_oid")
    if terminal:
        return terminal
    idle = upstream_noop(request, "update_branch_local_oid")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="verify_updated_branch_local_oid")
    updated = cond_blob(request, "update_branch_local_oid")
    clone = str(updated.get("clone_path") or input_of(request).get("clone_path") or "").strip()
    branch = str(updated.get("branch") or input_of(request).get("branch") or "").strip()
    expected = str(updated.get("local_oid") or "").strip()
    values = {
        "operation": "verify_updated_branch_local_oid",
        "clone_path": clone,
        "branch": branch,
        "local_oid": expected,
        "repo": updated.get("repo"),
        "issue": updated.get("issue"),
        "board": updated.get("board"),
        "task_id": updated.get("task_id"),
    }
    if not clone or not branch or not expected:
        return fail(
            "missing_branch_local_oid_context",
            failure_class="terminal",
            retry_safe=False,
            **values,
        )
    if dry_run_flag(request):
        return planned(**values)
    try:
        actual = branch_config_get(clone, branch, "lokay-local-oid").strip()
    except CommandError as exc:
        return fail(
            "branch_local_oid_readback_failed",
            failure_class="retryable_read",
            retry_safe=True,
            error=str(exc),
            **values,
        )
    if actual != expected:
        return fail(
            "branch_local_oid_readback_mismatch",
            failure_class="terminal",
            retry_safe=False,
            expected=expected,
            actual=actual,
            **values,
        )
    return ok(status="verified", mutated=False, **values)


def _read_open_prs(repo: str, branch: str, base: str, gh: str, *, operation: str, identity: dict[str, Any]) -> Result:
    if not repo or not branch:
        return fail("missing_repo_or_branch", failure_class="terminal", retry_safe=False, operation=operation)
    try:
        proc = run_cmd([gh, "pr", "list", "--repo", repo, "--head", branch, "--base", base, "--state", "open", "--json", "number,url,baseRefName,headRefName"])
        rows = json.loads(proc.stdout or "[]")
    except (CommandError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
        return fail("pr_list_failed", failure_class="retryable_read", retry_safe=True, operation=operation, error=str(exc))
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return fail("invalid_pr_list", failure_class="terminal", retry_safe=False, operation=operation)
    return ok(status="read", operation=operation, prs=rows, repo=repo, branch=branch, base=base, **identity)


def _read_open_prs_for_issue(repo: str, issue: int, gh: str, *, operation: str) -> Result:
    try:
        proc = run_cmd([gh, "pr", "list", "--repo", repo, "--state", "open", "--limit", "1000", "--json", "number,url,baseRefName,headRefName,closingIssuesReferences"])
        rows = json.loads(proc.stdout or "[]")
    except (CommandError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
        return fail("issue_pr_list_failed", failure_class="retryable_read", retry_safe=True, operation=operation, error=str(exc), repo=repo, issue=issue)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return fail("invalid_issue_pr_list", failure_class="retryable_read", retry_safe=True, operation=operation, repo=repo, issue=issue)
    matches: list[dict[str, Any]] = []
    for row in rows:
        references = row.get("closingIssuesReferences")
        if not isinstance(references, list) or any(not isinstance(ref, dict) for ref in references):
            return fail("invalid_issue_pr_links", failure_class="retryable_read", retry_safe=True, operation=operation, repo=repo, issue=issue, pr=row)
        if any(ref.get("number") == issue for ref in references):
            matches.append(row)
    return ok(status="read", operation=operation, prs=matches, repo=repo, issue=issue)


def read_open_pr_for_branch(request: Request) -> Result:
    terminal = _atomic_terminal(request, "read_open_pr_for_branch", "verify_updated_branch_local_oid", "update_branch_local_oid", "verify_push_oid")
    if terminal: return terminal
    idle = upstream_noop(request, "verify_updated_branch_local_oid", "update_branch_local_oid", "verify_push_oid")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="read_open_pr_for_branch")
    data, cfg = input_of(request), cfg_of(request)
    # Prefer the conducted post-push peer. Package conduction only wires
    # verify_updated_branch_local_oid here; verify_push_oid is not available.
    verified = cond_blob(request, "verify_updated_branch_local_oid", "update_branch_local_oid", "verify_push_oid")
    repo = str(data.get("repo") or verified.get("repo") or "")
    branch = str(data.get("branch") or verified.get("branch") or "")
    base = str(data.get("base_branch") or cfg.get("base_branch") or "main")
    return _read_open_prs(
        repo,
        branch,
        base,
        str(cfg.get("gh_cli") or "gh"),
        operation="read_open_pr_for_branch",
        identity={"issue": verified.get("issue"), "board": verified.get("board"), "task_id": verified.get("task_id")},
    )


def decide_existing_pr(request: Request) -> Result:
    terminal = _atomic_terminal(request, "decide_existing_pr", "read_open_pr_for_branch")
    if terminal: return terminal
    idle = upstream_noop(request, "read_open_pr_for_branch")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="decide_existing_pr")
    read = cond_blob(request, "read_open_pr_for_branch"); rows = read.get("prs") or []
    if len(rows) > 1: return fail("ambiguous_existing_prs", failure_class="terminal", retry_safe=False, operation="decide_existing_pr", prs=rows)
    return ok(status="exists" if rows else "create", operation="decide_existing_pr", existing=rows[0] if rows else None, should_create=not bool(rows), repo=read.get("repo"), issue=read.get("issue"), board=read.get("board"), task_id=read.get("task_id"), branch=read.get("branch"), base=read.get("base"))


def _pr_creation_lock_path(request: Request, repo: str, issue: int) -> Path:
    data, cfg = input_of(request), cfg_of(request)
    # Live effectors put path roots in declared inputs; package config is handler-only.
    root = str(
        data.get("task_receipts")
        or cfg.get("task_receipts")
        or (data.get("paths") if isinstance(data.get("paths"), Mapping) else {}).get("task_receipts")
        or (cfg.get("paths") if isinstance(cfg.get("paths"), Mapping) else {}).get("task_receipts")
        or ""
    ).strip()
    if not root:
        raise ValueError("missing_pr_creation_lock_root")
    digest = hashlib.sha256(f"{repo}\0{issue}".encode()).hexdigest()
    directory = Path(root) / "pr-creation-locks"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{digest}.lock"


def _observe_open_pr_after_create(
    *,
    repo: str,
    issue: int,
    branch: str,
    base: str,
    gh: str,
    identity: dict[str, Any],
    observed_result,
    status: str,
) -> Result | None:
    """Prefer issue-linked PR; fall back to exact branch/base head after create."""
    linked = _read_open_prs_for_issue(repo, issue, gh, operation="create_pull_request")
    if linked.get("ok") is True:
        rows = linked.get("prs") or []
        if len(rows) == 1:
            result = observed_result(rows[0], status=status, mutated=True)
            if result.get("ok") is True:
                return result
        if len(rows) > 1:
            return fail(
                "pr_create_reconciliation_ambiguous",
                failure_class="terminal",
                retry_safe=False,
                operation="create_pull_request",
                repo=repo,
                issue=issue,
                board=identity.get("board"),
                task_id=identity.get("task_id"),
                branch=branch,
                base=base,
                prs=rows,
                mutated=True,
            )
    elif linked.get("ok") is not True and status == "created":
        # Hard read failure after successful create: keep retryable signal when no branch fallback.
        pass

    by_branch = _read_open_prs(repo, branch, base, gh, operation="create_pull_request", identity=identity)
    if by_branch.get("ok") is not True:
        if linked.get("ok") is not True:
            return fail(
                "pr_create_reconciliation_failed",
                failure_class="retryable_read",
                retry_safe=True,
                operation="create_pull_request",
                repo=repo,
                issue=issue,
                board=identity.get("board"),
                task_id=identity.get("task_id"),
                branch=branch,
                base=base,
                upstream=linked,
                mutated=True,
            )
        return None
    rows = by_branch.get("prs") or []
    if len(rows) == 1:
        result = observed_result(rows[0], status=status, mutated=True)
        if result.get("ok") is True:
            return result
        return result
    if len(rows) > 1:
        return fail(
            "pr_create_reconciliation_ambiguous",
            failure_class="terminal",
            retry_safe=False,
            operation="create_pull_request",
            repo=repo,
            issue=issue,
            board=identity.get("board"),
            task_id=identity.get("task_id"),
            branch=branch,
            base=base,
            prs=rows,
            mutated=True,
        )
    return None

def create_pull_request(request: Request) -> Result:
    terminal = _atomic_terminal(request, "create_pull_request", "decide_existing_pr")
    if terminal:
        return terminal
    idle = upstream_noop(request, "decide_existing_pr")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="create_pull_request")

    data, cfg = input_of(request), cfg_of(request)
    decision = cond_blob(request, "decide_existing_pr")
    repo = str(data.get("repo") or decision.get("repo") or "")
    branch = str(data.get("branch") or decision.get("branch") or "")
    base = str(data.get("base_branch") or decision.get("base") or cfg.get("base_branch") or "main")
    issue = data.get("issue") or decision.get("issue")
    if not repo or not branch:
        return fail("missing_repo_or_branch", failure_class="terminal", retry_safe=False, operation="create_pull_request")
    if not isinstance(issue, int) or issue <= 0:
        return fail("missing_issue", failure_class="terminal", retry_safe=False, operation="create_pull_request")
    if dry_run_flag(request):
        return planned(operation="create_pull_request", repo=repo, issue=issue, branch=branch, base=base)

    gh = str(data.get("gh_cli") or cfg.get("gh_cli") or "gh")
    title = str(data.get("title") or f"fix: {repo}#{issue}")
    body = str(data.get("body") or f"Closes #{issue}.\n\nAutomated fix via lokay.")
    identity = {"issue": issue, "board": decision.get("board"), "task_id": decision.get("task_id")}

    def observed_result(row: dict[str, Any], *, status: str, mutated: bool) -> Result:
        if str(row.get("headRefName") or "") != branch or str(row.get("baseRefName") or "") != base:
            return fail("pr_identity_mismatch", failure_class="terminal", retry_safe=False, operation="create_pull_request", repo=repo, issue=issue, branch=branch, base=base, pr=row, mutated=mutated)
        number = row.get("number")
        if not isinstance(number, int) or number <= 0:
            return fail("invalid_pr_number", failure_class="terminal", retry_safe=False, operation="create_pull_request", repo=repo, issue=issue, pr=row, mutated=mutated)
        return ok(status=status, operation="create_pull_request", repo=repo, issue=issue, board=identity["board"], task_id=identity["task_id"], branch=branch, base=base, number=number, url=row.get("url"), mutated=mutated)

    try:
        lock_path = _pr_creation_lock_path(request, repo, issue)
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            linked = _read_open_prs_for_issue(repo, issue, gh, operation="create_pull_request")
            if linked.get("ok") is not True:
                return linked
            linked_rows = linked.get("prs") or []
            if len(linked_rows) > 1:
                return fail("ambiguous_issue_prs", failure_class="terminal", retry_safe=False, operation="create_pull_request", repo=repo, issue=issue, board=identity["board"], task_id=identity["task_id"], prs=linked_rows, mutated=False)
            if linked_rows:
                existing = linked_rows[0]
                if str(existing.get("headRefName") or "") != branch or str(existing.get("baseRefName") or "") != base:
                    number = existing.get("number")
                    if not isinstance(number, int) or number <= 0:
                        return fail(
                            "invalid_pr_number",
                            failure_class="terminal",
                            retry_safe=False,
                            operation="create_pull_request",
                            repo=repo,
                            issue=issue,
                            board=identity["board"],
                            task_id=identity["task_id"],
                            pr=existing,
                            mutated=False,
                        )
                    existing_branch = str(existing.get("headRefName") or "").strip()
                    existing_base = str(existing.get("baseRefName") or "").strip() or base
                    if not existing_branch:
                        return fail(
                            "pr_identity_mismatch",
                            failure_class="terminal",
                            retry_safe=False,
                            operation="create_pull_request",
                            repo=repo,
                            issue=issue,
                            board=identity["board"],
                            task_id=identity["task_id"],
                            branch=branch,
                            base=base,
                            pr=existing,
                            mutated=False,
                        )
                    # Existing issue PR owns completion identity; do not idle on the
                    # fix-pr branch mismatch or complete_task will never finish.
                    return ok(
                        status="already_open",
                        operation="create_pull_request",
                        repo=repo,
                        issue=issue,
                        board=identity["board"],
                        task_id=identity["task_id"],
                        branch=existing_branch,
                        base=existing_base,
                        number=number,
                        url=existing.get("url"),
                        existing=existing,
                        mutated=False,
                    )
                return observed_result(existing, status="exists", mutated=False)

            current = _read_open_prs(repo, branch, base, gh, operation="create_pull_request", identity=identity)
            if current.get("ok") is not True:
                return current
            rows = current.get("prs") or []
            if len(rows) > 1:
                return fail("ambiguous_existing_prs", failure_class="terminal", retry_safe=False, operation="create_pull_request", repo=repo, issue=issue, board=identity["board"], task_id=identity["task_id"], prs=rows, mutated=False)
            if rows:
                return fail("branch_pr_issue_mismatch", failure_class="terminal", retry_safe=False, operation="create_pull_request", repo=repo, issue=issue, board=identity["board"], task_id=identity["task_id"], branch=branch, base=base, prs=rows, mutated=False)

            try:
                proc = run_cmd([gh, "pr", "create", "--repo", repo, "--base", base, "--head", branch, "--title", title, "--body", body], timeout=120)
            except (CommandError, subprocess.TimeoutExpired, OSError) as exc:
                reconciled = _observe_open_pr_after_create(
                    repo=repo,
                    issue=issue,
                    branch=branch,
                    base=base,
                    gh=gh,
                    identity=identity,
                    observed_result=observed_result,
                    status="reconciled",
                )
                if reconciled is not None and reconciled.get("ok") is True:
                    return reconciled
                return fail(
                    "pr_create_failed",
                    failure_class="reconcile_then_retry",
                    retry_safe=False,
                    operation="create_pull_request",
                    repo=repo,
                    issue=issue,
                    board=identity["board"],
                    task_id=identity["task_id"],
                    branch=branch,
                    base=base,
                    error=str(exc),
                    reconciliation=reconciled,
                    mutated=True,
                )

            reconciled = _observe_open_pr_after_create(
                repo=repo,
                issue=issue,
                branch=branch,
                base=base,
                gh=gh,
                identity=identity,
                observed_result=observed_result,
                status="created",
            )
            if reconciled is not None:
                if reconciled.get("ok") is True:
                    reconciled["stdout"] = (proc.stdout or "")[-400:]
                return reconciled
            return fail(
                "pr_create_reconciliation_missing",
                failure_class="terminal",
                retry_safe=False,
                operation="create_pull_request",
                repo=repo,
                issue=issue,
                board=identity["board"],
                task_id=identity["task_id"],
                branch=branch,
                base=base,
                prs=[],
                mutated=True,
            )
    except (OSError, ValueError) as exc:
        return fail(str(exc) if isinstance(exc, ValueError) else "pr_creation_lock_failed", failure_class="terminal", retry_safe=False, operation="create_pull_request", repo=repo, issue=issue, board=identity["board"], task_id=identity["task_id"], branch=branch, base=base, error=str(exc), mutated=False)


def reconcile_pull_request(request: Request) -> Result:
    terminal = _atomic_terminal(request, "reconcile_pull_request", "create_pull_request")
    if terminal:
        return terminal
    idle = upstream_noop(request, "create_pull_request")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="reconcile_pull_request")
    created = cond_blob(request, "create_pull_request")
    # Reuse create identity when the PR already exists for the issue so a later
    # closed/merged state cannot fail open-PR recon on the fix-pr branch.
    if created.get("ok") is True and created.get("status") in {"already_open", "exists"}:
        number = created.get("number")
        repo = str(created.get("repo") or "")
        branch = str(created.get("branch") or "")
        base = str(created.get("base") or cfg_of(request).get("base_branch") or "main")
        if isinstance(number, int) and number > 0 and repo and branch:
            return ok(
                status="reconciled",
                operation="reconcile_pull_request",
                repo=repo,
                branch=branch,
                base=base,
                number=number,
                url=created.get("url"),
                issue=created.get("issue"),
                board=created.get("board"),
                task_id=created.get("task_id"),
                prs=[{"number": number, "url": created.get("url"), "headRefName": branch, "baseRefName": base}],
                mutated=False,
            )
    data = input_of(request)
    repo = str(data.get("repo") or created.get("repo") or "")
    branch = str(data.get("branch") or created.get("branch") or "")
    base = str(data.get("base_branch") or created.get("base") or cfg_of(request).get("base_branch") or "main")
    read = _read_open_prs(repo, branch, base, str(cfg_of(request).get("gh_cli") or "gh"), operation="reconcile_pull_request", identity={"issue": created.get("issue"), "board": created.get("board"), "task_id": created.get("task_id")})
    if read.get("ok") is not True:
        return read
    prs = read.get("prs") or []
    if not prs:
        return fail("no_matching_pr", failure_class="terminal", retry_safe=False, operation="reconcile_pull_request", repo=repo, branch=branch)
    if len(prs) > 1:
        return fail("ambiguous_matching_prs", failure_class="terminal", retry_safe=False, operation="reconcile_pull_request", prs=prs)
    pr = prs[0]
    num = pr.get("number")
    if not isinstance(num, int) or num <= 0:
        return fail("invalid_pr_number", failure_class="terminal", retry_safe=False, operation="reconcile_pull_request", number=num)
    return ok(status="reconciled", operation="reconcile_pull_request", repo=repo, branch=branch, base=base, prs=prs, number=num, url=pr.get("url"), issue=created.get("issue"), board=created.get("board"), task_id=created.get("task_id"))


def normalize_pr_labels(request: Request) -> Result:
    terminal = _atomic_terminal(request, "normalize_pr_labels", "reconcile_pull_request")
    if terminal:
        return terminal
    idle = upstream_noop(request, "reconcile_pull_request")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="normalize_pr_labels")
    reconciled = cond_blob(request, "reconcile_pull_request")
    labels = input_of(request).get("labels") or ["ai:generated", "ai:pr-opened"]
    if not isinstance(labels, list) or any(not str(label).strip() for label in labels): return fail("invalid_pr_labels", failure_class="terminal", retry_safe=False, operation="normalize_pr_labels")
    normalized = list(dict.fromkeys(str(label).strip() for label in labels))
    return ok(status="normalized", operation="normalize_pr_labels", labels=normalized, repo=reconciled.get("repo"), number=reconciled.get("number") or reconciled.get("pr_number"), issue=reconciled.get("issue"), board=reconciled.get("board"), task_id=reconciled.get("task_id"))


def add_pr_label(request: Request) -> Result:
    terminal = _atomic_terminal(request, "add_pr_label", "normalize_pr_labels")
    if terminal: return terminal
    idle = upstream_noop(request, "normalize_pr_labels")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="add_pr_label")
    data, cfg = input_of(request), cfg_of(request); normalized = cond_blob(request, "normalize_pr_labels")
    repo = str(data.get("repo") or normalized.get("repo") or "")
    number = int(data.get("number") or data.get("pr_number") or normalized.get("number") or 0)
    labels = data.get("labels") or normalized.get("labels") or ([data.get("label")] if data.get("label") else [])
    if not repo or not number or not isinstance(labels, list) or not labels or any(not str(label).strip() for label in labels): return fail("missing_pr_label_inputs", failure_class="terminal", retry_safe=False, operation="add_pr_label")
    labels = [str(label).strip() for label in labels]
    if dry_run_flag(request): return planned(operation="add_pr_label", repo=repo, number=number, labels=labels)
    added = []
    try:
        for label in labels:
            run_cmd([str(cfg.get("gh_cli") or "gh"), "pr", "edit", str(number), "--repo", repo, "--add-label", str(label)], timeout=60)
            added.append(str(label))
    except CommandError as exc: return fail("pr_label_failed", failure_class="reconcile_then_retry", retry_safe=False, operation="add_pr_label", error=str(exc), labels=labels, added=added, mutated=True)
    return ok(status="added", operation="add_pr_label", repo=repo, number=number, issue=normalized.get("issue"), board=normalized.get("board"), task_id=normalized.get("task_id"), labels=added, results=[{"ok": True, "label": label} for label in added], mutated=True)


def aggregate_pr_label_results(request: Request) -> Result:
    terminal = _atomic_terminal(request, "aggregate_pr_label_results", "add_pr_label")
    if terminal: return terminal
    idle = upstream_noop(request, "add_pr_label")
    if idle: return noop(str(idle.get("reason") or "no_ready_task"), operation="aggregate_pr_label_results")
    labeled = cond_blob(request, "add_pr_label")
    results = input_of(request).get("results") or labeled.get("results") or []
    if not isinstance(results, list): return fail("missing_pr_label_results", failure_class="terminal", retry_safe=False, operation="aggregate_pr_label_results")
    failed = [r for r in results if isinstance(r, dict) and r.get("ok") is False]
    if failed: return fail("partial_labels_failed" if len(failed) < len(results) else "all_labels_failed", failure_class="reconcile_then_retry", retry_safe=False, operation="aggregate_pr_label_results", results=results, mutated=bool(results))
    return ok(status="labeled", operation="aggregate_pr_label_results", repo=labeled.get("repo"), number=labeled.get("number"), issue=labeled.get("issue"), board=labeled.get("board"), task_id=labeled.get("task_id"), results=results, mutated=bool(results))


def add_issue_label(request: Request) -> Result:
    terminal = _atomic_terminal(request, "add_issue_label", "aggregate_pr_label_results")
    if terminal: return terminal
    idle = upstream_noop(request, "aggregate_pr_label_results")
    if idle: return noop(str(idle.get("reason") or "no_ready_task"), operation="add_issue_label")
    data, cfg = input_of(request), cfg_of(request); labeled = cond_blob(request, "aggregate_pr_label_results")
    repo = str(data.get("repo") or labeled.get("repo") or ""); issue = int(data.get("issue") or data.get("number") or labeled.get("issue") or 0)
    labels = data.get("labels") or ([data.get("label")] if data.get("label") else ["ai:pr-opened"])
    if not repo or not issue or not isinstance(labels, list) or not labels or any(not str(label).strip() for label in labels): return fail("missing_issue_label_inputs", failure_class="terminal", retry_safe=False, operation="add_issue_label")
    if dry_run_flag(request): return planned(operation="add_issue_label", repo=repo, issue=issue, labels=labels)
    added = []
    try:
        for label in labels:
            run_cmd([str(cfg.get("gh_cli") or "gh"), "issue", "edit", str(issue), "--repo", repo, "--add-label", str(label)], timeout=60); added.append(str(label))
    except CommandError as exc: return fail("issue_label_failed", failure_class="reconcile_then_retry", retry_safe=False, operation="add_issue_label", error=str(exc), labels=labels, added=added, mutated=True)
    return ok(status="added", operation="add_issue_label", repo=repo, issue=issue, board=labeled.get("board"), task_id=labeled.get("task_id"), labels=added, results=[{"ok": True, "label": label} for label in added], mutated=True)


def aggregate_issue_label_results(request: Request) -> Result:
    terminal = _atomic_terminal(request, "aggregate_issue_label_results", "add_issue_label")
    if terminal: return terminal
    idle = upstream_noop(request, "add_issue_label")
    if idle: return noop(str(idle.get("reason") or "no_ready_task"), operation="aggregate_issue_label_results")
    labeled = cond_blob(request, "add_issue_label"); results = input_of(request).get("results") or labeled.get("results") or []
    if not isinstance(results, list): return fail("missing_issue_label_results", failure_class="terminal", retry_safe=False, operation="aggregate_issue_label_results")
    failed = [r for r in results if isinstance(r, dict) and r.get("ok") is False]
    if failed: return fail("partial_labels_failed" if len(failed) < len(results) else "all_labels_failed", failure_class="reconcile_then_retry", retry_safe=False, operation="aggregate_issue_label_results", results=results, mutated=bool(results))
    return ok(status="labeled", operation="aggregate_issue_label_results", repo=labeled.get("repo"), issue=labeled.get("issue"), board=labeled.get("board"), task_id=labeled.get("task_id"), results=results, mutated=bool(results))


def build_dispatch_receipt(request: Request) -> Result:
    terminal = _atomic_terminal(request, "build_dispatch_receipt", "aggregate_issue_label_results")
    if terminal: return terminal
    idle = upstream_noop(request, "aggregate_issue_label_results")
    if idle: return noop(str(idle.get("reason") or "no_ready_task"), operation="build_dispatch_receipt")
    data = input_of(request); labeled = cond_blob(request, "aggregate_issue_label_results")
    payload = dict(data.get("payload") or {"repo": labeled.get("repo"), "issue": labeled.get("issue"), "board": labeled.get("board"), "task_id": labeled.get("task_id")})
    return ok(status="built", operation="build_dispatch_receipt", receipt_path=data.get("receipt_path") or cfg_of(request).get("receipt_path"), payload=payload, repo=labeled.get("repo"), issue=labeled.get("issue"), board=labeled.get("board"), task_id=labeled.get("task_id"))


def publish_dispatch_receipt(request: Request) -> Result:
    terminal = _atomic_terminal(request, "publish_dispatch_receipt", "build_dispatch_receipt")
    if terminal: return terminal
    idle = upstream_noop(request, "build_dispatch_receipt")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="publish_dispatch_receipt")
    data = input_of(request)
    path = str(data.get("receipt_path") or cfg_of(request).get("receipt_path") or cond_get(request, "receipt_path", "build_dispatch_receipt") or "")
    payload = data.get("payload") or cond_get(request, "payload", "build_dispatch_receipt")
    if not path or not isinstance(payload, dict):
        return fail("missing_receipt_inputs", failure_class="terminal", retry_safe=False, operation="publish_dispatch_receipt")
    if dry_run_flag(request):
        return planned(operation="publish_dispatch_receipt", receipt_path=path, payload=payload)
    target = Path(path)
    temporary: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
        temporary = Path(temporary_name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, TypeError, ValueError) as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        return fail("receipt_write_failed", failure_class="retryable", retry_safe=True, operation="publish_dispatch_receipt", receipt_path=path, error=str(exc), mutated=True)
    return ok(status="published", operation="publish_dispatch_receipt", receipt_path=path, payload=payload, mutated=True)


def verify_dispatch_receipt(request: Request) -> Result:
    terminal = _atomic_terminal(request, "verify_dispatch_receipt", "publish_dispatch_receipt", "build_dispatch_receipt")
    if terminal: return terminal
    idle = upstream_noop(request, "publish_dispatch_receipt", "build_dispatch_receipt")
    if idle:
        return noop(str(idle.get("reason") or "no_ready_task"), operation="verify_dispatch_receipt")
    data = input_of(request); path = str(data.get("receipt_path") or cfg_of(request).get("receipt_path") or cond_get(request, "receipt_path", "publish_dispatch_receipt") or cond_get(request, "receipt_path", "build_dispatch_receipt") or ""); payload = data.get("payload") or cond_get(request, "payload", "build_dispatch_receipt")
    if not path or not Path(path).is_file(): return fail("receipt_missing", failure_class="terminal", retry_safe=False, operation="verify_dispatch_receipt")
    try: actual = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: return fail("receipt_readback_failed", failure_class="terminal", retry_safe=False, operation="verify_dispatch_receipt", error=str(exc))
    if payload and actual != payload: return fail("receipt_conflict", failure_class="terminal", retry_safe=False, operation="verify_dispatch_receipt")
    return ok(status="verified", operation="verify_dispatch_receipt", receipt_path=path)
