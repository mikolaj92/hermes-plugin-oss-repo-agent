from __future__ import annotations

import re

from .executor import CommandSpec, SafetyError, gh_spec


REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ASSIGNEE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
API_ALLOWLIST = (
    re.compile(r"^/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[0-9]+/timeline$"),
)


def require_repo(repo: str) -> str:
    if not REPO_RE.match(repo):
        raise SafetyError(f"invalid repo: {repo}")
    return repo


def require_assignee(assignee: str) -> str:
    if not ASSIGNEE_RE.match(assignee):
        raise SafetyError(f"invalid assignee: {assignee}")
    return assignee


def issue_list(repo: str, limit: int) -> CommandSpec:
    repo = require_repo(repo)
    return gh_spec(("issue", "list", "--repo", repo, "--state", "open", "--limit", str(limit), "--json", "number,title,url,labels,isLocked"))


def issue_view(repo: str, number: int) -> CommandSpec:
    repo = require_repo(repo)
    return gh_spec(("issue", "view", str(number), "--repo", repo, "--json", "number,title,body,url,labels,state"))


def issue_claim(repo: str, number: int, assignee: str, ready_label: str | None = None) -> CommandSpec:
    repo = require_repo(repo)
    assignee = require_assignee(assignee)
    args = ["issue", "edit", str(number), "--repo", repo, "--add-assignee", assignee]
    if ready_label:
        args.extend(("--add-label", ready_label))
    return gh_spec(tuple(args))


def pr_list(repo: str, limit: int = 50) -> CommandSpec:
    repo = require_repo(repo)
    return gh_spec(("pr", "list", "--repo", repo, "--state", "open", "--limit", str(limit), "--json", "number,title,author,headRefName,baseRefName,isDraft,labels,mergeStateStatus"))


def pr_view(repo: str, number: int) -> CommandSpec:
    repo = require_repo(repo)
    return gh_spec(("pr", "view", str(number), "--repo", repo, "--json", "number,title,body,author,headRefName,baseRefName,isDraft,labels,reviewDecision,mergeStateStatus"))


def pr_claim(repo: str, number: int, assignee: str) -> CommandSpec:
    repo = require_repo(repo)
    assignee = require_assignee(assignee)
    return gh_spec(("pr", "edit", str(number), "--repo", repo, "--add-assignee", assignee))


def pr_checks(repo: str, number: int) -> CommandSpec:
    repo = require_repo(repo)
    return gh_spec(("pr", "checks", str(number), "--repo", repo, "--json", "name,state,bucket"))


def api_get(endpoint: str) -> CommandSpec:
    if not any(pattern.match(endpoint) for pattern in API_ALLOWLIST):
        raise SafetyError(f"gh api endpoint is not allowlisted: {endpoint}")
    return gh_spec(("api", endpoint, "--method", "GET"))
