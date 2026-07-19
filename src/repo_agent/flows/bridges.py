"""Thin conduction → flat-input bridges so atomic effectors compose without glue in paths.

Each bridge is itself a mega-atomic python_function effector: it only remaps
upstream conduction into the next atomic's input and delegates.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fala.adapters import EffectorRunRequest, EffectorRunResult

from repo_agent.envelope import cfg_of, conduction_of, dry_run_flag, fail, ok
from repo_agent.steps import cleanup, issue_to_pr, triage


def _req(
    input_data: dict[str, Any],
    config: dict[str, Any],
    *,
    parent: EffectorRunRequest | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        input=input_data,
        config=config,
        process_id=getattr(parent, "process_id", "bridge"),
        impulse_id=getattr(parent, "impulse_id", None),
        work_dir=getattr(parent, "work_dir", None),
        adapter=getattr(parent, "adapter", None),
    )


def _out(result: EffectorRunResult) -> EffectorRunResult:
    return result


def _failed(result: EffectorRunResult | None) -> bool:
    if result is None:
        return False
    output = result.output or {}
    return output.get("status") == "failed" or output.get("ok") is False
def _noop(result: EffectorRunResult | None) -> bool:
    return bool(result and (result.output or {}).get("status") == "noop")


def _child_stop(
    result: EffectorRunResult,
    stage: str,
    prior_mutated: bool = False,
) -> EffectorRunResult:
    """Stop a dependent mutation after a controlled child no-op."""
    output = dict(result.output or {})
    output["stage"] = stage
    output["mutated"] = prior_mutated or bool(output.get("mutated"))
    return EffectorRunResult(output=output)


def _child_failure(
    result: EffectorRunResult,
    stage: str,
    prior_mutated: bool = False,
) -> EffectorRunResult:
    output = dict(result.output or {})
    return fail(
        str(output.get("reason") or f"{stage}_failed"),
        failure_class=str(output.get("failure_class") or "terminal"),
        retry_safe=bool(output.get("retry_safe", False)),
        mutated=prior_mutated or bool(output.get("mutated", False)),
        stage=stage,
        child=output,
    )


def _mutated(*results: EffectorRunResult | None) -> bool:
    return any(bool(result and (result.output or {}).get("mutated")) for result in results)


def prepare_worktree_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cond = conduction_of(request)
    cfg = cfg_of(request)
    parse = cond.get("parse_issue_ref") or request.input.get("parse_issue_ref") or {}
    branch = str(parse.get("branch") or request.input.get("branch") or "")
    clone_path = str(cfg.get("clone_path") or request.input.get("clone_path") or "")
    worktree_root = str(
        cfg.get("worktree_root") or request.input.get("worktree_root") or ""
    )
    base_branch = str(cfg.get("base_branch") or "main")
    return _out(
        issue_to_pr.prepare_worktree(
            _req({
                "clone_path": clone_path,
                "branch": branch,
                "worktree_root": worktree_root,
                "base_branch": base_branch,
                "dry_run": dry_run_flag(request),
            }, cfg, parent=request)
        )
    )


def run_omp_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cond = conduction_of(request)
    cfg = cfg_of(request)
    prep = cond.get("prepare_worktree") or request.input.get("prepare_worktree") or {}
    parse = cond.get("parse_issue_ref") or request.input.get("parse_issue_ref") or {}
    load = cond.get("load_kanban_task") or request.input.get("load_kanban_task") or {}
    task = load.get("task") or {}
    worktree_path = str(prep.get("worktree_path") or "")
    title = str(task.get("title") or parse.get("task_title") or "fix task")
    repo = str(parse.get("repo") or "")
    issue = parse.get("issue")
    prompt = (
        f"Implement the Kanban/GitHub work for {repo}#{issue}: {title}\n"
        f"Work only in this worktree. Create commits on the current branch.\n"
        f"Do not force-push. Do not open a PR (another step does that).\n"
        f"Keep the change minimal and include tests if appropriate.\n"
    )
    return _out(
        issue_to_pr.run_omp_worker(
            _req({
                "worktree_path": worktree_path,
                "prompt": prompt,
                "model": cfg.get("model") or "omniroute/omp/default",
                "timeout_seconds": cfg.get("timeout_seconds") or 1800,
                "dry_run": dry_run_flag(request),
            }, cfg, parent=request)
        )
    )


def verify_commits_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cond = conduction_of(request)
    cfg = cfg_of(request)
    prep = cond.get("prepare_worktree") or request.input.get("prepare_worktree") or {}
    return _out(
        issue_to_pr.verify_branch_has_commits(
            _req({
                "worktree_path": prep.get("worktree_path"),
                "clone_path": cfg.get("clone_path"),
                "base_branch": cfg.get("base_branch") or "main",
                "dry_run": dry_run_flag(request),
            }, cfg, parent=request)
        )
    )


def push_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cond = conduction_of(request)
    cfg = cfg_of(request)
    prep = cond.get("prepare_worktree") or request.input.get("prepare_worktree") or {}
    parse = cond.get("parse_issue_ref") or request.input.get("parse_issue_ref") or {}
    return _out(
        issue_to_pr.push_branch(
            _req({
                "worktree_path": prep.get("worktree_path"),
                "branch": parse.get("branch"),
                "dry_run": dry_run_flag(request),
            }, cfg, parent=request)
        )
    )


def open_pr_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cond = conduction_of(request)
    cfg = cfg_of(request)
    parse = cond.get("parse_issue_ref") or request.input.get("parse_issue_ref") or {}
    load = cond.get("load_kanban_task") or request.input.get("load_kanban_task") or {}
    task = load.get("task") or {}
    repo = str(parse.get("repo") or "")
    issue = parse.get("issue")
    branch = str(parse.get("branch") or "")
    title = str(task.get("title") or f"fix: {repo}#{issue}")
    body = (
        f"Automated PR for {repo}#{issue}.\n\n"
        f"Closes #{issue}\n\n"
        f"Test plan:\n- automated checks\n"
    )
    return _out(
        issue_to_pr.open_pull_request(
            _req({
                "repo": repo,
                "branch": branch,
                "base_branch": cfg.get("base_branch") or "main",
                "title": title if title.startswith("[") else f"[ai] {title}",
                "body": body,
                "dry_run": dry_run_flag(request),
            }, cfg, parent=request)
        )
    )


def label_pr_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cond = conduction_of(request)
    cfg = cfg_of(request)
    pr = cond.get("open_pull_request") or request.input.get("open_pull_request") or {}
    parse = cond.get("parse_issue_ref") or request.input.get("parse_issue_ref") or {}
    return _out(
        issue_to_pr.apply_pr_labels(
            _req({
                "repo": parse.get("repo"),
                "number": pr.get("number"),
                "labels": ["ai:generated", "ai:pr-opened"],
                "dry_run": dry_run_flag(request),
            }, cfg, parent=request)
        )
    )


def dispatch_receipt_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    import time

    cond = conduction_of(request)
    cfg = cfg_of(request)
    pr = cond.get("open_pull_request") or request.input.get("open_pull_request") or {}
    parse = cond.get("parse_issue_ref") or request.input.get("parse_issue_ref") or {}
    prep = cond.get("prepare_worktree") or request.input.get("prepare_worktree") or {}
    receipt_dir = str(
        cfg.get("dispatch_receipts")
        or Path.home() / ".hermes/state/repo-agent-dispatch-live"
    )
    pr_number = pr.get("number") or "unknown"
    path = str(Path(receipt_dir) / f"pr-{pr_number}.json")
    payload = {
        "phase": "PR_OPENED" if pr.get("number") else "DISPATCHED",
        "repo": parse.get("repo"),
        "issue": parse.get("issue"),
        "branch": parse.get("branch"),
        "worktree_path": prep.get("worktree_path"),
        "pr_number": pr.get("number"),
        "pr_url": pr.get("url"),
        "ts": int(time.time()),
        "dry_run": dry_run_flag(request),
    }
    return _out(
        issue_to_pr.write_dispatch_receipt(
            _req({
                "receipt_path": path,
                "payload": payload,
                "dry_run": dry_run_flag(request),
            }, cfg, parent=request)
        )
    )


def complete_task_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cond = conduction_of(request)
    cfg = cfg_of(request)
    load = cond.get("load_kanban_task") or request.input.get("load_kanban_task") or {}
    pr = cond.get("open_pull_request") or request.input.get("open_pull_request") or {}
    task = load.get("task") or {}
    task_id = task.get("id") or task.get("task_id")
    board = load.get("board") or cfg.get("board")
    result = f"PR #{pr.get('number')}" if pr.get("number") else "completed"
    return _out(
        issue_to_pr.complete_kanban_task(
            _req({
                "board": board,
                "task_id": task_id,
                "result": result,
                "dry_run": dry_run_flag(request),
            }, cfg, parent=request)
        )
    )


# --- triage bridges ---


def load_first_ai_pr(request: EffectorRunRequest) -> EffectorRunResult:
    """List ai/fix PRs and load the first one for triage path."""
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    repo = str(request.input.get("repo") or cfg.get("repo") or "")
    listed = triage.list_ai_fix_prs(
        _req({"repo": repo, "limit": int(request.input.get("limit") or 20)}, cfg, parent=request)
    )
    if not listed.output.get("ok"):
        return listed
    prs = listed.output.get("prs") or []
    if not prs:
        return ok(status="noop", reason="no_ai_fix_prs", prs=[], dry_run=dry)
    number = int(prs[0].get("number") or 0)
    loaded = triage.load_pr_fields(_req({"repo": repo, "number": number}, cfg, parent=request))
    out = dict(loaded.output)
    out["listed_count"] = listed.output.get("count")
    out["repo"] = repo
    return EffectorRunResult(output=out)


def evaluate_checks_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cond = conduction_of(request)
    pr = (cond.get("load_pr_fields") or request.input.get("load_pr_fields") or {}).get("pr") or request.input.get("pr") or {}
    return triage.evaluate_checks(
        _req({
            "pr": pr,
            "allow_no_checks": bool(
                cfg_of(request).get("allow_no_checks", True)
            ),
        }, cfg_of(request), parent=request)
    )


def evaluate_evidence_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cond = conduction_of(request)
    pr = (cond.get("load_pr_fields") or request.input.get("load_pr_fields") or {}).get("pr") or request.input.get("pr") or {}
    return triage.evaluate_test_evidence(
        _req({
            "pr": pr,
            "require_test_evidence": bool(
                cfg_of(request).get("require_test_evidence", False)
            ),
        }, cfg_of(request), parent=request)
    )


def decide_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cond = conduction_of(request)
    load = cond.get("load_pr_fields") or request.input.get("load_pr_fields") or {}
    checks = cond.get("evaluate_checks") or request.input.get("evaluate_checks") or {}
    evidence = cond.get("evaluate_test_evidence") or request.input.get("evaluate_test_evidence") or {}
    cfg = cfg_of(request)
    return triage.decide_triage_action(
        _req({
            "pr": load.get("pr") or {},
            "checks_pass": bool(checks.get("pass_", checks.get("pass"))),
            "evidence_pass": bool(evidence.get("pass_", True)),
            "automerge": bool(cfg.get("automerge", True)),
        }, cfg, parent=request)
    )


def apply_triage_decision(request: EffectorRunRequest) -> EffectorRunResult:
    """Single mutator step that applies decide_triage_action result."""
    cond = conduction_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    load = cond.get("load_pr_fields") or request.input.get("load_pr_fields") or {}
    decide = cond.get("decide_triage_action") or request.input.get("decide_triage_action") or {}
    action = str(decide.get("action") or "skip")
    pr = load.get("pr") or {}
    repo = str(load.get("repo") or cfg.get("repo") or "")
    number = int(pr.get("number") or load.get("number") or 0)
    head_oid = str(pr.get("headRefOid") or "")
    issue = None
    branch = str(pr.get("headRefName") or "")
    parsed = cleanup.parse_issue_from_branch(_req({"branch": branch}, cfg, parent=request))
    if _failed(parsed):
        return _child_failure(parsed, "parse_issue_from_branch")
    if parsed.output.get("ok"):
        issue = parsed.output.get("issue")
    elif _noop(parsed) and action == "merge":
        return _child_stop(parsed, "parse_issue_from_branch")

    if action == "skip":
        return ok(status="skipped", action=action, reason=decide.get("reason"), dry_run=dry)
    if action == "comment_block":
        body = (
            f"repo-agent triage: blocked ({decide.get('reason')}). "
            "Automerge will not proceed until this is resolved."
        )
        return triage.comment_pr_once(
            _req({"repo": repo, "number": number, "body": body, "dry_run": dry}, cfg, parent=request)
        )
    if action == "repair":
        from repo_agent.steps.repair import create_review_fix_task

        return create_review_fix_task(
            _req({
                "board": cfg.get("board"),
                "repo": repo,
                "number": number,
                "reason": str(decide.get("reason") or "checks_failed"),
                "dry_run": dry,
            }, cfg, parent=request)
        )
    if action == "merge":
        claim = triage.claim_pr_assignee(
            _req({"repo": repo, "number": number, "dry_run": dry}, cfg, parent=request)
        )
        if _failed(claim):
            return _child_failure(claim, "claim")
        if _noop(claim):
            return _child_stop(claim, "claim")
        merged = triage.merge_pull_request(
            _req({"repo": repo, "number": number, "head_oid": head_oid, "dry_run": dry}, cfg, parent=request)
        )
        if _failed(merged):
            return _child_failure(merged, "merge", _mutated(claim))
        if _noop(merged):
            return _child_stop(merged, "merge", _mutated(claim))
        receipt_dir = str(
            cfg.get("merge_receipts")
            or Path.home() / ".hermes/state/repo-agent-merge-live"
        )
        receipt = triage.write_merge_receipt(
            _req({
                "receipt_path": str(Path(receipt_dir) / f"merge-pr-{number}.json"),
                "payload": {
                    "repo": repo,
                    "pr": number,
                    "head_oid": head_oid,
                    "phase": "MERGED",
                    "dry_run": dry,
                },
                "dry_run": dry,
            }, cfg, parent=request)
        )
        if _failed(receipt):
            return _child_failure(receipt, "receipt", _mutated(claim, merged))
        if _noop(receipt):
            return _child_stop(receipt, "receipt", _mutated(claim, merged))
        closed = None
        if issue:
            closed = triage.close_linked_issue(
                _req({"repo": repo, "issue": issue, "dry_run": dry}, cfg, parent=request)
            )
            if _failed(closed):
                return _child_failure(closed, "close", _mutated(claim, merged, receipt))
            if _noop(closed):
                return _child_stop(closed, "close", _mutated(claim, merged, receipt))
        return ok(
            status="merge_pipeline_done",
            action=action,
            claim=claim.output,
            merge=merged.output,
            receipt=receipt.output,
            close=closed.output if closed else None,
            dry_run=dry,
            mutated=_mutated(claim, merged, receipt, closed),
        )
    return fail("unknown_action", failure_class="terminal", retry_safe=False, mutated=False, action=action)


# --- cleanup bridges ---


def cleanup_all_from_list(request: EffectorRunRequest) -> EffectorRunResult:
    """For each listed worktree, run cleanup_candidate_from_input safety pipeline."""
    cond = conduction_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    listed = cond.get("list_worktrees") or request.input.get("list_worktrees") or {}
    rows = listed.get("worktrees") if isinstance(listed, dict) else listed
    rows = rows or []
    clone_path = str(request.input.get("clone_path") or cfg.get("clone_path") or "")
    repo = str(request.input.get("repo") or cfg.get("repo") or "")
    cleaned: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    aggregate_mutated = False
    for row in rows:
        if not isinstance(row, dict):
            return fail("malformed_worktree_row", failure_class="terminal", retry_safe=False, mutated=aggregate_mutated, details_cleaned=cleaned[:20], details_skipped=skipped[:20])
        branch = str(row.get("branch") or "")
        path = str(row.get("path") or "")
        if not branch or not path:
            return fail("malformed_worktree_row", failure_class="terminal", retry_safe=False, mutated=aggregate_mutated, details_cleaned=cleaned[:20], details_skipped=skipped[:20])
        result = cleanup_candidate_from_input(
            EffectorRunRequest(
                process_id=request.process_id,
                adapter=request.adapter,
                input={
                    "branch": branch,
                    "worktree_path": path,
                    "clone_path": clone_path,
                    "repo": repo,
                    "dry_run": dry,
                },
                config=cfg,
            )
        )
        out = result.output
        if _failed(result):
            failure = _child_failure(result, "cleanup", aggregate_mutated)
            failure.output["details_cleaned"] = cleaned[:20]
            failure.output["details_skipped"] = skipped[:20]
            return failure
        if _noop(result):
            aggregate_mutated = aggregate_mutated or bool(out.get("mutated"))
            skipped.append(out)
            continue
        aggregate_mutated = aggregate_mutated or bool(out.get("mutated"))
        if out.get("status") == "cleaned":
            cleaned.append(out)
        else:
            skipped.append(out)
    return ok(
        status="done",
        cleaned=len(cleaned),
        skipped=len(skipped),
        details_cleaned=cleaned[:20],
        details_skipped=skipped[:20],
        dry_run=dry,
        mutated=aggregate_mutated,
    )


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
    parsed = cleanup.parse_issue_from_branch(_req({"branch": branch}, cfg, parent=request))
    if _failed(parsed):
        return _child_failure(parsed, "parse_issue_from_branch")
    if _noop(parsed):
        return _child_stop(parsed, "parse_issue_from_branch")
    issue = parsed.output.get("issue")
    closed = cleanup.check_issue_closed(_req({"repo": repo, "issue": issue}, cfg, parent=request))
    if _failed(closed):
        return _child_failure(closed, "check_issue_closed")
    if _noop(closed):
        return _child_stop(closed, "check_issue_closed")
    if not closed.output.get("closed"):
        return ok(status="skipped", reason="issue_still_open", issue=issue, dry_run=dry, mutated=False)
    no_pr = cleanup.check_no_open_pr_for_branch(_req({"repo": repo, "branch": branch}, cfg, parent=request))
    if _failed(no_pr):
        return _child_failure(no_pr, "check_no_open_pr")
    if _noop(no_pr):
        return _child_stop(no_pr, "check_no_open_pr")
    if not no_pr.output.get("safe_to_cleanup"):
        return ok(status="skipped", reason="open_pr_remains", issue=issue, dry_run=dry, mutated=False)
    removed = cleanup.remove_worktree(
        _req({
            "clone_path": clone_path,
            "worktree_path": worktree_path,
            "force": False,
            "dry_run": dry,
            "conduction": {
                "check_issue_closed": closed.output,
                "check_no_open_pr": no_pr.output,
            },
        }, cfg, parent=request)
    )
    if _failed(removed):
        return _child_failure(removed, "remove_worktree")
    if _noop(removed):
        return _child_stop(removed, "remove_worktree")
    deleted = cleanup.delete_local_fix_branch(
        _req({
            "clone_path": clone_path,
            "branch": branch,
            "force": True,
            "dry_run": dry,
            "conduction": {"remove_worktree": removed.output},
        }, cfg, parent=request)
    )
    if _failed(deleted):
        return _child_failure(deleted, "delete_branch", _mutated(removed))
    if _noop(deleted):
        return _child_stop(deleted, "delete_branch", _mutated(removed))
    claim_path = str(
        cfg.get("active_issue_path")
        or Path.home() / ".hermes/state/repo-agent-active-live/active.json"
    )
    released = cleanup.release_active_issue_claim(
        _req({
            "claim_path": claim_path,
            "repo": repo,
            "issue": str(issue),
            "dry_run": dry,
            "conduction": {
                "parse_issue_from_branch": parsed.output,
                "check_issue_closed": closed.output,
                "remove_worktree": removed.output,
            },
        }, cfg, parent=request)
    )
    if _failed(released):
        return _child_failure(released, "release_claim", _mutated(removed, deleted))
    if _noop(released):
        return _child_stop(released, "release_claim", _mutated(removed, deleted))
    return ok(
        status="cleaned",
        issue=issue,
        branch=branch,
        remove=removed.output,
        delete_branch=deleted.output,
        release=released.output,
        dry_run=dry,
        mutated=_mutated(removed, deleted, released),
    )
