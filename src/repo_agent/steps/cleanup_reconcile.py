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

from repo_agent.adapters_cli import CommandError, run_cmd
from repo_agent.adapters_git import parse_worktree_porcelain, worktree_list
from repo_agent.envelope import Request, Result, cfg_of, dry_run_flag, fail, input_of, ok, planned
from repo_agent.steps.cleanup import _publish_cleanup_receipt, _receipt_directory_lock
from repo_agent.steps.claim import _claim_file, _read_claim, claim_directory_lock

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


def reconcile_no_target_cleanup(request: Request) -> Result:
    """Prove an exact terminal state while retaining the authorized remote branch."""
    data = input_of(request)
    missing = [name for name in _REQUIRED if data.get(name) in (None, "")]
    if missing:
        return fail("cleanup_identity_missing", failure_class="terminal", retry_safe=False, missing=missing)
    if data.get("remote_retention_authorized") is not True:
        return fail("remote_retention_not_authorized", failure_class="terminal", retry_safe=False)
    try:
        issue = _positive_int(data["issue"], "issue")
        pr_number = _positive_int(data["pr_number"], "pr_number")
        identity = {name: str(data[name]).strip() for name in _REQUIRED if name not in {"issue", "pr_number"}}
        identity.update({"issue": issue, "pr_number": pr_number})
        branch = identity["branch"]
        if re.fullmatch(r"ai/fix/[1-9][0-9]*(?:-[A-Za-z0-9._-]+)?", branch) is None or int(branch.split("/", 2)[2].split("-", 1)[0]) != issue:
            raise ValueError("branch must be the canonical ai/fix/<issue>[-slug] form")
        for sha_name in ("base_sha", "head_oid", "merge_oid", "origin_main_sha"):
            value = identity[sha_name]
            if len(value) != 40 or any(char not in "0123456789abcdefABCDEF" for char in value):
                raise ValueError(f"{sha_name} must be a full object id")
    except (TypeError, ValueError) as exc:
        return fail("cleanup_identity_invalid", failure_class="terminal", retry_safe=False, error=str(exc))

    config = cfg_of(request)
    try:
        repo = str(config["repo"]).strip()
        clone_path = str(Path(config["clone_path"]).expanduser().resolve(strict=True))
        worktree_root = Path(config["worktree_root"]).expanduser().resolve(strict=False)
        expected_worktree = (worktree_root / branch).resolve(strict=False)
        if not expected_worktree.is_relative_to(worktree_root):
            raise ValueError("worktree path escapes configured root")
        worktree_path = str(expected_worktree)
        claim_root = Path(config["claim_root"]).expanduser().resolve(strict=False)
        claim_path = _claim_file(str(claim_root))
        db_path = Path(config["db_path"]).expanduser().resolve(strict=False)
        task_receipt_root = Path(config["task_receipt_root"]).expanduser().resolve(strict=False)
        merge_receipt_root = Path(config["merge_receipt_root"]).expanduser().resolve(strict=False)
        cleanup_receipt_root = Path(config["cleanup_receipt_root"]).expanduser().resolve(strict=False)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        return fail("cleanup_context_missing", failure_class="terminal", retry_safe=False, error=str(exc))
    if claim_path is None:
        return fail("cleanup_context_missing", failure_class="terminal", retry_safe=False, error="claim_root is invalid")
    claim_path = claim_path.expanduser().resolve(strict=False)
    supplied = {
        "repo": repo, "clone_path": clone_path, "worktree_path": worktree_path,
        "db_path": str(db_path),
    }
    for key, expected in supplied.items():
        actual = str(identity[key] if key in identity else data[key]).strip()
        if key.endswith("_path"):
            actual = str(Path(actual).expanduser().resolve(strict=False))
        if actual != expected:
            return fail("cleanup_context_mismatch", failure_class="terminal", retry_safe=False, field=key)
    supplied_claim_path = _claim_file(str(data["claim_path"]))
    if supplied_claim_path is None or supplied_claim_path.expanduser().resolve(strict=False) != claim_path:
        return fail("cleanup_context_mismatch", failure_class="terminal", retry_safe=False, field="claim_path")
    receipt_path = Path(identity["receipt_path"]).expanduser().resolve(strict=False)
    task_receipt_path = Path(identity["task_receipt_path"]).expanduser().resolve(strict=False)
    merge_receipt_path = Path(identity["merge_receipt_path"]).expanduser().resolve(strict=False)
    for path, root, field in (
        (task_receipt_path, task_receipt_root, "task_receipt_path"),
        (merge_receipt_path, merge_receipt_root, "merge_receipt_path"),
        (receipt_path, cleanup_receipt_root, "receipt_path"),
    ):
        if path.parent != root:
            return fail("cleanup_context_mismatch", failure_class="terminal", retry_safe=False, field=field)
    lock_path = Path(str(task_receipt_path) + ".lock")
    dry_run = dry_run_flag(request)

    lock_handle = None
    claim_guard = None
    db_connection = None
    receipt_published = False
    try:
        lock_metadata = os.lstat(lock_path)
        if not stat.S_ISREG(lock_metadata.st_mode) or lock_metadata.st_nlink != 1:
            raise ValueError("unsafe task lock artifact")
        lock_fd = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        lock_handle = os.fdopen(lock_fd, "r+b")
        locked_metadata = os.fstat(lock_handle.fileno())
        if (lock_metadata.st_dev, lock_metadata.st_ino) != (locked_metadata.st_dev, locked_metadata.st_ino):
            raise ValueError("task lock artifact changed concurrently")
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        current_lock_metadata = os.lstat(lock_path)
        if (locked_metadata.st_dev, locked_metadata.st_ino) != (current_lock_metadata.st_dev, current_lock_metadata.st_ino):
            raise ValueError("task lock artifact changed concurrently")
        if not claim_path.parent.is_dir():
            raise ValueError("claim directory is absent")
        task_receipt = _read_regular_json(task_receipt_path, private=True)
        claim_guard = claim_directory_lock(claim_path)
        claim_guard.__enter__()
        merge_receipt = _read_regular_json(merge_receipt_path)

        expected_task = {
            "repo": identity["repo"], "issue": str(issue), "task_id": identity["task_id"],
            "branch": identity["branch"], "base_sha": identity["base_sha"],
        }
        if any(str(task_receipt.get(key) or "") != expected for key, expected in expected_task.items()) or str(Path(str(task_receipt.get("clone_path") or "")).expanduser().resolve(strict=False)) != clone_path:
            raise ValueError("task receipt identity mismatch")
        expected_merge = {
            "repo": identity["repo"], "issue": issue, "pr": pr_number, "branch": identity["branch"],
            "baseSha": identity["base_sha"], "headSha": identity["head_oid"], "mergeSha": identity["merge_oid"],
        }
        if any(merge_receipt.get(key) != expected for key, expected in expected_merge.items()):
            raise ValueError("merge receipt identity mismatch")
        if merge_receipt.get("originMainSha") != identity["merge_oid"]:
            raise ValueError("merge receipt origin provenance mismatch")
        if str(merge_receipt.get("phase")) != "ISSUE_CLOSED_CONFIRMED":
            raise ValueError("merge receipt is not terminal")

        gh_issue = json.loads(run_cmd(["gh", "issue", "view", str(issue), "--repo", identity["repo"], "--json", "number,state"], timeout=60).stdout)
        gh_pr = json.loads(run_cmd(["gh", "pr", "view", str(pr_number), "--repo", identity["repo"], "--json", "number,state,mergedAt,mergeCommit,headRefName,headRefOid,baseRefName"], timeout=60).stdout)
        merge_commit = gh_pr.get("mergeCommit") if isinstance(gh_pr, dict) else None
        if gh_issue != {"number": issue, "state": "CLOSED"}:
            raise ValueError("GitHub issue is not exactly closed")
        if not isinstance(gh_pr, dict) or gh_pr.get("number") != pr_number or gh_pr.get("state") != "MERGED" or not gh_pr.get("mergedAt") or gh_pr.get("headRefName") != identity["branch"] or gh_pr.get("headRefOid") != identity["head_oid"] or gh_pr.get("baseRefName") != "main" or not isinstance(merge_commit, dict) or merge_commit.get("oid") != identity["merge_oid"]:
            raise ValueError("GitHub PR provenance mismatch")
        open_prs = json.loads(run_cmd(["gh", "pr", "list", "--repo", identity["repo"], "--head", identity["branch"], "--state", "open", "--json", "number"], timeout=60).stdout or "[]")
        if open_prs != []:
            raise ValueError("branch still has an open PR or PR readback is ambiguous")

        if _origin_repo(clone_path) != identity["repo"]:
            raise ValueError("clone origin repository mismatch")
        refs = _ls_remote(clone_path, "refs/heads/main", f"refs/heads/{identity['branch']}")
        if refs.get("refs/heads/main") != identity["origin_main_sha"] or refs.get(f"refs/heads/{identity['branch']}") != identity["head_oid"]:
            raise ValueError("remote ref provenance mismatch")
        run_cmd(["git", "-C", clone_path, "merge-base", "--is-ancestor", identity["merge_oid"], identity["origin_main_sha"]], timeout=60)

        rows = parse_worktree_porcelain(worktree_list(clone_path))
        if any(str(Path(row.get("path") or "").resolve()) == worktree_path or row.get("branch") == identity["branch"] for row in rows):
            raise ValueError("cleanup worktree is still present")
        if os.path.lexists(worktree_path) or not _local_branch_absent(clone_path, identity["branch"]):
            raise ValueError("local cleanup target is still present")
        active_claims = _matching_claims(claim_root, identity["repo"], issue)
        if active_claims:
            raise ValueError(f"active claim is still present: {active_claims[0]}")
        if _process_alive(task_receipt.get("worker_pid")) or _process_alive(task_receipt.get("worker_pgid"), group=True):
            raise ValueError("task worker is still alive")
        db_metadata = os.lstat(db_path)
        if not stat.S_ISREG(db_metadata.st_mode) or db_metadata.st_nlink != 1:
            raise ValueError("unsafe Fala database artifact")
        db_connection = sqlite3.connect(f"file:{db_path}?mode={'ro' if dry_run else 'rw'}", uri=True, timeout=0)
        if not dry_run:
            db_connection.execute("BEGIN IMMEDIATE")
        if _matching_active_leases(db_connection, identity["task_id"]):
            raise ValueError("task has an active Fala process or lease")
        worker_lock = Path(worktree_path) / ".agent.lock" / "pid"
        if worker_lock.exists():
            raise ValueError("task worker lock is still present")

        terminal_task = dict(task_receipt)
        update_task_receipt = False
        if task_receipt.get("phase") == "CLEANUP_TERMINAL" or task_receipt.get("outcome") == "no-target-reconciled" or task_receipt.get("cleanup_receipt"):
            expected_terminal = {"phase": "CLEANUP_TERMINAL", "outcome": "no-target-reconciled", "cleanup_receipt": str(receipt_path), "worker_pid": "", "worker_pgid": "", "next_retry_after": ""}
            if any(task_receipt.get(key) != value for key, value in expected_terminal.items()):
                raise ValueError("terminal task receipt identity mismatch")
        else:
            if task_receipt.get("phase") != "PR_OPEN" or task_receipt.get("outcome") != "pr-open":
                raise ValueError("task receipt is not eligible for terminal cleanup")
            terminal_task.update({"phase": "CLEANUP_TERMINAL", "outcome": "no-target-reconciled", "cleanup_receipt": str(receipt_path), "worker_pid": "", "worker_pgid": "", "next_retry_after": ""})
            update_task_receipt = True

        postconditions = {
            "issue_closed": True, "pr_merged": True, "open_pr_count": 0, "merge_on_current_main": True,
            "worktree_absent": True, "local_branch_absent": True, "active_claim_absent": True,
            "task_process_absent": True, "task_lease_absent": True, "task_lock_active": False,
            "worker_lock_absent": True, "remote_branch_retained": True, "remote_branch_deleted": False,
            "remote_retention_authorized": True,
        }
        previous_phase = task_receipt.get("phase")
        previous_outcome = task_receipt.get("outcome")
        if receipt_path.exists():
            existing_receipt = _read_regular_json(receipt_path, private=True)
            previous_phase = existing_receipt.get("task_receipt_previous_phase")
            previous_outcome = existing_receipt.get("task_receipt_previous_outcome")
            if (previous_phase, previous_outcome) not in {("PR_OPEN", "pr-open"), ("CLEANUP_TERMINAL", "no-target-reconciled")}:
                raise ValueError("cleanup receipt prior task state is invalid")

        payload = {
            "version": 2, "phase": "CLEANUP_TERMINAL", "outcome": "NO_TARGET_RECONCILED",
            "entity": identity, "postconditions": postconditions,
            "task_receipt_previous_phase": previous_phase, "task_receipt_previous_outcome": previous_outcome,
        }
        if dry_run:
            db_connection.rollback()
            return planned(receipt_path=str(receipt_path), entity=identity, postconditions=postconditions, remote_branch_retained=True)
        current_lock_metadata = os.lstat(lock_path)
        if (locked_metadata.st_dev, locked_metadata.st_ino) != (current_lock_metadata.st_dev, current_lock_metadata.st_ino):
            raise ValueError("task lock artifact changed concurrently")
        with _receipt_directory_lock(receipt_path.parent):
            published = _publish_cleanup_receipt(receipt_path, payload, str(receipt_path))
        if published.get("ok") is not True or published.get("status") not in {"written", "exists"}:
            return published
        receipt_published = True
        if update_task_receipt:
            _atomic_replace_json(task_receipt_path, terminal_task)
        db_connection.commit()
        return ok(status="reconciled", receipt_path=str(receipt_path), entity=identity, postconditions=postconditions, task_receipt_path=str(task_receipt_path), mutated=bool(published.get("mutated")) or update_task_receipt)
    except BlockingIOError as exc:
        return fail("task_lock_active", failure_class="retryable_read", retry_safe=True, lock_path=str(lock_path), error=str(exc), mutated=receipt_published)
    except sqlite3.OperationalError as exc:
        locked = (getattr(exc, "sqlite_errorcode", -1) & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
        return fail("database_lock_active" if locked else "cleanup_reconciliation_failed", failure_class="retryable_read" if locked else "terminal", retry_safe=locked, lock_path=str(lock_path), error=str(exc), mutated=receipt_published)
    except (CommandError, OSError, sqlite3.Error, json.JSONDecodeError, TypeError, ValueError) as exc:
        return fail("cleanup_reconciliation_failed", failure_class="reconcile_then_retry" if receipt_published else "terminal", retry_safe=False, error=str(exc), receipt_path=str(receipt_path), mutated=receipt_published)
    finally:
        if db_connection is not None:
            db_connection.close()
        if claim_guard is not None:
            claim_guard.__exit__(None, None, None)
        if lock_handle is not None:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            finally:
                lock_handle.close()
