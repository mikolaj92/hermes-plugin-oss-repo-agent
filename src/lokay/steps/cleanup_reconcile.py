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
from lokay.envelope import Request, Result, cfg_of, cond_blob, dry_run_flag, fail, input_of, ok, planned
from lokay.steps.cleanup import _publish_cleanup_receipt, _receipt_directory_lock
from lokay.steps.claim import _claim_file, _read_claim, claim_directory_lock

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
        claim_root = Path(str(cfg.get("claim_root") or data.get("claim_path") or "")).expanduser().resolve(strict=False)
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
