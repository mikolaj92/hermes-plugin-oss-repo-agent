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


def _req(input_data: dict[str, Any], config: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        input=input_data,
        config=config,
        process_id="bridge",
        impulse_id=None,
        work_dir=None,
        adapter=None,
    )


def _out(result: EffectorRunResult) -> EffectorRunResult:
    return result


def prepare_worktree_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cond = conduction_of(request)
    cfg = cfg_of(request)
    parse = cond.get("parse_ref") or {}
    branch = str(parse.get("branch") or request.input.get("branch") or "")
    clone_path = str(cfg.get("clone_path") or request.input.get("clone_path") or "")
    worktree_root = str(
        cfg.get("worktree_root") or request.input.get("worktree_root") or ""
    )
    base_branch = str(cfg.get("base_branch") or "main")
    return _out(
        issue_to_pr.prepare_worktree(
            _req(
                {
                    "clone_path": clone_path,
                    "branch": branch,
                    "worktree_root": worktree_root,
                    "base_branch": base_branch,
                    "dry_run": dry_run_flag(request),
                },
                cfg,
            )
        )
    )


def run_omp_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cond = conduction_of(request)
    cfg = cfg_of(request)
    prep = cond.get("prepare_wt") or {}
    parse = cond.get("parse_ref") or {}
    load = cond.get("load_task") or {}
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
            _req(
                {
                    "worktree_path": worktree_path,
                    "prompt": prompt,
                    "model": cfg.get("model") or "omniroute/omp/default",
                    "timeout_seconds": cfg.get("timeout_seconds") or 1800,
                    "dry_run": dry_run_flag(request),
                },
                cfg,
            )
        )
    )


def verify_commits_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cond = conduction_of(request)
    cfg = cfg_of(request)
    prep = cond.get("prepare_wt") or {}
    return _out(
        issue_to_pr.verify_branch_has_commits(
            _req(
                {
                    "worktree_path": prep.get("worktree_path"),
                    "clone_path": cfg.get("clone_path"),
                    "base_branch": cfg.get("base_branch") or "main",
                },
                cfg,
            )
        )
    )


def push_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cond = conduction_of(request)
    cfg = cfg_of(request)
    prep = cond.get("prepare_wt") or {}
    parse = cond.get("parse_ref") or {}
    return _out(
        issue_to_pr.push_branch(
            _req(
                {
                    "worktree_path": prep.get("worktree_path"),
                    "branch": parse.get("branch"),
                    "dry_run": dry_run_flag(request),
                },
                cfg,
            )
        )
    )


def open_pr_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cond = conduction_of(request)
    cfg = cfg_of(request)
    parse = cond.get("parse_ref") or {}
    load = cond.get("load_task") or {}
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
            _req(
                {
                    "repo": repo,
                    "branch": branch,
                    "base_branch": cfg.get("base_branch") or "main",
                    "title": title if title.startswith("[") else f"[ai] {title}",
                    "body": body,
                    "dry_run": dry_run_flag(request),
                },
                cfg,
            )
        )
    )


def label_pr_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cond = conduction_of(request)
    cfg = cfg_of(request)
    pr = cond.get("open_pr") or {}
    parse = cond.get("parse_ref") or {}
    return _out(
        issue_to_pr.apply_pr_labels(
            _req(
                {
                    "repo": parse.get("repo"),
                    "number": pr.get("number"),
                    "labels": ["ai:generated", "ai:pr-opened"],
                    "dry_run": dry_run_flag(request),
                },
                cfg,
            )
        )
    )


def dispatch_receipt_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    import time

    cond = conduction_of(request)
    cfg = cfg_of(request)
    pr = cond.get("open_pr") or {}
    parse = cond.get("parse_ref") or {}
    prep = cond.get("prepare_wt") or {}
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
            _req(
                {
                    "receipt_path": path,
                    "payload": payload,
                    "dry_run": dry_run_flag(request),
                },
                cfg,
            )
        )
    )


def complete_task_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cond = conduction_of(request)
    cfg = cfg_of(request)
    load = cond.get("load_task") or {}
    pr = cond.get("open_pr") or {}
    task = load.get("task") or {}
    task_id = task.get("id") or task.get("task_id")
    board = load.get("board") or cfg.get("board")
    result = f"PR #{pr.get('number')}" if pr.get("number") else "completed"
    return _out(
        issue_to_pr.complete_kanban_task(
            _req(
                {
                    "board": board,
                    "task_id": task_id,
                    "result": result,
                    "dry_run": dry_run_flag(request),
                },
                cfg,
            )
        )
    )


# --- triage bridges ---


def load_first_ai_pr(request: EffectorRunRequest) -> EffectorRunResult:
    """List ai/fix PRs and load the first one for triage path."""
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    repo = str(request.input.get("repo") or cfg.get("repo") or "")
    listed = triage.list_ai_fix_prs(
        _req({"repo": repo, "limit": int(request.input.get("limit") or 20)}, cfg)
    )
    if not listed.output.get("ok"):
        return listed
    prs = listed.output.get("prs") or []
    if not prs:
        return ok(status="noop", reason="no_ai_fix_prs", prs=[], dry_run=dry)
    number = int(prs[0].get("number") or 0)
    loaded = triage.load_pr_fields(_req({"repo": repo, "number": number}, cfg))
    out = dict(loaded.output)
    out["listed_count"] = listed.output.get("count")
    out["repo"] = repo
    return EffectorRunResult(output=out)


def evaluate_checks_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cond = conduction_of(request)
    pr = (cond.get("load_pr") or {}).get("pr") or request.input.get("pr") or {}
    return triage.evaluate_checks(
        _req(
            {
                "pr": pr,
                "allow_no_checks": bool(
                    cfg_of(request).get("allow_no_checks", True)
                ),
            },
            cfg_of(request),
        )
    )


def evaluate_evidence_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cond = conduction_of(request)
    pr = (cond.get("load_pr") or {}).get("pr") or {}
    return triage.evaluate_test_evidence(
        _req(
            {
                "pr": pr,
                "require_test_evidence": bool(
                    cfg_of(request).get("require_test_evidence", False)
                ),
            },
            cfg_of(request),
        )
    )


def decide_from_conduction(request: EffectorRunRequest) -> EffectorRunResult:
    cond = conduction_of(request)
    load = cond.get("load_pr") or {}
    checks = cond.get("checks") or {}
    evidence = cond.get("evidence") or {}
    cfg = cfg_of(request)
    return triage.decide_triage_action(
        _req(
            {
                "pr": load.get("pr") or {},
                "checks_pass": bool(checks.get("pass_", checks.get("pass"))),
                "evidence_pass": bool(evidence.get("pass_", True)),
                "automerge": bool(cfg.get("automerge", True)),
            },
            cfg,
        )
    )


def apply_triage_decision(request: EffectorRunRequest) -> EffectorRunResult:
    """Single mutator step that applies decide_triage_action result."""
    cond = conduction_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    load = cond.get("load_pr") or {}
    decide = cond.get("decide") or {}
    action = str(decide.get("action") or "skip")
    pr = load.get("pr") or {}
    repo = str(load.get("repo") or cfg.get("repo") or "")
    number = int(pr.get("number") or load.get("number") or 0)
    head_oid = str(pr.get("headRefOid") or "")
    issue = None
    branch = str(pr.get("headRefName") or "")
    # best-effort issue from branch
    from repo_agent.steps.cleanup import parse_issue_from_branch

    parsed = parse_issue_from_branch(_req({"branch": branch}, cfg))
    if parsed.output.get("ok"):
        issue = parsed.output.get("issue")

    if action == "skip":
        return ok(status="skipped", action=action, reason=decide.get("reason"), dry_run=dry)
    if action == "comment_block":
        body = (
            f"repo-agent triage: blocked ({decide.get('reason')}). "
            f"Automerge will not proceed until this is resolved."
        )
        return triage.comment_pr_once(
            _req(
                {
                    "repo": repo,
                    "number": number,
                    "body": body,
                    "dry_run": dry,
                },
                cfg,
            )
        )
    if action == "repair":
        from repo_agent.steps.repair import create_review_fix_task

        return create_review_fix_task(
            _req(
                {
                    "board": cfg.get("board"),
                    "repo": repo,
                    "number": number,
                    "reason": str(decide.get("reason") or "checks_failed"),
                    "dry_run": dry,
                },
                cfg,
            )
        )
    if action == "merge":
        # claim → merge → receipt → close issue (still atomic steps, sequential here)
        claim = triage.claim_pr_assignee(
            _req({"repo": repo, "number": number, "dry_run": dry}, cfg)
        )
        if not claim.output.get("ok") and claim.output.get("status") != "planned":
            return claim
        merged = triage.merge_pull_request(
            _req(
                {
                    "repo": repo,
                    "number": number,
                    "head_oid": head_oid,
                    "dry_run": dry,
                },
                cfg,
            )
        )
        if not merged.output.get("ok") and merged.output.get("status") not in (
            "planned",
            "merged",
        ):
            return merged
        receipt_dir = str(
            cfg.get("merge_receipts")
            or Path.home() / ".hermes/state/repo-agent-merge-live"
        )
        path = str(Path(receipt_dir) / f"merge-pr-{number}.json")
        receipt = triage.write_merge_receipt(
            _req(
                {
                    "receipt_path": path,
                    "payload": {
                        "repo": repo,
                        "pr": number,
                        "head_oid": head_oid,
                        "phase": "MERGED",
                        "dry_run": dry,
                    },
                    "dry_run": dry,
                },
                cfg,
            )
        )
        closed = None
        if issue:
            closed = triage.close_linked_issue(
                _req({"repo": repo, "issue": issue, "dry_run": dry}, cfg)
            )
        return ok(
            status="merge_pipeline_done",
            action="merge",
            claim=claim.output,
            merge=merged.output,
            receipt=receipt.output,
            close=(closed.output if closed else None),
            dry_run=dry,
            mutated=not dry,
        )
    return fail("unknown_action", action=action)


# --- cleanup bridges ---


def cleanup_all_from_list(request: EffectorRunRequest) -> EffectorRunResult:
    """For each listed worktree, run cleanup_candidate_from_input safety pipeline."""
    cond = conduction_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    listed = cond.get("list_wts") or {}
    rows = listed.get("worktrees") or []
    clone_path = str(cfg.get("clone_path") or "")
    repo = str(cfg.get("repo") or "")
    cleaned: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        branch = str(row.get("branch") or "")
        path = str(row.get("path") or "")
        if not branch or not path:
            continue
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
        mutated=not dry and bool(cleaned),
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
        return fail("missing_branch")
    parsed = cleanup.parse_issue_from_branch(_req({"branch": branch}, cfg))
    if not parsed.output.get("ok"):
        return parsed
    issue = parsed.output.get("issue")
    closed = cleanup.check_issue_closed(
        _req({"repo": repo, "issue": issue}, cfg)
    )
    if not closed.output.get("ok"):
        return closed
    if not closed.output.get("closed"):
        return ok(
            status="skipped",
            reason="issue_still_open",
            issue=issue,
            dry_run=dry,
        )
    no_pr = cleanup.check_no_open_pr_for_branch(
        _req({"repo": repo, "branch": branch}, cfg)
    )
    if not no_pr.output.get("ok"):
        return no_pr
    if not no_pr.output.get("safe_to_cleanup"):
        return ok(
            status="skipped",
            reason="open_pr_remains",
            issue=issue,
            dry_run=dry,
        )
    removed = cleanup.remove_worktree(
        _req(
            {
                "clone_path": clone_path,
                "worktree_path": worktree_path,
                "force": False,
                "dry_run": dry,
            },
            cfg,
        )
    )
    deleted = cleanup.delete_local_fix_branch(
        _req(
            {
                "clone_path": clone_path,
                "branch": branch,
                "force": True,
                "dry_run": dry,
            },
            cfg,
        )
    )
    claim_path = str(
        cfg.get("active_issue_path")
        or Path.home() / ".hermes/state/repo-agent-active-live/active.json"
    )
    released = cleanup.release_active_issue_claim(
        _req(
            {
                "claim_path": claim_path,
                "repo": repo,
                "issue": str(issue),
                "dry_run": dry,
            },
            cfg,
        )
    )
    return ok(
        status="cleaned",
        issue=issue,
        branch=branch,
        remove=removed.output,
        delete_branch=deleted.output,
        release=released.output,
        dry_run=dry,
        mutated=not dry,
    )
