from __future__ import annotations

import re

from .executor import CommandSpec, SafetyError, gh_spec


REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
API_ALLOWLIST = (
    re.compile(r"^/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[0-9]+/timeline$"),
)


def require_repo(repo: str) -> str:
    if not REPO_RE.match(repo):
        raise SafetyError(f"invalid repo: {repo}")
    return repo


def issue_list(repo: str, limit: int) -> CommandSpec:
    repo = require_repo(repo)
    return gh_spec(("issue", "list", "--repo", repo, "--state", "open", "--limit", str(limit), "--json", "number,title,url,labels,isLocked"))


def issue_view(repo: str, number: int) -> CommandSpec:
    repo = require_repo(repo)
    return gh_spec(("issue", "view", str(number), "--repo", repo, "--json", "number,title,body,url,labels,state"))


def pr_list(repo: str, limit: int = 50) -> CommandSpec:
    repo = require_repo(repo)
    return gh_spec(("pr", "list", "--repo", repo, "--state", "open", "--limit", str(limit), "--json", "number,title,author,headRefName,baseRefName,isDraft,labels,mergeStateStatus"))


def pr_view(repo: str, number: int) -> CommandSpec:
    repo = require_repo(repo)
    return gh_spec(("pr", "view", str(number), "--repo", repo, "--json", "number,title,body,author,headRefName,baseRefName,isDraft,labels,reviewDecision,mergeStateStatus"))


def pr_checks(repo: str, number: int) -> CommandSpec:
    repo = require_repo(repo)
    return gh_spec(("pr", "checks", str(number), "--repo", repo, "--json", "name,state,bucket"))


def api_get(endpoint: str) -> CommandSpec:
    if not any(pattern.match(endpoint) for pattern in API_ALLOWLIST):
        raise SafetyError(f"gh api endpoint is not allowlisted: {endpoint}")
    return gh_spec(("api", endpoint, "--method", "GET"))
