"""Durable immutable receipts for pre-intake issue triage."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any

from lokay.envelope import Request, Result, fail, noop, ok, planned, cfg_of, input_of, cond_blob, cond_get, dry_run_flag, terminal_upstream
from lokay.steps.issue_triage import triage_gate, triage_identity, triage_selected

_SCHEMA_VERSION = 1
_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
_STAGES = frozenset({"decision", "feedback-verified", "feedback-observed", "mutation-authorized", "mutation-verified", "close-authorized", "close-verified"})


def _component(value: Any, field: str) -> str:
    text = value if isinstance(value, str) else ""
    if not text or text in {".", ".."} or not _COMPONENT.fullmatch(text):
        raise ValueError(f"unsafe_{field}")
    return text


def _repo(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    if not _REPO.fullmatch(text):
        raise ValueError("unsafe_repo")
    return text


def _issue(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("invalid_issue")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.isdigit():
        number = int(value)
    else:
        raise ValueError("invalid_issue")
    if number <= 0:
        raise ValueError("invalid_issue")
    return number


def _root(value: Any) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("unsafe_receipt_root")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = path.resolve()
    if path == Path(path.anchor):
        raise ValueError("unsafe_receipt_root")
    return path

def _receipt_root(data: Mapping[str, Any], cfg: Mapping[str, Any]) -> Any:
    paths = cfg.get("paths")
    nested = paths.get("triage_receipts") if isinstance(paths, Mapping) else None
    return data.get("triage_receipts") or data.get("receipt_root") or cfg.get("triage_receipts") or cfg.get("receipt_root") or nested


def _issue_dir(root: Any, repo: Any, issue: Any) -> Path:
    owner, name = _repo(repo).split("/", 1)
    return _root(root) / f"{owner}__{name}" / str(_issue(issue))


def _run_budget_path(root: Any, run_id: Any) -> Path:
    return _root(root) / "run-budgets" / f"{_component(run_id, 'run_id')}.json"


def _receipt_path(root: Any, repo: Any, issue: Any, stage: Any, identity: Any) -> Path:
    if stage not in _STAGES:
        raise ValueError("unknown_receipt_stage")
    raw_identity = str(identity or "")
    if stage in {"decision", "feedback-observed"}:
        if not raw_identity:
            raise ValueError("unsafe_receipt_identity")
        identity = hashlib.sha256(raw_identity.encode()).hexdigest()
    else:
        identity = _component(raw_identity, "receipt_identity")
        if stage == "feedback-verified":
            if not identity.isdigit() or int(identity) <= 0:
                raise ValueError("invalid_comment_id")
        elif not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise ValueError("invalid_decision_digest")
    return _issue_dir(root, repo, issue) / f"{stage}-{identity}.json"

def _identity(data: Mapping[str, Any], cfg: Mapping[str, Any], request: Request | None = None, *upstream_ids: str) -> tuple[str, str, int]:
    selected: Mapping[str, Any] = {}
    if request is not None:
        selected = triage_selected(request, *upstream_ids)
    repo = data.get("repo") or selected.get("repo") or cfg.get("repo")
    issue = data.get("issue") if data.get("issue") is not None else data.get("number")
    if issue is None:
        issue = selected.get("issue") if selected.get("issue") is not None else selected.get("number")
    if issue is None:
        issue = cfg.get("issue") if cfg.get("issue") is not None else cfg.get("number")
    return _repo(repo), str(issue), _issue(issue)


_RECEIPT_UPSTREAMS = (
    "select_triage_candidate", "reserve_triage_run_budget", "classify_triage_issue",
    "build_triage_context", "verify_triage_repository_unchanged", "decide_triage_mutation",
    "mutate_triage_issue_labels", "verify_triage_feedback", "observe_triage_feedback",
    "close_triage_issue", "verify_triage_issue_closed", "publish_triage_decision_receipt",
    "publish_triage_mutation_authorization", "publish_triage_mutation_verification",
    "publish_triage_feedback_receipt", "publish_triage_close_authorization",
    "publish_triage_close_verification",
)


def _receipt_upstream(request: Request, stage: str) -> tuple[str, ...]:
    specific = {
        "decision": ("classify_triage_issue", "decide_triage_action"),
        "mutation-authorized": ("decide_triage_mutation",),
        "mutation-verified": ("mutate_triage_issue_labels", "verify_triage_feedback"),
        "feedback-verified": ("verify_triage_feedback", "observe_triage_feedback"),
        "close-authorized": ("decide_triage_mutation", "close_triage_issue"),
        "close-verified": ("verify_triage_issue_closed",),
    }
    return tuple(dict.fromkeys((*specific.get(stage, ()), *_RECEIPT_UPSTREAMS)))


def _selected_gate(request: Request, operation: str, *upstream_ids: str) -> Result | None:
    gate = triage_gate(request, operation, *upstream_ids)
    if gate is not None:
        return gate
    selected = triage_selected(request, *upstream_ids)
    data, cfg = input_of(request), cfg_of(request)
    if not selected:
        explicit_repo = data.get("repo") or cfg.get("repo")
        explicit_issue = data.get("issue") if data.get("issue") is not None else data.get("number", cfg.get("issue"))
        if explicit_repo and explicit_issue not in (None, "", 0):
            return None
        return noop("triage_candidate_missing", selected=None, operation=operation)
    return None

def _conduction_payload(request: Request, stage: str) -> Mapping[str, Any] | None:
    names = _receipt_upstream(request, stage)
    for name in names:
        blob = cond_blob(request, name)
        candidate = blob.get("payload") if isinstance(blob.get("payload"), Mapping) else blob
        if isinstance(candidate, Mapping) and candidate:
            return candidate
    return None

@contextmanager
def _receipt_directory_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _private_stat(path: Path) -> os.stat_result | None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_nlink != 1:
        raise ValueError("receipt_not_private_regular_single_link")
    return metadata


def _fsync_dir(directory: Path) -> None:
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read(path: Path) -> dict[str, Any]:
    if _private_stat(path) is None:
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("receipt_not_object")
    return value


def _safe_existing_path(path: Path, root: Any) -> Path:
    base = _root(root)
    candidate = path if path.is_absolute() else path.resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError("receipt_path_escape") from exc
    return candidate


def _publish(path: Path, payload: Mapping[str, Any], operation: str = "receipt") -> Result:
    value = dict(payload)
    with _receipt_directory_lock(path.parent):
        try:
            current = _read(path)
        except FileNotFoundError:
            current = None
        except (OSError, ValueError, json.JSONDecodeError, UnicodeError) as exc:
            return fail(f"{operation}_conflict", error=str(exc), receipt_path=str(path))
        if current is not None:
            if current == value:
                try:
                    _fsync_dir(path.parent)
                except OSError as exc:
                    return fail(f"{operation}_durability_unconfirmed", error=str(exc), receipt_path=str(path))
                return ok(status="exists", receipt_path=str(path), payload=value)
            return fail(f"{operation}_conflict", receipt_path=str(path))
        temp: Path | None = None
        linked = False
        try:
            fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
            temp = Path(name)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.link(str(temp), str(path))
            linked = True
            os.unlink(str(temp))
            temp = None
            _fsync_dir(path.parent)
            if _read(path) != value:
                raise ValueError("receipt_readback_mismatch")
        except (OSError, ValueError, TypeError, json.JSONDecodeError, UnicodeError) as exc:
            rollback_error: Exception | None = None
            if temp is not None:
                try:
                    os.unlink(str(temp))
                except OSError:
                    pass
            if linked:
                try:
                    os.unlink(str(path))
                    _fsync_dir(path.parent)
                except Exception as rollback_exc:
                    rollback_error = rollback_exc
            text = str(exc) if rollback_error is None else f"{exc}; rollback durability unconfirmed: {rollback_error}"
            return fail(f"{operation}_write_failed", error=text, receipt_path=str(path), mutated=linked)
    return ok(status="written", receipt_path=str(path), payload=value, mutated=True)


def _payload(request: Request, stage: str) -> dict[str, Any]:
    data, cfg = input_of(request), cfg_of(request)
    candidate = _conduction_payload(request, stage)
    if not isinstance(candidate, Mapping):
        candidate = data.get("payload") if isinstance(data.get("payload"), Mapping) else data
    value = dict(candidate)
    selected = triage_selected(request, *_receipt_upstream(request, stage))
    value.setdefault("schema_version", _SCHEMA_VERSION)
    value.setdefault("stage", stage)
    if selected:
        value.setdefault("selected", dict(selected))
    for key in ("repo", "issue", "number", "issue_url", "updated_at", "issue_updated_at", "classification_revision", "candidate_class", "decision_digest", "run_id", "comment_id"):
        if key not in value:
            if key in data:
                value[key] = data[key]
            elif key in selected:
                value[key] = selected[key]
            elif key in cfg:
                value[key] = cfg[key]
    if "issue_updated_at" not in value and isinstance(selected.get("updatedAt"), str):
        value["issue_updated_at"] = selected["updatedAt"]
    if "issue" not in value and "number" in value:
        value["issue"] = value["number"]
    if stage == "feedback-verified":
        verified = cond_blob(request, "verify_triage_feedback")
        observed = cond_blob(request, "observe_triage_feedback")
        for blob in (verified, observed):
            if not isinstance(blob, Mapping) or not blob:
                continue
            if "comment_id" not in value and blob.get("comment_id") not in (None, ""):
                value["comment_id"] = blob.get("comment_id")
            if "decision_digest" not in value and blob.get("decision_digest"):
                value["decision_digest"] = blob.get("decision_digest")
            if "issue_updated_at" not in value and isinstance(blob.get("issue_updated_at") or blob.get("updated_at") or blob.get("updatedAt"), str):
                value["issue_updated_at"] = blob.get("issue_updated_at") or blob.get("updated_at") or blob.get("updatedAt")
            if "verified_readback_state" not in value and blob.get("verified_readback_state"):
                value["verified_readback_state"] = blob.get("verified_readback_state")
            elif "verified_readback_state" not in value and blob.get("verified") is True:
                value["verified_readback_state"] = "verified"
        comment = verified.get("comment") if isinstance(verified.get("comment"), Mapping) else None
        if comment is not None and "comment_id" not in value:
            database_id = comment.get("databaseId")
            if isinstance(database_id, int) and not isinstance(database_id, bool) and database_id > 0:
                value["comment_id"] = database_id
            else:
                url = str(comment.get("url") or "")
                match = re.search(r"issuecomment-(\d+)", url)
                if match:
                    value["comment_id"] = int(match.group(1))
    return value


def _stage_action(request: Request) -> str:
    data = input_of(request)
    raw = data.get("action")
    if raw not in (None, ""):
        return str(raw).strip().casefold()
    decision = cond_blob(request, "decide_triage_mutation")
    raw = decision.get("action") if isinstance(decision, Mapping) else None
    return str(raw or "").strip().casefold()


def _stage_request(request: Request, stage: str) -> Result:
    operation = f"publish_triage_{stage.replace('-', '_')}_receipt"
    gate = _selected_gate(request, operation, *_receipt_upstream(request, stage))
    if gate is not None:
        return gate
    terminal = terminal_upstream(request, operation, *_receipt_upstream(request, stage))
    if terminal:
        return terminal
    action = _stage_action(request)
    if stage in {"close-authorized", "close-verified"} and action and action != "close":
        return noop("action_not_selected", action=action, operation=operation)
    if stage == "mutation-authorized" and action and action not in {"add_ready", "remove_ready", "label", "close"}:
        return noop("action_not_selected", action=action, operation=operation)
    if stage == "mutation-verified":
        # Graph wires labels+feedback verify, not decide. Treat non-label/close
        # actions (including absent action with feedback-only verification) as idle.
        if action and action not in {"add_ready", "remove_ready", "label", "close"}:
            return noop("action_not_selected", action=action, operation=operation)
        if not action:
            labels = cond_blob(request, "mutate_triage_issue_labels")
            feedback = cond_blob(request, "verify_triage_feedback")
            label_idle = labels.get("ok") is True and (
                labels.get("status") in {"labels_verified", "planned"}
                or (labels.get("status") == "noop" and labels.get("reason") in {"action_not_selected", "already_labeled", "ready_absent", "frozen"})
            )
            feedback_done = feedback.get("ok") is True and feedback.get("status") in {"feedback_verified", "planned"}
            if feedback_done and (not labels or label_idle):
                return noop("action_not_selected", action=action or "feedback", operation=operation)
    if stage == "feedback-verified" and action and action != "feedback":
        verified = cond_blob(request, "verify_triage_feedback")
        if not (verified.get("ok") is True and verified.get("status") in {"feedback_verified", "planned"}):
            return noop("action_not_selected", action=action, operation=operation)
    data, cfg = input_of(request), cfg_of(request)
    payload = _payload(request, stage)
    try:
        root = _receipt_root(data, cfg)
        repo, _, issue = _identity({**data, **payload}, cfg, request, *_receipt_upstream(request, stage))
        identity = data.get("identity") or payload.get("updated_at") or payload.get("decision_digest") or payload.get("comment_id")
        if stage == "feedback-verified":
            identity = payload.get("comment_id") or data.get("database_id") or identity
        elif stage in {"mutation-authorized", "mutation-verified", "close-authorized", "close-verified"}:
            identity = payload.get("decision_digest") or identity
        path = _receipt_path(root, repo, issue, stage, identity)
    except (TypeError, ValueError) as exc:
        return fail("receipt_identity_invalid", error=str(exc), operation=operation)
    if dry_run_flag(request):
        return planned(receipt_path=str(path), payload=payload, selected=payload.get("selected"))
    return _publish(path, payload)


def _summary_for_issue(root: Any, repo: str, issue: int) -> dict[str, Any]:
    directory = _issue_dir(root, repo, issue)
    entries: list[dict[str, Any]] = []
    if directory.exists():
        if not directory.is_dir():
            raise ValueError("receipt_directory_not_directory")
        for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
            item = _read(path)
            item["receipt_path"] = str(path)
            entries.append(item)
    return _reduce(entries)


def read_triage_receipt_index(request: Request) -> Result:
    data, cfg = input_of(request), cfg_of(request)
    terminal = terminal_upstream(request, "read_triage_receipt_index", "normalize_issue_rows")
    if terminal:
        return terminal
    enabled = data.get("triage_enabled", cfg.get("triage_enabled", True))
    if enabled is False:
        return noop("triage_disabled", selected=None, index={})
    if enabled is not True:
        return fail("invalid_triage_enabled", failure_class="terminal", retry_safe=False, mutated=False)
    source = cond_blob(request, "normalize_issue_rows")
    rows = data.get("rows") if isinstance(data.get("rows"), list) else source.get("rows")
    selected = triage_selected(request, "select_triage_candidate", "reserve_triage_run_budget")
    try:
        root = _receipt_root(data, cfg)
        if selected:
            repo, _, issue = _identity(data, cfg, request, "select_triage_candidate", "reserve_triage_run_budget")
            keys = [(repo, issue)]
        elif isinstance(rows, list):
            keys = []
            seen: set[tuple[str, int]] = set()
            for row in rows:
                if not isinstance(row, Mapping):
                    raise ValueError("malformed_issue_rows")
                repo = _repo(row.get("repo"))
                issue = _issue(row.get("number", row.get("issue")))
                if (repo, issue) not in seen:
                    seen.add((repo, issue))
                    keys.append((repo, issue))
        else:
            repo, _, issue = _identity(data, cfg)
            keys = [(repo, issue)]
        flat: dict[str, Any] = {}
        all_entries: list[dict[str, Any]] = []
        for repo, issue in keys:
            summary = _summary_for_issue(root, repo, issue)
            flat[f"{repo}#{issue}"] = {key: value for key, value in summary.items() if key != "receipts"}
            all_entries.extend(summary["receipts"])
        if len(keys) == 1:
            summary = _summary_for_issue(root, keys[0][0], keys[0][1])
            result_index = dict(flat)
            result_index.update({key: value for key, value in summary.items() if key != "receipts"})
        else:
            result_index = flat
        return ok(status="read", receipts=all_entries, index=result_index, receipt_index=flat, repo=(keys[0][0] if len(keys) == 1 else None), issue=(keys[0][1] if len(keys) == 1 else None), selected=selected or None)
    except (OSError, TypeError, ValueError, json.JSONDecodeError, UnicodeError) as exc:
        return fail("triage_receipt_index_failed", error=str(exc))


def _reduce(receipts: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    entries = [dict(item) for item in receipts if isinstance(item, Mapping)]
    entries.sort(key=lambda item: str(item.get("receipt_path") or ""))
    pending: list[dict[str, Any]] = []
    terminal_digests: set[str] = set()
    latest_feedback_watermark: str | None = None
    latest_decision_watermark: str | None = None
    decision_digests: set[str] = set()
    for item in entries:
        stage = str(item.get("stage") or "")
        digest = str(item.get("decision_digest") or "")
        attempted = str(item.get("mutation_attempt_state") or item.get("attempt_state") or "").casefold()
        verified = str(item.get("verified_readback_state") or item.get("readback_state") or "").casefold()
        selected = item.get("selected") if isinstance(item.get("selected"), Mapping) else {}
        watermark = item.get("issue_updated_at") or item.get("updated_at") or item.get("issue_watermark") or selected.get("updatedAt")
        if stage == "decision":
            if digest:
                decision_digests.add(digest)
            if watermark is not None and (latest_decision_watermark is None or str(watermark) > latest_decision_watermark):
                latest_decision_watermark = str(watermark)
        if stage in {"mutation-verified", "feedback-verified", "close-verified"} or verified in {"verified", "succeeded", "closed"}:
            if digest:
                terminal_digests.add(digest)
        if stage in {"feedback-observed", "feedback-verified"}:
            if watermark is not None and (latest_feedback_watermark is None or str(watermark) > latest_feedback_watermark):
                latest_feedback_watermark = str(watermark)
        if stage in {"mutation-authorized", "close-authorized"} or attempted in {"planned", "attempted", "uncertain", "started"}:
            if not digest or digest not in terminal_digests:
                pending.append(item)
    pending = [item for item in pending if str(item.get("decision_digest") or "") not in terminal_digests]
    return {
        "receipts": entries,
        "pending": pending,
        "reconcile_pending": bool(pending),
        "feedback_watermark": latest_feedback_watermark,
        "decision_recorded": bool(decision_digests),
        "decision_watermark": latest_decision_watermark,
        "triage_verified": bool(decision_digests.intersection(terminal_digests)),
    }


def reserve_triage_run_budget(request: Request) -> Result:
    gate = _selected_gate(request, "reserve_triage_run_budget", "select_triage_candidate")
    if gate is not None:
        return gate
    terminal = terminal_upstream(request, "reserve_triage_run_budget", "select_triage_candidate")
    if terminal:
        return terminal
    data, cfg = input_of(request), cfg_of(request)
    selected = triage_selected(request, "select_triage_candidate")
    try:
        root = _receipt_root(data, cfg)
        run_id = _component(data.get("run_id") or cond_get(request, "run_id", "select_triage_candidate") or cfg.get("run_id"), "run_id")
        repo, _, issue = _identity(data, cfg, request, "select_triage_candidate")
        path = _run_budget_path(root, run_id)
    except (TypeError, ValueError) as exc:
        return fail("run_budget_identity_invalid", error=str(exc))
    payload = {"schema_version": _SCHEMA_VERSION, "run_id": run_id, "repo": repo, "issue": issue, "selected": dict(selected)}
    if dry_run_flag(request):
        return planned(receipt_path=str(path), **payload)
    result = _publish(path, payload, operation="run_budget")
    if result.get("reason") == "run_budget_conflict":
        return result
    return result | payload | {"selected": dict(selected)}

def publish_triage_decision_receipt(request: Request) -> Result:
    return _stage_request(request, "decision")


def publish_triage_mutation_authorization(request: Request) -> Result:
    return _stage_request(request, "mutation-authorized")


def publish_triage_mutation_verification(request: Request) -> Result:
    return _stage_request(request, "mutation-verified")


def publish_triage_feedback_receipt(request: Request) -> Result:
    return _stage_request(request, "feedback-verified")


def publish_triage_close_authorization(request: Request) -> Result:
    return _stage_request(request, "close-authorized")


def publish_triage_close_verification(request: Request) -> Result:
    return _stage_request(request, "close-verified")


def verify_triage_receipt(request: Request) -> Result:
    gate = _selected_gate(request, "verify_triage_receipt", *_RECEIPT_UPSTREAMS)
    if gate is not None:
        return gate
    terminal = terminal_upstream(request, "verify_triage_receipt", *_RECEIPT_UPSTREAMS)
    if terminal:
        return terminal
    data, cfg = input_of(request), cfg_of(request)
    root = _receipt_root(data, cfg)
    upstream = _conduction_payload(request, str(data.get("stage") or "decision"))
    path_value = data.get("receipt_path") or (upstream or {}).get("receipt_path") or cfg.get("receipt_path")
    try:
        if not path_value:
            stage = str(data.get("stage") or (upstream or {}).get("stage") or "decision")
            payload = _payload(request, stage)
            repo, _, issue = _identity({**data, **payload}, cfg, request, *_RECEIPT_UPSTREAMS)
            identity = data.get("identity") or payload.get("updated_at") or payload.get("decision_digest") or payload.get("comment_id")
            path_value = str(_receipt_path(root, repo, issue, stage, identity))
        path = _safe_existing_path(Path(str(path_value)), root)
    except (TypeError, ValueError) as exc:
        return fail("receipt_identity_invalid", error=str(exc))
    expected = data.get("payload")
    if not isinstance(expected, Mapping) and isinstance(upstream, Mapping):
        expected = upstream
    try:
        actual = _read(path)
        if isinstance(expected, Mapping) and actual != dict(expected):
            return fail("receipt_conflict", receipt_path=str(path))
        return ok(status="verified", receipt_path=str(path), payload=actual)
    except (OSError, ValueError, json.JSONDecodeError, UnicodeError) as exc:
        return fail("receipt_verify_failed", error=str(exc), receipt_path=str(path))


__all__ = [
    "read_triage_receipt_index", "reserve_triage_run_budget", "publish_triage_decision_receipt",
    "publish_triage_mutation_authorization", "publish_triage_mutation_verification", "publish_triage_feedback_receipt",
    "publish_triage_close_authorization", "publish_triage_close_verification", "verify_triage_receipt",
]
