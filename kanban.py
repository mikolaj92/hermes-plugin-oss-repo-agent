from __future__ import annotations

from dataclasses import dataclass

from .schema import branch_for_issue, fix_key, issue_key, untrusted_github_block


PLUGIN_NAME = "oss-repo-agent"


@dataclass(frozen=True)
class TaskDraft:
    board: str
    title: str
    body: str
    idempotency_key: str
    skills: tuple[str, ...]
    workspace: str | None = None
    branch: str | None = None


def qualified_skill(name: str) -> str:
    return f"{PLUGIN_NAME}:{name}"


def issue_task(repo: str, board: str, number: int, title: str, body: str | None, clone_path: str | None) -> TaskDraft:
    return TaskDraft(
        board=board,
        title=f"[issue] {repo}#{number}: {title}",
        body="\n\n".join(
            (
                "Triage this issue using the repository policy. Do not create branches or pull requests from intake tasks.",
                untrusted_github_block(title, body),
            )
        ),
        idempotency_key=issue_key(repo, number),
        skills=(qualified_skill("repo-gh-cli-policy"), qualified_skill("repo-audit-finding-format")),
        workspace=f"dir:{clone_path}" if clone_path else None,
    )


def fix_task(repo: str, board: str, number: int, title: str, body: str | None, clone_path: str | None, branch_prefix: str) -> TaskDraft:
    branch = branch_for_issue(branch_prefix, repo, number)
    return TaskDraft(
        board=board,
        title=f"[fix-pr] {repo}#{number}: {title}",
        body="\n\n".join(
            (
                "Fix the approved issue in an isolated worktree. Inspect existing pull requests first. Do not merge, delete branches, force push, or expose secrets.",
                untrusted_github_block(title, body),
            )
        ),
        idempotency_key=fix_key(repo, number),
        skills=(qualified_skill("repo-gh-cli-policy"), qualified_skill("repo-fix-issue-pr")),
        workspace=f"worktree:{clone_path}" if clone_path else None,
        branch=branch,
    )
