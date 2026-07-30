from __future__ import annotations

import json
import fcntl
from contextlib import contextmanager

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lokay.envelope import Request, Result

from lokay.adapters_cli import CommandError, run_cmd
from lokay.envelope import cfg_of, cond_blob, dry_run_flag, fail, input_of, noop, ok, planned, terminal_upstream, upstream_noop

def _claim_file(configured: str) -> Path | None:
    value = str(configured or "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    if path.exists() and path.is_dir():
        return path / "claim.json"
    return path if path.suffix.lower() == ".json" else path / "claim.json"
def _claim_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _claim_identity(payload: Any) -> tuple[str, int, str, str] | None:
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return None
    repo = _claim_text(payload, "repo")
    board = _claim_text(payload, "board")
    assignee = _claim_text(payload, "assignee")
    claimed_at = _claim_text(payload, "claimedAt")
    issue = payload.get("issue")
    if not repo or not board or not assignee or not claimed_at or isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0:
        return None
    return repo, issue, board, assignee


def _claims_in_directory(path: Path) -> tuple[list[tuple[Path, dict[str, Any]]], str | None]:
    if not path.exists() or not path.is_dir():
        return [], None
    claims: list[tuple[Path, dict[str, Any]]] = []
    try:
        entries = sorted(path.glob("*.json"))
        for entry in entries:
            payload, error = _read_claim(entry)
            if error:
                return [], error
            if payload is not None:
                claims.append((entry, payload))
    except OSError as exc:
        return [], f"claim_malformed:{exc}"
    return claims, None





def _read_claim(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, None
    try:
        claim = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"claim_malformed:{exc}"
    if _claim_identity(claim) is None:
        return None, "claim_malformed:invalid_identity"
    return dict(claim), None

@contextmanager
def claim_directory_lock(path: Path):
    """Serialize claim creation and deletion in the claim directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    directory_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(directory_fd, fcntl.LOCK_UN)
        os.close(directory_fd)


def _reserve_claim(path: Path, *, repo: str, issue: int, board: str, assignee: str) -> tuple[dict[str, Any] | None, str | None, bool]:
    payload = {
        "version": 1,
        "repo": repo,
        "issue": issue,
        "board": board,
        "assignee": assignee,
        "claimedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        parent_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except FileExistsError:
        existing, error = _read_claim(path)
        if error:
            return None, error, False
        assert existing is not None
        identity = (repo, issue, board, assignee)
        return (existing, None, True) if _claim_identity(existing) == identity else (existing, "claim_busy", False)
    except OSError as exc:
        if path.exists():
            return payload, f"claim_uncertain:{exc}", False
        return None, f"claim_create_failed:{exc}", False
    existing, error = _read_claim(path)
    if error or existing is None:
        return payload, error or "claim_malformed:empty_readback", False
    if _claim_identity(existing) != (repo, issue, board, assignee):
        return existing, "claim_readback_mismatch", False
    return existing, None, False



def reserve_claim_file(request: Request) -> Result:
    """Reserve the local claim file without touching GitHub."""
    from lokay.envelope import terminal_upstream
    terminal = terminal_upstream(request, "reserve_claim_file", "select_issue_candidate", "decide_issue_priority", "decide_issue_action")
    if terminal:
        return terminal
    priority = upstream_noop(request, "decide_issue_priority", "decide_issue_action")
    if priority:
        return noop(
            str(priority.get("reason") or "pr_priority_repair_required"),
            dry_run=dry_run_flag(request),
            selected=priority.get("selected"),
            action="skip",
            priority_action=priority.get("priority_action") or priority.get("action"),
        )
    data, cfg = input_of(request), cfg_of(request)
    decision = cond_blob(request, "decide_issue_action")
    if str(decision.get("action") or "") == "reject_comment":
        return noop("no_selected_issue", dry_run=dry_run_flag(request), selected=None, action="skip")
    selected = data.get("selected") or cond_blob(request, "select_issue_candidate", "decide_issue_action").get("selected")
    dry = dry_run_flag(request)
    if not selected:
        return noop("no_selected_issue", dry_run=dry, selected=None)
    if not isinstance(selected, dict):
        return fail("invalid_selected_issue", failure_class="terminal", retry_safe=False, selected=selected)
    repo, board, number = selected.get("repo"), selected.get("board"), selected.get("number", selected.get("issue"))
    assignee = str(data.get("assignee") or cfg.get("assignee") or "mikolaj92")
    path_value = data.get("active_issue_path") or cfg.get("active_issue_path") or (cfg.get("paths") or {}).get("active_issue")
    if not isinstance(repo, str) or not repo.strip() or isinstance(number, bool) or not isinstance(number, int) or number <= 0 or (not dry and (not isinstance(board, str) or not board.strip())):
        return fail("invalid_selected_issue", failure_class="terminal", retry_safe=False, selected=selected)
    if dry:
        return planned(selected=selected, planned={"repo": repo, "issue": number, "board": str(board or ""), "assignee": assignee})
    max_active = cfg.get("max_active_issues", 1)
    if isinstance(max_active, bool) or not isinstance(max_active, int) or max_active < 1:
        return fail("invalid_max_active_issues", failure_class="terminal", retry_safe=False, selected=selected)
    configured = __import__("pathlib").Path(str(path_value or "")).expanduser() if path_value else None
    path = _claim_file(str(path_value or ""))
    if path is None:
        return fail("missing_claim_path", failure_class="terminal", retry_safe=False, selected=selected)
    is_directory = configured is not None and ((configured.exists() and configured.is_dir()) or configured.suffix.lower() != ".json")
    try:
        with claim_directory_lock(path):
            claims, scan_error = _claims_in_directory(path.parent) if is_directory else ([], None)
            if scan_error:
                return fail(scan_error.split(":", 1)[0], failure_class="terminal", retry_safe=False, selected=selected, claim_path=str(path), error=scan_error)
            identity = (repo.strip(), number, str(board).strip(), assignee)
            match = next(((claim_path, claim) for claim_path, claim in claims if _claim_identity(claim) == identity), None)
            if match:
                return ok(status="claim_reserved", claim=match[1], claim_path=str(match[0]), reused=True, mutated=False, selected=selected)
            if is_directory and len(claims) >= max_active:
                return noop("claim_busy", selected=selected, claim_path=str(path), active_claims=[claim for _, claim in claims], max_active_issues=max_active)
            claim, error, reused = _reserve_claim(path, repo=repo.strip(), issue=number, board=str(board).strip(), assignee=assignee)
    except OSError as exc:
        return fail("claim_create_failed", failure_class="terminal", retry_safe=False, error=str(exc), mutated=False, selected=selected, claim_path=str(path))
    if error:
        return fail("claim_busy" if error == "claim_busy" else error.split(":", 1)[0], failure_class="reconcile_then_retry" if error.startswith("claim_uncertain") else "terminal", retry_safe=False, error=error, claim=claim, claim_path=str(path), mutated=not reused, selected=selected)
    return ok(status="claim_reserved", claim=claim, claim_path=str(path), reused=reused, mutated=not reused, selected=selected)


def read_issue_claim_state(request: Request) -> Result:
    """Read authoritative GitHub assignee/label state."""
    from lokay.envelope import cond_blob, terminal_upstream
    terminal=terminal_upstream(request,"read_issue_claim_state","reserve_claim_file")
    if terminal: return terminal
    idle = upstream_noop(request, "reserve_claim_file", "select_issue_candidate", "decide_issue_action")
    if idle:
        return noop(str(idle.get("reason") or "no_selected_issue"), operation="read_issue_claim_state")
    data=input_of(request); cfg=cfg_of(request); reserve=cond_blob(request,"reserve_claim_file"); selected=data.get("selected") or reserve.get("selected") or {}; repo=str(data.get("repo") or selected.get("repo") or ""); number=data.get("number") or selected.get("number") or 0
    if not repo or isinstance(number,bool) or not isinstance(number,int) or number<=0: return fail("invalid_selected_issue",failure_class="terminal",retry_safe=False,selected=selected)
    try: proc=run_cmd([str(cfg.get("gh_cli") or "gh"),"issue","view",str(number),"--repo",repo,"--json","assignees,labels"],timeout=60); current=json.loads((proc.stdout or "").strip())
    except CommandError as exc: return fail("claim_read_failed",failure_class="retryable_read",retry_safe=True,error=str(exc),mutated=False,repo=repo,number=number)
    except (subprocess.TimeoutExpired,json.JSONDecodeError,TypeError,ValueError) as exc: return fail("claim_read_failed",failure_class="terminal",retry_safe=False,error=str(exc),mutated=False,repo=repo,number=number)
    if not isinstance(current,dict) or not isinstance(current.get("assignees"),list) or not isinstance(current.get("labels"),list): return fail("claim_read_failed",failure_class="terminal",retry_safe=False,error="invalid claim read-back shape",mutated=False,repo=repo,number=number)
    assignees={str(x.get("login") or "").strip() if isinstance(x,dict) else str(x).strip() for x in current["assignees"]}; labels={str(x.get("name") or "").strip() if isinstance(x,dict) else str(x).strip() for x in current["labels"]}
    if "" in assignees or "" in labels: return fail("claim_read_failed",failure_class="terminal",retry_safe=False,error="blank claim read-back item",mutated=False,repo=repo,number=number)
    return ok(status="claim_state_read",assignees=sorted(assignees),labels=sorted(labels),repo=repo,number=number,selected=selected,dry_run=dry_run_flag(request))


def assign_issue(request: Request) -> Result:
    """Assign one issue on GitHub."""
    from lokay.envelope import cond_blob, terminal_upstream
    terminal=terminal_upstream(request,"assign_issue","read_issue_claim_state","reserve_claim_file")
    if terminal: return terminal
    idle = upstream_noop(request, "read_issue_claim_state", "reserve_claim_file")
    if idle:
        return noop(str(idle.get("reason") or "no_selected_issue"), operation="assign_issue")
    data=input_of(request); cfg=cfg_of(request); state=cond_blob(request,"read_issue_claim_state"); selected=data.get("selected") or state.get("selected") or {}; repo=str(data.get("repo") or state.get("repo") or ""); number=data.get("number") or state.get("number") or 0; assignee=str(data.get("assignee") or cfg.get("assignee") or "mikolaj92"); current=set(state.get("assignees") or [])
    if assignee in current: return ok(status="issue_assigned",mutated=False,reused=True,repo=repo,number=number,assignee=assignee,dry_run=dry_run_flag(request))
    if dry_run_flag(request): return planned(repo=repo,number=number,assignee=assignee)
    try: run_cmd([str(cfg.get("gh_cli") or "gh"),"issue","edit",str(number),"--repo",repo,"--add-assignee",assignee],timeout=60)
    except (CommandError,subprocess.TimeoutExpired) as exc: return fail("assign_issue_failed",failure_class="reconcile_then_retry",retry_safe=False,error=str(exc),mutated=True,repo=repo,number=number,assignee=assignee)
    return ok(status="issue_assigned",mutated=True,repo=repo,number=number,assignee=assignee)


def add_issue_label(request: Request) -> Result:
    """Add exactly one configured label to the issue, provisioning it if needed."""
    from lokay.envelope import cond_blob, terminal_upstream
    terminal=terminal_upstream(request,"add_issue_label","read_issue_claim_state","reserve_claim_file")
    if terminal: return terminal
    idle = upstream_noop(request, "read_issue_claim_state", "reserve_claim_file", "assign_issue")
    if idle:
        return noop(str(idle.get("reason") or "no_selected_issue"), operation="add_issue_label")
    data=input_of(request); cfg=cfg_of(request); state=cond_blob(request,"read_issue_claim_state"); repo=str(data.get("repo") or state.get("repo") or ""); number=data.get("number") or state.get("number") or 0; label=str(data.get("label") or cfg.get("label") or cfg.get("in_progress_label") or ""); labels=set(state.get("labels") or []); gh=str(cfg.get("gh_cli") or "gh")
    if not label: return fail("missing_label",failure_class="terminal",retry_safe=False,repo=repo,number=number)
    if label in labels: return ok(status="issue_label_added",mutated=False,reused=True,repo=repo,number=number,label=label,dry_run=dry_run_flag(request))
    if dry_run_flag(request): return planned(repo=repo,number=number,label=label)
    provisioned=False
    try:
        listed=run_cmd([gh,"label","list","--repo",repo,"--limit","200","--json","name"],timeout=60)
        available=json.loads((listed.stdout or "").strip() or "[]")
        if not isinstance(available,list):
            raise ValueError("invalid label list")
        matches=[item for item in available if isinstance(item,dict) and str(item.get("name") or "").casefold()==label.casefold()]
        if len(matches)>1:
            return fail("ambiguous_configured_label",failure_class="terminal",retry_safe=False,repo=repo,number=number,label=label)
        if not matches:
            color=str(data.get("label_color") or cfg.get("label_color") or "FBCA04")
            description=str(data.get("label_description") or cfg.get("label_description") or "Issue currently being handled by repo-agent")
            create=[gh,"label","create",label,"--repo",repo,"--color",color]
            if description:
                create.extend(["--description",description])
            try:
                run_cmd(create,timeout=60)
                provisioned=True
            except CommandError as exc:
                # Concurrent create races resolve by re-listing.
                if "already exists" not in str(exc).casefold():
                    raise
            after=run_cmd([gh,"label","list","--repo",repo,"--limit","200","--json","name"],timeout=60)
            available=json.loads((after.stdout or "").strip() or "[]")
            matches=[item for item in available if isinstance(item,dict) and str(item.get("name") or "").casefold()==label.casefold()]
            if len(matches)!=1:
                return fail("label_provision_readback_mismatch",failure_class="reconcile_then_retry",retry_safe=False,mutated=provisioned,repo=repo,number=number,label=label)
            label=str(matches[0].get("name") or label)
        else:
            label=str(matches[0].get("name") or label)
        run_cmd([gh,"issue","edit",str(number),"--repo",repo,"--add-label",label],timeout=60)
    except (CommandError,subprocess.TimeoutExpired) as exc:
        return fail("add_issue_label_failed",failure_class="reconcile_then_retry",retry_safe=False,error=str(exc),mutated=True,repo=repo,number=number,label=label,provisioned=provisioned)
    except (TypeError,ValueError,json.JSONDecodeError) as exc:
        return fail("add_issue_label_failed",failure_class="terminal",retry_safe=False,error=str(exc),mutated=provisioned,repo=repo,number=number,label=label,provisioned=provisioned)
    return ok(status="issue_label_added",mutated=True,repo=repo,number=number,label=label,provisioned=provisioned)


def verify_issue_claim(request: Request) -> Result:
    """Verify assignee and required labels from a fresh read."""
    from lokay.envelope import cond_blob, terminal_upstream
    terminal=terminal_upstream(request,"verify_issue_claim","assign_issue","intake_add_issue_label")
    if terminal: return terminal
    idle = upstream_noop(request, "read_issue_claim_state", "reserve_claim_file", "assign_issue", "intake_add_issue_label")
    if idle:
        return noop(str(idle.get("reason") or "no_selected_issue"), operation="verify_issue_claim")
    data=input_of(request); cfg=cfg_of(request); state=cond_blob(request,"read_issue_claim_state"); assign=cond_blob(request,"assign_issue"); labels=cond_blob(request,"intake_add_issue_label"); selected=data.get("selected") or state.get("selected") or {}; repo=str(data.get("repo") or state.get("repo") or selected.get("repo") or ""); number=data.get("number") or state.get("number") or selected.get("number") or selected.get("issue") or 0; assignee=str(data.get("assignee") or assign.get("assignee") or cfg.get("assignee") or "mikolaj92"); required_label=str(data.get("label") or cfg.get("label") or cfg.get("in_progress_label") or labels.get("label") or ""); required=set(data.get("required_labels") or labels.get("required_labels") or ([required_label] if required_label else [])); mutated=bool(assign.get("mutated") or labels.get("mutated"))
    if dry_run_flag(request): return ok(status="claim_verified",verified=False,mutated=False,dry_run=True,assignee=assignee,required_labels=sorted(required),repo=repo,number=number)
    if not repo or isinstance(number,bool) or not isinstance(number,int) or number<=0:
        return fail("invalid_selected_issue",failure_class="terminal",retry_safe=False,selected=selected)
    if not required:
        return fail("missing_required_labels",failure_class="terminal",retry_safe=False,repo=repo,number=number,assignee=assignee)
    try:
        proc=run_cmd([str(cfg.get("gh_cli") or "gh"),"issue","view",str(number),"--repo",repo,"--json","assignees,labels"],timeout=60)
        current=json.loads((proc.stdout or "").strip())
    except CommandError as exc:
        return fail("claim_read_failed",failure_class="retryable_read",retry_safe=True,error=str(exc),mutated=mutated,repo=repo,number=number)
    except (subprocess.TimeoutExpired,json.JSONDecodeError,TypeError,ValueError) as exc:
        return fail("claim_read_failed",failure_class="terminal",retry_safe=False,error=str(exc),mutated=mutated,repo=repo,number=number)
    if not isinstance(current,dict) or not isinstance(current.get("assignees"),list) or not isinstance(current.get("labels"),list):
        return fail("claim_read_failed",failure_class="terminal",retry_safe=False,error="invalid claim read-back shape",mutated=mutated,repo=repo,number=number)
    actual_a={str(x.get("login") or "").strip() if isinstance(x,dict) else str(x).strip() for x in current["assignees"]}
    actual_l={str(x.get("name") or "").strip() if isinstance(x,dict) else str(x).strip() for x in current["labels"]}
    if "" in actual_a or "" in actual_l:
        return fail("claim_read_failed",failure_class="terminal",retry_safe=False,error="blank claim read-back item",mutated=mutated,repo=repo,number=number)
    if assignee not in actual_a or not required.issubset(actual_l):
        return fail("claim_readback_mismatch",failure_class="reconcile_then_retry" if mutated else "terminal",retry_safe=False,mutated=mutated,assignee=assignee,assignees=sorted(actual_a),required_labels=sorted(required),labels=sorted(actual_l),repo=repo,number=number)
    return ok(status="claim_verified",verified=True,mutated=mutated,assignee=assignee,required_labels=sorted(required),assignees=sorted(actual_a),labels=sorted(actual_l),repo=repo,number=number)


def build_issue_claim_result(request: Request) -> Result:
    """Purely aggregate claim reservation, mutations, and verification."""
    from lokay.envelope import cond_blob, terminal_upstream
    terminal=terminal_upstream(request,"build_issue_claim_result","verify_issue_claim")
    if terminal: return terminal
    data=input_of(request); verify=cond_blob(request,"verify_issue_claim"); reserve=cond_blob(request,"reserve_claim_file");
    if verify.get("status")=="noop": return noop(str(verify.get("reason") or "no_selected_issue"),dry_run=dry_run_flag(request))
    if verify.get("ok") is not True:
        return fail("claim_failed",failure_class=str(verify.get("failure_class") or reserve.get("failure_class") or "terminal"),retry_safe=False,verify=verify,reserve=reserve,reserve_reason=reserve.get("reason"),mutated=bool(verify.get("mutated") or reserve.get("mutated")))
    return ok(status="claimed",selected=data.get("selected") or reserve.get("selected"),claim=reserve.get("claim"),claim_path=reserve.get("claim_path"),verified=verify.get("verified",False),mutated=bool(reserve.get("mutated") or verify.get("mutated")),dry_run=dry_run_flag(request))
