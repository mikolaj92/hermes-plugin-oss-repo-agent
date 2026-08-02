"""Exact, fail-closed terminal reconciliation for an already-absent cleanup target."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import re
import stat
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

from lokay.adapters_cli import CommandError, run_cmd
from lokay.adapters_git import parse_worktree_porcelain, worktree_list
from lokay.envelope import Request, Result, cfg_of, cond_blob, dry_run_flag, fail, input_of, noop, ok, planned, upstream_noop
from lokay.steps.cleanup import _publish_cleanup_receipt, _receipt_directory_lock
from lokay.steps.claim import _claim_file, _claims_in_directory, _read_claim, claim_directory_lock

_ACTIVE_PROCESS_STATUSES = {"pending", "ready", "running", "waiting", "retry_wait", "cancel_requested"}
_TERMINAL_PROCESS_STATUSES = {"succeeded", "failed", "cancelled", "timed_out"}
_REQUIRED = (
    "repo",
    "issue",
    "pr_number",
    "task_id",
    "branch",
    "clone_path",
    "worktree_path",
    "task_receipt_path",
    "claim_path",
    "merge_receipt_path",
    "receipt_path",
    "db_path",
    "base_sha",
    "head_oid",
    "merge_oid",
    "origin_main_sha",
)


def _read_regular_json(path: Path, *, private: bool = False) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or (private and metadata.st_mode & 0o077):
            raise ValueError(f"unsafe JSON artifact: {path}")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            value = json.load(handle)
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact is not an object: {path}")
    return value


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _process_alive(value: Any, *, group: bool = False) -> bool:
    try:
        pid = _positive_int(value, "process id")
    except (TypeError, ValueError):
        return False
    try:
        (os.killpg if group else os.kill)(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _ls_remote(clone_path: str, *refs: str) -> dict[str, str]:
    proc = run_cmd(["git", "-C", clone_path, "ls-remote", "origin", *refs], timeout=120)
    resolved: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) != 2 or parts[1] in resolved:
            raise ValueError("ambiguous remote ref readback")
        resolved[parts[1]] = parts[0]
    return resolved


def _origin_repo(clone_path: str) -> str:
    value = run_cmd(["git", "-C", clone_path, "remote", "get-url", "origin"], timeout=60).stdout.strip()
    if value.endswith(".git"):
        value = value[:-4]
    for prefix in ("https://github.com/", "ssh://git@github.com/", "git@github.com:"):
        if value.startswith(prefix):
            return value[len(prefix):]
    raise ValueError("origin is not a canonical GitHub repository URL")


def _local_branch_absent(clone_path: str, branch: str) -> bool:
    try:
        run_cmd(["git", "-C", clone_path, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], timeout=60)
    except CommandError as exc:
        if exc.returncode == 1:
            return True
        raise
    return False


def _contains_task_id(value: Any, task_id: str) -> bool:
    if isinstance(value, dict):
        return value.get("task_id") == task_id or any(_contains_task_id(item, task_id) for item in value.values())
    if isinstance(value, list):
        return any(_contains_task_id(item, task_id) for item in value)
    return False


def _matching_claims(root: Path, repo: str, issue: int) -> list[Path]:
    paths = [root] if root.suffix.lower() == ".json" and not root.is_dir() else sorted(root.glob("*.json"))
    matches: list[Path] = []
    for path in paths:
        if not path.exists():
            continue
        claim, error = _read_claim(path)
        if error:
            raise ValueError(error)
        if claim is not None and claim.get("repo") == repo and claim.get("issue") == issue:
            matches.append(path)
    return matches


def _matching_active_leases(connection: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(processes)")}
    required = {"run_id", "id", "status", "lease_owner", "lease_expires_at", "input_json", "output_json", "metadata"}
    if not required.issubset(columns):
        raise ValueError("Fala processes schema lacks lease evidence columns")
    rows = connection.execute(
        "SELECT run_id,id,status,lease_owner,lease_expires_at,input_json,output_json,metadata FROM processes"
    ).fetchall()
    matches: list[dict[str, Any]] = []
    for run_id, process_id, status, owner, expires, input_json, output_json, metadata in rows:
        artifacts = []
        for raw in (input_json, output_json, metadata):
            try:
                artifacts.append(json.loads(str(raw or "{}")))
            except json.JSONDecodeError as exc:
                raise ValueError("malformed Fala process evidence") from exc
        if any(_contains_task_id(item, task_id) for item in artifacts):
            normalized_status = str(status)
            if normalized_status not in _ACTIVE_PROCESS_STATUSES | _TERMINAL_PROCESS_STATUSES:
                raise ValueError(f"unknown Fala process status: {normalized_status or '<blank>'}")
            if normalized_status in _ACTIVE_PROCESS_STATUSES:
                matches.append({"run_id": run_id, "process_id": process_id, "status": status, "lease_owner": owner, "lease_expires_at": expires})
    return matches


def _atomic_replace_json(path: Path, payload: dict[str, Any]) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        parent_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        if _read_regular_json(path, private=True) != payload:
            raise ValueError("task receipt read-back mismatch")
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass



def _reconcile_upstream_failure(request: Request, operation: str, *ids: str) -> Result | None:
    from lokay.envelope import terminal_upstream

    return terminal_upstream(request, operation, *ids)


def _trusted_path(value: Any, root: Any, field: str, *, must_exist: bool = False) -> Path:
    path = Path(str(value or "")).expanduser()
    root_path = Path(str(root or "")).expanduser().resolve(strict=False)
    resolved = path.resolve(strict=False)
    if not str(resolved).startswith(str(root_path) + os.sep) and resolved != root_path:
        raise ValueError(f"cleanup_context_mismatch:{field}")
    if must_exist and not path.exists():
        raise FileNotFoundError(path)
    return path


def _read_task_lock(path: Path) -> None:
    lock = Path(f"{path}.lock")
    fd = os.open(lock, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("task lock active") from exc
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)

def validate_reconcile_identity(request: Request) -> Result:
    """Validate identity and all local preconditions before any GitHub call."""
    data, cfg = input_of(request), cfg_of(request)
    missing = [name for name in _REQUIRED if data.get(name) in (None, "")]
    if missing:
        return fail("cleanup_identity_missing", failure_class="terminal", retry_safe=False, missing=missing)
    try:
        issue = _positive_int(data["issue"], "issue")
        pr = _positive_int(data["pr_number"], "pr_number")
        branch = str(data["branch"]).strip()
        if re.fullmatch(r"ai/fix/[1-9][0-9]*(?:-[A-Za-z0-9._-]+)?", branch) is None or int(branch.split("/", 2)[2].split("-", 1)[0]) != issue:
            raise ValueError("branch must be canonical")
        if str(cfg.get("repo") or data["repo"]) != str(data["repo"]):
            raise ValueError("repository context mismatch")
        clone = Path(str(data["clone_path"])).expanduser()
        if not clone.is_dir():
            return fail("cleanup_context_missing", failure_class="terminal", retry_safe=False, field="clone_path", error=f"No such file or directory: '{clone}'")
        _trusted_path(data["task_receipt_path"], cfg.get("task_receipt_root"), "task_receipt_path", must_exist=True)
        _trusted_path(data["merge_receipt_path"], cfg.get("merge_receipt_root"), "merge_receipt_path", must_exist=True)
        _trusted_path(data["receipt_path"], cfg.get("cleanup_receipt_root"), "receipt_path")
        _trusted_path(data["worktree_path"], cfg.get("worktree_root"), "worktree_path")
        _read_task_lock(Path(str(data["task_receipt_path"])))
        identity = {key: data[key] for key in _REQUIRED if key not in {"issue", "pr_number"}}
        identity.update(issue=issue, pr_number=pr)
    except RuntimeError as exc:
        return fail("task_lock_active", failure_class="terminal", retry_safe=False, error=str(exc))
    except FileNotFoundError as exc:
        return fail("cleanup_context_missing", failure_class="terminal", retry_safe=False, field="task_receipt_path", error=str(exc))
    except ValueError as exc:
        text = str(exc)
        if text.startswith("cleanup_context_mismatch:"):
            return fail("cleanup_context_mismatch", failure_class="terminal", retry_safe=False, field=text.split(":", 1)[1])
        return fail("cleanup_identity_invalid", failure_class="terminal", retry_safe=False, error=text)
    if data.get("remote_retention_authorized") is not True:
        return fail("remote_retention_not_authorized", failure_class="terminal", retry_safe=False)
    return ok(status="validated", identity=identity, **identity)


def read_local_receipts(request: Request) -> Result:
    """Read task and merge receipts only."""
    upstream = _reconcile_upstream_failure(request, "read_local_receipts", "validate_reconcile_identity")
    if upstream:
        return upstream
    data = input_of(request)
    try:
        task = _read_regular_json(Path(str(data["task_receipt_path"])), private=True)
        merge = _read_regular_json(Path(str(data["merge_receipt_path"])))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return fail("receipt_read_failed", failure_class="terminal", retry_safe=False, error=str(exc))
    return ok(status="read", task_receipt=task, merge_receipt=merge)


def read_claim_process_evidence(request: Request) -> Result:
    """Read active claims and task leases from the configured stores."""
    upstream = _reconcile_upstream_failure(request, "read_claim_process_evidence", "validate_reconcile_identity", "read_local_receipts")
    if upstream:
        return upstream
    data, cfg = input_of(request), cfg_of(request)
    try:
        claim_root_value = (
            data.get("claim_root")
            or data.get("active_issue_path")
            or data.get("active_issue")
            or cfg.get("claim_root")
            or cfg.get("active_issue_path")
            or cfg.get("active_issue")
            or (cfg.get("paths") if isinstance(cfg.get("paths"), dict) else {}).get("active_issue")
            or data.get("claim_path")
        )
        claim_root = Path(str(claim_root_value or "")).expanduser().resolve(strict=False)
        claims = _matching_claims(claim_root, str(data["repo"]), _positive_int(data["issue"], "issue"))
        db = Path(str(data["db_path"])).expanduser().resolve(strict=True)
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0)
        try:
            leases = _matching_active_leases(conn, str(data["task_id"]))
        finally:
            conn.close()


    except sqlite3.OperationalError as exc:
        if getattr(exc, "sqlite_errorcode", 0) & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            return fail("database_lock_active", failure_class="retryable_read", retry_safe=True, error=str(exc))
        return fail("claim_process_evidence_read_failed", failure_class="terminal", retry_safe=False, error=str(exc))
    except (OSError, ValueError, sqlite3.Error) as exc:
        return fail("claim_process_evidence_read_failed", failure_class="terminal", retry_safe=False, error=str(exc))
    return ok(status="read", active_claims=[str(path) for path in claims], active_leases=leases)

def read_remote_provenance(request: Request) -> Result:
    """Read origin repository and remote branch/base refs."""
    upstream = _reconcile_upstream_failure(request, "read_remote_provenance", "validate_reconcile_identity")
    if upstream:
        return upstream
    data = input_of(request)
    try:
        repo = _origin_repo(str(data["clone_path"]))
        refs = _ls_remote(str(data["clone_path"]), "refs/heads/main", f"refs/heads/{data['branch']}")
    except (CommandError, OSError, ValueError) as exc:
        return fail("remote_provenance_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc))
    return ok(status="read", origin_repo=repo, refs=refs)


def read_github_terminal_state(request: Request) -> Result:
    """Read issue and PR terminal state from GitHub."""
    upstream = _reconcile_upstream_failure(request, "read_github_terminal_state", "validate_reconcile_identity", "read_reconcile_worktree_state", "read_remote_provenance")
    if upstream:
        return upstream
    data, cfg = input_of(request), cfg_of(request)
    gh, repo = str(cfg.get("gh_cli") or "gh"), str(data["repo"])
    try:
        issue = json.loads(run_cmd([gh, "issue", "view", str(data["issue"]), "--repo", repo, "--json", "number,state"], timeout=60).stdout)
        pr = json.loads(run_cmd([gh, "pr", "view", str(data["pr_number"]), "--repo", repo, "--json", "number,state,mergedAt,mergeCommit,headRefName,headRefOid,baseRefName"], timeout=60).stdout)
        opens = json.loads(run_cmd([gh, "pr", "list", "--repo", repo, "--head", str(data["branch"]), "--state", "open", "--json", "number"], timeout=60).stdout or "[]")
    except (CommandError, OSError, ValueError, json.JSONDecodeError) as exc:
        return fail("github_terminal_state_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc))
    return ok(status="read", issue=issue, pr=pr, open_prs=opens)
def read_reconcile_worktree_state(request: Request) -> Result:
    """Read worktree and local branch absence state."""
    upstream = _reconcile_upstream_failure(request, "read_reconcile_worktree_state", "validate_reconcile_identity", "read_remote_provenance")
    if upstream:
        return upstream
    data = input_of(request)
    try:
        rows = parse_worktree_porcelain(worktree_list(str(data["clone_path"])))
        worktree_absent = not any(str(Path(row.get("path") or "").resolve()) == str(Path(str(data["worktree_path"])).resolve()) or row.get("branch") == data["branch"] for row in rows)
        branch_absent = _local_branch_absent(str(data["clone_path"]), str(data["branch"]))
    except (CommandError, OSError, ValueError) as exc:
        return fail("reconcile_worktree_state_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc))
    return ok(status="read", worktree_absent=worktree_absent and not os.path.lexists(str(data["worktree_path"])), local_branch_absent=branch_absent)


def decide_no_target_reconciliation(request: Request) -> Result:
    """Purely decide whether no-target terminal reconciliation is safe."""
    upstream = _reconcile_upstream_failure(
        request,
        "decide_no_target_reconciliation",
        "read_local_receipts",
        "read_claim_process_evidence",
        "read_github_terminal_state",
        "read_remote_provenance",
        "read_reconcile_worktree_state",
    )
    if upstream:
        return upstream

    data = input_of(request)
    receipts = cond_blob(request, "read_local_receipts")
    claims = cond_blob(request, "read_claim_process_evidence")
    github = cond_blob(request, "read_github_terminal_state")
    remote = cond_blob(request, "read_remote_provenance")
    worktree = cond_blob(request, "read_reconcile_worktree_state")
    task = receipts.get("task_receipt") or {}
    merge = receipts.get("merge_receipt") or {}

    if claims.get("active_claims"):
        return fail("cleanup_reconciliation_failed", failure_class="terminal", retry_safe=False, error=f"active claim evidence: {claims['active_claims']}", claims=claims)
    if claims.get("active_leases"):
        return fail("cleanup_reconciliation_failed", failure_class="terminal", retry_safe=False, error=f"active Fala process evidence: {claims['active_leases']}", claims=claims)
    if worktree.get("worktree_absent") is not True or worktree.get("local_branch_absent") is not True:
        return fail("cleanup_reconciliation_failed", failure_class="terminal", retry_safe=False, error=f"worktree evidence is not absent: {worktree}", worktree=worktree)
    if task.get("phase") not in {"PR_OPEN", "CLEANUP_TERMINAL"} or task.get("outcome") not in {"pr-open", "no-target-reconciled"}:
        return fail("cleanup_reconciliation_failed", failure_class="terminal", retry_safe=False, error="task receipt is not eligible for no-target reconciliation")
    if remote.get("origin_repo") != data.get("repo"):
        return fail("cleanup_reconciliation_failed", failure_class="terminal", retry_safe=False, error="origin repository mismatch")

    issue = github.get("issue") or {}
    pr = github.get("pr") or {}
    refs = remote.get("refs") or {}
    branch_ref = f"refs/heads/{data['branch']}"
    if issue.get("state") != "CLOSED" or pr.get("state") != "MERGED" or github.get("open_prs"):
        return fail("cleanup_reconciliation_failed", failure_class="terminal", retry_safe=False, error="GitHub terminal state mismatch")
    if pr.get("headRefName") != data.get("branch") or pr.get("headRefOid") != data.get("head_oid") or (pr.get("mergeCommit") or {}).get("oid") != data.get("merge_oid"):
        return fail("cleanup_reconciliation_failed", failure_class="terminal", retry_safe=False, error="pull request provenance mismatch")
    if refs.get(branch_ref) != data.get("head_oid") or refs.get("refs/heads/main") != data.get("origin_main_sha"):
        return fail("cleanup_reconciliation_failed", failure_class="terminal", retry_safe=False, error="remote branch provenance mismatch")

    postconditions = {
        "remote_branch_retained": True,
        "remote_branch_deleted": False,
        "worktree_absent": True,
        "local_branch_absent": True,
        "active_claim_absent": True,
        "task_process_absent": True,
        "task_lease_absent": True,
        "worker_lock_absent": True,
    }
    return ok(
        status="decided",
        outcome="NO_TARGET_RECONCILED",
        identity=cond_blob(request, "validate_reconcile_identity").get("identity"),
        postconditions=postconditions,
        receipts=receipts,
        github=github,
        remote=remote,
    )


def update_task_receipt(request: Request) -> Result:
    """Update the existing task receipt after reconciliation is decided."""
    upstream = _reconcile_upstream_failure(request, "update_task_receipt", "decide_no_target_reconciliation")
    if upstream:
        return upstream
    data = input_of(request)
    path = Path(str(data.get("task_receipt_path") or ""))
    if dry_run_flag(request):
        return planned(task_receipt_path=str(path))
    try:
        payload = _read_regular_json(path, private=True)
        payload.update({"phase": "CLEANUP_TERMINAL", "outcome": "no-target-reconciled", "cleanup_receipt": str(Path(str(data.get("receipt_path") or "")).resolve()), "worker_pid": "", "worker_pgid": "", "next_retry_after": ""})
        _atomic_replace_json(path, payload)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return fail("task_receipt_update_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), mutated=True)
    return ok(status="updated", task_receipt_path=str(path), mutated=True)


def publish_reconcile_receipt(request: Request) -> Result:
    """Publish no-target reconciliation receipt."""
    upstream = _reconcile_upstream_failure(request, "publish_reconcile_receipt", "decide_no_target_reconciliation", "update_task_receipt")
    if upstream:
        return upstream
    data = input_of(request)
    path = str(data.get("receipt_path") or "")
    decision = cond_blob(request, "decide_no_target_reconciliation")
    if not path or decision.get("outcome") != "NO_TARGET_RECONCILED":
        return fail("reconcile_receipt_payload_missing", failure_class="terminal", retry_safe=False)
    payload = {"version": 2, "phase": "CLEANUP_TERMINAL", "outcome": "NO_TARGET_RECONCILED", "entity": decision.get("identity") or data.get("identity") or {}, "postconditions": decision.get("postconditions") or {}}
    if dry_run_flag(request):
        return planned(receipt_path=path, payload=payload, postconditions=payload["postconditions"])
    try:
        with _receipt_directory_lock(Path(path).parent):
            return _publish_cleanup_receipt(Path(path), payload, path)
    except OSError as exc:
        return fail("receipt_write_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), receipt_path=path, mutated=True)


def verify_no_target_reconciliation(request: Request) -> Result:
    """Verify no-target receipt and terminal postconditions."""
    upstream = _reconcile_upstream_failure(request, "verify_no_target_reconciliation", "publish_reconcile_receipt")
    if upstream:
        return upstream
    publication = cond_blob(request, "publish_reconcile_receipt")
    if dry_run_flag(request):
        return planned(receipt_path=publication.get("receipt_path"), postconditions=publication.get("postconditions") or (publication.get("payload") or {}).get("postconditions") or {})
    path = Path(str(input_of(request).get("receipt_path") or ""))
    try:
        payload = _read_regular_json(path, private=True)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return fail("reconcile_receipt_readback_failed", failure_class="retryable_read", retry_safe=True, error=str(exc))
    post = payload.get("postconditions")
    required = {"remote_branch_retained", "worktree_absent", "local_branch_absent", "active_claim_absent", "task_process_absent", "task_lease_absent", "worker_lock_absent"}
    if payload.get("phase") != "CLEANUP_TERMINAL" or payload.get("outcome") != "NO_TARGET_RECONCILED" or not isinstance(post, dict) or post.get("remote_branch_deleted") is not False or any(post.get(key) is not True for key in required):
        return fail("reconcile_receipt_mismatch", failure_class="terminal", retry_safe=False)
    return ok(status="reconciled", receipt_path=str(path), postconditions=post)
_LIFECYCLE_ACTIVE_STATUSES = {"pending", "ready", "running", "waiting", "retry_wait", "cancel_requested"}
_LIFECYCLE_FAILURE_CONCLUSIONS = {"FAILURE", "FAIL", "FAILED", "CANCELLED", "CANCELED", "TIMED_OUT", "ERROR", "ACTION_REQUIRED", "STARTUP_FAILURE"}
_LIFECYCLE_SUCCESS_CONCLUSIONS = {"SUCCESS", "PASSED", "PASS", "NEUTRAL", "SKIPPED"}
_LIFECYCLE_PENDING_STATES = {"PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "REQUESTED", "WAITING"}
_LIFECYCLE_SUCCESS_STATES = {"SUCCESS", "PASSED", "PASS", "COMPLETED"}
_LIFECYCLE_FAILURE_STATES = {"FAILURE", "FAIL", "FAILED", "CANCELLED", "CANCELED", "TIMED_OUT", "ERROR", "ACTION_REQUIRED", "STARTUP_FAILURE"}


def _lifecycle_identity(data: dict[str, Any]) -> dict[str, Any]:
    """Return only the externally bound identity supplied by the caller."""
    identity = {key: data.get(key) for key in ("repo", "issue", "pr_number", "branch", "head_oid", "expected_head_oid")}
    if identity.get("issue") not in (None, ""):
        try:
            identity["issue"] = _positive_int(identity["issue"], "issue")
        except (TypeError, ValueError):
            pass
    if identity.get("pr_number") not in (None, ""):
        try:
            identity["pr_number"] = _positive_int(identity["pr_number"], "pr_number")
        except (TypeError, ValueError):
            pass
    for key in ("repo", "branch", "head_oid", "expected_head_oid"):
        if identity.get(key) is not None:
            identity[key] = str(identity[key]).strip()
    return identity
def _lifecycle_context_conflict(error: str, **extra: Any) -> Result:
    return fail("lifecycle_context_conflict", failure_class="terminal", retry_safe=False, mutated=False, error=error, **extra)


def _lifecycle_pr_repo(pr: dict[str, Any]) -> str:
    value = pr.get("repo") or pr.get("repo_full_name") or pr.get("headRepository") or pr.get("repository")
    if isinstance(value, dict):
        name_with_owner = value.get("nameWithOwner") or value.get("name_with_owner") or value.get("fullName")
        if name_with_owner:
            return str(name_with_owner).strip()
        owner = value.get("owner")
        owner = owner.get("login") if isinstance(owner, dict) else owner
        name = value.get("name")
        value = f"{owner}/{name}" if owner and name else ""
    return str(value or "").strip()


def _lifecycle_pr_number(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = _positive_int(value, "pr_number")
    except (TypeError, ValueError):
        return None
    return number


def _lifecycle_linked_numbers(pr: dict[str, Any]) -> list[int] | None:
    refs = pr.get("closingIssuesReferences")
    if refs is None:
        return []
    if not isinstance(refs, list):
        return None
    numbers: list[int] = []
    for ref in refs:
        value = ref.get("number") if isinstance(ref, dict) else None
        try:
            numbers.append(_positive_int(value, "issue"))
        except (TypeError, ValueError):
            return None
    return sorted(set(numbers))


def _durable_merged_lifecycle_context(request: Request) -> Result | None:
    """Recover one claimed merged identity; GitHub readback remains authoritative."""
    data, cfg = input_of(request), cfg_of(request)
    claim_value = (
        data.get("claim_path")
        or data.get("claim_root")
        or data.get("active_issue_path")
        or data.get("active_issue")
        or cfg.get("claim_root")
        or cfg.get("active_issue_path")
        or cfg.get("active_issue")
        or (cfg.get("paths") if isinstance(cfg.get("paths"), dict) else {}).get("active_issue")
    )
    receipt_value = data.get("merge_receipts") or cfg.get("merge_receipts") or (cfg.get("paths") if isinstance(cfg.get("paths"), dict) else {}).get("merge_receipts")
    if not claim_value or not receipt_value:
        return None
    claim_path = Path(str(claim_value)).expanduser()
    receipt_root = Path(str(receipt_value)).expanduser()
    if not claim_path.exists() or not receipt_root.exists():
        return None
    try:
        if claim_path.is_dir():
            claim_rows, claim_error = _claims_in_directory(claim_path)
            claims = [claim for _, claim in claim_rows]
        else:
            claim, claim_error = _read_claim(claim_path)
            claims = [claim] if claim is not None else []
        if claim_error or not claims:
            raise ValueError(claim_error or "missing active claim")
        claimed = {(str(claim.get("repo") or "").strip(), _positive_int(claim.get("issue"), "issue")) for claim in claims}
        if not receipt_root.is_dir():
            raise ValueError("merge receipt root is not a directory")
        paths = sorted(receipt_root.glob("*.json"))
        if len(paths) > 256:
            raise ValueError("too many merge receipts")
        identities: set[tuple[str, int, int, str, str]] = set()
        for path in paths:
            try:
                receipt = _read_regular_json(path)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            provenance = receipt.get("verified_provenance")
            receipt_repo = str(receipt.get("repo") or "")
            if (
                receipt.get("phase") != "MERGED"
                or not isinstance(provenance, dict)
                or provenance.get("source") != "github_pr_readback"
                or provenance.get("state") != "MERGED"
                or str(provenance.get("repo") or "") != receipt_repo
            ):
                continue
            branch = str(provenance.get("head_ref") or "").strip()
            branch_match = re.fullmatch(r"ai/fix/([1-9][0-9]*)(?:-[A-Za-z0-9._-]+)?", branch)
            if branch_match:
                receipt_issue = int(branch_match.group(1))
            else:
                try:
                    receipt_issue = int(receipt.get("issue"))
                except (TypeError, ValueError):
                    receipt_issue = 0
            if receipt_issue <= 0 or (receipt_repo, receipt_issue) not in claimed:
                continue
            number = _positive_int(provenance.get("number"), "pr_number")
            if receipt.get("pr") != number:
                raise ValueError("merge receipt PR identity mismatch")
            head = str(provenance.get("head_oid") or "").strip()
            if not head or receipt.get("headSha") != head:
                raise ValueError("merge receipt head identity mismatch")
            identities.add((receipt_repo, receipt_issue, number, branch, head))
        matches = [{"repo": item[0], "issue": item[1], "pr_number": item[2], "branch": item[3], "head_oid": item[4]} for item in sorted(identities)]
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return fail("lifecycle_durable_context_invalid", failure_class="terminal", retry_safe=False, mutated=False, error=str(exc))
    if not matches:
        return None
    if len(matches) != 1:
        return fail("lifecycle_durable_context_ambiguous", failure_class="terminal", retry_safe=False, mutated=False, count=len(matches))
    return ok(status="resolved", mutated=False, **matches[0])

def _resolve_lifecycle_context(request: Request) -> Result:
    """Resolve one lifecycle identity without choosing a configured repository."""
    data, cfg = input_of(request), cfg_of(request)
    upstream = _reconcile_upstream_failure(
        request,
        "resolve_lifecycle_context",
        "triage_load_pr_fields",
        "load_pr_fields",
        "triage_decide_triage_action",
        "decide_triage_action",
    )
    if upstream:
        return upstream
    load_idle = upstream_noop(request, "triage_load_pr_fields", "load_pr_fields")
    decide_idle = upstream_noop(request, "triage_decide_triage_action", "decide_triage_action")
    durable = _durable_merged_lifecycle_context(request)
    if durable is not None and durable.get("ok") is not True:
        return durable
    if durable is not None and durable.get("status") == "resolved":
        return ok(
            status="resolved",
            repo=str(durable["repo"]),
            issue=durable["issue"],
            pr_number=durable["pr_number"],
            branch=str(durable["branch"]),
            head_oid=str(durable.get("head_oid") or ""),
            board=str(data.get("board") or cfg.get("board") or ""),
            clone_path=str(data.get("clone_path") or cfg.get("clone_path") or ""),
            priority=data.get("priority") if data.get("priority") is not None else cfg.get("priority") if cfg.get("priority") is not None else 0,
            selected_pr=None,
            linked_issue_numbers=[durable["issue"]],
            durable_merged=True,
            mutated=False,
        )
    already = cond_blob(
        request,
        "dispatch_decide_held_issue_already_merged",
        "decide_held_issue_already_merged",
    )
    if (
        already.get("status") == "noop"
        and already.get("already_merged") is True
        and already.get("reason") == "held_claim_task_unavailable"
    ):
        try:
            issue = _positive_int(already.get("issue"), "issue")
            pr_number = _positive_int(already.get("pr_number") or already.get("number"), "pr_number")
        except (TypeError, ValueError):
            issue = 0
            pr_number = 0
        repo = str(already.get("repo") or "").strip()
        branch = str(already.get("branch") or already.get("head_ref") or already.get("headRefName") or "").strip()
        head = str(already.get("head_oid") or already.get("headRefOid") or "").strip()
        if repo and issue > 0 and pr_number > 0 and branch and head:
            return ok(
                status="resolved",
                repo=repo,
                issue=issue,
                pr_number=pr_number,
                branch=branch,
                head_oid=head,
                board=str(already.get("board") or data.get("board") or cfg.get("board") or ""),
                clone_path=str(already.get("clone_path") or data.get("clone_path") or cfg.get("clone_path") or ""),
                priority=already.get("priority") if already.get("priority") is not None else (
                    data.get("priority") if data.get("priority") is not None else cfg.get("priority") if cfg.get("priority") is not None else 0
                ),
                selected_pr=None,
                linked_issue_numbers=[issue],
                durable_merged=False,
                already_merged_gate=True,
                mutated=False,
            )
    if load_idle and decide_idle:
        return noop(str(decide_idle.get("reason") or load_idle.get("reason") or "no_selected_pr"), operation="resolve_lifecycle_context", worked=False)
    load = cond_blob(request, "triage_load_pr_fields", "load_pr_fields")
    decide = cond_blob(request, "triage_decide_triage_action", "decide_triage_action", "decide")
    conduction = [blob for blob in (load, decide) if blob and blob.get("status") != "noop"]
    explicit_prs = [data.get("pr")] if isinstance(data.get("pr"), dict) else []
    triage_prs: list[dict[str, Any]] = []
    for blob in conduction:
        for key in ("pr", "selected_pr"):
            value = blob.get(key)
            if isinstance(value, dict):
                triage_prs.append(value)
    prs = [*explicit_prs, *triage_prs]
    sources = [data, *conduction]

    def first_value(*keys: str) -> Any:
        for source in sources:
            for key in keys:
                value = source.get(key)
                if value not in (None, "", []):
                    return value
        for pr in prs:
            for key in keys:
                value = pr.get(key)
                if value not in (None, "", []):
                    return value
        return None

    repo = str(first_value("repo", "repository", "nameWithOwner") or "").strip()
    explicit_sources = [data, cfg]
    explicit_issue_raw = next((source.get(key) for source in explicit_sources for key in ("issue", "issue_number") if source.get(key) not in (None, "", [])), None)
    issue_raw = explicit_issue_raw if explicit_issue_raw not in (None, "") else (durable.get("issue") if durable else None)
    pr_raw = first_value("pr_number", "number")
    branch = str(first_value("branch", "head_ref", "headRefName") or "").strip()
    head = str(first_value("head_oid", "headRefOid", "expected_head_oid") or "").strip()
    try:
        issue = _positive_int(issue_raw, "issue") if issue_raw not in (None, "") else None
    except (TypeError, ValueError):
        return _lifecycle_context_conflict("invalid issue")
    pr_number = _lifecycle_pr_number(pr_raw)
    if pr_raw not in (None, "") and pr_number is None:
        return _lifecycle_context_conflict("invalid PR number")

    for pr in prs:
        candidate_repo = _lifecycle_pr_repo(pr)
        candidate_number = _lifecycle_pr_number(pr.get("number") or pr.get("pr_number"))
        candidate_branch = str(pr.get("headRefName") or pr.get("head_ref") or "").strip()
        candidate_head = str(pr.get("headRefOid") or pr.get("head_oid") or "").strip()
        if candidate_repo and repo and candidate_repo != repo:
            return _lifecycle_context_conflict("PR repository disagrees with lifecycle repository", field="repo")
        if candidate_repo and not repo:
            repo = candidate_repo
        if candidate_number is None:
            return _lifecycle_context_conflict("selected PR has no exact number")
        if pr_number is not None and candidate_number != pr_number:
            return _lifecycle_context_conflict("selected PR number disagrees with lifecycle PR", field="pr_number")
        if pr_number is None:
            pr_number = candidate_number
        if candidate_branch and branch and candidate_branch != branch:
            return _lifecycle_context_conflict("PR branch disagrees with lifecycle branch", field="branch")
        if candidate_branch and not branch:
            branch = candidate_branch
        if candidate_head and head and candidate_head != head:
            return _lifecycle_context_conflict("PR head disagrees with lifecycle head", field="head_oid")
        if candidate_head and not head:
            head = candidate_head

    for source in conduction:
        source_repo = str(source.get("repo") or "").strip()
        if source_repo and repo and source_repo != repo:
            return _lifecycle_context_conflict("triage repository disagrees with lifecycle repository", field="repo")
        source_branch = str(source.get("branch") or source.get("head_ref") or source.get("headRefName") or "").strip()
        if source_branch and branch and source_branch != branch:
            return _lifecycle_context_conflict("triage branch disagrees with lifecycle branch", field="branch")
        source_head = str(source.get("head_oid") or source.get("headRefOid") or "").strip()
        if source_head and head and source_head != head:
            return _lifecycle_context_conflict("triage head disagrees with lifecycle head", field="head_oid")

    linked: list[int] | None = None
    for pr in prs:
        values = _lifecycle_linked_numbers(pr)
        if values is None:
            return _lifecycle_context_conflict("selected PR has invalid closing issue references", field="issue")
        if linked is not None and values != linked:
            return _lifecycle_context_conflict("selected PR linked issues disagree", field="issue")
        linked = values
    if issue is None:
        if linked is None or len(linked) != 1:
            return _lifecycle_context_conflict("issue requires exactly one closing issue reference", field="issue")
        issue = linked[0]
    elif linked and issue not in linked:
        return _lifecycle_context_conflict("explicit issue is not linked by selected PR", field="issue")
    if not repo or issue is None or pr_number is None or not branch:
        return fail("lifecycle_github_context_missing", failure_class="terminal", retry_safe=False, mutated=False)
    return ok(
        status="resolved",
        repo=repo,
        issue=issue,
        pr_number=pr_number,
        branch=branch,
        head_oid=head,
        board=str(first_value("board") or ""),
        clone_path=str(first_value("clone_path") or ""),
        priority=first_value("priority") if first_value("priority") is not None else 0,
        selected_pr=triage_prs[-1] if triage_prs else (explicit_prs[0] if explicit_prs else None),
        linked_issue_numbers=linked or [],
    )


def _lifecycle_check_state(pr: dict[str, Any]) -> str:
    """Classify check rollup without treating absent evidence as green."""
    checks = pr.get("statusCheckRollup")
    if checks is None:
        checks = pr.get("checks")
    if not isinstance(checks, list):
        return "pending"
    if not checks:
        return "pending"
    pending = False
    for check in checks:
        if not isinstance(check, dict):
            return "pending"
        state = str(check.get("state") or check.get("status") or "").upper()
        conclusion = str(check.get("conclusion") or "").upper()
        if conclusion in _LIFECYCLE_FAILURE_CONCLUSIONS or state in _LIFECYCLE_FAILURE_STATES:
            return "failed"
        if state in _LIFECYCLE_PENDING_STATES or (conclusion and conclusion not in _LIFECYCLE_SUCCESS_CONCLUSIONS) or (not conclusion and state not in _LIFECYCLE_SUCCESS_STATES):
            pending = True
    return "pending" if pending else "passed"


def _lifecycle_pr_matches(pr: dict[str, Any], identity: dict[str, Any]) -> bool:
    expected_branch = str(identity.get("branch") or "")
    expected_head = str(identity.get("head_oid") or identity.get("expected_head_oid") or "")
    return (
        str(pr.get("headRefName") or pr.get("head_ref") or "") == expected_branch
        and str(pr.get("headRefOid") or pr.get("head_oid") or "") == expected_head
        and str(pr.get("baseRefName") or pr.get("base_ref") or "main") == "main"
    )


def _lifecycle_labels(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {str(item.get("name") or "").strip() if isinstance(item, dict) else str(item).strip() for item in value} - {""}
def _lifecycle_missing_marker(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().upper() in {"NOT_FOUND", "MISSING"}
    if isinstance(value, dict):
        return any(_lifecycle_missing_marker(value.get(key)) for key in ("status", "state", "reason", "error"))
    return False


def _lifecycle_remote_absent(github: dict[str, Any]) -> bool:
    """Accept absence only when GitHub explicitly reports both lifecycle objects missing."""
    explicit = github.get("missing_lifecycle") is True or github.get("remote_lifecycle_missing") is True
    issue = github.get("issue")
    pr = github.get("pr")
    issue_missing = github.get("issue_missing") is True or _lifecycle_missing_marker(issue)
    pr_missing = github.get("pr_missing") is True or _lifecycle_missing_marker(pr)
    return explicit or (issue_missing and pr_missing)


def _lifecycle_orphan_local(local: dict[str, Any]) -> bool:
    """Require one claim and no local ownership or receipt evidence."""
    paths = local.get("claim_paths") or []
    return (
        len(paths) == 1
        and local.get("claim_present") is True
        and local.get("task_receipt") is None
        and local.get("receipt") is None
        and local.get("receipt_conflict") is not True
        and local.get("worktree_present") is False
        and not local.get("active_leases")
    )


def _github_lifecycle_not_found(exc: CommandError, object_type: str) -> bool:
    """Recognize only GitHub's explicit object-not-found diagnostics."""
    text = " ".join(f"{exc.stderr}\n{exc.stdout}".upper().split())
    common = "COULD NOT RESOLVE TO AN ISSUE OR PULL REQUEST WITH THE NUMBER OF"
    specific = {
        "issue": ("COULD NOT RESOLVE TO AN ISSUE WITH THE NUMBER OF",),
        "pr": (
            "COULD NOT RESOLVE TO A PULL REQUEST WITH THE NUMBER OF",
            "COULD NOT RESOLVE TO A PULLREQUEST WITH THE NUMBER OF",
        ),
    }
    expected = specific[object_type]
    return common in text or any(marker in text for marker in expected)


def read_lifecycle_github_state(request: Request) -> Result:
    """Read one authoritative GitHub issue/PR/link/head/check snapshot."""
    context = _resolve_lifecycle_context(request)
    if context.get("ok") is not True or context.get("status") != "resolved":
        return context
    repo = str(context["repo"])
    issue_number = context["issue"]
    pr_number = context["pr_number"]
    branch = str(context["branch"])
    expected_head = str(context.get("head_oid") or "")
    data, cfg = input_of(request), cfg_of(request)
    gh = str(data.get("gh_cli") or cfg.get("gh_cli") or "gh")
    missing_issue = False
    missing_pr = False
    try:
        issue = json.loads(run_cmd([gh, "issue", "view", str(issue_number), "--repo", repo, "--json", "number,state,labels,assignees"], timeout=60).stdout)
    except CommandError as exc:
        if not _github_lifecycle_not_found(exc, "issue"):
            return fail("lifecycle_github_state_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), mutated=False, repo=repo, issue=issue_number, pr_number=pr_number, branch=branch)
        issue, missing_issue = "NOT_FOUND", True
    try:
        pr = json.loads(run_cmd([gh, "pr", "view", str(pr_number), "--repo", repo, "--json", "number,state,mergedAt,mergeCommit,headRefName,headRefOid,baseRefName,statusCheckRollup,closingIssuesReferences,labels"], timeout=60).stdout)
    except CommandError as exc:
        if not _github_lifecycle_not_found(exc, "pr"):
            return fail("lifecycle_github_state_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), mutated=False, repo=repo, issue=issue_number, pr_number=pr_number, branch=branch)
        pr, missing_pr = "NOT_FOUND", True
    if missing_issue or missing_pr:
        if not (missing_issue and missing_pr):
            return fail("lifecycle_identity_conflict", failure_class="terminal", retry_safe=False, mutated=False, field="issue_or_pr")
        return ok(
            status="read",
            repo=repo,
            issue=issue,
            pr=pr,
            issue_missing=True,
            pr_missing=True,
            missing_lifecycle=True,
            open_prs=[],
            linked_issue_numbers=[],
            checks_state="pending",
            requested_issue=issue_number,
            requested_pr=pr_number,
            issue_number=issue_number,
            pr_number=pr_number,
            mutated=False,
        )
    try:
        opens = json.loads(run_cmd([gh, "pr", "list", "--repo", repo, "--head", branch, "--state", "open", "--json", "number,state,headRefName,headRefOid,baseRefName,closingIssuesReferences,labels,statusCheckRollup"], timeout=60).stdout or "[]")
    except (CommandError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("lifecycle_github_state_read_failed", failure_class="retryable_read", retry_safe=True, error=str(exc), mutated=False, repo=repo, issue=issue_number, pr_number=pr_number, branch=branch)
    if not isinstance(issue, dict) or not isinstance(pr, dict) or not isinstance(opens, list) or any(not isinstance(row, dict) for row in opens):
        return fail("lifecycle_github_state_invalid", failure_class="terminal", retry_safe=False, mutated=False)
    linked = pr.get("closingIssuesReferences") or pr.get("linkedIssues") or []
    linked_numbers = []
    if str(pr.get("headRefName") or "") != branch or (expected_head and str(pr.get("headRefOid") or "") != expected_head) or _lifecycle_pr_repo(pr) not in ("", repo):
        return _lifecycle_context_conflict("GitHub PR identity disagrees with selected lifecycle context", field="repo_branch_head")
    if isinstance(linked, list):
        for item in linked:
            value = item.get("number") if isinstance(item, dict) else item
            try:
                linked_numbers.append(int(value))
            except (TypeError, ValueError):
                continue
    return ok(
        status="read",
        repo=repo,
        issue=issue,
        pr=pr,
        open_prs=opens,
        linked_issue_numbers=sorted(set(linked_numbers)),
        checks_state=_lifecycle_check_state(pr),
        board=context.get("board", ""),
        clone_path=context.get("clone_path", ""),
        priority=context.get("priority", 0),
        requested_issue=issue_number,
        requested_pr=pr_number,
        issue_number=issue_number,
        pr_number=pr_number,
        mutated=False,
    )


def read_lifecycle_local_evidence(request: Request) -> Result:
    """Read local claim/task/process/worktree/receipt ledgers without mutation."""
    context = _resolve_lifecycle_context(request)
    if context.get("ok") is not True or context.get("status") != "resolved":
        return context
    data, cfg = input_of(request), cfg_of(request)
    repo = str(context["repo"])
    issue_number = context["issue"]
    try:
        issue = _positive_int(issue_number, "issue")
        claim_root_value = (
            data.get("claim_path")
            or data.get("claim_root")
            or data.get("active_issue_path")
            or data.get("active_issue")
            or cfg.get("claim_root")
            or cfg.get("active_issue_path")
            or cfg.get("active_issue")
            or (cfg.get("paths") if isinstance(cfg.get("paths"), dict) else {}).get("active_issue")
        )
        claim_root = Path(str(claim_root_value)).expanduser().resolve(strict=False) if claim_root_value else None
        claims = _matching_claims(claim_root, repo, issue) if claim_root is not None else []
        task_path_value = data.get("task_receipt_path")
        task_path = Path(str(task_path_value)).expanduser() if task_path_value else None
        task = _read_regular_json(task_path, private=True) if task_path is not None and task_path.exists() else None
        receipt_path_value = data.get("receipt_path")
        receipt_path = Path(str(receipt_path_value)).expanduser() if receipt_path_value else None
        receipt = _read_regular_json(receipt_path, private=True) if receipt_path is not None and receipt_path.exists() else None
        worktree_path_value = data.get("worktree_path")
        worktree_path = Path(str(worktree_path_value)).expanduser() if worktree_path_value else None
        worktree_present = os.path.lexists(str(worktree_path)) if worktree_path is not None else False
        leases: list[dict[str, Any]] = []
        db_value = data.get("db_path") or cfg.get("db_path")
        if db_value and Path(str(db_value)).exists():
            connection = sqlite3.connect(f"file:{Path(str(db_value)).resolve()}?mode=ro", uri=True, timeout=0)
            try:
                leases = _matching_active_leases(connection, str(data.get("task_id") or ""))
            finally:
                connection.close()
    except sqlite3.OperationalError as exc:
        if getattr(exc, "sqlite_errorcode", 0) & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            return fail("lifecycle_local_evidence_locked", failure_class="retryable_read", retry_safe=True, error=str(exc), mutated=False)
        return fail("lifecycle_local_evidence_read_failed", failure_class="terminal", retry_safe=False, error=str(exc), mutated=False)
    except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
        return fail("lifecycle_local_evidence_read_failed", failure_class="terminal", retry_safe=False, error=str(exc), mutated=False)
    claim_paths = [str(x) for x in claims]
    return ok(status="read", repo=repo, issue=issue, pr_number=context.get("pr_number"), branch=context.get("branch", ""), head_oid=context.get("head_oid", ""), board=context.get("board", ""), clone_path=context.get("clone_path", ""), priority=context.get("priority", 0), claim_paths=claim_paths, claim_present=bool(claim_paths), task_receipt=task, receipt=receipt, worktree_present=worktree_present, active_leases=leases, requested_issue=issue, requested_pr=context.get("pr_number"), mutated=False)


def decide_lifecycle_transition(request: Request) -> Result:
    """Purely choose the only safe recovery transition from authoritative evidence."""
    upstream = _reconcile_upstream_failure(request, "decide_lifecycle_transition", "read_lifecycle_github_state", "read_lifecycle_local_evidence")
    if upstream:
        return upstream
    github_idle = upstream_noop(request, "read_lifecycle_github_state")
    local_idle = upstream_noop(request, "read_lifecycle_local_evidence")
    if github_idle and local_idle:
        return noop(str(github_idle.get("reason") or local_idle.get("reason") or "no_selected_pr"), operation="decide_lifecycle_transition", worked=False)
    data = input_of(request)
    github = cond_blob(request, "read_lifecycle_github_state")
    local = cond_blob(request, "read_lifecycle_local_evidence")
    triage = cond_blob(request, "decide_triage_action", "triage_decide_triage_action")
    identity = _lifecycle_identity(data)
    if not identity.get("repo") or identity.get("issue") in (None, ""):
        dec_repo = github.get("repo") or local.get("repo")
        dec_issue = github.get("requested_issue") or github.get("issue_number") or local.get("issue")
        dec_pr = github.get("requested_pr") or github.get("pr_number") or local.get("pr_number")
        dec_branch = github.get("branch") or local.get("branch")
        dec_head = github.get("head_oid") or local.get("head_oid")
        if not identity.get("repo") and dec_repo:
            identity["repo"] = dec_repo
        if identity.get("issue") in (None, "") and dec_issue is not None:
            identity["issue"] = dec_issue
        if identity.get("pr_number") in (None, "") and dec_pr is not None:
            identity["pr_number"] = dec_pr
        if not identity.get("branch") and dec_branch:
            identity["branch"] = dec_branch
        if not identity.get("head_oid") and dec_head:
            identity["head_oid"] = dec_head
    if _lifecycle_remote_absent(github):
        if _lifecycle_orphan_local(local):
            return ok(status="decided", outcome="release_orphan", action="release_orphan", mutated=False, identity=identity, remote_lifecycle="absent", repo=identity.get("repo"), issue=identity.get("issue"))
        if not (local.get("claim_paths") or []) and local.get("claim_present") is False and local.get("task_receipt") is None and local.get("receipt") is None and local.get("worktree_present") is False and not local.get("active_leases"):
            return ok(status="decided", outcome="already_absent", action="already_absent", mutated=False, identity=identity, remote_lifecycle="absent", repo=identity.get("repo"), issue=identity.get("issue"))
        return fail("lifecycle_state_conflict", failure_class="terminal", retry_safe=False, mutated=False, error="remote lifecycle absent but local ownership is not an unambiguous orphan")
    issue = github.get("issue") or {}
    pr = github.get("pr") or {}
    if str(github.get("repo") or data.get("repo")) != str(identity.get("repo") or ""):
        return fail("lifecycle_identity_conflict", failure_class="terminal", retry_safe=False, mutated=False, field="repo")
    try:
        if int(issue.get("number") or 0) != int(identity.get("issue") or 0) or int(pr.get("number") or 0) != int(identity.get("pr_number") or 0):
            raise ValueError("issue_or_pr")
    except (TypeError, ValueError):
        return fail("lifecycle_identity_conflict", failure_class="terminal", retry_safe=False, mutated=False, field="issue_or_pr")
    if not _lifecycle_pr_matches(pr, identity):
        return fail("lifecycle_identity_conflict", failure_class="terminal", retry_safe=False, mutated=False, field="repo_issue_pr_branch_head")
    linked = set(github.get("linked_issue_numbers") or [])
    if not linked or int(identity["issue"]) not in linked:
        return fail("lifecycle_identity_conflict", failure_class="terminal", retry_safe=False, mutated=False, field="linked_issue")
    opens = github.get("open_prs") or []
    matching = [row for row in opens if isinstance(row, dict) and int(row.get("number") or 0) == int(identity["pr_number"])]
    if len(opens) > 1 or len(matching) != (1 if str(pr.get("state") or "").upper() == "OPEN" else 0):
        return fail("lifecycle_identity_conflict", failure_class="terminal", retry_safe=False, mutated=False, field="open_prs")
    state = str(pr.get("state") or "").upper()
    if state == "MERGED":
        return ok(status="decided", outcome="finalize_merged", action="finalize_merged", mutated=False, identity=identity)
    if state == "CLOSED":
        return ok(status="decided", outcome="finalize_closed", action="finalize_closed", mutated=False, identity=identity)
    if state != "OPEN":
        return fail("lifecycle_state_conflict", failure_class="terminal", retry_safe=False, mutated=False, state=state)
    labels = _lifecycle_labels(issue.get("labels"))
    check_state = str(github.get("checks_state") or _lifecycle_check_state(pr))
    raw_checks = pr.get("statusCheckRollup") if "statusCheckRollup" in pr else pr.get("checks")
    require_checks = bool(data.get("require_checks", cfg_of(request).get("require_checks", True)))
    if not require_checks and (raw_checks is None or raw_checks == []):
        check_state = "passed"
    if check_state == "pending":
        return ok(status="decided", outcome="wait_pending_checks", action="wait_pending_checks", mutated=False, identity=identity, checks_state=check_state)
    if (
        triage.get("ok") is True
        and triage.get("status") == "decided"
        and triage.get("action") == "repair"
        and triage.get("reason") == "missing_test_evidence"
        and check_state == "passed"
    ):
        return ok(
            status="decided",
            outcome="resume_repair",
            action="resume_repair",
            mutated=False,
            identity=identity,
            checks_state=check_state,
            labels=sorted(labels),
            repair_reason="missing_test_evidence",
        )
    if check_state == "failed":
        return ok(status="decided", outcome="resume_repair", action="resume_repair", mutated=False, identity=identity, checks_state=check_state, labels=sorted(labels))
    if check_state == "passed":
        return ok(status="decided", outcome="ready_for_merge", action="ready_for_merge", mutated=False, identity=identity, checks_state=check_state, labels=sorted(labels))
    return fail("lifecycle_state_conflict", failure_class="terminal", retry_safe=False, mutated=False, error="open PR has no actionable failed or pending check state")


def release_orphan_claim(request: Request) -> Result:
    """Release only a claim selected as a proven orphan, never a GitHub label."""
    data = input_of(request)
    upstream = _reconcile_upstream_failure(request, "release_orphan_claim", "decide_lifecycle_transition", "read_lifecycle_local_evidence")
    if upstream:
        return upstream
    decision = cond_blob(request, "decide_lifecycle_transition")
    local = cond_blob(request, "read_lifecycle_local_evidence")
    if decision.get("outcome") == "already_absent":
        return ok(status="skipped", outcome="already_absent", claim_path=str(data.get("claim_path") or ""), mutated=False)
    if decision.get("outcome") != "release_orphan":
        return ok(status="skipped", outcome="not_orphan", mutated=False)
    paths = local.get("claim_paths") or []
    if local.get("claim_present") is False and not paths:
        return ok(
            status="skipped",
            outcome="already_absent",
            claim_path=str(data.get("claim_path") or ""),
            mutated=False,
        )
    if len(paths) != 1 or not local.get("claim_present") or local.get("task_receipt") is not None or local.get("receipt") is not None or local.get("receipt_conflict") is True or local.get("worktree_present") or local.get("active_leases"):
        return fail("orphan_ownership_unproven", failure_class="terminal", retry_safe=False, mutated=False)
    path = Path(str(paths[0]))
    if not path.exists():
        return ok(status="skipped", outcome="already_absent", claim_path=str(path), mutated=False)
    try:
        claim = _read_regular_json(path, private=True)
        claim_repo = claim.get("repo")
        claim_issue = claim.get("issue")

        repo_ref = data.get("repo")
        issue_ref = data.get("issue")

        dec_repo = decision.get("repo") or decision.get("identity", {}).get("repo")
        dec_issue = decision.get("issue") or decision.get("identity", {}).get("issue")

        local_repo = local.get("repo")
        local_issue = local.get("issue")

        if dec_repo and local_repo and dec_repo != local_repo:
            return fail("lifecycle_identity_conflict", failure_class="terminal", retry_safe=False, mutated=False, field="repo")
        if dec_issue not in (None, "") and local_issue not in (None, "") and int(dec_issue) != int(local_issue):
            return fail("lifecycle_identity_conflict", failure_class="terminal", retry_safe=False, mutated=False, field="issue")

        resolved_repo = repo_ref or dec_repo or local_repo
        resolved_issue = issue_ref if issue_ref not in (None, "") else (dec_issue if dec_issue not in (None, "") else local_issue)

        if not resolved_repo or resolved_issue in (None, ""):
            return fail("lifecycle_identity_conflict", failure_class="terminal", retry_safe=False, mutated=False, field="repo_issue")

        if str(claim_repo) != str(resolved_repo) or int(claim_issue) != int(resolved_issue):
            return fail("lifecycle_identity_conflict", failure_class="terminal", retry_safe=False, mutated=False, field="claim")

        if repo_ref and dec_repo and repo_ref != dec_repo:
            return fail("lifecycle_identity_conflict", failure_class="terminal", retry_safe=False, mutated=False, field="repo")
        if repo_ref and local_repo and repo_ref != local_repo:
            return fail("lifecycle_identity_conflict", failure_class="terminal", retry_safe=False, mutated=False, field="repo")
        if issue_ref not in (None, "") and dec_issue not in (None, "") and int(issue_ref) != int(dec_issue):
            return fail("lifecycle_identity_conflict", failure_class="terminal", retry_safe=False, mutated=False, field="issue")
        if issue_ref not in (None, "") and local_issue not in (None, "") and int(issue_ref) != int(local_issue):
            return fail("lifecycle_identity_conflict", failure_class="terminal", retry_safe=False, mutated=False, field="issue")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return fail("orphan_claim_read_failed", failure_class="terminal", retry_safe=False, error=str(exc), mutated=False)
    if dry_run_flag(request):
        return planned(claim_path=str(path), outcome="release_orphan")
    try:
        path.unlink()
        parent_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        return fail("orphan_claim_release_failed", failure_class="reconcile_then_retry", retry_safe=False, error=str(exc), claim_path=str(path), mutated=True)
    return ok(status="released", outcome="release_orphan", claim_path=str(path), mutated=True)


def verify_orphan_claim_release(request: Request) -> Result:
    """Read back the local claim ledger after a proven orphan release."""
    upstream = _reconcile_upstream_failure(request, "verify_orphan_claim_release", "release_orphan_claim")
    if upstream:
        return upstream
    released = cond_blob(request, "release_orphan_claim")
    if released.get("status") == "skipped" and released.get("outcome") in {"not_orphan", "already_absent"}:
        return ok(
            status="skipped",
            outcome=str(released.get("outcome")),
            claim_path=str(released.get("claim_path") or ""),
            absent=released.get("outcome") == "already_absent",
            mutated=False,
        )
    if dry_run_flag(request):
        return planned(claim_path=released.get("claim_path"))
    path = Path(str(released.get("claim_path") or input_of(request).get("claim_path") or ""))
    if path.exists():
        return fail("orphan_claim_not_absent", failure_class="reconcile_then_retry", retry_safe=False, claim_path=str(path), mutated=bool(released.get("mutated")))
    return ok(status="verified", outcome="release_orphan", claim_path=str(path), absent=True, mutated=False)
