"""Mega-atomic effectors: PR triage domain."""

from __future__ import annotations

import json
from typing import Any

from fala.adapters import EffectorRunRequest, EffectorRunResult

from repo_agent.adapters_cli import CommandError, run_cmd
from repo_agent.envelope import (
    cfg_of,
    cond_blob,
    cond_get,
    dry_run_flag,
    fail,
    input_of,
    noop,
    ok,
    planned,
)


def list_ai_fix_prs(request: EffectorRunRequest) -> EffectorRunResult:
    """List open PRs with head branch matching ai/fix/* (or branch_prefix)."""
    data = input_of(request)
    cfg = cfg_of(request)
    repo = str(data.get("repo") or cfg.get("repo") or "")
    prefix = str(data.get("branch_prefix") or cfg.get("branch_prefix") or "ai/fix")
    limit = int(data.get("limit") or 50)
    gh = str(cfg.get("gh_cli") or "gh")
    if not repo:
        return fail("missing_repo")
    try:
        proc = run_cmd(
            [
                gh,
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--limit",
                str(limit),
                "--json",
                "number,title,url,headRefName,author,labels,mergeable,statusCheckRollup",
            ],
            timeout=90,
        )
        prs = json.loads(proc.stdout or "[]")
    except (CommandError, json.JSONDecodeError) as exc:
        return fail("pr_list_failed", error=str(exc))
    selected = [
        p
        for p in prs
        if str(p.get("headRefName") or "").startswith(prefix)
    ]
    return ok(
        status="listed",
        repo=repo,
        count=len(selected),
        prs=selected,
        all_open_count=len(prs),
    )


def load_pr_fields(request: EffectorRunRequest) -> EffectorRunResult:
    """Load one PR JSON bundle for triage decisions."""
    data = input_of(request)
    cfg = cfg_of(request)
    listed = cond_blob(request, "list_ai_fix_prs", "list")
    repo = str(data.get("repo") or listed.get("repo") or cfg.get("repo") or "")
    number = int(data.get("number") or data.get("pr_number") or 0)
    if not number:
        prs = listed.get("prs") or []
        if isinstance(prs, list) and prs:
            first = prs[0] if isinstance(prs[0], dict) else {}
            number = int(first.get("number") or 0)
    gh = str(cfg.get("gh_cli") or "gh")
    if not repo or not number:
        return fail("missing_repo_or_number")
    try:
        proc = run_cmd(
            [
                gh,
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "number,title,url,body,state,headRefName,headRefOid,baseRefName,"
                "author,labels,mergeable,reviewDecision,statusCheckRollup,commits",
            ],
            timeout=60,
        )
        pr = json.loads(proc.stdout or "{}")
    except (CommandError, json.JSONDecodeError) as exc:
        return fail("pr_view_failed", error=str(exc))
    return ok(status="loaded", pr=pr, repo=repo, number=number)


def evaluate_checks(request: EffectorRunRequest) -> EffectorRunResult:
    """Pure decision: do status checks pass? (from pr.statusCheckRollup)."""
    data = input_of(request)
    pr = data.get("pr") or cond_get(request, "pr", "load_pr_fields") or {}
    allow_no_checks = bool(data.get("allow_no_checks", True))
    rollup = pr.get("statusCheckRollup") or []
    if not rollup:
        if allow_no_checks:
            return ok(status="no_checks", pass_=True, allow_no_checks=True)
        return ok(status="no_checks", pass_=False, allow_no_checks=False)
    failures = []
    pending = []
    for item in rollup:
        if not isinstance(item, dict):
            continue
        conclusion = str(item.get("conclusion") or item.get("state") or "").upper()
        name = str(item.get("name") or item.get("context") or "?")
        if conclusion in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"):
            failures.append(name)
        elif conclusion in ("PENDING", "IN_PROGRESS", "QUEUED", ""):
            # GitHub sometimes uses state SUCCESS
            state = str(item.get("state") or "").upper()
            if state in ("PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED"):
                pending.append(name)
            elif state == "FAILURE":
                failures.append(name)
    if failures:
        return ok(status="checks_failed", pass_=False, failures=failures, pending=pending)
    if pending:
        return ok(status="checks_pending", pass_=False, pending=pending)
    return ok(status="checks_passed", pass_=True)


def evaluate_test_evidence(request: EffectorRunRequest) -> EffectorRunResult:
    """Pure: does PR body contain test evidence markers?"""
    data = input_of(request)
    pr = data.get("pr") or cond_get(request, "pr", "load_pr_fields") or {}
    require = bool(
        data.get("require_test_evidence", cfg_of(request).get("require_test_evidence", False))
    )
    body = str(pr.get("body") or "")
    markers = data.get("markers") or [
        "Test plan",
        "test evidence",
        "How to test",
        "pytest",
        "unittest",
        "Verified",
    ]
    hits = [m for m in markers if m.lower() in body.lower()]
    present = bool(hits)
    if not require:
        return ok(status="evidence_optional", pass_=True, present=present, hits=hits)
    if present:
        return ok(status="evidence_present", pass_=True, hits=hits)
    return ok(status="evidence_missing", pass_=False, hits=[])


def decide_triage_action(request: EffectorRunRequest) -> EffectorRunResult:
    """Pure router decision: merge | comment_block | repair | skip."""
    data = input_of(request)
    cfg = cfg_of(request)
    pr = data.get("pr") or cond_get(request, "pr", "load_pr_fields") or {}
    checks = cond_blob(request, "evaluate_checks", "checks")
    evidence = cond_blob(request, "evaluate_test_evidence", "evidence")
    checks_pass = bool(
        data.get(
            "checks_pass",
            data.get("pass_", checks.get("pass_", checks.get("pass"))),
        )
    )
    evidence_pass = bool(
        data.get(
            "evidence_pass",
            evidence.get("pass_", evidence.get("pass", True)),
        )
    )
    automerge = bool(data.get("automerge", cfg.get("automerge", False)))
    mergeable = str(pr.get("mergeable") or "").upper()
    state = str(pr.get("state") or "").upper()
    labels = {
        str(x.get("name") or "")
        for x in (pr.get("labels") or [])
        if isinstance(x, dict)
    }
    if state and state != "OPEN":
        return ok(status="decided", action="skip", reason=f"state_{state.lower()}")
    if "ai:blocked" in labels:
        return ok(status="decided", action="skip", reason="ai_blocked_label")
    if not checks_pass:
        # failing checks → repair if agent-owned, else comment
        return ok(status="decided", action="repair", reason="checks_not_green")
    if not evidence_pass:
        return ok(status="decided", action="comment_block", reason="missing_test_evidence")
    if mergeable == "CONFLICTING":
        return ok(status="decided", action="repair", reason="merge_conflict")
    if automerge and mergeable in ("MERGEABLE", "UNKNOWN", ""):
        return ok(status="decided", action="merge", reason="ready")
    if automerge is False:
        return ok(status="decided", action="comment_block", reason="automerge_disabled")
    return ok(status="decided", action="skip", reason="not_mergeable", mergeable=mergeable)


def claim_pr_assignee(request: EffectorRunRequest) -> EffectorRunResult:
    """Assign PR to configured maintainer once."""
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    loaded = cond_blob(request, "load_pr_fields", "list_ai_fix_prs")
    pr = data.get("pr") or loaded.get("pr") or {}
    repo = str(data.get("repo") or loaded.get("repo") or cfg.get("repo") or "")
    number = int(
        data.get("number")
        or data.get("pr_number")
        or loaded.get("number")
        or (pr.get("number") if isinstance(pr, dict) else 0)
        or 0
    )
    assignee = str(data.get("assignee") or cfg.get("assignee") or "mikolaj92")
    gh = str(cfg.get("gh_cli") or "gh")
    if not repo or not number:
        return fail("missing_repo_or_number")
    if dry:
        return planned(repo=repo, number=number, assignee=assignee)
    try:
        run_cmd(
            [
                gh,
                "pr",
                "edit",
                str(number),
                "--repo",
                repo,
                "--add-assignee",
                assignee,
            ],
            timeout=60,
        )
    except CommandError as exc:
        return fail("claim_failed", error=str(exc))
    return ok(status="claimed", repo=repo, number=number, assignee=assignee, mutated=True)


def comment_pr_once(request: EffectorRunRequest) -> EffectorRunResult:
    """Post one PR comment body (caller ensures once-semantics via receipt/key)."""
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    loaded = cond_blob(request, "load_pr_fields", "decide_triage_action")
    decide = cond_blob(request, "decide_triage_action", "decide")
    pr = data.get("pr") or loaded.get("pr") or {}
    repo = str(data.get("repo") or loaded.get("repo") or cfg.get("repo") or "")
    number = int(
        data.get("number")
        or loaded.get("number")
        or (pr.get("number") if isinstance(pr, dict) else 0)
        or 0
    )
    body = str(data.get("body") or "")
    if not body:
        reason = decide.get("reason") or "needs human review"
        body = (
            f"repo-agent triage: action=comment_block reason={reason}. "
            f"Please add test evidence or address blockers."
        )
    gh = str(cfg.get("gh_cli") or "gh")
    if not repo or not number or not body:
        return fail("missing_repo_number_or_body")
    if dry:
        return planned(repo=repo, number=number, body=body[:200])
    try:
        run_cmd(
            [gh, "pr", "comment", str(number), "--repo", repo, "--body", body],
            timeout=60,
        )
    except CommandError as exc:
        return fail("comment_failed", error=str(exc))
    return ok(status="commented", repo=repo, number=number, mutated=True)


def merge_pull_request(request: EffectorRunRequest) -> EffectorRunResult:
    """Merge PR with optional --match-head-commit."""
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    loaded = cond_blob(request, "load_pr_fields", "claim_pr", "claim_pr_assignee")
    pr = data.get("pr") or loaded.get("pr") or {}
    repo = str(data.get("repo") or loaded.get("repo") or cfg.get("repo") or "")
    number = int(
        data.get("number")
        or loaded.get("number")
        or (pr.get("number") if isinstance(pr, dict) else 0)
        or 0
    )
    head_oid = str(
        data.get("head_oid")
        or data.get("headRefOid")
        or (pr.get("headRefOid") if isinstance(pr, dict) else "")
        or ""
    )
    method = str(data.get("merge_method") or cfg.get("merge_method") or "merge")
    gh = str(cfg.get("gh_cli") or "gh")
    if not repo or not number:
        return fail("missing_repo_or_number")
    if dry:
        return planned(repo=repo, number=number, head_oid=head_oid, method=method)
    args = [gh, "pr", "merge", str(number), "--repo", repo, f"--{method}"]
    if head_oid:
        args += ["--match-head-commit", head_oid]
    try:
        run_cmd(args, timeout=120)
    except CommandError as exc:
        return fail("merge_failed", error=str(exc), stderr=exc.stderr[-400:])
    return ok(status="merged", repo=repo, number=number, mutated=True)


def close_linked_issue(request: EffectorRunRequest) -> EffectorRunResult:
    """Close GitHub issue after merge."""
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    loaded = cond_blob(request, "load_pr_fields", "merge", "merge_pull_request")
    pr = data.get("pr") or loaded.get("pr") or {}
    repo = str(data.get("repo") or loaded.get("repo") or cfg.get("repo") or "")
    issue = int(data.get("issue") or data.get("number") or 0)
    if not issue and isinstance(pr, dict):
        # Try branch name ai/fix/<n>-...
        import re

        head = str(pr.get("headRefName") or "")
        m = re.search(r"(?:^|/)ai/fix/(\d+)", head)
        if m:
            issue = int(m.group(1))
    gh = str(cfg.get("gh_cli") or "gh")
    if not repo or not issue:
        return fail("missing_repo_or_issue")
    if dry:
        return planned(repo=repo, issue=issue)
    try:
        run_cmd(
            [
                gh,
                "issue",
                "close",
                str(issue),
                "--repo",
                repo,
                "--reason",
                "completed",
            ],
            timeout=60,
        )
    except CommandError as exc:
        return fail("close_failed", error=str(exc))
    return ok(status="closed", repo=repo, issue=issue, mutated=True)


def write_merge_receipt(request: EffectorRunRequest) -> EffectorRunResult:
    """Write merge receipt JSON atomically."""
    import os
    from pathlib import Path

    data = input_of(request)
    dry = dry_run_flag(request)
    path = str(data.get("receipt_path") or cfg_of(request).get("receipt_path") or "")
    payload = data.get("payload")
    if not isinstance(payload, dict) or not payload:
        merge = cond_blob(request, "merge", "merge_pull_request")
        claim = cond_blob(request, "claim_pr", "claim_pr_assignee")
        payload = {
            "phase": "MERGED",
            "repo": merge.get("repo") or claim.get("repo"),
            "number": merge.get("number") or claim.get("number"),
            "dry_run": dry,
            "merge_status": merge.get("status"),
        }
    if not path:
        return fail("missing_receipt_path")
    if dry:
        return planned(receipt_path=path, payload=payload)
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        with tmp.open("r+b") as fh:
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(p)
    except OSError as exc:
        return fail("receipt_write_failed", error=str(exc))
    return ok(status="written", receipt_path=path, mutated=True)
