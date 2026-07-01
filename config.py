from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


class ConfigError(ValueError):
    pass


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
    timeout_seconds: int = 1800

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "ExecutorConfig":
        data = data or {}
        timeout = int(data.get("timeout_seconds", 1800))
        if timeout < 1:
            raise ConfigError("executor.timeout_seconds must be positive")
        command = str(data.get("command", "claude"))
        if not command or any(part in command for part in ("/", "\\", " ")):
            raise ConfigError("executor.command must be a command name")
        return cls(enabled=bool(data.get("enabled", False)), command=command, timeout_seconds=timeout)


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
        if live and clone_text and not Path(clone_text).is_absolute():
            raise ConfigError(f"repo {repo} live clone_path must be absolute")
        return cls(
            repo=repo,
            board=board,
            clone_path=clone_text,
            trusted_authors=tuple(str(x) for x in data.get("trusted_authors", ())),
            trusted_branch_prefixes=tuple(str(x) for x in data.get("trusted_branch_prefixes", ())),
            allowed_base_branches=tuple(str(x) for x in data.get("allowed_base_branches", ("main",))),
            external_pr_policy=policy,
        )


@dataclass(frozen=True)
class OssRepoAgentConfig:
    version: int = 1
    mode: str = "dry-run"
    clone_root: str | None = None
    worktree_root: str | None = None
    branch_prefix: str = "ai/fix"
    automerge: bool = False
    github: GitHubConfig = field(default_factory=GitHubConfig)
    labels: Labels = field(default_factory=Labels)
    executor: ExecutorConfig = field(default_factory=ExecutorConfig)
    repos: tuple[RepoConfig, ...] = ()

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "OssRepoAgentConfig":
        mode = str(data.get("mode", "dry-run"))
        if mode not in {"dry-run", "live"}:
            raise ConfigError("mode must be dry-run or live")
        branch_prefix = str(data.get("branch_prefix", "ai/fix")).strip("/")
        if not branch_prefix or not BRANCH_PREFIX_RE.match(branch_prefix):
            raise ConfigError("branch_prefix contains unsafe characters")
        automerge = bool(data.get("automerge", False))
        live = mode == "live"
        clone_root = data.get("clone_root")
        worktree_root = data.get("worktree_root")
        if live:
            for key, value in (("clone_root", clone_root), ("worktree_root", worktree_root)):
                if value and not Path(str(value)).is_absolute():
                    raise ConfigError(f"{key} must be absolute in live mode")
        repos = tuple(RepoConfig.from_mapping(item, live=live) for item in data.get("repos", ()))
        return cls(
            version=int(data.get("version", 1)),
            mode=mode,
            clone_root=str(clone_root) if clone_root is not None else None,
            worktree_root=str(worktree_root) if worktree_root is not None else None,
            branch_prefix=branch_prefix,
            automerge=automerge,
            github=GitHubConfig.from_mapping(data.get("github")),
            labels=Labels.from_mapping(data.get("labels")),
            executor=ExecutorConfig.from_mapping(data.get("executor")),
            repos=repos,
        )

    def effective_live(self, cli_requested_live: bool) -> bool:
        return self.mode == "live" and bool(cli_requested_live)

    def executor_runs(self, cli_requested_live: bool, run_executor: bool) -> bool:
        return self.effective_live(cli_requested_live) and bool(run_executor) and self.executor.enabled


def default_config_path() -> Path:
    configured = os.environ.get("HERMES_OSS_REPO_AGENT_CONFIG")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".hermes" / "oss-repo-agent" / "config.yaml"


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"", "''", '""'}:
        return ""
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    if (value.startswith("'") and value.endswith("'")) or (value.startswith('"') and value.endswith('"')):
        return value[1:-1]
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none", "~"}:
        return None
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def _parse_yaml_block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines):
        return {}, index
    if lines[index][1].startswith("- "):
        return _parse_yaml_list(lines, index, indent)
    return _parse_yaml_map(lines, index, indent)


def _parse_yaml_map(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        current_indent, text = lines[index]
        if current_indent < indent:
            break
        if current_indent != indent or text.startswith("- "):
            break
        key, separator, raw_value = text.partition(":")
        if not separator or not key.strip():
            raise ConfigError(f"unsupported YAML line: {text}")
        index += 1
        if raw_value.strip():
            result[key.strip()] = _parse_scalar(raw_value.strip())
        elif index < len(lines) and lines[index][0] > current_indent:
            result[key.strip()], index = _parse_yaml_block(lines, index, lines[index][0])
        else:
            result[key.strip()] = {}
    return result, index


def _parse_yaml_list(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[list[Any], int]:
    result: list[Any] = []
    while index < len(lines):
        current_indent, text = lines[index]
        if current_indent < indent:
            break
        if current_indent != indent or not text.startswith("- "):
            break
        item = text[2:].strip()
        index += 1
        if not item:
            if index < len(lines) and lines[index][0] > current_indent:
                value, index = _parse_yaml_block(lines, index, lines[index][0])
            else:
                value = None
            result.append(value)
            continue
        if ":" in item and not item.startswith(("'", '"')):
            key, _, raw_value = item.partition(":")
            value: dict[str, Any] = {}
            if raw_value.strip():
                value[key.strip()] = _parse_scalar(raw_value.strip())
            elif index < len(lines) and lines[index][0] > current_indent:
                value[key.strip()], index = _parse_yaml_block(lines, index, lines[index][0])
            else:
                value[key.strip()] = {}
            if index < len(lines) and lines[index][0] > current_indent:
                extra, index = _parse_yaml_block(lines, index, lines[index][0])
                if not isinstance(extra, dict):
                    raise ConfigError("unsupported YAML list structure")
                value.update(extra)
            result.append(value)
        else:
            result.append(_parse_scalar(item))
    return result, index


def _load_simple_yaml(text: str) -> dict[str, Any]:
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent % 2:
            raise ConfigError("YAML indentation must use multiples of two spaces")
        lines.append((indent, raw.strip()))
    if not lines:
        return {}
    loaded, index = _parse_yaml_block(lines, 0, lines[0][0])
    if index != len(lines) or not isinstance(loaded, dict):
        raise ConfigError("unsupported YAML config shape")
    return loaded


def load_config(path: str | os.PathLike[str] | None = None) -> OssRepoAgentConfig:
    config_path = Path(path).expanduser() if path else default_config_path()
    if not config_path.exists():
        raise ConfigError(f"config not found: {config_path}")
    text = config_path.read_text(encoding="utf-8")
    if config_path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        try:
            import yaml
        except Exception:
            data = _load_simple_yaml(text)
        else:
            data = yaml.safe_load(text) or {}
    if not isinstance(data, Mapping):
        raise ConfigError("config root must be a mapping")
    return OssRepoAgentConfig.from_mapping(data)
