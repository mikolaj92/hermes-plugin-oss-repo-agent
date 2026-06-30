from __future__ import annotations

import json
from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import Any

from . import github_cli
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


def init_project(config_path: str | None, repo: str, board: str, clone_root: str, worktree_root: str, force: bool) -> dict[str, Any]:
    target = Path(config_path).expanduser() if config_path else default_config_path()
    if target.exists() and not force:
        raise ConfigError(f"config already exists: {target}; pass --force to overwrite")
    target.parent.mkdir(parents=True, exist_ok=True)
    leaf = repo.split("/")[-1] or "example-repo"
    text = starter_config(repo, board, clone_root, worktree_root, leaf)
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


def starter_config(repo: str, board: str, clone_root: str, worktree_root: str, leaf: str) -> str:
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


def intake(cfg: OssRepoAgentConfig, live_flag: bool, limit: int, runner: Runner) -> dict[str, Any]:
    live = cfg.effective_live(live_flag)
    commands = [github_cli.issue_list(repo.repo, limit) for repo in cfg.repos]
    results = [runner.run(command, live=live) for command in commands]
    return {
        "ok": True,
        "effective_live": live,
        "planned_work": [
            {"repo": repo.repo, "action": "read open issues through gh issue list", "mutation": live}
            for repo in cfg.repos
        ],
        "safety_guards": safety_guards(),
        "commands": [planned_command(command) for command in commands],
        "executed": [result.executed for result in results],
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


def pr_triage(cfg: OssRepoAgentConfig, live_flag: bool, comment: bool, runner: Runner) -> dict[str, Any]:
    live = cfg.effective_live(live_flag)
    commands = [github_cli.pr_list(repo.repo) for repo in cfg.repos]
    results = [runner.run(command, live=live) for command in commands]
    return {
        "ok": True,
        "effective_live": live,
        "comment_enabled": bool(comment) and live,
        "merge_behavior": "not-supported-in-v0",
        "commands": [planned_command(command) for command in commands],
        "executed": [result.executed for result in results],
    }


def render_launchd(cfg: OssRepoAgentConfig, output: str) -> dict[str, Any]:
    return {
        "ok": True,
        "output": output,
        "templates": ["intake", "dispatch", "pr-triage", "health"],
        "macos_only": True,
        "mode": cfg.mode,
    }
