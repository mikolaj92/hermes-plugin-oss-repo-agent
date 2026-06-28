from __future__ import annotations

import json
from argparse import ArgumentParser, Namespace
from typing import Any

from . import github_cli
from .config import ConfigError, OssRepoAgentConfig, load_config
from .executor import Runner, planned_command


def setup_parser(parser: ArgumentParser) -> None:
    parser.add_argument("--config", default=None)
    subparsers = parser.add_subparsers(dest="oss_repo_agent_command")
    subparsers.required = True
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
    cfg = load_config(getattr(args, "config", None))
    command = getattr(args, "oss_repo_agent_command")
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


def validate(cfg: OssRepoAgentConfig) -> dict[str, Any]:
    return {
        "ok": True,
        "mode": cfg.mode,
        "repos": [repo.repo for repo in cfg.repos],
        "automerge": False,
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
        "commands": [planned_command(command) for command in commands],
        "executed": [result.executed for result in results],
    }


def dispatch(cfg: OssRepoAgentConfig, live_flag: bool, run_executor: bool, max_tasks: int) -> dict[str, Any]:
    return {
        "ok": True,
        "effective_live": cfg.effective_live(live_flag),
        "executor_runs": cfg.executor_runs(live_flag, run_executor),
        "max_tasks": max_tasks,
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
        "templates": ["intake", "dispatch", "pr-triage"],
        "macos_only": True,
        "mode": cfg.mode,
    }
