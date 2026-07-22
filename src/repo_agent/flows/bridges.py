"""Thin conduction → flat-input bridges so atomic effectors compose without glue in paths.

Each bridge is itself a mega-atomic python_function effector: it only remaps
upstream conduction into the next atomic's input and delegates.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fala.adapters import EffectorRunRequest, EffectorRunResult

from repo_agent.envelope import cfg_of, conduction_of, cond_blob, dry_run_flag, fail, input_of, ok
from repo_agent.steps import cleanup, issue_to_pr, triage


def _req(input_data: dict[str, Any], config: dict[str, Any], request: EffectorRunRequest | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        input=input_data,
        config=config,
        process_id=request.process_id if request else "bridge",
        impulse_id=request.impulse_id if request else None,
        work_dir=request.work_dir if request else None,
        adapter=request.adapter if request else None,
    )


def _out(result: EffectorRunResult) -> EffectorRunResult:
    return result


def _input_value(request: EffectorRunRequest, key: str, *aliases: str, default: Any = None) -> Any:
    """Resolve explicit request input before any conduction/config fallback."""
    data = input_of(request)
    for name in (key, *aliases):
        if name in data and data[name] not in (None, "", [], {}):
            return data[name]
    return default


def _upstream_blob(request: EffectorRunRequest, canonical: str, *aliases: str) -> dict[str, Any]:
    """Resolve explicit payload, then canonical conduction ID before aliases."""
    data = input_of(request)
    for name in (canonical, *aliases):
        value = data.get(name)
        if isinstance(value, dict) and value:
            return dict(value)
    return cond_blob(request, canonical, *aliases)


def _failed(result: EffectorRunResult | None) -> bool:
    if result is None:
        return False
    output = result.output or {}
    return output.get("status") in {"failed", "cancelled", "timed_out"} or output.get("ok") is False

def _noop(result: EffectorRunResult | None) -> bool:
    return bool(result and (result.output or {}).get("status") == "noop")


def _child_stop(result: EffectorRunResult, upstream_id: str) -> EffectorRunResult:
    output = result.output or {}
    return EffectorRunResult(
        output={
            **output,
            "ok": True,
            "status": "noop",
            "reason": str(output.get("reason") or "upstream_noop"),
            "stopped_by": upstream_id,
            "upstream": output,
            "mutated": bool(output.get("mutated", False)),
            "dry_run": bool(output.get("dry_run", False)),
        }
    )


def _child_failure(result: EffectorRunResult, upstream_id: str) -> EffectorRunResult:
    output = result.output or {}
    return EffectorRunResult(
        output={
            **output,
            "ok": False,
            "status": "failed",
            "reason": str(output.get("reason") or "upstream_failed"),
            "stopped_by": upstream_id,
            "upstream": output,
            "mutated": bool(output.get("mutated", False)),
            "failure_class": str(output.get("failure_class") or "terminal"),
            "retry_safe": bool(output.get("retry_safe", False)),
        }
    )


def _issue_to_pr_gate(request: EffectorRunRequest, *upstream_ids: str) -> EffectorRunResult | None:
    """Stop issue-to-PR bridges on failed/no-op upstream conduction."""
    cond = conduction_of(request)
    input_data = request.input or {}
    for upstream_id in upstream_ids:
        blob = cond.get(upstream_id)
        if not isinstance(blob, dict):
            blob = input_data.get(upstream_id)
        if not isinstance(blob, dict):
            continue
        child = EffectorRunResult(output=dict(blob))
        if _failed(child):
            return _child_failure(child, upstream_id)
        if _noop(child):
            return _child_stop(child, upstream_id)
    return None


def prepare_worktree_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    blocked = _issue_to_pr_gate(request, "load_kanban_task", "parse_issue_ref")
    if blocked:
        return blocked
    cfg = cfg_of(request)
    parse = cond_blob(request, "parse_issue_ref", "parse_ref")
    branch = str(request.input.get("branch") or parse.get("branch") or "")
    clone_path = str(request.input.get("clone_path") or cfg.get("clone_path") or "")
    worktree_root = str(request.input.get("worktree_root") or cfg.get("worktree_root") or "")
    base_branch = str(request.input.get("base_branch") or cfg.get("base_branch") or "main")
    return _out(issue_to_pr.prepare_worktree(_req({"clone_path": clone_path, "branch": branch, "worktree_root": worktree_root, "base_branch": base_branch, "dry_run": dry_run_flag(request)}, cfg, request)))


def run_omp_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    blocked = _issue_to_pr_gate(request, "load_kanban_task", "parse_issue_ref", "prepare_worktree")
    if blocked:
        return blocked
    cond = conduction_of(request)
    cfg = cfg_of(request)
    prep = cond_blob(request, "prepare_worktree", "prepare_wt")
    parse = cond_blob(request, "parse_issue_ref", "parse_ref")
    load = cond_blob(request, "load_kanban_task", "load_task")
    task = load.get("task") or {}
    worktree_path = str(request.input.get("worktree_path") or prep.get("worktree_path") or "")
    title = str(request.input.get("task_title") or task.get("title") or parse.get("task_title") or "fix task")
    repo = str(request.input.get("repo") or parse.get("repo") or "")
    issue = request.input.get("issue") or parse.get("issue")
    prompt = f"Implement the Kanban/GitHub work for {repo}#{issue}: {title}\nWork only in this worktree. Create commits on the current branch.\nDo not force-push. Do not open a PR (another step does that).\nKeep the change minimal and include tests if appropriate.\n"
    return _out(issue_to_pr.run_omp_worker(_req({"worktree_path": worktree_path, "prompt": prompt, "model": cfg.get("model") or "omniroute/omp/default", "timeout_seconds": cfg.get("timeout_seconds") or 1800, "dry_run": dry_run_flag(request)}, cfg, request)))


def verify_commits_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    blocked = _issue_to_pr_gate(request, "load_kanban_task", "parse_issue_ref", "prepare_worktree", "run_omp")
    if blocked:
        return blocked
    cond = conduction_of(request)
    cfg = cfg_of(request)
    prep = cond_blob(request, "prepare_worktree", "prepare_wt")
    clone_path = str(request.input.get("clone_path") or cfg.get("clone_path") or "")
    base_branch = str(request.input.get("base_branch") or cfg.get("base_branch") or "main")
    return _out(issue_to_pr.verify_branch_has_commits(_req({"worktree_path": request.input.get("worktree_path") or prep.get("worktree_path"), "clone_path": clone_path, "base_branch": base_branch, "dry_run": dry_run_flag(request)}, cfg, request)))


def push_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    blocked = _issue_to_pr_gate(request, "load_kanban_task", "parse_issue_ref", "prepare_worktree", "run_omp", "verify_branch")
    if blocked:
        return blocked
    cond = conduction_of(request)
    cfg = cfg_of(request)
    prep = cond_blob(request, "prepare_worktree", "prepare_wt")
    parse = cond_blob(request, "parse_issue_ref", "parse_ref")
    return _out(issue_to_pr.push_branch(_req({"worktree_path": request.input.get("worktree_path") or prep.get("worktree_path"), "branch": request.input.get("branch") or parse.get("branch"), "dry_run": dry_run_flag(request)}, cfg, request)))


def open_pr_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    blocked = _issue_to_pr_gate(request, "load_kanban_task", "parse_issue_ref", "prepare_worktree", "run_omp", "verify_branch", "push_branch")
    if blocked:
        return blocked
    cond = conduction_of(request)
    cfg = cfg_of(request)
    parse = cond_blob(request, "parse_issue_ref", "parse_ref")
    load = cond_blob(request, "load_kanban_task", "load_task")
    task = load.get("task") or {}
    repo = str(request.input.get("repo") or parse.get("repo") or "")
    issue = request.input.get("issue") or parse.get("issue")
    branch = str(request.input.get("branch") or parse.get("branch") or "")
    title = str(request.input.get("task_title") or task.get("title") or f"fix: {repo}#{issue}")
    body = f"Automated PR for {repo}#{issue}.\n\nCloses #{issue}\n\nTest plan:\n- automated checks\n"
    return _out(issue_to_pr.open_pull_request(_req({"repo": repo, "branch": branch, "base_branch": request.input.get("base_branch") or cfg.get("base_branch") or "main", "title": title if title.startswith("[") else f"[ai] {title}", "body": body, "dry_run": dry_run_flag(request)}, cfg, request)))


def label_pr_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    blocked = _issue_to_pr_gate(request, "load_kanban_task", "parse_issue_ref", "prepare_worktree", "run_omp", "verify_branch", "push_branch", "open_pull_request")
    if blocked:
        return blocked
    cond = conduction_of(request)
    cfg = cfg_of(request)
    pr = cond_blob(request, "open_pull_request", "open_pr")
    parse = cond_blob(request, "parse_issue_ref", "parse_ref")
    return _out(issue_to_pr.apply_pr_labels(_req({"repo": request.input.get("repo") or parse.get("repo"), "number": request.input.get("number") or pr.get("number"), "labels": ["ai:generated", "ai:pr-opened"], "dry_run": dry_run_flag(request)}, cfg, request)))


def dispatch_receipt_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    blocked = _issue_to_pr_gate(request, "load_kanban_task", "parse_issue_ref", "prepare_worktree", "run_omp", "verify_branch", "push_branch", "open_pull_request", "apply_pr_labels")
    if blocked:
        return blocked
    import time
    cond = conduction_of(request)
    cfg = cfg_of(request)
    pr = cond_blob(request, "open_pull_request", "open_pr")
    parse = cond_blob(request, "parse_issue_ref", "parse_ref")
    prep = cond_blob(request, "prepare_worktree", "prepare_wt")
    receipt_dir = str(cfg.get("dispatch_receipts") or Path.home() / ".hermes/state/repo-agent-dispatch-live")
    pr_number = pr.get("number") or "unknown"
    path = str(Path(receipt_dir) / f"pr-{pr_number}.json")
    payload = {"phase": "PR_OPENED" if pr.get("number") else "DISPATCHED", "repo": request.input.get("repo") or parse.get("repo"), "issue": request.input.get("issue") or parse.get("issue"), "branch": request.input.get("branch") or parse.get("branch"), "worktree_path": request.input.get("worktree_path") or prep.get("worktree_path"), "pr_number": pr.get("number"), "pr_url": pr.get("url"), "ts": int(time.time()), "dry_run": dry_run_flag(request)}
    return _out(issue_to_pr.write_dispatch_receipt(_req({"receipt_path": path, "payload": payload, "dry_run": dry_run_flag(request)}, cfg, request)))


def complete_task_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    blocked = _issue_to_pr_gate(request, "load_kanban_task", "parse_issue_ref", "prepare_worktree", "run_omp", "verify_branch", "push_branch", "open_pull_request", "apply_pr_labels", "write_dispatch_receipt")
    if blocked:
        return blocked
    cond = conduction_of(request)
    cfg = cfg_of(request)
    load = cond_blob(request, "load_kanban_task", "load_task")
    pr = cond_blob(request, "open_pull_request", "open_pr")
    task = load.get("task") or {}
    task_id = request.input.get("task_id") or task.get("id") or task.get("task_id")
    board = request.input.get("board") or load.get("board") or cfg.get("board")
    result = f"PR #{pr.get('number')}" if pr.get("number") else "completed"
    return _out(issue_to_pr.complete_kanban_task(_req({"board": board, "task_id": task_id, "result": result, "dry_run": dry_run_flag(request)}, cfg, request)))


# --- triage bridges ---


def load_first_ai_pr(request: EffectorRunRequest) -> EffectorRunResult:
    """List ai/fix PRs and load the first one for triage path."""
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    repo = str(request.input.get("repo") or cfg.get("repo") or "")
    listed = triage.list_ai_fix_prs(_req({"repo": repo, "limit": int(request.input.get("limit") or 20)}, cfg, request))
    if not listed.output.get("ok"):
        return listed
    prs = listed.output.get("prs") or []
    if not prs:
        return EffectorRunResult(output={**listed.output, "reason": listed.output.get("reason") or "no_ai_fix_prs", "prs": [], "dry_run": dry})
    number = int(prs[0].get("number") or 0)
    loaded = triage.load_pr_fields(_req({"repo": repo, "number": number}, cfg, request))
    out = dict(loaded.output)
    out["listed_count"] = listed.output.get("count")
    out["repo"] = repo
    return EffectorRunResult(output=out)

def evaluate_checks_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cfg = cfg_of(request)
    pr = _input_value(request, "pr", default=None)
    if not isinstance(pr, dict):
        pr = _upstream_blob(request, "load_pr_fields", "load_pr").get("pr") or {}
    return _out(triage.evaluate_checks(_req({
        "pr": pr,
        "allow_no_checks": bool(_input_value(request, "allow_no_checks", default=cfg.get("allow_no_checks", False))),
        "require_checks": _input_value(request, "require_checks", default=cfg.get("require_checks", True)),
    }, cfg, request)))


def evaluate_evidence_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cfg = cfg_of(request)
    pr = _input_value(request, "pr", default=None)
    if not isinstance(pr, dict):
        pr = _upstream_blob(request, "load_pr_fields", "load_pr").get("pr") or {}
    return _out(triage.evaluate_test_evidence(_req({
        "pr": pr,
        "require_test_evidence": bool(_input_value(request, "require_test_evidence", default=cfg.get("require_test_evidence", True))),
    }, cfg, request)))


def decide_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cfg = cfg_of(request)
    load = _upstream_blob(request, "load_pr_fields", "load_pr")
    checks = _upstream_blob(request, "evaluate_checks", "checks")
    evidence = _upstream_blob(request, "evaluate_test_evidence", "evidence")
    pr = _input_value(request, "pr", default=None)
    return _out(triage.decide_triage_action(_req({
        "pr": pr if isinstance(pr, dict) else load.get("pr") or {},
        "checks_pass": bool(_input_value(request, "checks_pass", default=checks.get("pass_", checks.get("pass", False)))),
        "evidence_pass": bool(_input_value(request, "evidence_pass", default=evidence.get("pass_", evidence.get("pass", False)))),
        "automerge": bool(_input_value(request, "automerge", default=cfg.get("automerge", False))),
    }, cfg, request)))


def apply_triage_decision(request: EffectorRunRequest) -> EffectorRunResult:
    """Apply a triage decision while fail-closing every child boundary."""
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    load = _upstream_blob(request, "load_pr_fields", "load_pr")
    decide = _upstream_blob(request, "decide_triage_action", "decide")
    action = str(_input_value(request, "action", default=decide.get("action") or "skip"))
    explicit_pr = _input_value(request, "pr", default=None)
    pr = explicit_pr if isinstance(explicit_pr, dict) else (load.get("pr") if isinstance(load.get("pr"), dict) else {})
    for upstream_id, blob in (("load_pr_fields", load), ("decide_triage_action", decide)):
        child = EffectorRunResult(output=dict(blob))
        if _failed(child):
            return _child_failure(child, upstream_id)
        if _noop(child):
            return _child_stop(child, upstream_id)
    repo = str(_input_value(request, "repo", default=load.get("repo") or pr.get("repo") or cfg.get("repo") or ""))
    raw_number = _input_value(request, "number", "pr_number", default=pr.get("number") or load.get("number") or 0)
    number = int(raw_number or 0)
    head_oid = str(_input_value(request, "head_oid", "headRefOid", default=pr.get("head_oid") or pr.get("headRefOid") or ""))
    branch = str(_input_value(request, "branch", "headRefName", default=pr.get("headRefName") or ""))
    issue = _input_value(request, "issue", default=None)
    parsed = cleanup.parse_issue_from_branch(_req({"branch": branch}, cfg, request)) if branch else None
    if parsed is not None and parsed.output.get("ok") and issue in (None, ""):
        issue = parsed.output.get("issue")
    elif action == "merge" and parsed is not None:
        return parsed
    if action == "skip":
        return ok(status="skipped", action=action, reason=decide.get("reason"), dry_run=dry, mutated=False)
    if action == "comment_block":
        body = f"repo-agent triage: blocked ({decide.get('reason')}). Automerge will not proceed until this is resolved."
        return triage.comment_pr_once(_req({"repo": repo, "number": number, "body": body, "dry_run": dry}, cfg, request))
    if action == "repair":
        from repo_agent.steps.repair import create_review_fix_task
        return create_review_fix_task(_req({"board": cfg.get("board"), "repo": repo, "number": number, "reason": str(decide.get("reason") or "checks_failed"), "dry_run": dry}, cfg, request))
    if action != "merge":
        return fail("unknown_triage_action", failure_class="terminal", retry_safe=False, action=action, mutated=False)
    claim = triage.claim_pr_assignee(_req({"repo": repo, "number": number, "issue": issue, "dry_run": dry}, cfg, request))
    if _failed(claim) or (not claim.output.get("ok") and claim.output.get("status") != "planned"):
        return claim
    merged = triage.merge_pull_request(_req({"repo": repo, "number": number, "issue": issue, "head_oid": head_oid, "dry_run": dry}, cfg, request))
    if _failed(merged) or (not merged.output.get("ok") and merged.output.get("status") not in ("planned", "merged")):
        return merged
    base_mutated = bool(claim.output.get("mutated")) or bool(merged.output.get("mutated"))
    if merged.output.get("status") == "noop":
        return ok(status="noop", reason=merged.output.get("reason"), action="merge", claim=claim.output, merge=merged.output, dry_run=dry, mutated=base_mutated)
    provenance = merged.output.get("verified_provenance")
    receipt_dir = str(cfg.get("merge_receipts") or Path.home() / ".hermes/state/repo-agent-merge-live")
    path = str(Path(receipt_dir) / f"merge-pr-{number}.json")
    receipt = triage.write_merge_receipt(_req({"receipt_path": path, "payload": {"repo": repo, "pr": number, "issue": issue, "head_oid": head_oid, "phase": "MERGED", "dry_run": dry}, "dry_run": dry, "verified_provenance": provenance, "head_oid": head_oid, "issue": issue}, cfg, request))
    closed = None
    if issue not in (None, ""):
        closed = triage.close_linked_issue(_req({"repo": repo, "issue": issue, "head_oid": head_oid, "dry_run": dry, "verified_provenance": provenance}, cfg, request))
    failures = [item.output for item in (receipt, closed) if item is not None and _failed(item)]
    mutated = base_mutated or any(bool(item.get("mutated")) for item in [receipt.output, closed.output if closed else {}])
    if failures:
        return fail("merge_pipeline_failed", failure_class="reconcile_then_retry", retry_safe=False, action="merge", claim=claim.output, merge=merged.output, receipt=receipt.output, close=(closed.output if closed else None), failures=failures, dry_run=dry, mutated=mutated)
    return ok(status="merge_pipeline_done", action="merge", claim=claim.output, merge=merged.output, receipt=receipt.output, close=(closed.output if closed else None), dry_run=dry, mutated=mutated)
def cleanup_all_from_list(request: EffectorRunRequest) -> EffectorRunResult:
    """Clean every listed worktree and preserve no-op/partial mutation evidence."""
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    listed = _upstream_blob(request, "list_controlled_worktrees", "list_worktrees", "list_wts")
    if not listed:
        listed = _input_value(request, "list_worktrees", default={}) or {}
    rows = listed.get("worktrees") or []
    clone_path = str(_input_value(request, "clone_path", default=cfg.get("clone_path") or ""))
    repo = str(_input_value(request, "repo", default=cfg.get("repo") or ""))
    cleaned: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        branch = str(row.get("branch") or "")
        path = str(row.get("path") or "")
        if not branch or not path:
            continue
        result = cleanup_candidate_from_input(_req({"branch": branch, "worktree_path": path, "clone_path": clone_path, "repo": repo, "dry_run": dry}, cfg, request))
        out = result.output or {}
        if out.get("status") in {"cleaned", "done"}:
            cleaned.append(out)
        elif _failed(result):
            failures.append(out)
        else:
            skipped.append(out)
    mutated = any(bool(item.get("mutated")) for item in [*cleaned, *skipped, *failures])
    noop_reasons = [str(item.get("reason")) for item in skipped if item.get("status") == "noop" and item.get("reason")]
    noop_count = sum(1 for item in skipped if item.get("status") == "noop")
    common = {"cleaned": len(cleaned), "skipped": len(skipped), "noop_count": noop_count, "noop_reasons": noop_reasons[:20], "details_cleaned": cleaned[:20], "details_skipped": skipped[:20], "dry_run": dry, "mutated": mutated}
    if failures:
        return fail("cleanup_candidate_failed", **common, details_failed=failures[:20])
    if rows and not cleaned and not failures:
        return ok(status="noop", reason=(noop_reasons[0] if noop_reasons else listed.get("reason") or "all_noop"), **common)
    return ok(status="done", **common)


def cleanup_candidate_from_input(request: EffectorRunRequest) -> EffectorRunResult:
    """Given clone/worktree/branch/repo, run safety checks then cleanup atomics."""
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    data = dict(request.input or {})
    branch = str(data.get("branch") or "")
    repo = str(data.get("repo") or cfg.get("repo") or "")
    clone_path = str(data.get("clone_path") or cfg.get("clone_path") or "")
    worktree_path = str(data.get("worktree_path") or "")
    if not branch:
        return ok(status="skipped", reason="no_branch", dry_run=dry, mutated=False)
    parsed = cleanup.parse_issue_from_branch(_req({"branch": branch}, cfg, request))
    if not parsed.output.get("ok"):
        return parsed
    issue = parsed.output.get("issue")
    closed = cleanup.check_issue_closed(_req({"repo": repo, "issue": issue, "conduction": {"parse": parsed.output}}, cfg, request))
    if not closed.output.get("ok"):
        return closed
    if not closed.output.get("closed"):
        return ok(status="skipped", reason="issue_still_open", issue=issue, dry_run=dry)
    no_pr = cleanup.check_no_open_pr_for_branch(_req({"repo": repo, "branch": branch, "conduction": {"parse": parsed.output}}, cfg, request))
    if not no_pr.output.get("ok"):
        return no_pr
    if not no_pr.output.get("safe_to_cleanup"):
        return ok(status="skipped", reason="open_pr_remains", issue=issue, dry_run=dry)
    conduction = {"parse_issue_from_branch": parsed.output, "parse": parsed.output, "check_issue_closed": closed.output, "check_no_open_pr": no_pr.output}
    removed = cleanup.remove_worktree(_req({"clone_path": clone_path, "worktree_path": worktree_path, "force": False, "dry_run": dry, "conduction": conduction}, cfg, request))
    conduction["remove_worktree"] = removed.output
    if _failed(removed):
        return removed
    deleted = cleanup.delete_local_fix_branch(_req({"clone_path": clone_path, "branch": branch, "force": True, "dry_run": dry, "conduction": conduction}, cfg, request))
    conduction["delete_local_fix_branch"] = deleted.output
    if _failed(deleted):
        return deleted
    if deleted.output.get("status") in {"noop", "planned"}:
        return ok(status=deleted.output.get("status"), reason=deleted.output.get("reason"), issue=issue, branch=branch, remove=removed.output, delete_branch=deleted.output, dry_run=dry, mutated=bool(removed.output.get("mutated")) or bool(deleted.output.get("mutated")))
    claim_path = str(cfg.get("active_issue_path") or Path.home() / ".hermes/state/repo-agent-active-live/active.json")
    released = cleanup.release_active_issue_claim(_req({"claim_path": claim_path, "repo": repo, "issue": str(issue), "dry_run": dry, "conduction": conduction}, cfg, request))
    if _failed(released):
        return released
    return ok(status="cleaned" if released.output.get("status") != "noop" else "noop", issue=issue, branch=branch, remove=removed.output, delete_branch=deleted.output, release=released.output, dry_run=dry, mutated=not dry and any(bool(item.get("mutated")) for item in (removed.output, deleted.output, released.output)))
