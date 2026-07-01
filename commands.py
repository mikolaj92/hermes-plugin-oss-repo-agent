from __future__ import annotations

import json
from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import Any

from . import github_cli, kanban
from .config import ConfigError, OssRepoAgentConfig, default_config_path, load_config
from .executor import Runner, planned_command


def setup_parser(parser: ArgumentParser) -> None:
    parser.add_argument("--config", default=None)
    subparsers = parser.add_subparsers(dest="oss_repo_agent_command")
    subparsers.required = True
    init = subparsers.add_parser("init")
    init.add_argument("--repo", default="owner/example-repo")
    init.add_argument("--board", default="owner-example-repo")
    init.add_argument("--clone-root", default="./repos")
    init.add_argument("--worktree-root", default="./worktrees")
    init.add_argument("--assignee", default=None)
    init.add_argument("--force", action="store_true")
    subparsers.add_parser("validate")
    bootstrap = subparsers.add_parser("bootstrap")
    bootstrap.add_argument("--apply", action="store_true")
    intake = subparsers.add_parser("intake")
    intake.add_argument("--live", action="store_true")
    intake.add_argument("--limit", type=int, default=None)
    dispatch = subparsers.add_parser("dispatch")
    dispatch.add_argument("--live", action="store_true")
    dispatch.add_argument("--run-executor", action="store_true")
    dispatch.add_argument("--max", type=int, default=20)
    triage = subparsers.add_parser("pr-triage")
    triage.add_argument("--live", action="store_true")
    triage.add_argument("--comment", action="store_true")
    launchd = subparsers.add_parser("render-launchd")
    launchd.add_argument("--output", required=True)


def handle_cli(args: Namespace) -> int:
    try:
        result = run_from_args(args)
    except ConfigError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def run_from_args(args: Namespace, runner: Runner | None = None) -> dict[str, Any]:
    command = getattr(args, "oss_repo_agent_command")
    if command == "init":
        return init_project(
            getattr(args, "config", None),
            str(getattr(args, "repo", "owner/example-repo")),
            str(getattr(args, "board", "owner-example-repo")),
            str(getattr(args, "clone_root", "./repos")),
            str(getattr(args, "worktree_root", "./worktrees")),
            getattr(args, "assignee", None),
            bool(getattr(args, "force", False)),
        )
    cfg = load_config(getattr(args, "config", None))
    if command == "validate":
        return validate(cfg)
    if command == "bootstrap":
        return bootstrap(cfg, bool(getattr(args, "apply", False)))
    if command == "intake":
        limit = getattr(args, "limit", None) or cfg.github.default_limit
        return intake(cfg, bool(getattr(args, "live", False)), int(limit), runner or Runner())
    if command == "dispatch":
        return dispatch(cfg, bool(getattr(args, "live", False)), bool(getattr(args, "run_executor", False)), int(getattr(args, "max", 20)))
    if command == "pr-triage":
        return pr_triage(cfg, bool(getattr(args, "live", False)), bool(getattr(args, "comment", False)), runner or Runner())
    if command == "render-launchd":
        return render_launchd(cfg, str(getattr(args, "output")))
    raise ConfigError(f"unknown command: {command}")


def init_project(config_path: str | None, repo: str, board: str, clone_root: str, worktree_root: str, assignee: str | None, force: bool) -> dict[str, Any]:
    target = Path(config_path).expanduser() if config_path else default_config_path()
    if target.exists() and not force:
        raise ConfigError(f"config already exists: {target}; pass --force to overwrite")
    target.parent.mkdir(parents=True, exist_ok=True)
    leaf = repo.split("/")[-1] or "example-repo"
    text = starter_config(repo, board, clone_root, worktree_root, leaf, assignee or repo.split("/", 1)[0])
    target.write_text(text, encoding="utf-8")
    config_arg = str(target)
    return {
        "ok": True,
        "config": config_arg,
        "created": [config_arg],
        "next_commands": [
            f"hermes oss-repo-agent --config {config_arg} validate",
            f"hermes oss-repo-agent --config {config_arg} intake --limit 3",
            f"hermes oss-repo-agent --config {config_arg} dispatch --max 2",
        ],
        "safety": safety_guards(),
    }


def starter_config(repo: str, board: str, clone_root: str, worktree_root: str, leaf: str, assignee: str) -> str:
    clone_path = f"{clone_root.rstrip('/')}/{leaf}"
    return "\n".join([
        "version: 1",
        "mode: dry-run",
        f"clone_root: {clone_root}",
        f"worktree_root: {worktree_root}",
        "branch_prefix: ai/fix",
        "automerge: false",
        "github:",
        "  cli: gh",
        "  default_limit: 10",
        f"  assignee: {assignee}",
        "labels:",
        "  ready: ai:ready",
        "  in_progress: ai:in-progress",
        "  blocked: ai:blocked",
        "  pr_opened: ai:pr-opened",
        "  generated: ai:generated",
        "executor:",
        "  enabled: false",
        "  command: opencode",
        "  timeout_seconds: 1800",
        "repos:",
        f"  - repo: {repo}",
        f"    board: {board}",
        f"    clone_path: {clone_path}",
        "    trusted_authors: []",
        "    trusted_branch_prefixes: [ai/fix]",
        "    allowed_base_branches: [main]",
        "    external_pr_policy: block",
        "",
    ])


def safety_guards() -> list[str]:
    return [
        "dry-run unless config mode is live and --live or --apply is passed",
        "GitHub operations use gh CLI wrappers only",
        "GitHub issues are claimed before Kanban intake when github.assignee is configured",
        "GitHub content is treated as untrusted evidence",
        "no PR merge support in v0",
        "no force-push or branch deletion behavior",
    ]


def validate(cfg: OssRepoAgentConfig) -> dict[str, Any]:
    return {
        "ok": True,
        "mode": cfg.mode,
        "repos": [repo.repo for repo in cfg.repos],
        "automerge": cfg.automerge,
        "github_assignee": cfg.github.assignee,
        "safe_defaults": {
            "dry_run": cfg.mode == "dry-run",
            "automerge": cfg.automerge,
            "executor_enabled": cfg.executor.enabled,
        },
        "skills": [
            "oss-repo-agent:repo-gh-cli-policy",
            "oss-repo-agent:repo-audit-finding-format",
            "oss-repo-agent:repo-fix-issue-pr",
            "oss-repo-agent:repo-review-agent-pr",
        ],
    }


def bootstrap(cfg: OssRepoAgentConfig, apply: bool) -> dict[str, Any]:
    live = cfg.effective_live(apply)
    return {
        "ok": True,
        "effective_live": live,
        "planned_boards": [{"repo": repo.repo, "board": repo.board} for repo in cfg.repos],
        "message": "bootstrap renders board and label intent; mutation adapters are intentionally explicit",
    }


def _issue_labels(issue: dict[str, Any]) -> set[str]:
    return {str(label.get("name", "")) for label in issue.get("labels", []) if isinstance(label, dict)}


def _eligible_issue(issue: dict[str, Any], cfg: OssRepoAgentConfig) -> bool:
    labels = _issue_labels(issue)
    return not labels.intersection({cfg.labels.in_progress, cfg.labels.blocked, cfg.labels.pr_opened})


def _issue_rows(result_stdout: str) -> list[dict[str, Any]]:
    if not result_stdout.strip():
        return []
    data = json.loads(result_stdout)
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def intake(cfg: OssRepoAgentConfig, live_flag: bool, limit: int, runner: Runner) -> dict[str, Any]:
    live = cfg.effective_live(live_flag)
    list_commands = [github_cli.issue_list(repo.repo, limit) for repo in cfg.repos]
    list_results = [runner.run(command, live=live) for command in list_commands]
    mutation_commands = []
    mutation_results = []
    ensured_tasks = []
    if live:
        for repo, result in zip(cfg.repos, list_results):
            if result.returncode != 0:
                continue
            for issue in _issue_rows(result.stdout):
                if not _eligible_issue(issue, cfg):
                    continue
                number = int(issue.get("number", 0))
                if number <= 0:
                    continue
                title = str(issue.get("title") or "")
                body = f"GitHub issue: {issue.get('url') or ''}"
                if cfg.github.assignee:
                    claim = github_cli.issue_claim(repo.repo, number, cfg.github.assignee)
                    mutation_commands.append(claim)
                    claim_result = runner.run(claim, live=True)
                    mutation_results.append(claim_result)
                    if claim_result.returncode != 0:
                        continue
                draft = kanban.issue_task(repo.repo, repo.board, number, title, body, repo.clone_path)
                create = kanban.create_task_spec(draft, assignee="repo-orchestrator")
                mutation_commands.append(create)
                create_result = runner.run(create, live=True)
                mutation_results.append(create_result)
                if create_result.returncode == 0:
                    ensured_tasks.append({"repo": repo.repo, "issue": number, "board": repo.board, "idempotency_key": draft.idempotency_key})
    return {
        "ok": True,
        "effective_live": live,
        "planned_work": [
            {"repo": repo.repo, "action": "claim eligible GitHub issues and ensure idempotent Kanban intake tasks", "mutation": live}
            for repo in cfg.repos
        ],
        "safety_guards": safety_guards(),
        "commands": [planned_command(command) for command in (*list_commands, *mutation_commands)],
        "executed": [result.executed for result in (*list_results, *mutation_results)],
        "ensured_tasks": ensured_tasks,
    }


def dispatch(cfg: OssRepoAgentConfig, live_flag: bool, run_executor: bool, max_tasks: int) -> dict[str, Any]:
    live = cfg.effective_live(live_flag)
    executor_runs = cfg.executor_runs(live_flag, run_executor)
    return {
        "ok": True,
        "effective_live": live,
        "executor_runs": executor_runs,
        "max_tasks": max_tasks,
        "planned_work": [
            {
                "repo": repo.repo,
                "action": "draft guarded Kanban tasks for approved issues",
                "mutation": live,
                "executor_runs": executor_runs,
            }
            for repo in cfg.repos
        ],
        "safety_guards": safety_guards(),
        "message": "dispatch uses Kanban task drafts and requires explicit executor gates",
    }


def _pr_rows(result_stdout: str) -> list[dict[str, Any]]:
    if not result_stdout.strip():
        return []
    data = json.loads(result_stdout)
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _claimable_pr(repo_name: str, pr: dict[str, Any], branch_prefix: str) -> bool:
    author = pr.get("author") if isinstance(pr.get("author"), dict) else {}
    owner = repo_name.split("/", 1)[0]
    head = str(pr.get("headRefName") or "")
    return str(author.get("login") or "").lower() == owner.lower() and head.startswith(f"{branch_prefix.rstrip('/')}/")


def pr_triage(cfg: OssRepoAgentConfig, live_flag: bool, comment: bool, runner: Runner) -> dict[str, Any]:
    live = cfg.effective_live(live_flag)
    list_commands = [github_cli.pr_list(repo.repo) for repo in cfg.repos]
    list_results = [runner.run(command, live=live) for command in list_commands]
    mutation_commands = []
    mutation_results = []
    claimed_prs = []
    if live and cfg.github.assignee:
        for repo, result in zip(cfg.repos, list_results):
            if result.returncode != 0:
                continue
            for pr in _pr_rows(result.stdout):
                if not _claimable_pr(repo.repo, pr, cfg.branch_prefix):
                    continue
                number = int(pr.get("number", 0))
                if number <= 0:
                    continue
                claim = github_cli.pr_claim(repo.repo, number, cfg.github.assignee)
                mutation_commands.append(claim)
                claim_result = runner.run(claim, live=True)
                mutation_results.append(claim_result)
                if claim_result.returncode == 0:
                    claimed_prs.append({"repo": repo.repo, "pr": number, "assignee": cfg.github.assignee})
    return {
        "ok": True,
        "effective_live": live,
        "comment_enabled": bool(comment) and live,
        "merge_behavior": "not-supported-in-v0",
        "planned_work": [
            {"repo": repo.repo, "action": "claim owner-authored agent PRs for triage; merge remains outside the CLI facade", "mutation": live}
            for repo in cfg.repos
        ],
        "commands": [planned_command(command) for command in (*list_commands, *mutation_commands)],
        "executed": [result.executed for result in (*list_results, *mutation_results)],
        "claimed_prs": claimed_prs,
    }


def render_launchd(cfg: OssRepoAgentConfig, output: str) -> dict[str, Any]:
    return {
        "ok": True,
        "output": output,
        "templates": ["intake", "dispatch", "pr-triage", "health"],
        "macos_only": True,
        "mode": cfg.mode,
    }
