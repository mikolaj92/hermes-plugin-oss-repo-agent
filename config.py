from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from lokay.registry import (
    ConfigError as RegistryConfigError,
    canonical_toml,
    load_registry,
    process_defaults,
)


class ConfigError(ValueError):
    """Raised when the production Lokay configuration is invalid."""


REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_PREFIX_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
ASSIGNEE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
POLICIES = {"block", "report", "ignore"}


@dataclass(frozen=True)
class Labels:
    ready: str = "ai:ready"
    in_progress: str = "ai:in-progress"
    blocked: str = "ai:blocked"
    pr_opened: str = "ai:pr-opened"
    generated: str = "ai:generated"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "Labels":
        data = data or {}
        return cls(
            ready=str(data.get("ready", cls.ready)),
            in_progress=str(data.get("in_progress", cls.in_progress)),
            blocked=str(data.get("blocked", cls.blocked)),
            pr_opened=str(data.get("pr_opened", cls.pr_opened)),
            generated=str(data.get("generated", cls.generated)),
        )


@dataclass(frozen=True)
class GitHubConfig:
    cli: str = "gh"
    default_limit: int = 10
    assignee: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "GitHubConfig":
        data = data or {}
        limit = int(data.get("default_limit", 10))
        if limit < 1 or limit > 100:
            raise ConfigError("github.default_limit must be between 1 and 100")
        cli = str(data.get("cli", "gh"))
        if not cli or any(part in cli for part in ("/", "\\", " ")):
            raise ConfigError("github.cli must be a command name such as gh")
        assignee = data.get("assignee")
        assignee_text = str(assignee).strip() if assignee is not None else None
        if assignee_text == "":
            assignee_text = None
        if assignee_text is not None and not ASSIGNEE_RE.match(assignee_text):
            raise ConfigError("github.assignee must be a GitHub username")
        return cls(cli=cli, default_limit=limit, assignee=assignee_text)


@dataclass(frozen=True)
class ExecutorConfig:
    enabled: bool = False
    command: str = "claude"
    model: str = "omniroute/omp/default"
    thinking: str = "medium"
    timeout_seconds: float = 1800

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "ExecutorConfig":
        data = data or {}
        timeout = float(data.get("timeout_seconds", 1800))
        if timeout <= 0:
            raise ConfigError("executor.timeout_seconds must be positive")
        command = str(data.get("command", "claude"))
        if not command or any(part in command for part in ("/", "\\", " ")):
            raise ConfigError("executor.command must be a command name")
        return cls(
            enabled=bool(data.get("enabled", False)),
            command=command,
            model=str(data.get("model", "omniroute/omp/default")),
            thinking=str(data.get("thinking", "medium")),
            timeout_seconds=timeout,
        )


@dataclass(frozen=True)
class RepoConfig:
    repo: str
    board: str
    clone_path: str | None = None
    trusted_authors: tuple[str, ...] = ()
    trusted_branch_prefixes: tuple[str, ...] = ()
    allowed_base_branches: tuple[str, ...] = ("main",)
    external_pr_policy: str = "block"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], live: bool) -> "RepoConfig":
        repo = str(data.get("repo", ""))
        if not REPO_RE.match(repo):
            raise ConfigError(f"invalid repo name: {repo}")
        board = str(data.get("board", ""))
        if not board:
            raise ConfigError(f"repo {repo} is missing board")
        policy = str(data.get("external_pr_policy", "block"))
        if policy not in POLICIES:
            raise ConfigError(f"repo {repo} has invalid external_pr_policy")
        clone_path = data.get("clone_path")
        clone_text = str(clone_path) if clone_path is not None else None
        expanded_clone = Path(clone_text).expanduser() if clone_text else None
        if live and expanded_clone and not expanded_clone.is_absolute():
            raise ConfigError(f"repo {repo} live clone_path must be absolute")
        return cls(
            repo=repo,
            board=board,
            clone_path=str(expanded_clone.absolute()) if live and expanded_clone else clone_text,
            trusted_authors=tuple(str(x) for x in data.get("trusted_authors", ())),
            trusted_branch_prefixes=tuple(str(x) for x in data.get("trusted_branch_prefixes", ())),
            allowed_base_branches=tuple(str(x) for x in data.get("allowed_base_branches", ("main",))),
            external_pr_policy=policy,
        )


@dataclass(frozen=True)
class LokayConfig:
    version: int = 1
    mode: str = "dry-run"
    clone_root: str | None = None
    worktree_root: str | None = None
    dispatch_receipts: str | None = None
    merge_receipts: str | None = None
    active_issue: str | None = None
    base_branch: str = "main"
    branch_prefix: str = "ai/fix"
    automerge: bool = False
    require_human_approval: bool = True
    require_checks: bool = True
    require_test_evidence: bool = True
    github: GitHubConfig = field(default_factory=GitHubConfig)
    labels: Labels = field(default_factory=Labels)
    executor: ExecutorConfig = field(default_factory=ExecutorConfig)
    repos: tuple[RepoConfig, ...] = ()
    processes: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "LokayConfig":
        automation = data.get("automation") if isinstance(data.get("automation"), Mapping) else {}
        paths = data.get("paths") if isinstance(data.get("paths"), Mapping) else {}
        mode = str(data.get("mode", "dry-run"))
        if mode not in {"dry-run", "live"}:
            raise ConfigError("mode must be dry-run or live")
        branch_prefix = str(data.get("branch_prefix", "ai/fix")).strip("/")
        if not branch_prefix or not BRANCH_PREFIX_RE.match(branch_prefix):
            raise ConfigError("branch_prefix contains unsafe characters")
        automerge = bool(data.get("automerge", automation.get("automerge", False)))
        live = mode == "live"
        clone_root = data.get("clone_root")
        worktree_root = data.get("worktree_root", paths.get("worktree_root"))
        dispatch_receipts = data.get("dispatch_receipts", paths.get("dispatch_receipts"))
        merge_receipts = data.get("merge_receipts", paths.get("merge_receipts"))
        active_issue = data.get("active_issue", paths.get("active_issue"))
        if live:
            for key, value in (
                ("clone_root", clone_root),
                ("worktree_root", worktree_root),
                ("dispatch_receipts", dispatch_receipts),
                ("merge_receipts", merge_receipts),
                ("active_issue", active_issue),
            ):
                if value and not Path(str(value)).expanduser().is_absolute():
                    raise ConfigError(f"{key} must be absolute in live mode")
        repos = tuple(RepoConfig.from_mapping(item, live=live) for item in data.get("repos", ()))
        def runtime_path(value: Any) -> str | None:
            return str(Path(str(value)).expanduser().absolute()) if live and value is not None else (str(value) if value is not None else None)
        return cls(
            version=int(data.get("version", 1)),
            mode=mode,
            clone_root=runtime_path(clone_root),
            worktree_root=runtime_path(worktree_root),
            dispatch_receipts=runtime_path(dispatch_receipts),
            merge_receipts=runtime_path(merge_receipts),
            active_issue=runtime_path(active_issue),
            base_branch=str(data.get("base_branch", "main")),
            branch_prefix=branch_prefix,
            automerge=automerge,
            require_human_approval=bool(data.get("require_human_approval", automation.get("require_human_approval", True))),
            require_checks=bool(data.get("require_checks", automation.get("require_checks", True))),
            require_test_evidence=bool(data.get("require_test_evidence", automation.get("require_test_evidence", True))),
            github=GitHubConfig.from_mapping(data.get("github")),
            labels=Labels.from_mapping(data.get("labels")),
            executor=ExecutorConfig.from_mapping(data.get("executor")),
            repos=repos,
            processes=tuple(data.get("processes", ())),
        )

    def effective_live(self, cli_requested_live: bool) -> bool:
        return self.mode == "live" and bool(cli_requested_live)

    def executor_runs(self, cli_requested_live: bool, run_executor: bool) -> bool:
        return self.effective_live(cli_requested_live) and bool(run_executor) and self.executor.enabled


def default_config_path() -> Path:
    configured = os.environ.get("HERMES_LOKAY_CONFIG")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".hermes" / "lokay" / "config.toml"


def load_config(path: str | os.PathLike[str] | None = None) -> LokayConfig:
    """Load the canonical TOML registry without compatibility fallbacks."""
    config_path = Path(path).expanduser() if path is not None else default_config_path()
    try:
        registry = load_registry(config_path, env=os.environ)
        return LokayConfig.from_mapping(registry.data)
    except RegistryConfigError as exc:
        raise ConfigError(str(exc)) from exc
