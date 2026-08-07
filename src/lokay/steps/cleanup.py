"""Mega-atomic effectors: cleanup domain."""

from __future__ import annotations

from contextlib import contextmanager

import fcntl
import json
import os
import stat
import re
import tempfile
import sqlite3
from pathlib import Path
from typing import Any
from lokay.envelope import Request, Result

from lokay.adapters_cli import CommandError, hermes_kanban_json, run_cmd
from lokay.adapters_git import (
    branch_config_section,
    branch_config_get,
    branch_exists,
    delete_local_branch as git_delete_local_branch,
    delete_local_branch_if_head as git_delete_local_branch_if_head,
    git,
    is_dirty,
    local_branch_head,
    parse_worktree_porcelain,
    status_porcelain,
    worktree_list,
    worktree_remove,
)
from lokay.steps.claim import claim_directory_lock

from lokay.envelope import (
    cfg_of,
    conduction_of,
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
_TERMINAL_PROCESS_STATUSES = {"failed", "cancelled", "timed_out"}


def _cleanup_aggregate(request: Request) -> tuple[str, dict[str, Any]] | None:
    """Return the composed aggregate predecessor, including malformed blobs."""
    conduction = conduction_of(request)
    for name, value in conduction.items():
        if name == "aggregate_lane_results" or str(name).endswith("_aggregate_lane_results"):
            return str(name), dict(value) if isinstance(value, dict) else {}
    return None


def _cleanup_aggregate_gate(request: Request, operation: str) -> tuple[Result | None, dict[str, Any]]:
    """Require a verified, authorized aggregate identity before any cleanup work.

    Presence of an aggregate predecessor is authoritative: unauthorized, malformed,
    or incomplete identity fails closed and never falls back to bare input/config.
    """
    aggregate_entry = _cleanup_aggregate(request)
    if aggregate_entry is None:
        return None, {}
    name, aggregate = aggregate_entry
    if (
        not aggregate
        or aggregate.get("ok") is not True
        or str(aggregate.get("status") or "") in _TERMINAL_PROCESS_STATUSES
    ):
        return fail(
            "upstream_failed",
            failure_class="terminal",
            retry_safe=False,
            operation=operation,
            upstream_effector=name,
            upstream=aggregate,
        ), {}
    if aggregate.get("cleanup_authorized") is not True:
        return fail(
            "cleanup_not_authorized",
            failure_class="terminal",
            retry_safe=False,
            operation=operation,
            aggregate=name,
        ), {}
    identity = aggregate.get("cleanup_identity")
    repair_identity = isinstance(identity, dict) and identity.get("local_branch") not in (None, "")
    required = (
        ("repo", "issue", "pr_number", "branch", "head_oid", "board", "clone_path", "priority")
        if not repair_identity
        else (
            "repo",
            "issue",
            "pr_number",
            "branch",
            "local_branch",
            "worktree_path",
            "receipt",
            "remote_oid",
            "target_branch",
            "clone_path",
        )
    )
    if not isinstance(identity, dict) or any(identity.get(key) in (None, "") for key in required):
        return fail(
            "cleanup_identity_invalid",
            failure_class="terminal",
            retry_safe=False,
            operation=operation,
            upstream_effector=name,
            aggregate=aggregate,
        ), {}
    if repair_identity and str(identity.get("target_branch")) != str(identity.get("branch")):
        return fail(
            "cleanup_identity_invalid",
            failure_class="terminal",
            retry_safe=False,
            operation=operation,
            field="target_branch",
            aggregate=aggregate,
        ), {}
    try:
        if isinstance(identity["issue"], bool) or int(identity["issue"]) <= 0:
            raise ValueError("issue must be positive")
        if isinstance(identity["pr_number"], bool) or int(identity["pr_number"]) <= 0:
            raise ValueError("pr_number must be positive")
    except (TypeError, ValueError) as exc:
        return fail(
            "cleanup_identity_invalid",
            failure_class="terminal",
            retry_safe=False,
            operation=operation,
            error=str(exc),
            aggregate=aggregate,
        ), {}
    data, cfg = input_of(request), cfg_of(request)
    aliases = {
        "issue": ("issue",),
        "pr_number": ("pr_number", "number"),
        "branch": ("branch", "head_ref"),
        "repo": ("repo",),
        "clone_path": ("clone_path",),
        "head_oid": ("head_oid",),
    }
    if repair_identity:
        aliases.update(
            {
                "local_branch": ("local_branch",),
                "worktree_path": ("worktree_path",),
                "target_branch": ("target_branch",),
                "receipt": ("receipt", "receipt_path"),
                "remote_oid": ("remote_oid", "after_oid"),
                "task": ("task", "task_id"),
            }
        )
    else:
        aliases.update({"worktree_path": ("worktree_path",)})
    for key, names in aliases.items():
        expected = str(identity.get(key) or "")
        for source_name, source in (("input", data), ("config", cfg)):
            for candidate in names:
                value = source.get(candidate)
                if value not in (None, "") and str(value) != expected:
                    return fail(
                        "cleanup_identity_conflict",
                        failure_class="terminal",
                        retry_safe=False,
                        operation=operation,
                        field=key,
                        source=source_name,
                        expected=expected,
                        actual=value,
                    ), {}
    return None, dict(identity)


def _cleanup_value(request: Request, key: str, *aliases: str, default: Any = "") -> Any:
    """Resolve aggregate identity before static cleanup configuration.

    When an aggregate predecessor is present, only a verified authorized identity
    may supply values. Gate failures and missing identity fields never fall back
    to bare input/config (fail closed).
    """
    data, cfg = input_of(request), cfg_of(request)
    if _cleanup_aggregate(request) is not None:
        gate, identity = _cleanup_aggregate_gate(request, "resolve_cleanup_context")
        if gate is not None or not identity:
            return default
        for candidate in (key, *aliases):
            if identity.get(candidate) not in (None, ""):
                return identity[candidate]
        return default
    for candidate in (key, *aliases):
        if data.get(candidate) not in (None, ""):
            return data[candidate]
    for candidate in (key, *aliases):
        if cfg.get(candidate) not in (None, ""):
            return cfg[candidate]
    return default




def _task_marker_matches(task: object, marker: str) -> bool:
    if not isinstance(task, dict):
        return False
    body = str(task.get("body") or task.get("description") or "")
    return bool(re.search(r"(?m)^Idempotency-Key:\s*" + re.escape(marker) + r"$", body))


def _cleanup_provenance(data: dict[str, object], cfg: dict[str, object], branch: str, conduction: dict[str, object] | None = None) -> dict[str, str]:
    conduction = conduction or {}
    parsed = next((conduction[key] for key in ("parse_cleanup_issue_number", "dispatch_parse_issue_ref") if isinstance(conduction.get(key), dict)), {})
    receipt = next((conduction[key] for key in ("dispatch_write_dispatch_receipt",) if isinstance(conduction.get(key), dict)), {})
    return {
        "task": str(data.get("task_id") or cfg.get("task_id") or parsed.get("task_id") or "").strip(),
        "issue": str(data.get("issue") or cfg.get("issue") or parsed.get("issue") or "").strip(),
        "receipt": str(data.get("receipt_id") or data.get("receipt_path") or cfg.get("receipt_id") or cfg.get("receipt_path") or parsed.get("receipt_id") or parsed.get("receipt_path") or receipt.get("receipt_path") or "").strip(),
        "repo": str(data.get("repo") or cfg.get("repo") or parsed.get("repo") or "").strip(),
        "branch": branch,
    }

def _cleanup_owner_matches(clone_path: str, branch: str, expected: dict[str, str], *, task_optional: bool = False) -> bool:
    keys = ("issue", "receipt", "repo")
    if not all(expected.get(key) for key in keys):
        return False
    if not task_optional and not expected.get("task"):
        return False
    for key in (*keys, *(('task',) if expected.get("task") else ())):
        try:
            if branch_config_get(clone_path, branch, f"lokay-{key}").strip() != expected[key]:
                return False
        except CommandError:
            return False
    return True


def _task_id(task: dict[str, object]) -> object:
    return task.get("id") or task.get("task_id")




def check_issue_closed(request: Request) -> Result:
    """Read GitHub issue state."""
    import json

    data = input_of(request)
    cfg = cfg_of(request)
    parsed = cond_blob(request, "parse_cleanup_issue_number")
    upstream = upstream_noop(request, "parse_cleanup_issue_number", "resolve_cleanup_branch_source")
    if upstream:
        return noop(str(upstream.get("reason") or "no_branch"), **{k: v for k, v in upstream.items() if k not in {"status", "ok", "mutated", "reason", "dry_run"}})
    aggregate, identity = _cleanup_aggregate_gate(request, "check_issue_closed")
    if aggregate is not None:
        return aggregate
    repo = str(data.get("repo") or identity.get("repo") or parsed.get("repo") or cfg.get("repo") or "")
    issue = int(data.get("issue") or identity.get("issue") or parsed.get("issue") or 0)
    gh = str(cfg.get("gh_cli") or "gh")
    if not repo or not issue:
        return fail("missing_repo_or_issue", failure_class="terminal", retry_safe=False, repo=repo, issue=issue, idempotency_key=f"cleanup:issue:{repo}:{issue}:check-closed")
    try:
        proc = run_cmd(
            [gh, "issue", "view", str(issue), "--repo", repo, "--json", "state"],
            timeout=60,
        )
        raw = (proc.stdout or "").strip()
        if not raw:
            raise ValueError("blank issue state read-back")
        payload = json.loads(raw)
        if not isinstance(payload, dict) or str(payload.get("state") or "").upper() not in {"OPEN", "CLOSED"}:
            raise ValueError("invalid issue state read-back")
        state = str(payload["state"]).upper()
    except CommandError as exc:
        return fail("issue_view_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), repo=repo, issue=issue, idempotency_key=f"cleanup:issue:{repo}:{issue}:check-closed")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("invalid_issue_readback", failure_class="terminal", retry_safe=False, error=str(exc), repo=repo, issue=issue, idempotency_key=f"cleanup:issue:{repo}:{issue}:check-closed")
    return ok(status="checked", state=state, closed=state == "CLOSED", repo=repo, issue=issue)


def check_no_open_pr_for_branch(request: Request) -> Result:
    """True when no open PR exists for head branch."""
    import json

    data = input_of(request)
    cfg = cfg_of(request)
    aggregate, identity = _cleanup_aggregate_gate(request, "check_no_open_pr_for_branch")
    if aggregate is not None:
        return aggregate
    parsed = cond_blob(request, "parse_cleanup_issue_number")
    repo = str(data.get("repo") or identity.get("repo") or parsed.get("repo") or cfg.get("repo") or "")
    branch = str(data.get("branch") or identity.get("branch") or parsed.get("branch") or cfg.get("branch") or "")
    gh = str(cfg.get("gh_cli") or "gh")
    if not repo or not branch:
        return fail("missing_repo_or_branch", failure_class="terminal", retry_safe=False, repo=repo, branch=branch, idempotency_key=f"cleanup:pr:{repo}:{branch}:check-open")
    try:
        proc = run_cmd(
            [gh, "pr", "list", "--repo", repo, "--head", branch, "--state", "open", "--json", "number"],
            timeout=60,
        )
        raw = (proc.stdout or "").strip()
        if not raw:
            raise ValueError("blank PR list read-back")
        prs = json.loads(raw)
        if not isinstance(prs, list) or any(not isinstance(pr, dict) for pr in prs):
            raise ValueError("invalid PR list read-back")
    except CommandError as exc:
        return fail("pr_list_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), repo=repo, branch=branch, idempotency_key=f"cleanup:pr:{repo}:{branch}:check-open")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("invalid_pr_list", failure_class="terminal", retry_safe=False, error=str(exc), repo=repo, branch=branch, idempotency_key=f"cleanup:pr:{repo}:{branch}:check-open")
    return ok(status="checked", open_count=len(prs), safe_to_cleanup=len(prs) == 0, prs=prs)


def remove_worktree(request: Request) -> Result:
    """Remove the already-read, owned, clean worktree."""
    data, cfg = input_of(request), cfg_of(request)
    cleanup_key = f"cleanup:worktree:{data.get('clone_path') or cfg.get('clone_path') or ''}:{data.get('worktree_path') or cfg.get('worktree_path') or ''}:remove"
    upstream = _cleanup_upstream_failure(request, "remove_worktree", "verify_cleanup_guards", "read_worktree_ownership", "read_worktree_cleanliness")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "remove_worktree", "verify_cleanup_guards", "read_worktree_ownership", "read_worktree_cleanliness", "validate_cleanup_identity", "parse_cleanup_issue_number", "resolve_cleanup_branch_source")
    if idle:
        return idle
    guard = cond_blob(request, "verify_cleanup_guards")
    if guard.get("ok") is not True:
        return fail("cleanup_guard_failed", failure_class=str(guard.get("failure_class") or "terminal"), retry_safe=bool(guard.get("retry_safe", False)), guard="verify_cleanup_guards", guard_output=guard, idempotency_key=cleanup_key)
    ownership = cond_blob(request, "read_worktree_ownership")
    if ownership.get("ok") is not True:
        return fail("worktree_ownership_read_failed", failure_class=str(ownership.get("failure_class") or "terminal"), retry_safe=bool(ownership.get("retry_safe", False)), evidence=ownership, idempotency_key=cleanup_key)
    clone_path = str(data.get("clone_path") or cfg.get("clone_path") or ownership.get("clone_path") or "").strip()
    worktree_path = str(data.get("worktree_path") or cfg.get("worktree_path") or ownership.get("worktree_path") or "").strip()
    if ownership.get("status") == "already_absent" or ownership.get("absent") is True:
        if not clone_path or not worktree_path:
            return fail("missing_clone_or_worktree", failure_class="terminal", retry_safe=False, clone_path=clone_path, worktree_path=worktree_path, idempotency_key=cleanup_key)
        return ok(status="already_absent", clone_path=clone_path, worktree_path=worktree_path, branch=ownership.get("branch"), absent=True, idempotency_key=cleanup_key, mutated=False, retry_safe=True)
    cleanliness = cond_blob(request, "read_worktree_cleanliness")
    if cleanliness.get("ok") is not True:
        return fail("worktree_cleanliness_read_failed", failure_class=str(cleanliness.get("failure_class") or "terminal"), retry_safe=bool(cleanliness.get("retry_safe", False)), evidence=cleanliness, idempotency_key=cleanup_key)
    if cleanliness.get("dirty") is True or cleanliness.get("clean") is not True:
        return fail("worktree_dirty", failure_class="terminal", retry_safe=False, clone_path=ownership.get("clone_path"), worktree_path=ownership.get("worktree_path"), mutated=False, idempotency_key=cleanup_key)
    if not clone_path or not worktree_path:
        return fail("missing_clone_or_worktree", failure_class="terminal", retry_safe=False, clone_path=clone_path, worktree_path=worktree_path, idempotency_key=cleanup_key)
    if dry_run_flag(request):
        return planned(clone_path=clone_path, worktree_path=str(Path(worktree_path).resolve()), force=bool(data.get("force", False)))
    try:
        worktree_remove(clone_path, worktree_path, force=bool(data.get("force", False)))
    except CommandError as exc:
        return fail("remove_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), clone_path=clone_path, worktree_path=worktree_path, mutated=True, idempotency_key=cleanup_key)
    return ok(status="removed", clone_path=clone_path, worktree_path=worktree_path, branch=ownership.get("branch"), idempotency_key=cleanup_key, mutated=True, retry_safe=True)






@contextmanager
def _receipt_directory_lock(directory: Path):
    """Serialize publication, reconciliation, and rollback in one directory."""
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

def _private_receipt_stat(path: Path) -> os.stat_result | None:
    """Return metadata only for a private, regular, single-link receipt inode."""
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_nlink != 1:
        raise ValueError("receipt is not a private single-link regular file")
    return metadata



def _publish_cleanup_receipt(p: Path, payload: dict[str, Any], path: str) -> Result:
    def existing_result() -> Result | None:
        try:
            if _private_receipt_stat(p) is None:
                return None
            existing = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return fail("receipt_conflict", failure_class="terminal", retry_safe=False, error=str(exc), receipt_path=path)
        if existing == payload:
            try:
                dir_fd = os.open(str(p.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError as exc:
                return fail("receipt_durability_unconfirmed", failure_class="terminal", retry_safe=False, error=str(exc), receipt_path=path)
            return ok(status="exists", receipt_path=path, payload=payload, mutated=False)
        return fail("receipt_conflict", failure_class="terminal", retry_safe=False, receipt_path=path)

    prior = existing_result()
    if prior is not None:
        return prior
    tmp_path: Path | None = None
    published_identity: tuple[int, int] | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
            published = os.fstat(fh.fileno())
            published_identity = (published.st_dev, published.st_ino)
        try:
            os.link(tmp_path, p)
        except FileExistsError:
            prior = existing_result()
            if prior is not None:
                return prior
            return fail("receipt_conflict", failure_class="terminal", retry_safe=False, receipt_path=path)
        os.unlink(tmp_path)
        tmp_path = None
        dir_fd = os.open(str(p.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        if _private_receipt_stat(p) is None or json.loads(p.read_text(encoding="utf-8")) != payload:
            raise ValueError("receipt read-back mismatch")
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        rollback_error: Exception | None = None
        if published_identity is not None:
            try:
                current = _private_receipt_stat(p)
                if current is not None and (current.st_dev, current.st_ino) == published_identity:
                    os.unlink(p)
                dir_fd = os.open(str(p.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception as rollback_exc:
                rollback_error = rollback_exc
        error = str(exc)
        if rollback_error is not None:
            error = f"{error}; receipt rollback durability unconfirmed: {rollback_error}"
        return fail("receipt_write_failed", failure_class="terminal", retry_safe=False, error=error, receipt_path=path, mutated=True)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
    return ok(status="written", receipt_path=path, payload=payload, mutated=True)


def _cleanup_identity(data: dict[str, Any], cfg: dict[str, Any], payload: dict[str, Any], evidence: dict[str, dict[str, Any]], receipt_path: str) -> dict[str, Any] | None:
    payload_entity = payload.get("entity") if isinstance(payload.get("entity"), dict) else {}
    sources: tuple[dict[str, Any], ...] = (data, cfg, payload_entity, *tuple(evidence.values()))

    aliases = {
        "task": ("task_id", "task"),
        "repo": ("repo",),
        "issue": ("issue",),
        "receipt": ("receipt_id", "receipt"),
        "branch": ("branch",),
        "local_branch": ("local_branch",),
        "worktree_path": ("worktree_path",),
        "pr_number": ("pr_number",),
        "head_oid": ("head_oid",),
        "remote_oid": ("remote_oid", "after_oid"),
        "target_branch": ("target_branch", "branch"),
        "clone_path": ("clone_path",),
        "base_sha": ("base_sha",),
        "merge_oid": ("merge_oid",),
        "origin_main_sha": ("origin_main_sha",),
    }

    def present(value: Any) -> bool:
        return value is not None and bool(str(value).strip())

    def task_value(value: Any) -> Any:
        if isinstance(value, dict):
            return value.get("id") or value.get("task_id") or value.get("task")
        return value

    def values_for(keys: tuple[str, ...]) -> list[Any]:
        values: list[Any] = []
        for source in sources:
            for key in keys:
                candidate = source.get(key)
                if key in {"task", "task_id"}:
                    candidate = task_value(candidate)
                if not present(candidate):
                    continue
                if candidate not in values:
                    values.append(candidate)
        return values

    identity: dict[str, Any] = {}
    for key, keys in aliases.items():
        candidates = values_for(keys)
        # Ownership/provenance receipt stays distinct from the generated cleanup output path.
        # Fall back to the output path only when no ownership receipt is available.
        if key == "receipt" and not candidates and present(receipt_path):
            candidates.append(receipt_path)
        if not candidates:
            continue
        if key == "issue":
            normalized: list[int] = []
            for candidate in candidates:
                if isinstance(candidate, bool):
                    return None
                try:
                    number = int(candidate)
                except (TypeError, ValueError):
                    return None
                if number <= 0:
                    return None
                if number not in normalized:
                    normalized.append(number)
            if len(normalized) != 1:
                return None
            identity[key] = normalized[0]
            continue
        normalized = [str(candidate).strip() for candidate in candidates]
        if len(set(normalized)) != 1:
            return None
        identity[key] = normalized[0]

    repair = present(identity.get("local_branch"))
    required = ("repo", "receipt", "branch", "clone_path", "worktree_path", "remote_oid", "target_branch") if repair else ("repo", "receipt", "branch", "clone_path", "worktree_path")
    if any(not present(identity.get(key)) for key in required) or "issue" not in identity:
        return None
    if repair and identity.get("target_branch") != identity.get("branch"):
        return None
    if not repair:
        identity.pop("local_branch", None)
        identity.pop("remote_oid", None)
        identity.pop("target_branch", None)
    return identity
    removed = evidence["remove_worktree"]
    deleted = evidence["delete_local_branch"]
    released = evidence["release_claim_file"]
    if parse.get("ok") is not True or closed.get("ok") is not True or no_open_pr.get("ok") is not True:
        return None
    if closed.get("status") != "checked" or closed.get("closed") is not True:
        return None
    if no_open_pr.get("status") != "checked" or no_open_pr.get("safe_to_cleanup") is not True or no_open_pr.get("open_count") != 0:
        return None
    if parse.get("status") == "parsed" and removed.get("status") == "removed" and deleted.get("status") == "deleted" and released.get("status") == "released":
        if all(blob.get("ok") is True for blob in (removed, deleted, released)):
            return "success"
    return None


def _cleanup_upstream_failure(request: Request, operation: str, *ids: str) -> Result | None:
    """Return a fail-closed terminal peer result for a cleanup operation.

    Any aggregate gate decision (unauthorized, invalid, or failed) is authoritative
    and blocks cleanup; never ignore non-failed aggregate statuses.
    """
    aggregate, _ = _cleanup_aggregate_gate(request, operation)
    if aggregate is not None:
        return aggregate
    from lokay.envelope import terminal_upstream
    return terminal_upstream(request, operation, *ids)


def _cleanup_upstream_noop(request: Request, operation: str, *ids: str) -> Result | None:
    """Return a canonical upstream no-target noop for a cleanup operation."""
    aggregate, _ = _cleanup_aggregate_gate(request, operation)
    if aggregate is not None:
        return aggregate
    upstream = upstream_noop(request, *ids)
    if upstream:
        return noop(str(upstream.get("reason") or "no_branch"), operation=operation)
    return None



def resolve_cleanup_branch_source(request: Request) -> Result:
    """Resolve the branch from the verified aggregate identity when composed."""
    data, cfg = input_of(request), cfg_of(request)
    upstream = _cleanup_upstream_failure(request, "resolve_cleanup_branch_source", "triage_close_linked_issue")
    if upstream:
        return upstream
    if _cleanup_aggregate(request) is not None:
        gate, identity = _cleanup_aggregate_gate(request, "resolve_cleanup_branch_source")
        if gate is not None:
            return gate
        return ok(status="resolved", branch=str(identity["branch"]).strip(), source="aggregate_cleanup_identity", cleanup_identity=identity, **{key: value for key, value in identity.items() if key != "branch"})
    conduction = data.get("conduction") if isinstance(data.get("conduction"), dict) else {}
    sources = [data, cfg]
    for name in ("triage_close_linked_issue", "triage_load_pr_fields", "dispatch_prepare_worktree", "dispatch_parse_issue_ref"):
        blob = conduction.get(name)
        if isinstance(blob, dict):
            sources.append(blob)
            for key in ("payload", "entity", "verified_provenance"):
                nested = blob.get(key)
                if isinstance(nested, dict):
                    sources.append(nested)
    branch = next((str(source.get(key)).strip() for source in sources for key in ("branch", "head_ref", "headRefName") if source.get(key) not in (None, "")), "")
    if not branch:
        return noop("no_branch", branch="")
    return ok(status="resolved", branch=branch, source="input_or_conduction")


def parse_cleanup_issue_number(request: Request) -> Result:
    """Purely parse the issue number encoded in a cleanup branch."""
    data = input_of(request)
    upstream = _cleanup_upstream_failure(request, "parse_cleanup_issue_number", "resolve_cleanup_branch_source")
    if upstream:
        return upstream
    source = cond_blob(request, "resolve_cleanup_branch_source")
    branch = str(data.get("branch") or source.get("branch") or "").strip()
    if not branch:
        return noop("no_branch", branch="")
    match = re.search(r"(?:^|/)ai/fix/([1-9][0-9]*)(?:-|$)", branch)
    if match is None:
        return fail("unparseable_branch", failure_class="terminal", retry_safe=False, branch=branch)
    return ok(status="parsed", branch=branch, issue=int(match.group(1)))


def _durable_branch_local_oid(db_path: str, receipt: str, *, repo: str, issue: str, branch: str, task: str) -> str:
    """Recover one legacy branch head from its exact dispatch run evidence."""
    match = re.fullmatch(r"auto-worker-dispatch-(auto-worker-[A-Za-z0-9-]+)\.json", Path(receipt).name)
    if not db_path or match is None:
        return ""
    connection = sqlite3.connect(f"file:{Path(db_path).expanduser().resolve()}?mode=ro", uri=True, timeout=0)
    try:
        rows = connection.execute(
            "SELECT output_json FROM processes WHERE run_id = ? AND id LIKE ? AND status = 'succeeded'",
            (match.group(1), "%:dispatch_verify_worktree_head"),
        ).fetchall()
    finally:
        connection.close()
    heads: set[str] = set()
    for (raw,) in rows:
        try:
            values = json.loads(raw or "{}").get("values", {})
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            isinstance(values, dict) and values.get("ok") is True and values.get("status") == "verified"
            and str(values.get("repo") or "") == repo and str(values.get("issue") or "") == issue
            and str(values.get("branch") or "") == branch and str(values.get("task_id") or "") == task
            and str(values.get("head") or "").strip()
        ):
            heads.add(str(values["head"]).strip())
    return next(iter(heads)) if len(heads) == 1 else ""


def read_branch_ownership(request: Request) -> Result:
    """Read all branch ownership keys with one git-config read."""
    data, cfg = input_of(request), cfg_of(request)
    upstream = _cleanup_upstream_failure(request, "read_branch_ownership", "resolve_cleanup_branch_source", "parse_cleanup_issue_number")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "read_branch_ownership", "resolve_cleanup_branch_source", "parse_cleanup_issue_number")
    if idle:
        return idle
    aggregate_gate, aggregate_identity = _cleanup_aggregate_gate(request, "read_branch_ownership")
    if aggregate_gate is not None:
        return aggregate_gate
    branch = str(data.get("local_branch") or aggregate_identity.get("local_branch") or data.get("branch") or cond_get(request, "branch", "parse_cleanup_issue_number", "resolve_cleanup_branch_source", default=cfg.get("branch", ""))).strip()
    clone = str(data.get("clone_path") or aggregate_identity.get("clone_path") or _cleanup_value(request, "clone_path", default=cfg.get("clone_path", "")) or "").strip()
    if not clone or not branch:
        return fail("missing_branch_ownership_context", failure_class="terminal", retry_safe=False, clone_path=clone, branch=branch)
    pattern = rf"^branch\.{re.escape(branch_config_section(branch))}\.lokay-(task|issue|receipt|repo|local-oid)$"
    try:
        raw = git(["config", "--local", "--get-regexp", pattern], cwd=clone)
    except CommandError as exc:
        if exc.returncode != 1:
            return fail("branch_ownership_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), clone_path=clone, branch=branch)
        raw = ""
    ownership: dict[str, str] = {}
    prefix = f"branch.{branch_config_section(branch)}.lokay-"
    for line in raw.splitlines():
        key, separator, value = line.partition(" ")
        if not separator or not key.startswith(prefix):
            continue
        name = key.removeprefix(prefix)
        if name in {"task", "issue", "receipt", "repo", "local-oid"} and value.strip():
            ownership[name] = value.strip()
    if not {"task", "issue", "receipt", "repo"}.issubset(ownership):
        return fail("branch_ownership_missing", failure_class="terminal", retry_safe=False, clone_path=clone, branch=branch, ownership=ownership)
    if "local-oid" not in ownership:
        db_path = str(data.get("db_path") or cfg.get("db_path") or "").strip()
        try:
            recovered = _durable_branch_local_oid(db_path, ownership["receipt"], repo=ownership["repo"], issue=ownership["issue"], branch=branch, task=ownership["task"])
        except sqlite3.Error as exc:
            return fail("branch_ownership_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), clone_path=clone, branch=branch)
        if not recovered:
            return fail("branch_head_provenance_missing", failure_class="terminal", retry_safe=False, clone_path=clone, branch=branch, ownership=ownership)
        ownership["local-oid"] = recovered
    return ok(status="read", clone_path=clone, branch=branch, ownership=ownership, local_oid=ownership["local-oid"], **ownership)


def derive_cleanup_paths(request: Request) -> Result:
    """Purely derive a confined worktree path from a branch and configured root."""
    data, cfg = input_of(request), cfg_of(request)
    upstream = _cleanup_upstream_failure(request, "derive_cleanup_paths", "parse_cleanup_issue_number", "resolve_cleanup_branch_source")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "derive_cleanup_paths", "parse_cleanup_issue_number", "resolve_cleanup_branch_source")
    if idle:
        return idle
    parsed = cond_blob(request, "parse_cleanup_issue_number")
    aggregate_gate, aggregate_identity = _cleanup_aggregate_gate(request, "derive_cleanup_paths")
    if aggregate_gate is not None:
        return aggregate_gate
    branch = str(data.get("branch") or aggregate_identity.get("branch") or parsed.get("branch") or cond_get(request, "branch", "resolve_cleanup_branch_source", default=cfg.get("branch", ""))).strip()
    local_branch = str(data.get("local_branch") or aggregate_identity.get("local_branch") or "").strip() or branch
    root_value = str(data.get("worktree_root") or cfg.get("worktree_root") or "").strip()
    if not local_branch or not root_value:
        return fail("missing_cleanup_path_context", failure_class="terminal", retry_safe=False, branch=branch, local_branch=local_branch, worktree_root=root_value)
    root = Path(root_value).expanduser().resolve(strict=False)
    worktree = (root / re.sub(r"[^a-zA-Z0-9._/-]+", "-", local_branch)).resolve(strict=False)
    if not worktree.is_relative_to(root):
        return fail("worktree_path_escape", failure_class="terminal", retry_safe=False, branch=branch, local_branch=local_branch, worktree_root=str(root))
    return ok(status="derived", branch=branch, local_branch=local_branch, worktree_root=str(root), worktree_path=str(worktree))


def validate_cleanup_identity(request: Request) -> Result:
    """Validate cleanup identity, treating composed aggregate identity as authoritative."""
    data, cfg = input_of(request), cfg_of(request)
    upstream = _cleanup_upstream_failure(request, "validate_cleanup_identity", "read_branch_ownership", "derive_cleanup_paths", "parse_cleanup_issue_number")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "validate_cleanup_identity", "read_branch_ownership", "derive_cleanup_paths", "parse_cleanup_issue_number", "resolve_cleanup_branch_source")
    if idle:
        return idle
    aggregate_present = _cleanup_aggregate(request) is not None
    aggregate, authoritative = _cleanup_aggregate_gate(request, "validate_cleanup_identity")
    if aggregate is not None:
        return aggregate
    if authoritative:
        identity = dict(authoritative)
        issue = identity.get("issue")
        try:
            if isinstance(issue, bool) or int(issue) <= 0:
                raise ValueError("issue must be positive")
        except (TypeError, ValueError) as exc:
            return fail("cleanup_identity_invalid", failure_class="terminal", retry_safe=False, error=str(exc))
        branch = str(identity.get("branch") or "").strip()
        clone = str(identity.get("clone_path") or "").strip()
        derived_path = str(cond_get(request, "worktree_path", "derive_cleanup_paths", default="")).strip()
        worktree = str(identity.get("worktree_path") or derived_path).strip()
        if derived_path and (not worktree or Path(worktree).resolve(strict=False) != Path(derived_path).resolve(strict=False)):
            return fail("cleanup_identity_mismatch", failure_class="terminal", retry_safe=False, field="worktree_path", expected=derived_path, actual=worktree)
        ownership = cond_blob(request, "read_branch_ownership").get("ownership")
        if not branch or not clone or not worktree or not isinstance(ownership, dict):
            return fail("cleanup_identity_missing", failure_class="terminal", retry_safe=False, identity=identity, branch=branch, clone_path=clone, worktree_path=worktree)
        if str(ownership.get("repo") or "") != str(identity.get("repo") or "") or str(ownership.get("issue") or "") != str(issue):
            return fail("cleanup_identity_mismatch", failure_class="terminal", retry_safe=False, identity=identity, ownership=ownership)
        if any(ownership.get(key) in (None, "") for key in ("task", "receipt")):
            return fail("cleanup_identity_missing", failure_class="terminal", retry_safe=False, identity=identity, ownership=ownership)
        identity.update(branch=branch, local_branch=str(identity.get("local_branch") or branch), clone_path=clone, worktree_path=worktree,
                        task=str(ownership["task"]), receipt=str(ownership["receipt"]), local_oid=str(ownership.get("local-oid") or ""), remote_oid=str(identity.get("remote_oid") or identity.get("head_oid") or ""))
        return ok(status="validated", identity=identity, **identity)
    if aggregate_present:
        return fail("cleanup_identity_invalid", failure_class="terminal", retry_safe=False)
    conduction = conduction_of(request)
    values: dict[str, list[str]] = {key: [] for key in ("task", "issue", "receipt", "repo")}
    for key in values:
        direct = data.get(key if key != "task" else "task_id") or cfg.get(key if key != "task" else "task_id")
        if direct not in (None, ""):
            values[key].append(str(direct).strip())
        for name, blob in conduction.items():
            if not isinstance(blob, dict) or not str(name).endswith("read_branch_ownership"):
                continue
            if str(blob.get("key") or "").removeprefix("lokay-") == key and blob.get("value") not in (None, ""):
                values[key].append(str(blob["value"]).strip())
            ownership = blob.get("ownership")
            if isinstance(ownership, dict) and ownership.get(key) not in (None, ""):
                values[key].append(str(ownership[key]).strip())
    identity: dict[str, Any] = {}
    for key, candidates in values.items():
        unique = list(dict.fromkeys(item for item in candidates if item))
        if len(unique) != 1:
            return fail("cleanup_identity_mismatch" if unique else "cleanup_identity_missing", failure_class="terminal", retry_safe=False, field=key, values=unique)
        identity[key] = int(unique[0]) if key == "issue" and unique[0].isdigit() else unique[0]
    branch = str(data.get("branch") or cond_get(request, "branch", "parse_cleanup_issue_number", default=cfg.get("branch", ""))).strip()
    clone = str(data.get("clone_path") or _cleanup_value(request, "clone_path", default=cfg.get("clone_path", "")) or "").strip()
    worktree = str(data.get("worktree_path") or cond_get(request, "worktree_path", "derive_cleanup_paths", default=cfg.get("worktree_path", ""))).strip()
    if not branch or not clone or not worktree or not identity.get("issue") or int(identity["issue"]) <= 0:
        return fail("cleanup_identity_missing", failure_class="terminal", retry_safe=False, identity=identity, branch=branch, clone_path=clone, worktree_path=worktree)
    identity.update(branch=branch, clone_path=clone, worktree_path=worktree)
    return ok(status="validated", identity=identity, **identity)


def verify_cleanup_guards(request: Request) -> Result:
    """Pure decision gate proving issue close and no-open-PR guards."""
    upstream = _cleanup_upstream_failure(request, "verify_cleanup_guards", "check_issue_closed", "check_no_open_pr_for_branch")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "verify_cleanup_guards", "check_issue_closed", "check_no_open_pr_for_branch", "parse_cleanup_issue_number", "resolve_cleanup_branch_source")
    if idle:
        return idle
    closed = cond_blob(request, "check_issue_closed")
    prs = cond_blob(request, "check_no_open_pr_for_branch")
    if closed.get("ok") is not True or closed.get("closed") is not True:
        return fail("cleanup_guard_failed", failure_class="terminal", retry_safe=False, guard="check_issue_closed", evidence=closed)
    if prs.get("ok") is not True or prs.get("safe_to_cleanup") is not True or prs.get("open_count") not in (0, None):
        return fail("cleanup_guard_failed", failure_class="terminal", retry_safe=False, guard="check_no_open_pr_for_branch", evidence=prs)
    return ok(status="verified", issue_closed=True, no_open_pr=True, issue=closed.get("issue"))


def read_worktree_ownership(request: Request) -> Result:
    """Read worktree inventory and branch ownership before removal."""
    upstream = _cleanup_upstream_failure(request, "read_worktree_ownership", "verify_cleanup_guards", "validate_cleanup_identity")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(
        request,
        "read_worktree_ownership",
        "verify_cleanup_guards",
        "validate_cleanup_identity",
        "read_branch_ownership",
        "derive_cleanup_paths",
        "parse_cleanup_issue_number",
        "resolve_cleanup_branch_source",
    )
    if idle:
        return idle
    data, cfg = input_of(request), cfg_of(request)
    validated = cond_blob(request, "validate_cleanup_identity").get("identity")
    identity = validated if isinstance(validated, dict) else {}
    clone = str(data.get("clone_path") or identity.get("clone_path") or _cleanup_value(request, "clone_path", default=cfg.get("clone_path", "")) or "").strip()
    path = str(data.get("worktree_path") or identity.get("worktree_path") or cond_get(request, "worktree_path", "derive_cleanup_paths", default=cfg.get("worktree_path", ""))).strip()
    branch = str(data.get("local_branch") or identity.get("local_branch") or _cleanup_value(request, "local_branch", default=cfg.get("local_branch", "")) or data.get("branch") or cfg.get("branch", "")).strip()
    if not clone or not path or not branch:
        return fail("missing_worktree_ownership_context", failure_class="terminal", retry_safe=False, clone_path=clone, worktree_path=path, branch=branch)
    try:
        rows = parse_worktree_porcelain(worktree_list(clone))
        matches = [row for row in rows if str(Path(row.get("path") or "").resolve()) == str(Path(path).resolve())]
    except CommandError as exc:
        return fail("worktree_ownership_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), clone_path=clone, worktree_path=path, branch=branch)
    if not matches:
        # Inventory absence is not enough: a dangling path/symlink still owns residual state.
        if os.path.lexists(path):
            return fail(
                "worktree_path_residual",
                failure_class="terminal",
                retry_safe=False,
                clone_path=clone,
                worktree_path=path,
                branch=branch,
                matches=matches,
            )
        return ok(status="already_absent", clone_path=clone, worktree_path=path, branch=branch, absent=True, matches=matches, mutated=False)
    if len(matches) != 1:
        return fail("worktree_ownership_mismatch", failure_class="terminal", retry_safe=False, clone_path=clone, worktree_path=path, branch=branch, matches=matches)
    row = matches[0]
    if row.get("locked") or str(row.get("branch") or "") != branch:
        return fail("foreign_worktree_ownership", failure_class="terminal", retry_safe=False, clone_path=clone, worktree_path=path, branch=branch, actual_branch=row.get("branch"), locked=bool(row.get("locked")))
    return ok(status="read", clone_path=clone, worktree_path=path, branch=branch, ownership=row)


def read_worktree_cleanliness(request: Request) -> Result:
    """Read one worktree's cleanliness state."""
    upstream = _cleanup_upstream_failure(request, "read_worktree_cleanliness", "read_worktree_ownership")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "read_worktree_cleanliness", "read_worktree_ownership", "verify_cleanup_guards", "validate_cleanup_identity", "read_branch_ownership", "derive_cleanup_paths", "parse_cleanup_issue_number", "resolve_cleanup_branch_source")
    if idle:
        return idle
    data, cfg = input_of(request), cfg_of(request)
    ownership = cond_blob(request, "read_worktree_ownership")
    path = str(data.get("worktree_path") or cfg.get("worktree_path") or ownership.get("worktree_path") or cond_get(request, "worktree_path", "read_worktree_ownership", default="")).strip()
    if ownership.get("status") == "already_absent" or ownership.get("absent") is True:
        return ok(status="already_absent", worktree_path=path, dirty=False, clean=True, absent=True, mutated=False)
    if not path:
        return fail("missing_worktree_path", failure_class="terminal", retry_safe=False)
    try:
        dirty = is_dirty(path)
    except CommandError as exc:
        return fail("worktree_cleanliness_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), worktree_path=path)
    return ok(status="checked", worktree_path=path, dirty=bool(dirty), clean=not bool(dirty))


def verify_worktree_absent(request: Request) -> Result:
    """Read worktree inventory and verify the target is absent."""
    upstream = _cleanup_upstream_failure(request, "verify_worktree_absent", "remove_worktree")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "verify_worktree_absent", "remove_worktree", "read_worktree_cleanliness", "read_worktree_ownership", "verify_cleanup_guards", "validate_cleanup_identity", "parse_cleanup_issue_number", "resolve_cleanup_branch_source")
    if idle:
        return idle
    data, cfg = input_of(request), cfg_of(request)
    mutation = cond_blob(request, "remove_worktree")
    gate, identity = _cleanup_aggregate_gate(request, "verify_worktree_absent")
    if gate is not None:
        return gate
    clone = str(mutation.get("clone_path") or identity.get("clone_path") or data.get("clone_path") or cfg.get("clone_path") or "").strip()
    path = str(mutation.get("worktree_path") or identity.get("worktree_path") or data.get("worktree_path") or cfg.get("worktree_path") or "").strip()
    if not clone or not path:
        return fail("missing_worktree_context", failure_class="terminal", retry_safe=False)
    try:
        rows = parse_worktree_porcelain(worktree_list(clone))
        present = any(str(Path(row.get("path") or "").resolve()) == str(Path(path).resolve()) for row in rows)
    except CommandError as exc:
        return fail("worktree_absence_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), clone_path=clone, worktree_path=path)
    residual = os.path.lexists(path)
    if present or residual:
        return fail(
            "worktree_not_absent",
            failure_class="reconcile_then_retry",
            retry_safe=False,
            clone_path=clone,
            worktree_path=path,
            inventory_present=present,
            path_residual=residual,
            mutated=bool(cond_blob(request, "remove_worktree").get("mutated")),
        )
    return ok(status="verified", clone_path=clone, worktree_path=path, absent=True)


def verify_branch_delete_guards(request: Request) -> Result:
    upstream = _cleanup_upstream_failure(request, "verify_branch_delete_guards", "verify_cleanup_guards", "verify_worktree_absent")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "verify_branch_delete_guards", "verify_cleanup_guards", "verify_worktree_absent", "remove_worktree", "read_worktree_cleanliness", "read_worktree_ownership", "validate_cleanup_identity", "parse_cleanup_issue_number", "resolve_cleanup_branch_source")
    if idle:
        return idle
    removed = cond_blob(request, "verify_worktree_absent")
    if removed.get("ok") is not True or removed.get("status") != "verified" or removed.get("absent") is not True:
        return fail("branch_delete_guard_failed", failure_class="terminal", retry_safe=False, evidence=removed)
    return ok(status="verified", worktree_absent=True)


def read_local_branch_ownership(request: Request) -> Result:
    data, cfg = input_of(request), cfg_of(request)
    upstream = _cleanup_upstream_failure(request, "read_local_branch_ownership", "verify_branch_delete_guards")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "read_local_branch_ownership", "verify_branch_delete_guards", "verify_worktree_absent", "remove_worktree", "verify_cleanup_guards", "parse_cleanup_issue_number", "resolve_cleanup_branch_source")
    if idle:
        return idle
    gate, identity = _cleanup_aggregate_gate(request, "read_local_branch_ownership")
    if gate is not None:
        return gate
    validated = cond_blob(request, "validate_cleanup_identity").get("identity")
    if isinstance(validated, dict):
        identity = {**identity, **validated}
    composed = bool(identity)
    clone = str(identity.get("clone_path") if composed else data.get("clone_path") or _cleanup_value(request, "clone_path", default=cfg.get("clone_path", "")) or "").strip()
    branch = str(identity.get("local_branch") if composed else data.get("local_branch") or _cleanup_value(request, "local_branch", default=cfg.get("local_branch", "")) or data.get("branch") or cfg.get("branch", "") or "").strip()
    if not clone or not branch:
        return fail("missing_branch_context", failure_class="terminal", retry_safe=False)
    authorized_head = str(identity.get("local_oid") or "").strip() if composed else ""
    merged_head = str(identity.get("remote_oid") or "").strip() if composed else ""
    try:
        exists = branch_exists(clone, branch)
        expected = ({"task": str(identity.get("task") or ""), "issue": str(identity.get("issue") or ""),
                     "receipt": str(identity.get("receipt") or ""), "repo": str(identity.get("repo") or "")}
                    if composed else _cleanup_provenance(data, cfg, branch, conduction_of(request)))
        owned = not exists or _cleanup_owner_matches(clone, branch, expected, task_optional=composed)
        head = local_branch_head(clone, branch) if exists else ""
        ancestry = run_cmd(["git", "merge-base", "--is-ancestor", authorized_head, merged_head], cwd=clone, check=False) if composed and authorized_head and merged_head else None
    except (CommandError, OSError) as exc:
        return fail("branch_ownership_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), clone_path=clone, branch=branch)
    if not owned:
        return fail("foreign_branch_ownership", failure_class="terminal", retry_safe=False, clone_path=clone, branch=branch)
    if ancestry is not None and ancestry.returncode not in (0, 1):
        return fail("branch_ownership_read_failed", failure_class="retryable_read", retry_safe=True, clone_path=clone, branch=branch, returncode=ancestry.returncode, stderr=ancestry.stderr)
    if composed and (not authorized_head or not merged_head or ancestry is None or ancestry.returncode == 1 or (exists and head != authorized_head)):
        return fail("local_branch_head_mismatch", failure_class="terminal", retry_safe=False, clone_path=clone, branch=branch, expected_head=authorized_head, actual_head=head, merged_head=merged_head)
    return ok(status="read", clone_path=clone, branch=branch, exists=exists, owned=True, head=head, local_oid=authorized_head, merged_oid=merged_head, exact_head_required=composed)


def delete_local_branch(request: Request) -> Result:
    upstream = _cleanup_upstream_failure(request, "delete_local_branch", "verify_branch_delete_guards", "read_local_branch_ownership")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "delete_local_branch", "verify_branch_delete_guards", "read_local_branch_ownership", "verify_worktree_absent", "remove_worktree", "verify_cleanup_guards", "parse_cleanup_issue_number", "resolve_cleanup_branch_source")
    if idle:
        return idle
    data, cfg = input_of(request), cfg_of(request)
    ownership = cond_blob(request, "read_local_branch_ownership")
    clone = str(ownership.get("clone_path") or data.get("clone_path") or cfg.get("clone_path") or "").strip()
    branch = str(ownership.get("branch") or data.get("local_branch") or cfg.get("local_branch") or data.get("branch") or cfg.get("branch") or "").strip()
    if not clone or not branch:
        return fail("missing_clone_or_branch", failure_class="terminal", retry_safe=False)
    if ownership.get("exists") is False:
        return ok(status="already_absent", clone_path=clone, branch=branch, mutated=False, absent=True)
    if ownership.get("owned") is not True:
        return fail("foreign_branch_ownership", failure_class="terminal", retry_safe=False, clone_path=clone, branch=branch)
    if ownership.get("exact_head_required") is True and (not ownership.get("head") or ownership.get("head") != ownership.get("local_oid")):
        return fail("local_branch_head_mismatch", failure_class="terminal", retry_safe=False, clone_path=clone, branch=branch)
    if dry_run_flag(request):
        return planned(clone_path=clone, branch=branch)
    try:
        if ownership.get("exact_head_required") is True:
            expected_oid = str(ownership.get("local_oid") or "")
            if not expected_oid:
                return fail("local_branch_head_mismatch", failure_class="terminal", retry_safe=False, clone_path=clone, branch=branch)
            git_delete_local_branch_if_head(clone, branch, expected_oid)
        else:
            git_delete_local_branch(clone, branch, force=bool(data.get("force", True)))
    except CommandError as exc:
        if ownership.get("exact_head_required") is True:
            return fail("local_branch_head_mismatch", failure_class="terminal", retry_safe=False, error=str(exc), clone_path=clone, branch=branch, mutated=False)
        return fail("branch_delete_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), clone_path=clone, branch=branch, mutated=True)
    return ok(status="deleted", clone_path=clone, branch=branch, mutated=True)


def verify_local_branch_absent(request: Request) -> Result:
    """Read local branch ref and verify deletion."""
    upstream = _cleanup_upstream_failure(request, "verify_local_branch_absent", "delete_local_branch")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "verify_local_branch_absent", "delete_local_branch", "read_local_branch_ownership", "verify_branch_delete_guards", "verify_worktree_absent", "parse_cleanup_issue_number", "resolve_cleanup_branch_source")
    if idle:
        return idle
    data, cfg = input_of(request), cfg_of(request)
    mutation = cond_blob(request, "delete_local_branch")
    gate, identity = _cleanup_aggregate_gate(request, "verify_local_branch_absent")
    if gate is not None:
        return gate
    clone = str(mutation.get("clone_path") or identity.get("clone_path") or data.get("clone_path") or cfg.get("clone_path") or "").strip()
    branch = str(mutation.get("branch") or identity.get("local_branch") or data.get("local_branch") or cfg.get("local_branch") or data.get("branch") or cfg.get("branch") or "").strip()
    if not clone or not branch:
        return fail("missing_branch_context", failure_class="terminal", retry_safe=False)
    try:
        exists = branch_exists(clone, branch)
    except CommandError as exc:
        return fail("branch_absence_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), mutated=bool(cond_blob(request, "delete_local_branch").get("mutated")))
    if exists:
        return fail("local_branch_not_absent", failure_class="reconcile_then_retry", retry_safe=False, clone_path=clone, branch=branch, mutated=bool(cond_blob(request, "delete_local_branch").get("mutated")))
    return ok(status="verified", clone_path=clone, branch=branch, absent=True)

def verify_claim_release_evidence(request: Request) -> Result:
    """Pure gate for canonical claim-release evidence."""
    upstream = _cleanup_upstream_failure(request, "verify_claim_release_evidence", "verify_cleanup_guards", "verify_local_branch_absent", "verify_worktree_absent")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "verify_claim_release_evidence", "verify_cleanup_guards", "verify_local_branch_absent", "verify_worktree_absent", "delete_local_branch", "remove_worktree", "parse_cleanup_issue_number", "resolve_cleanup_branch_source")
    if idle:
        return idle
    if not isinstance(input_of(request).get("conduction"), dict):
        return fail("cleanup_evidence_missing", failure_class="terminal", retry_safe=False)
    required = ("remove_worktree", "check_issue_closed", "check_no_open_pr_for_branch", "delete_local_branch")
    evidence = {}
    for canonical in required:
        blob = cond_blob(request, canonical)
        if not blob:
            return fail("cleanup_evidence_missing", failure_class="terminal", retry_safe=False, peer=canonical)
        evidence[canonical] = blob
    if evidence["check_issue_closed"].get("closed") is not True or evidence["check_no_open_pr_for_branch"].get("safe_to_cleanup") is not True:
        return fail("claim_release_guard_failed", failure_class="terminal", retry_safe=False, evidence=evidence)
    for name in ("remove_worktree", "delete_local_branch"):
        if evidence[name].get("ok") is not True or evidence[name].get("status") not in {"removed", "deleted", "already_absent"}:
            return fail("claim_release_guard_failed", failure_class="terminal", retry_safe=False, evidence=evidence)
    return ok(status="verified", evidence=evidence)



def read_claim_identity(request: Request) -> Result:
    """Read and parse one claim JSON identity."""
    data, cfg = input_of(request), cfg_of(request)
    upstream = _cleanup_upstream_failure(request, "read_claim_identity", "verify_claim_release_evidence")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "read_claim_identity", "verify_claim_release_evidence", "verify_cleanup_guards", "verify_local_branch_absent", "verify_worktree_absent", "parse_cleanup_issue_number", "resolve_cleanup_branch_source")
    if idle:
        return idle
    claim = str(data.get("claim_path") or cfg.get("active_issue_path") or "").strip()
    if not claim:
        return fail("missing_claim_path", failure_class="terminal", retry_safe=False)
    configured = Path(claim).expanduser()
    path = configured / "claim.json" if ((configured.exists() and configured.is_dir()) or configured.suffix.lower() != ".json") else configured
    if not path.exists():
        return ok(status="already_absent", claim_path=str(path), absent=True)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("claim payload is not an object")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return fail("claim_corrupt", failure_class="terminal", retry_safe=False, claim_path=str(path), error=str(exc))
    return ok(status="read", claim_path=str(path), payload=payload)


def release_claim_file(request: Request) -> Result:
    """Unlink one claim file after evidence and identity reads."""
    upstream = _cleanup_upstream_failure(request, "release_claim_file", "verify_claim_release_evidence", "read_claim_identity")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "release_claim_file", "verify_claim_release_evidence", "read_claim_identity", "verify_cleanup_guards", "verify_local_branch_absent", "verify_worktree_absent", "parse_cleanup_issue_number", "resolve_cleanup_branch_source")
    if idle:
        return idle
    data, cfg = input_of(request), cfg_of(request)
    identity = cond_blob(request, "read_claim_identity")
    path = Path(str(identity.get("claim_path") or data.get("claim_path") or cfg.get("active_issue_path") or "")).expanduser()
    if identity.get("status") == "already_absent" or not path.exists():
        return ok(status="already_absent", claim_path=str(path), mutated=False)
    if dry_run_flag(request):
        return planned(claim_path=str(path))
    try:
        path.unlink()
        parent_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        return fail("claim_release_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), claim_path=str(path), mutated=True)
    return ok(status="released", claim_path=str(path), mutated=True)


def verify_claim_absent(request: Request) -> Result:
    """Read the claim path and verify absence."""
    upstream = _cleanup_upstream_failure(request, "verify_claim_absent", "release_claim_file")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "verify_claim_absent", "release_claim_file", "read_claim_identity", "verify_claim_release_evidence", "verify_local_branch_absent", "parse_cleanup_issue_number", "resolve_cleanup_branch_source")
    if idle:
        return idle
    identity = cond_blob(request, "read_claim_identity", "release_claim_file")
    path = Path(str(identity.get("claim_path") or input_of(request).get("claim_path") or "")).expanduser()
    if path.exists():
        return fail("claim_not_absent", failure_class="reconcile_then_retry", retry_safe=False, claim_path=str(path), mutated=bool(cond_blob(request, "release_claim_file").get("mutated")))
    return ok(status="verified", claim_path=str(path), absent=True)

def collect_cleanup_receipt_evidence(request: Request) -> Result:
    """Read canonical cleanup peer evidence."""
    upstream = _cleanup_upstream_failure(request, "collect_cleanup_receipt_evidence", "verify_claim_absent")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "collect_cleanup_receipt_evidence", "verify_claim_absent")
    if idle:
        return idle
    if not isinstance(input_of(request).get("conduction"), dict):
        return fail("cleanup_evidence_missing", failure_class="terminal", retry_safe=False)
    names = ("parse_cleanup_issue_number", "check_issue_closed", "check_no_open_pr_for_branch", "remove_worktree", "delete_local_branch", "release_claim_file")
    evidence = {}
    for name in names:
        blob = cond_blob(request, name)
        if not blob:
            return fail("cleanup_evidence_missing", failure_class="terminal", retry_safe=False, peer=name)
        evidence[name] = blob
    return ok(status="collected", evidence=evidence)



def decide_cleanup_outcome(request: Request) -> Result:
    """Purely decide cleanup receipt outcome."""
    upstream = _cleanup_upstream_failure(request, "decide_cleanup_outcome", "collect_cleanup_receipt_evidence")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "decide_cleanup_outcome", "collect_cleanup_receipt_evidence", "parse_cleanup_issue_number", "resolve_cleanup_branch_source")
    if idle:
        return idle
    evidence = cond_blob(request, "collect_cleanup_receipt_evidence").get("evidence")
    if not isinstance(evidence, dict):
        return fail("cleanup_evidence_missing", failure_class="terminal", retry_safe=False)
    parse = evidence.get("parse_cleanup_issue_number") or {}
    if parse.get("status") == "noop" and parse.get("reason") == "no_branch" and all(not bool(item.get("mutated")) for item in evidence.values() if isinstance(item, dict)):
        return noop("no_branch")
    if any(item.get("ok") is not True for item in evidence.values() if isinstance(item, dict)):
        return ok(status="decided", outcome="partial" if any(item.get("mutated") for item in evidence.values() if isinstance(item, dict)) else "failure")
    return ok(status="decided", outcome="success")


def build_cleanup_receipt(request: Request) -> Result:
    """Purely build canonical cleanup receipt payload."""
    upstream = _cleanup_upstream_failure(request, "build_cleanup_receipt", "decide_cleanup_outcome", "collect_cleanup_receipt_evidence")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "build_cleanup_receipt", "decide_cleanup_outcome", "collect_cleanup_receipt_evidence", "parse_cleanup_issue_number", "resolve_cleanup_branch_source")
    if idle:
        return idle
    data, cfg = input_of(request), cfg_of(request)
    decision = cond_blob(request, "decide_cleanup_outcome")
    if decision.get("status") == "noop" and decision.get("reason") == "no_branch":
        return noop("no_branch")
    evidence = cond_blob(request, "collect_cleanup_receipt_evidence").get("evidence") or {}
    payload = dict(data.get("payload") or {})
    payload.update({"phase": "CLEANUP_TERMINAL", "outcome": decision.get("outcome"), "entity": data.get("entity") or cfg.get("entity") or {}, "steps": {name: {"status": item.get("status"), "mutated": bool(item.get("mutated")), "reason": item.get("reason")} for name, item in evidence.items() if isinstance(item, dict)}})
    return ok(status="built", payload=payload)


def publish_cleanup_receipt(request: Request) -> Result:
    """Publish only the canonical payload emitted by build_cleanup_receipt."""
    upstream = _cleanup_upstream_failure(request, "publish_cleanup_receipt", "build_cleanup_receipt")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "publish_cleanup_receipt", "build_cleanup_receipt", "decide_cleanup_outcome", "collect_cleanup_receipt_evidence", "parse_cleanup_issue_number", "resolve_cleanup_branch_source")
    if idle:
        return idle
    data, cfg = input_of(request), cfg_of(request)
    built = cond_blob(request, "build_cleanup_receipt")
    payload = data.get("payload") or built.get("payload")
    path = str(data.get("receipt_path") or cfg.get("receipt_path") or "").strip()
    if not isinstance(payload, dict) or payload.get("phase") != "CLEANUP_TERMINAL":
        return fail("cleanup_receipt_payload_missing", failure_class="terminal", retry_safe=False)
    if not path:
        return fail("missing_receipt_path", failure_class="terminal", retry_safe=False)
    if dry_run_flag(request):
        return planned(receipt_path=path, payload=payload)
    try:
        with _receipt_directory_lock(Path(path).parent):
            return _publish_cleanup_receipt(Path(path), payload, path)
    except OSError as exc:
        return fail("receipt_write_failed", failure_class="terminal", retry_safe=False, error=str(exc), receipt_path=path)


def verify_cleanup_receipt(request: Request) -> Result:
    """Read receipt JSON and verify it matches the canonical payload."""
    upstream = _cleanup_upstream_failure(request, "verify_cleanup_receipt", "publish_cleanup_receipt")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "verify_cleanup_receipt", "publish_cleanup_receipt", "build_cleanup_receipt", "decide_cleanup_outcome", "collect_cleanup_receipt_evidence", "parse_cleanup_issue_number", "resolve_cleanup_branch_source")
    if idle:
        return idle
    data, cfg = input_of(request), cfg_of(request)
    path = Path(str(data.get("receipt_path") or cfg.get("receipt_path") or "")).expanduser()
    expected = data.get("payload") or cond_blob(request, "build_cleanup_receipt").get("payload")
    try:
        actual = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return fail("receipt_readback_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), receipt_path=str(path))
    if not isinstance(expected, dict) or actual != expected:
        return fail("receipt_readback_mismatch", failure_class="terminal", retry_safe=False, receipt_path=str(path))
    return ok(status="verified", receipt_path=str(path), payload=actual)


def read_maintenance_tasks(request: Request) -> Result:
    """Read maintenance task list."""
    upstream = _cleanup_upstream_failure(request, "read_maintenance_tasks", "verify_cleanup_receipt")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "read_maintenance_tasks", "verify_cleanup_receipt")
    if idle:
        return idle
    data, cfg = input_of(request), cfg_of(request)
    board = str(_cleanup_value(request, "board") or data.get("board") or cfg.get("board") or "").strip()
    if not board:
        return fail("missing_board", failure_class="terminal", retry_safe=False)
    try:
        tasks = hermes_kanban_json(["--board", board, "list", "--json", "--sort", "created-desc"])
    except CommandError as exc:
        return fail("kanban_list_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), board=board)
    if not isinstance(tasks, list) or any(not isinstance(task, dict) for task in tasks):
        return fail("invalid_kanban_readback", failure_class="terminal", retry_safe=False, board=board)
    return ok(status="read", board=board, tasks=tasks)


def find_maintenance_marker(request: Request) -> Result:
    """Purely find one maintenance marker."""
    upstream = _cleanup_upstream_failure(request, "find_maintenance_marker", "read_maintenance_tasks")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "find_maintenance_marker", "read_maintenance_tasks")
    if idle:
        return idle
    data, cfg = input_of(request), cfg_of(request)
    tasks = cond_blob(request, "read_maintenance_tasks").get("tasks")
    repo, path, pr = str(data.get("repo") or cfg.get("repo") or ""), str(data.get("worktree_path") or ""), str(data.get("pr_number") or data.get("number") or "")
    marker = f"maintenance:{repo or path}:pr:{pr or 'none'}"
    if not isinstance(tasks, list):
        return fail("maintenance_tasks_missing", failure_class="terminal", retry_safe=False)
    matches = [task for task in tasks if _task_marker_matches(task, marker)]
    if len(matches) > 1:
        return fail("ambiguous_kanban_task", failure_class="terminal", retry_safe=False, marker=marker)
    return ok(status="found", marker=marker, task=matches[0] if matches else None, found=bool(matches))


def reconcile_maintenance_task(request: Request) -> Result:
    """Reconcile maintenance marker after creation."""
    upstream = _cleanup_upstream_failure(request, "reconcile_maintenance_task", "create_maintenance_task")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "reconcile_maintenance_task", "create_maintenance_task")
    if idle:
        return idle
    found = cond_blob(request, "find_maintenance_marker")
    if found.get("found") is True:
        task = found.get("task") or {}
        return ok(status="reconciled", task_id=_task_id(task), marker=found.get("marker"), mutated=False)
    return fail("maintenance_task_unresolved", failure_class="reconcile_then_retry", retry_safe=False, marker=found.get("marker"))

def create_maintenance_task(request: Request) -> Result:
    """Create one maintenance task after canonical read/find evidence."""
    upstream = _cleanup_upstream_failure(request, "create_maintenance_task", "read_maintenance_tasks", "find_maintenance_marker")
    if upstream:
        return upstream
    idle = _cleanup_upstream_noop(request, "create_maintenance_task", "read_maintenance_tasks", "find_maintenance_marker")
    if idle:
        return idle
    found = cond_blob(request, "find_maintenance_marker")
    if not found or found.get("ok") is not True:
        return fail("maintenance_marker_missing", failure_class="terminal", retry_safe=False, evidence=found)
    if found.get("found") is True:
        return ok(status="exists", task_id=_task_id(found.get("task") or {}), marker=found.get("marker"), mutated=False)
    data, cfg = input_of(request), cfg_of(request)
    board = str(_cleanup_value(request, "board") or data.get("board") or cfg.get("board") or "").strip()
    repo = str(_cleanup_value(request, "repo") or data.get("repo") or cfg.get("repo") or "").strip()
    path = str(_cleanup_value(request, "worktree_path") or data.get("worktree_path") or "").strip()
    pr = str(_cleanup_value(request, "pr_number", "number") or data.get("pr_number") or data.get("number") or "").strip()
    marker = str(found.get("marker") or f"maintenance:{repo or path}:pr:{pr or 'none'}")
    reason = str(data.get("reason") or "dirty_worktree")
    title = f"[maintenance] dirty worktree: {path or repo or reason}"
    body = f"Path: {path}\nRepository: {repo}\nPR: {pr}\nReason: {reason}\nIdempotency-Key: {marker}\n"
    if not board:
        return fail("missing_board", failure_class="terminal", retry_safe=False, marker=marker)
    if dry_run_flag(request):
        return planned(board=board, title=title, idempotency_key=marker)
    assignee = str(cfg.get("kanban_intake_assignee") or "lokay-intake")
    try:
        proc = run_cmd(["hermes", "kanban", "--board", board, "create", "--body", body, "--assignee", assignee, "--idempotency-key", marker, title], timeout=90)
    except CommandError as exc:
        return fail("create_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), board=board, marker=marker, mutated=True)
    return ok(status="created", board=board, marker=marker, title=title, stdout=proc.stdout[-300:], mutated=True)
