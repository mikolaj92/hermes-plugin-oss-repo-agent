"""Issue-side direction triage: sense/align gate + durable reject comments.

Pure decide outcomes (accept | reject_comment | skip) stay separable from gh I/O
so unit tests drive the real function. Reject is never a silent drop: callers
must run ``comment_issue_once`` when action is ``reject_comment``.
"""

from __future__ import annotations

import re
from typing import Any

from repo_agent.envelope import Request, Result

from repo_agent.adapters_cli import CommandError, run_cmd
from repo_agent.envelope import (
    cfg_of,
    cond_blob,
    dry_run_flag,
    fail,
    input_of,
    noop,
    ok,
    planned,
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
    data = input_of(request)
    cfg = cfg_of(request)
    upstream = upstream_noop(request, "poll", "poll_eligible_issues", "intake_poll")
    if upstream:
        return noop(str(upstream.get("reason") or "no_selected_issue"))
    poll = cond_blob(request, "poll", "poll_eligible_issues", "intake_poll")
    selected = data.get("selected") or poll.get("selected")
    if not selected:
        return noop("no_selected_issue", action="skip")
    if not isinstance(selected, dict):
        return fail(
            "invalid_selected_issue",
            failure_class="terminal",
            retry_safe=False,
            selected=selected,
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


def comment_issue_once(request: Request) -> Result:
    """Post one durable reject/triage comment on an issue (idempotent marker).

    No-ops unless decide_issue_action (or input) says action=reject_comment.
    """
    data = input_of(request)
    cfg = cfg_of(request)
    dry = dry_run_flag(request)
    decide = cond_blob(request, "decide_issue_action", "decide", "intake_decide_issue_action")
    poll = cond_blob(request, "poll", "poll_eligible_issues", "intake_poll")
    action = str(data.get("action") or decide.get("action") or "")
    if action and action != "reject_comment":
        return noop(
            "not_reject",
            action=action,
            decide_reason=decide.get("reason"),
            dry_run=dry,
        )
    if not action and not data.get("body"):
        # No decide context and no forced body → nothing to post
        return noop("no_reject_decision", dry_run=dry)

    selected = (
        data.get("selected")
        or decide.get("selected")
        or poll.get("selected")
        or {}
    )
    if not isinstance(selected, dict):
        selected = {}
    repo = str(data.get("repo") or decide.get("repo") or selected.get("repo") or cfg.get("repo") or "")
    number = int(
        data.get("number")
        or data.get("issue")
        or decide.get("number")
        or selected.get("number")
        or 0
    )
    reason = str(data.get("reason") or decide.get("reason") or "out_of_direction")
    body = str(data.get("body") or "")
    if not body:
        body = (
            f"repo-agent intake: skipping this issue (reason={reason}). "
            f"It does not appear to align with the configured repository direction/goal. "
            f"Adjust the issue (title/body/labels) or the agent direction config if this is wrong."
        )
    gh = str(cfg.get("gh_cli") or "gh")
    marker = f"repo-agent:{repo or 'unknown'}:{number or 'unknown'}:issue-direction"
    hidden = f"<!-- {marker} -->"
    key = f"issue:{repo or 'unknown'}:{number or 'unknown'}:comment:{marker}"
    context = {
        "repo": repo,
        "number": number,
        "comment_marker": marker,
        "idempotency_key": key,
        "reason": reason,
        "action": "reject_comment",
    }
    if not repo or not number:
        return fail("missing_repo_or_number", failure_class="terminal", retry_safe=False, **context)
    posted = body if hidden in body else f"{body.rstrip()}\n\n{hidden}"
    if dry:
        return planned(**context, body=body[:240])
    try:
        view = run_cmd(
            [gh, "issue", "view", str(number), "--repo", repo, "--json", "comments"],
            timeout=60,
        )
    except CommandError as exc:
        return fail(
            "comment_read_failed",
            failure_class="retryable_read",
            retry_safe=True,
            error=str(exc),
            mutated=False,
            **context,
        )
    raw = (getattr(view, "stdout", "") or "").strip()
    if not raw:
        return fail(
            "comment_read_failed",
            failure_class="terminal",
            retry_safe=False,
            error="blank comment read-back",
            mutated=False,
            **context,
        )
    import json

    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail(
            "comment_read_failed",
            failure_class="terminal",
            retry_safe=False,
            error=f"invalid comment JSON: {exc}",
            mutated=False,
            **context,
        )
    comments = payload.get("comments") if isinstance(payload, dict) else payload
    if not isinstance(comments, list) or any(not isinstance(item, dict) for item in comments):
        return fail(
            "comment_read_failed",
            failure_class="terminal",
            retry_safe=False,
            error="invalid comment read-back shape",
            mutated=False,
            **context,
        )
    matches = sum(str(item.get("body") or "").count(hidden) for item in comments)
    if matches > 1:
        return fail(
            "comment_marker_conflict",
            failure_class="terminal",
            retry_safe=False,
            matches=matches,
            mutated=False,
            **context,
        )
    if matches == 1:
        return ok(status="commented", reconciled=True, mutated=False, **context)
    try:
        run_cmd(
            [gh, "issue", "comment", str(number), "--repo", repo, "--body", posted],
            timeout=60,
        )
    except CommandError as exc:
        return fail(
            "comment_failed",
            failure_class="reconcile_then_retry",
            retry_safe=False,
            error=str(exc),
            mutated=True,
            **context,
        )
    return ok(status="commented", mutated=True, **context)
