"""Read-only, pinned GitHub and repository evidence for issue triage."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from lokay.adapters_cli import CommandError, run_cmd
from lokay.envelope import Request, Result, cfg_of, cond_get, fail, input_of, ok
from lokay.steps.issue_triage import triage_gate, triage_identity

_OID = re.compile(r"^[0-9a-fA-F]{40}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})$")


def _bad(reason: str, **extra: Any) -> Result:
    return fail(reason, failure_class="terminal", retry_safe=False, mutated=False, **extra)


def _identity(request: Request, *upstream_ids: str) -> dict[str, Any]:
    return triage_identity(request, *upstream_ids)


def _gate(request: Request, operation: str, *upstream_ids: str) -> Result | None:
    return triage_gate(request, operation, *upstream_ids)


def _repo(data: Mapping[str, Any], cfg: Mapping[str, Any]) -> str:
    selected = data.get("selected") if isinstance(data.get("selected"), Mapping) else {}
    return str(data.get("repo") or selected.get("repo") or cfg.get("repo") or "").strip()


def _gh(data: Mapping[str, Any], cfg: Mapping[str, Any]) -> str:
    return str(data.get("gh_cli") or cfg.get("gh_cli") or "gh")

def _number(data: Mapping[str, Any]) -> int:
    selected = data.get("selected") if isinstance(data.get("selected"), Mapping) else {}
    value = data.get("number", data.get("issue", selected.get("number", selected.get("issue"))))
    if isinstance(value, bool):
        return 0
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _json_command(command: list[str], *, cwd: str | Path | None = None) -> Any:
    proc = run_cmd(command, timeout=120, cwd=cwd)
    text = (proc.stdout or "").strip()
    if not text:
        raise ValueError("empty_json")
    return json.loads(text)


def _timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _labels(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    result: list[str] = []
    for item in value:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str) or not item["name"].strip():
            return None
        result.append(item["name"])
    return result


def _issue_shape(value: Any, repo: str, number: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("malformed_issue_payload")
    try:
        remote_number = value.get("number")
        state = value.get("state")
        url = value.get("url")
        updated = value.get("updatedAt")
        labels = _labels(value.get("labels"))
        if isinstance(remote_number, bool) or int(remote_number) != number or not isinstance(state, str) or state.upper() not in {"OPEN", "CLOSED"}:
            raise ValueError("malformed_issue_payload")
        if not isinstance(url, str) or not url.startswith("https://github.com/") or not _timestamp(updated):
            raise ValueError("malformed_issue_payload")
        if labels is None:
            raise ValueError("malformed_issue_payload")
        title, body = value.get("title", ""), value.get("body", "")
        if not isinstance(title, str) or not isinstance(body, str):
            raise ValueError("malformed_issue_payload")
        return {"repo": repo, "number": number, "title": title, "body": body, "url": url, "state": state.upper(), "updatedAt": updated, "labels": labels, "raw": dict(value)}
    except (TypeError, ValueError) as exc:
        raise ValueError("malformed_issue_payload") from exc
def read_triage_issue_state(request: Request) -> Result:
    """Read one fresh issue state, including its update watermark."""
    gate = _gate(request, "read_triage_issue_state", "select_triage_candidate", "reserve_triage_run_budget")
    if gate:
        return gate
    data, cfg = input_of(request), cfg_of(request)
    ident = _identity(request, "select_triage_candidate", "reserve_triage_run_budget")
    repo, number = ident["repo"], ident["number"]
    if not repo or not number:
        return _bad("missing_repo_or_number")
    try:
        value = _json_command([_gh(data, cfg), "issue", "view", str(number), "--repo", repo, "--json", "number,title,body,url,state,updatedAt,labels"])
        return ok(status="issue_read", issue=_issue_shape(value, repo, number), **ident)
    except (CommandError, subprocess.TimeoutExpired) as exc:
        return fail("fresh_issue_read_failed", failure_class="retryable_read", retry_safe=True, mutated=False, error=str(exc), **ident)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return _bad("malformed_issue_payload", error=str(exc), **ident)


def _comments_shape(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("malformed_comments_payload")
    comments: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping) or isinstance(item.get("databaseId"), bool) or not isinstance(item.get("databaseId"), int) or item["databaseId"] <= 0:
            raise ValueError("malformed_comments_payload")
        if not isinstance(item.get("body"), str) or not _timestamp(item.get("createdAt")):
            raise ValueError("malformed_comments_payload")
        author = item.get("author")
        if not isinstance(author, Mapping) or not isinstance(author.get("login"), str) or not author["login"].strip():
            raise ValueError("malformed_comments_payload")
        association = item.get("authorAssociation")
        comments.append(dict(item))
    return comments


def read_triage_comments(request: Request) -> Result:
    """Read comments with stable database IDs and author associations."""
    gate = _gate(request, "read_triage_comments", "select_triage_candidate", "reserve_triage_run_budget")
    if gate:
        return gate
    data, cfg = input_of(request), cfg_of(request)
    ident = _identity(request, "select_triage_candidate", "reserve_triage_run_budget")
    repo, number = ident["repo"], ident["number"]
    if not repo or not number:
        return _bad("missing_repo_or_number")
    try:
        value = _json_command([_gh(data, cfg), "issue", "view", str(number), "--repo", repo, "--json", "comments"])
        raw = value.get("comments") if isinstance(value, Mapping) else value
        return ok(status="comments_read", comments=_comments_shape(raw), **ident)
    except (CommandError, subprocess.TimeoutExpired) as exc:
        return fail("fresh_comments_read_failed", failure_class="retryable_read", retry_safe=True, mutated=False, error=str(exc), **ident)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return _bad("malformed_comments_payload", error=str(exc), **ident)


def read_triage_canonical_issue(request: Request) -> Result:
    """Read and validate a duplicate target in the same repository."""
    gate = _gate(request, "read_triage_canonical_issue", "select_triage_candidate")
    if gate:
        return gate
    data, cfg = input_of(request), cfg_of(request)
    ident = _identity(request, "select_triage_candidate")
    repo, source_number = ident["repo"], ident["number"]
    classified = cond_get(request, "classification", "classify_triage_issue", default=data.get("classification"))
    if isinstance(classified, Mapping):
        value = classified.get("canonical_issue")
        classification_name = classified.get("classification")
    else:
        value = data.get("canonical_issue", data.get("canonical_number"))
        classification_name = classified or ("duplicate" if value not in (None, "", 0) else "")
    if str(classification_name or "").strip().casefold() != "duplicate":
        return ok(status="noop", reason="canonical_issue_not_required", canonical=None, canonical_issue=0, **ident)
    try:
        target = 0 if isinstance(value, bool) else int(value)
    except (TypeError, ValueError):
        target = 0
    if not repo or not source_number or target <= 0 or target == source_number:
        return _bad("invalid_canonical_issue", **ident)
    try:
        payload = _json_command([_gh(data, cfg), "issue", "view", str(target), "--repo", repo, "--json", "number,title,body,url,state,updatedAt,labels,repository"])
        if not isinstance(payload, Mapping):
            raise ValueError("malformed_canonical_payload")
        repository = payload.get("repository")
        identity = repository.get("nameWithOwner") if isinstance(repository, Mapping) else repository
        if not isinstance(identity, str) or identity.casefold() != repo.casefold():
            raise ValueError("canonical_repository_mismatch")
        canonical = _issue_shape(payload, repo, target)
        return ok(status="canonical_read", canonical=canonical, **ident, canonical_issue=target)
    except (CommandError, subprocess.TimeoutExpired) as exc:
        return fail("canonical_issue_read_failed", failure_class="retryable_read", retry_safe=True, mutated=False, error=str(exc), **ident, canonical_issue=target)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return _bad(str(exc) if str(exc) == "canonical_repository_mismatch" else "malformed_canonical_payload", error=str(exc), **ident, canonical_issue=target)


def read_triage_repository_state(request: Request) -> Result:
    """Read repository identity and exact default-branch object ID."""
    gate = _gate(request, "read_triage_repository_state", "select_triage_candidate", "reserve_triage_run_budget")
    if gate:
        return gate
    data, cfg = input_of(request), cfg_of(request)
    ident = _identity(request, "select_triage_candidate", "reserve_triage_run_budget")
    repo = ident["repo"]
    if not repo:
        return _bad("missing_repo", **ident)
    try:
        value = _json_command([_gh(data, cfg), "repo", "view", repo, "--json", "nameWithOwner,defaultBranchRef"])
        if not isinstance(value, Mapping) or not isinstance(value.get("nameWithOwner"), str) or value["nameWithOwner"].casefold() != repo.casefold():
            raise ValueError("repository_identity_mismatch")
        branch = value.get("defaultBranchRef")
        branch_name = branch.get("name") if isinstance(branch, Mapping) else None
        if not isinstance(branch_name, str) or not branch_name.strip():
            raise ValueError("missing_default_branch")
        proc = run_cmd([_gh(data, cfg), "api", f"repos/{repo}/git/ref/heads/{branch_name}", "--jq", ".object.sha"], timeout=120)
        oid = (proc.stdout or "").strip()
        if not _OID.fullmatch(oid):
            raise ValueError("malformed_default_branch_oid")
        return ok(status="repository_read", nameWithOwner=value["nameWithOwner"], default_branch=branch_name, default_branch_oid=oid.lower(), **ident)
    except (CommandError, subprocess.TimeoutExpired) as exc:
        return fail("repository_read_failed", failure_class="retryable_read", retry_safe=True, mutated=False, error=str(exc), **ident)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return _bad("malformed_repository_payload", error=str(exc), **ident)


def _snapshot(clone: str) -> dict[str, Any]:
    head = (run_cmd(["git", "rev-parse", "HEAD"], cwd=clone, timeout=60).stdout or "").strip()
    upstream = (run_cmd(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], cwd=clone, timeout=60).stdout or "").strip()
    upstream_oid = (run_cmd(["git", "rev-parse", "@{upstream}"], cwd=clone, timeout=60).stdout or "").strip()
    porcelain = run_cmd(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=clone, timeout=60).stdout or ""
    if not _OID.fullmatch(head) or not _OID.fullmatch(upstream_oid) or not upstream or "\x00" in porcelain:
        raise ValueError("stale_or_missing_upstream")
    return {"head_oid": head.lower(), "upstream": upstream, "upstream_oid": upstream_oid.lower(), "porcelain": porcelain, "status_hash": hashlib.sha256(porcelain.encode()).hexdigest()}


def _local_snapshot(request: Request) -> Result:
    data, cfg = input_of(request), cfg_of(request)
    clone = str(data.get("clone_path") or cfg.get("clone_path") or "").strip()
    if not clone:
        return _bad("missing_clone_path")
    try:
        snap = _snapshot(clone)
        return ok(status="snapshot_read", clone_path=clone, snapshot=snap, **snap)
    except (CommandError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
        return _bad("local_snapshot_failed" if not isinstance(exc, ValueError) else str(exc), error=str(exc), clone_path=clone)


def _validate_context_paths(paths: Sequence[str], *, clone_path: str | Path | None = None) -> tuple[str, ...]:
    """Validate repository-relative paths before any object is read."""
    if not isinstance(paths, (list, tuple)):
        raise ValueError("invalid_context_paths")
    seen: set[str] = set(); output: list[str] = []
    root = Path(clone_path).resolve() if clone_path else None
    for raw in paths:
        if not isinstance(raw, str) or not raw or "\\" in raw:
            raise ValueError("invalid_context_path")
        path = PurePosixPath(raw)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in raw.split("/")):
            raise ValueError("invalid_context_path")
        normalized = str(path)
        if normalized in seen:
            raise ValueError("duplicate_context_path")
        if root is not None and not (root / normalized).is_file():
            raise ValueError("context_path_not_file")
        seen.add(normalized); output.append(normalized)
    return tuple(output)


def _committed_context(request: Request, head: str) -> Result:
    data, cfg = input_of(request), cfg_of(request)
    clone = str(data.get("clone_path") or cfg.get("clone_path") or "").strip()
    paths = data.get("context_paths", cfg.get("context_paths", ()))
    cap = data.get("context_max_bytes", cfg.get("context_max_bytes", 131072))
    try:
        cap = int(cap)
        if isinstance(cap, bool) or cap < 1:
            raise ValueError("invalid_context_cap")
        valid = _validate_context_paths(paths, clone_path=clone or None)
    except (TypeError, ValueError) as exc:
        return _bad(str(exc), clone_path=clone)
    if not clone or not valid:
        return _bad("missing_context")
    if not _OID.fullmatch(head):
        return _bad("missing_head_oid")
    files: list[dict[str, Any]] = []; total = 0
    try:
        for path in valid:
            # The explicit HEAD:path form is intentional: never read dirty bytes.
            proc = run_cmd(["git", "show", f"HEAD:{path}"], cwd=clone, timeout=60)
            content = proc.stdout or ""
            encoded = content.encode("utf-8"); total += len(encoded)
            if total > cap:
                return _bad("context_oversized", context_max_bytes=cap, total_bytes=total)
            files.append({"path": path, "sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded), "content": content})
    except (CommandError, subprocess.TimeoutExpired, OSError) as exc:
        return _bad("committed_context_read_failed", error=str(exc), head_oid=head)
    return ok(status="context_read", head_oid=head, context=files, context_paths=list(valid), total_bytes=total, context_max_bytes=cap)


def build_triage_context(request: Request) -> Result:
    """Pin remote/local OIDs, extract committed context, and prove no change."""
    gate = _gate(request, "build_triage_context", "read_triage_repository_state", "select_triage_candidate", "reserve_triage_run_budget")
    if gate:
        return gate
    data, cfg = input_of(request), cfg_of(request)
    ident = _identity(request, "read_triage_repository_state", "select_triage_candidate", "reserve_triage_run_budget")
    values = {**data, "selected": ident["selected"], "repo": ident["repo"] or data.get("repo"), "number": ident["number"] or data.get("number"), "clone_path": ident["clone_path"] or data.get("clone_path")}
    pre = _local_snapshot({"input": values, "config": cfg})
    if not pre.get("ok"):
        return pre
    remote = read_triage_repository_state({"input": values, "config": cfg})
    if not remote.get("ok"):
        return remote
    if pre.get("head_oid") != remote.get("default_branch_oid") or pre.get("upstream_oid") != remote.get("default_branch_oid"):
        return _bad("stale_repository_context", **ident)
    context = _committed_context({"input": {**values, "head_oid": pre["head_oid"]}, "config": cfg}, pre["head_oid"])
    if not context.get("ok"):
        return context
    post = _local_snapshot({"input": values, "config": cfg})
    if not post.get("ok"):
        return post
    if post.get("snapshot") != pre.get("snapshot"):
        return _bad("repository_changed", pre_snapshot=pre.get("snapshot"), post_snapshot=post.get("snapshot"), **ident)
    return ok(status="context_packet", packet={"repo": ident["repo"], "head_oid": pre["head_oid"], "default_branch_oid": remote["default_branch_oid"], "upstream": pre["upstream"], "status_hash": pre["status_hash"], "context": context["context"], "context_paths": context["context_paths"], "total_bytes": context["total_bytes"]}, pre_snapshot=pre["snapshot"], post_snapshot=post["snapshot"], **ident)

def verify_triage_repository_unchanged(request: Request) -> Result:
    gate = _gate(request, "verify_triage_repository_unchanged", "build_triage_context", "select_triage_candidate", "reserve_triage_run_budget")
    if gate and "pre_snapshot" not in input_of(request) and "snapshot" not in input_of(request):
        return gate
    data = input_of(request)
    ident = _identity(request, "build_triage_context", "select_triage_candidate", "reserve_triage_run_budget")
    expected = data.get("pre_snapshot") or data.get("snapshot") or cond_get(request, "pre_snapshot", "build_triage_context", default=None)
    if not isinstance(expected, Mapping) or not expected:
        return _bad("missing_pre_snapshot", **ident)
    current = _local_snapshot({"input": {**data, **ident}, "config": cfg_of(request)})
    if not current.get("ok"):
        return current
    actual = current.get("snapshot")
    if not isinstance(actual, Mapping) or dict(actual) != dict(expected):
        return _bad("repository_changed", expected=dict(expected), actual=actual, **ident)
    return ok(status="snapshot_unchanged", snapshot=dict(actual), mutated=False, **ident)
