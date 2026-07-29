
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from lokay.envelope import Request, Result

from lokay.adapters_cli import CommandError, run_cmd
from lokay.steps.issue_triage import triage_precedence_action
from lokay.envelope import (
    cfg_of,
    cond_blob,
    cond_get,
    conduction_of,
    dry_run_flag,
    fail,
    input_of,
    noop,
    ok,
    planned,
    terminal_upstream,
    upstream_noop,
)

_DEFAULT_REJECT_LABELS = ("ai:out-of-scope", "wontfix", "invalid")
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "for",
        "from",
        "in",
        "into",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
        "via",
        "by",
        "is",
        "are",
        "be",
        "this",
        "that",
        "as",
        "at",
        "it",
    }
)


def _tokens(text: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-z0-9][a-z0-9._-]{1,}", (text or "").lower())
        if t not in _STOPWORDS and len(t) > 1
    }


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _issue_text(issue: dict[str, Any]) -> str:
    return " ".join(
        str(issue.get(k) or "")
        for k in ("title", "body", "bodyText")
    )



_PR_PRIORITY_ACTIONS = frozenset({"merge", "comment_block", "repair", "skip"})
_PR_PRIORITY_CONDUCTED = ("triage_decide_triage_action", "decide_triage_action")


def _priority_decisions(request: Request) -> list[tuple[str, Any]]:
    """Collect explicit and suffix-matched conducted priority decisions."""
    data = input_of(request)
    decisions: list[tuple[str, Any]] = []
    if "pr_priority_decision" in data:
        decisions.append(("pr_priority_decision", data["pr_priority_decision"]))
    conduction = conduction_of(request)
    for name, value in conduction.items():
        if name in _PR_PRIORITY_CONDUCTED or any(
            name.endswith(f"_{alias}") for alias in _PR_PRIORITY_CONDUCTED
        ):
            decisions.append((name, value))
    return decisions


def decide_issue_priority(request: Request) -> Result:
    """Purely gate issue intake on authoritative PR triage priority."""
    terminal = terminal_upstream(
        request,
        "decide_issue_priority",
        *_PR_PRIORITY_CONDUCTED,
    )
    if terminal is not None:
        return terminal

    data = input_of(request)
    decisions = _priority_decisions(request)
    selected: Any = data.get("selected")
    if selected is None:
        selected = cond_get(request, "selected", "select_issue_candidate", default=None)
    if selected is None:
        for _, candidate in decisions:
            if isinstance(candidate, Mapping) and candidate.get("selected") is not None:
                selected = candidate.get("selected")
                break
    identity = {}
    if isinstance(selected, Mapping):
        identity = {
            key: selected[key]
            for key in ("repo", "number", "issue")
            if key in selected
        }
    if not decisions:
        return ok(status="decided", action="allow", reason="no_pr_priority_decision", selected=selected, **identity)

    actions: list[str] = []
    normalized: list[dict[str, Any]] = []
    for source, decision in decisions:
        if not isinstance(decision, Mapping):
            return fail(
                "invalid_pr_priority_decision",
                failure_class="terminal",
                retry_safe=False,
                source=source,
                selected=selected,
                **identity,
            )
        blob = dict(decision)
        if (
            blob.get("ok") is True
            and blob.get("status") == "noop"
            and blob.get("reason") in {"no_open_prs", "no_selected_pr", "not_selected"}
            and blob.get("action") in (None, "", "skip")
        ):
            actions.append("skip")
            normalized.append({**blob, "action": "skip"})
            continue
        action = blob.get("action")
        if (
            blob.get("ok") is not True
            or blob.get("status") not in {"decided", "noop"}
            or not isinstance(action, str)
            or action not in _PR_PRIORITY_ACTIONS
        ):
            return fail(
                "invalid_pr_priority_decision",
                failure_class="terminal",
                retry_safe=False,
                source=source,
                decision=blob,
                selected=selected,
                **identity,
            )
        actions.append(action)
        normalized.append(blob)
    if len(set(actions)) > 1:
        return fail(
            "invalid_pr_priority_decision",
            failure_class="terminal",
            retry_safe=False,
            reason_detail="contradictory_actions",
            actions=actions,
            selected=selected,
            **identity,
        )

    action = actions[0]
    if action == "repair":
        return noop(
            "pr_priority_repair_required",
            action="repair",
            priority_action="repair",
            blocked=True,
            selected=selected,
            **identity,
        )
    return ok(
        status="decided",
        action="allow",
        priority_action=action,
        decision=normalized[-1],
        selected=selected,
        **identity,
    )


def decide_issue_action(request: Request) -> Result:
    """Pure router: accept | reject_comment | skip for one selected issue.

    Alignment rules (first match wins after empty/noop checks):
    - reject labels (ai:out-of-scope / wontfix / invalid / configured)
    - deny keywords in title/body
    - require keywords when configured (must hit at least one)
    - repo_goal token overlap when goal is configured
    - empty title → reject_comment
    - otherwise accept (including when no direction policy is configured)
    """
    priority_failure = terminal_upstream(request, "decide_issue_action", "decide_issue_priority")
    if priority_failure is not None:
        return priority_failure
    priority_block = upstream_noop(request, "decide_issue_priority")
    if priority_block:
        return noop(
            str(priority_block.get("reason") or "pr_priority_repair_required"),
            action="skip",
            priority_action=priority_block.get("priority_action") or priority_block.get("action"),
            blocked=True,
            selected=priority_block.get("selected"),
        )
    data = input_of(request)
    cfg = cfg_of(request)
    upstream = upstream_noop(request, "select_issue_candidate")
    if upstream:
        return noop(str(upstream.get("reason") or "no_selected_issue"))
    selected = data.get("selected") or cond_blob(request, "select_issue_candidate").get("selected")
    if not selected:
        return noop("no_selected_issue", action="skip")
    if not isinstance(selected, dict):
        return fail(
            "invalid_selected_issue",
            failure_class="terminal",
            retry_safe=False,
            selected=selected,
        )
    triage_action = triage_precedence_action(selected)
    if triage_action is not None:
        return ok(
            status="decided",
            action=triage_action["action"],
            reason=triage_action["reason"],
            selected=selected,
            repo=selected.get("repo"),
            number=selected.get("number"),
        )

    title = str(selected.get("title") or "").strip()
    body = str(selected.get("body") or selected.get("bodyText") or "")
    labels: set[str] = set()
    for item in selected.get("labels") or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip().lower()
        else:
            name = str(item or "").strip().lower()
        if name:
            labels.add(name)
    text = f"{title}\n{body}".lower()
    text_tokens = _tokens(f"{title} {body}")

    reject_labels = {
        x.lower()
        for x in (
            _as_str_list(data.get("direction_reject_labels"))
            or _as_str_list(cfg.get("direction_reject_labels"))
            or list(_DEFAULT_REJECT_LABELS)
        )
    }
    deny = [
        x.lower()
        for x in (
            _as_str_list(data.get("direction_deny_keywords"))
            or _as_str_list(cfg.get("direction_deny_keywords"))
        )
    ]
    require = [
        x.lower()
        for x in (
            _as_str_list(data.get("direction_require_keywords"))
            or _as_str_list(cfg.get("direction_require_keywords"))
        )
    ]
    repo_goal = str(data.get("repo_goal") or cfg.get("repo_goal") or "").strip()
    min_overlap = int(data.get("direction_min_goal_overlap") or cfg.get("direction_min_goal_overlap") or 1)

    hit_reject = sorted(labels & reject_labels)
    if hit_reject:
        return ok(
            status="decided",
            action="reject_comment",
            reason="out_of_direction_label",
            labels=hit_reject,
            selected=selected,
            repo=selected.get("repo"),
            number=selected.get("number"),
        )
    if not title:
        return ok(
            status="decided",
            action="reject_comment",
            reason="empty_title",
            selected=selected,
            repo=selected.get("repo"),
            number=selected.get("number"),
        )
    for kw in deny:
        if kw and kw in text:
            return ok(
                status="decided",
                action="reject_comment",
                reason="deny_keyword",
                keyword=kw,
                selected=selected,
                repo=selected.get("repo"),
                number=selected.get("number"),
            )
    if require:
        if not any(kw in text for kw in require):
            return ok(
                status="decided",
                action="reject_comment",
                reason="missing_require_keyword",
                require=require,
                selected=selected,
                repo=selected.get("repo"),
                number=selected.get("number"),
            )
    if repo_goal:
        goal_tokens = _tokens(repo_goal)
        overlap = sorted(goal_tokens & text_tokens)
        if len(overlap) < max(1, min_overlap):
            return ok(
                status="decided",
                action="reject_comment",
                reason="out_of_direction_goal",
                repo_goal=repo_goal,
                overlap=overlap,
                selected=selected,
                repo=selected.get("repo"),
                number=selected.get("number"),
            )
        return ok(
            status="decided",
            action="accept",
            reason="goal_aligned",
            overlap=overlap,
            selected=selected,
            repo=selected.get("repo"),
            number=selected.get("number"),
        )

    if not require and not deny and not repo_goal:
        return ok(
            status="decided",
            action="accept",
            reason="direction_not_configured",
            selected=selected,
            repo=selected.get("repo"),
            number=selected.get("number"),
        )
    return ok(
        status="decided",
        action="accept",
        reason="direction_ok",
        selected=selected,
        repo=selected.get("repo"),
        number=selected.get("number"),
    )


def read_issue_comments(request: Request) -> Result:
    """Read comments for one issue; no policy or mutation."""
    from lokay.envelope import terminal_upstream
    terminal = terminal_upstream(request, "read_issue_comments", "select_issue_candidate", "decide_issue_action")
    if terminal:
        return terminal
    idle = upstream_noop(request, "select_issue_candidate", "decide_issue_action")
    if idle:
        return noop(str(idle.get("reason") or "no_selected_issue"), operation="read_issue_comments")
    data = input_of(request); cfg = cfg_of(request); selected = data.get("selected") or cond_blob(request, "select_issue_candidate", "decide_issue_action").get("selected") or {}
    if not isinstance(selected, dict): selected = {}
    repo = str(data.get("repo") or selected.get("repo") or cfg.get("repo") or ""); number = data.get("number") or data.get("issue") or selected.get("number") or 0; gh = str(cfg.get("gh_cli") or "gh")
    context={"repo":repo,"number":number,"comment_marker":str(data.get("comment_marker") or "")}
    if not repo or isinstance(number, bool) or not isinstance(number, int) or number <= 0: return fail("missing_repo_or_number", failure_class="terminal", retry_safe=False, **context)
    try: view=run_cmd([gh,"issue","view",str(number),"--repo",repo,"--json","comments"],timeout=60)
    except CommandError as exc: return fail("comment_read_failed",failure_class="retryable_read",retry_safe=True,error=str(exc),mutated=False,**context)
    import json
    try: payload=json.loads((getattr(view,"stdout","") or "").strip())
    except (TypeError,ValueError,json.JSONDecodeError) as exc: return fail("comment_read_failed",failure_class="terminal",retry_safe=False,error=str(exc),mutated=False,**context)
    comments=payload.get("comments") if isinstance(payload,dict) else payload
    if not isinstance(comments,list) or any(not isinstance(item,dict) for item in comments): return fail("comment_read_failed",failure_class="terminal",retry_safe=False,error="invalid comment read-back shape",mutated=False,**context)
    return ok(status="comments_read",comments=comments,selected=selected,dry_run=dry_run_flag(request),**context)


def decide_issue_comment(request: Request) -> Result:
    """Purely decide whether the marker already exists."""
    from lokay.envelope import cond_blob, terminal_upstream
    terminal=terminal_upstream(request,"decide_issue_comment","read_issue_comments")
    if terminal: return terminal
    idle = upstream_noop(request, "read_issue_comments", "select_issue_candidate", "decide_issue_action")
    if idle:
        return noop(str(idle.get("reason") or "no_selected_issue"), operation="decide_issue_comment")
    data=input_of(request); read=cond_blob(request,"read_issue_comments"); comments=data.get("comments") if isinstance(data.get("comments"),list) else read.get("comments",[]); marker=str(data.get("comment_marker") or read.get("comment_marker") or "")
    if not isinstance(comments,list) or any(not isinstance(x,dict) for x in comments): return fail("malformed_comments",failure_class="terminal",retry_safe=False,mutated=False)
    matches=sum(str(x.get("body") or "").count(f"<!-- {marker} -->") for x in comments) if marker else 0
    if matches>1: return fail("comment_marker_conflict",failure_class="terminal",retry_safe=False,matches=matches,mutated=False)
    return ok(status="comment_decided",should_post=matches==0,already_posted=matches==1,matches=matches,comment_marker=marker,selected=read.get("selected"),dry_run=dry_run_flag(request))


def post_issue_comment(request: Request) -> Result:
    """Post one comment; verification is a separate process."""
    from lokay.envelope import cond_blob, terminal_upstream
    terminal=terminal_upstream(request,"post_issue_comment","decide_issue_comment")
    if terminal: return terminal
    idle = upstream_noop(request, "decide_issue_comment", "read_issue_comments")
    if idle:
        return noop(str(idle.get("reason") or "no_selected_issue"), operation="post_issue_comment")
    if terminal: return terminal
    data=input_of(request); decide=cond_blob(request,"decide_issue_comment"); read=cond_blob(request,"read_issue_comments"); selected=data.get("selected") or read.get("selected") or {}; cfg=cfg_of(request); dry=dry_run_flag(request)
    should=decide.get("should_post", data.get("should_post", True)); marker=str(data.get("comment_marker") or decide.get("comment_marker") or read.get("comment_marker") or ""); repo=str(data.get("repo") or read.get("repo") or (selected.get("repo") if isinstance(selected,dict) else "")); number=data.get("number") or read.get("number") or (selected.get("number") if isinstance(selected,dict) else 0); reason=str(data.get("reason") or "out_of_direction"); body=str(data.get("body") or f"lokay intake: skipping this issue (reason={reason})."); context={"repo":repo,"number":number,"comment_marker":marker,"reason":reason,"idempotency_key":f"issue:{repo}:{number}:comment:{marker}"}
    if not should: return noop("already_commented",dry_run=dry,**{key: value for key, value in context.items() if key != "reason"})
    if not repo or isinstance(number,bool) or not isinstance(number,int) or number<=0: return fail("missing_repo_or_number",failure_class="terminal",retry_safe=False,**{key: value for key, value in context.items() if key != "reason"})
    if dry: return planned(**context,body=body[:240])
    gh=str(cfg.get("gh_cli") or "gh"); hidden=f"<!-- {marker} -->"; posted=body if hidden in body else f"{body.rstrip()}\n\n{hidden}"
    try: proc=run_cmd([gh,"issue","comment",str(number),"--repo",repo,"--body",posted],timeout=60)
    except CommandError as exc: return fail("comment_failed",failure_class="reconcile_then_retry",retry_safe=False,error=str(exc),mutated=True,**{key: value for key, value in context.items() if key != "reason"})
    return ok(status="comment_posted",mutated=True,stdout=(getattr(proc,"stdout","") or "")[-500:],body=body[:240],**context)


def verify_issue_comment(request: Request) -> Result:
    """Read back and verify the posted marker."""
    from lokay.envelope import cond_blob, terminal_upstream
    terminal=terminal_upstream(request,"verify_issue_comment","post_issue_comment")
    if terminal: return terminal
    data=input_of(request); post=cond_blob(request,"post_issue_comment"); read=cond_blob(request,"read_issue_comments"); repo=str(data.get("repo") or post.get("repo") or read.get("repo") or ""); number=data.get("number") or post.get("number") or read.get("number") or 0; marker=str(data.get("comment_marker") or post.get("comment_marker") or read.get("comment_marker") or ""); cfg=cfg_of(request); dry=dry_run_flag(request)
    if dry or post.get("status")=="noop": return ok(status="comment_verified",verified=False,mutated=False,dry_run=dry,**{"repo":repo,"number":number,"comment_marker":marker})
    try: view=run_cmd([str(cfg.get("gh_cli") or "gh"),"issue","view",str(number),"--repo",repo,"--json","comments"],timeout=60)
    except CommandError as exc: return fail("comment_verify_read_failed",failure_class="retryable_read",retry_safe=True,error=str(exc),mutated=True,**{"repo":repo,"number":number,"comment_marker":marker})
    import json
    try: payload=json.loads((getattr(view,"stdout","") or "").strip()); comments=payload.get("comments") if isinstance(payload,dict) else payload
    except (TypeError,ValueError,json.JSONDecodeError) as exc: return fail("comment_verify_read_failed",failure_class="terminal",retry_safe=False,error=str(exc),mutated=True,**{"repo":repo,"number":number,"comment_marker":marker})
    matches=sum(str(x.get("body") or "").count(f"<!-- {marker} -->") for x in comments) if isinstance(comments,list) else 0
    if matches!=1: return fail("comment_verify_mismatch",failure_class="reconcile_then_retry",retry_safe=False,matches=matches,mutated=True,**{"repo":repo,"number":number,"comment_marker":marker})
    return ok(status="comment_verified",verified=True,mutated=True,repo=repo,number=number,comment_marker=marker)
