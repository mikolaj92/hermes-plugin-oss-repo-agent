from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from lokay.registry import ConfigError as RegistryConfigError
from lokay.registry import load_registry
from lokay.runtime import DEFAULT_CONFIG


class ConfigError(ValueError):
    """Raised when the production Lokay configuration is invalid."""

@dataclass(frozen=True)
class RepoEntry:
    repo: str
    board: str
    clone_path: str
    priority: int = 50
    triage_goal: str = ""
    triage_context_paths: tuple[str, ...] = ()
    auto_close_duplicates: bool | None = None
    auto_close_out_of_scope: bool | None = None

    def __post_init__(self) -> None:
        for name in ("repo", "board", "clone_path"):
            if not str(getattr(self, name)).strip():
                raise ConfigError(f"repos.{name} must not be empty")
        if not isinstance(self.triage_goal, str):
            raise ConfigError("repos.triage_goal must be a string")
        object.__setattr__(self, "triage_goal", self.triage_goal.strip())
        object.__setattr__(self, "triage_context_paths", _context_paths(self.triage_context_paths, "repos.triage_context_paths"))
        for name in ("auto_close_duplicates", "auto_close_out_of_scope"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise ConfigError(f"repos.{name} must be a boolean")
        object.__setattr__(self, "clone_path", _absolute_path(self.clone_path))

def _validate_context_path(path: str, name: str = "triage.context_paths") -> str:
    value = str(path)
    if not value or value.strip() != value or Path(value).is_absolute():
        raise ConfigError(f"{name} contains an invalid path: {value!r}")
    parts = value.split("/")
    if any(not part or part == ".." for part in parts):
        raise ConfigError(f"{name} contains an invalid path: {value!r}")
    return value


def _context_paths(value: Any, name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    if isinstance(value, str):
        value = (value,)
    if value is None:
        value = default
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"{name} must be an array of paths")
    paths = tuple(_validate_context_path(path, name) for path in value)
    if len(set(paths)) != len(paths):
        raise ConfigError(f"{name} must not contain duplicate paths")
    return paths


@dataclass(frozen=True)
class TriageConfig:
    enabled: bool = True
    context_paths: tuple[str, ...] = ("README.md",)
    context_max_bytes: int = 131_072
    auto_close_duplicates: bool = False
    auto_close_out_of_scope: bool = True

    def __post_init__(self) -> None:
        for name in ("enabled", "auto_close_duplicates", "auto_close_out_of_scope"):
            if not isinstance(getattr(self, name), bool):
                raise ConfigError(f"triage.{name} must be a boolean")
        if isinstance(self.context_max_bytes, bool) or not isinstance(self.context_max_bytes, int):
            raise ConfigError("triage.context_max_bytes must be an integer")
        if self.context_max_bytes < 1:
            raise ConfigError("triage.context_max_bytes must be at least 1")
        object.__setattr__(self, "context_paths", _context_paths(self.context_paths, "triage.context_paths", ("README.md",)))


def validate_context_paths(paths: tuple[str, ...] | list[str], root: Path | str, max_bytes: int) -> tuple[str, ...]:
    """Validate configured repository-relative files and their aggregate size."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ConfigError("triage.context_max_bytes must be at least 1")
    normalized = _context_paths(paths, "triage.context_paths")
    total = 0
    base = Path(root)
    for path in normalized:
        candidate = base / path
        if not candidate.is_file():
            raise ConfigError(f"triage context path is not a file: {path}")
        total += candidate.stat().st_size
    if total > max_bytes:
        raise ConfigError(f"triage context exceeds context_max_bytes ({total} > {max_bytes})")
    return normalized


@dataclass(frozen=True)
class LabelConfig:
    ready: str = "ai:ready"
    in_progress: str = "ai:in-progress"
    blocked: str = "ai:blocked"
    pr_opened: str = "ai:pr-opened"
    generated: str = "ai:generated"
    needs_feedback: str = "ai:needs-feedback"
    duplicate: str = "duplicate"
    out_of_scope: str = "ai:out-of-scope"
    frozen: str = "frozen"

    def __post_init__(self) -> None:
        for name in ("ready", "in_progress", "blocked", "pr_opened", "generated", "needs_feedback", "duplicate", "out_of_scope", "frozen"):
            if not str(getattr(self, name)).strip():
                raise ConfigError(f"labels.{name} must not be empty")


@dataclass(frozen=True)
class AutomationConfig:
    max_active_issues: int = 1
    automerge: bool = False
    require_human_approval: bool = True
    require_checks: bool = True
    require_test_evidence: bool = True
    fixer_assignee: str = "lokay-fixer"
    merge_method: str = "merge"

    def __post_init__(self) -> None:
        if self.max_active_issues < 1:
            raise ConfigError("automation.max_active_issues must be at least 1")
        if not self.fixer_assignee.strip():
            raise ConfigError("automation.fixer_assignee must not be empty")
        if not self.merge_method.strip():
            raise ConfigError("automation.merge_method must not be empty")


@dataclass(frozen=True)
class DirectionConfig:
    """Issue-side sense/direction gate (accept vs durable reject+comment)."""

    repo_goal: str = ""
    require_keywords: tuple[str, ...] = ()
    deny_keywords: tuple[str, ...] = ()
    reject_labels: tuple[str, ...] = ("ai:out-of-scope", "wontfix", "invalid")
    min_goal_overlap: int = 1

    def __post_init__(self) -> None:
        if self.min_goal_overlap < 1:
            raise ConfigError("direction.min_goal_overlap must be at least 1")
        object.__setattr__(
            self,
            "require_keywords",
            tuple(str(x).strip() for x in self.require_keywords if str(x).strip()),
        )
        object.__setattr__(
            self,
            "deny_keywords",
            tuple(str(x).strip() for x in self.deny_keywords if str(x).strip()),
        )
        object.__setattr__(
            self,
            "reject_labels",
            tuple(str(x).strip() for x in self.reject_labels if str(x).strip()),
        )


MAX_EXECUTOR_TIMEOUT_SECONDS = 7200.0


@dataclass(frozen=True)
class ExecutorConfig:
    enabled: bool = False
    command: str = "omp"
    model: str = "omniroute/omp/default"
    thinking: str = "medium"
    timeout_seconds: float = 7200.0

    def __post_init__(self) -> None:
        if not 0 < self.timeout_seconds <= MAX_EXECUTOR_TIMEOUT_SECONDS:
            raise ConfigError(f"executor.timeout_seconds must be greater than 0 and at most {MAX_EXECUTOR_TIMEOUT_SECONDS:g}")
        if self.enabled and not self.command.strip():
            raise ConfigError("executor.command must not be empty when enabled")
        if self.enabled and not self.model.strip():
            raise ConfigError("executor.model must not be empty when enabled")


@dataclass(frozen=True)
class PathConfig:
    worktree_root: str = "~/.hermes/worktrees/lokay"
    dispatch_receipts: str = "~/.hermes/state/lokay-dispatch"
    task_receipts: str = "~/.hermes/state/lokay-receipts"
    merge_receipts: str = "~/.hermes/state/lokay-merge"
    active_issue: str = "~/.hermes/state/lokay-active"
    triage_receipts: str = "~/.hermes/state/lokay-triage"

    def __post_init__(self) -> None:
        for name in ("worktree_root", "dispatch_receipts", "task_receipts", "merge_receipts", "active_issue", "triage_receipts"):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ConfigError(f"paths.{name} must not be empty")
            object.__setattr__(self, name, _absolute_path(value))


@dataclass(frozen=True)
class AgentConfig:
    mode: str = "dry-run"
    branch_prefix: str = "ai/fix"
    base_branch: str = "main"
    gh_cli: str = "gh"
    assignee: str = "mikolaj92"
    kanban_intake_assignee: str = "lokay-intake"
    labels: LabelConfig = field(default_factory=LabelConfig)
    automation: AutomationConfig = field(default_factory=AutomationConfig)
    direction: DirectionConfig = field(default_factory=DirectionConfig)
    triage: TriageConfig = field(default_factory=TriageConfig)
    executor: ExecutorConfig = field(default_factory=ExecutorConfig)
    paths: PathConfig = field(default_factory=PathConfig)
    repos: tuple[RepoEntry, ...] = field(default_factory=tuple)
    processes: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mode not in {"dry-run", "live"}:
            raise ConfigError("mode must be dry-run or live")
        for name in ("branch_prefix", "base_branch", "gh_cli", "assignee", "kanban_intake_assignee"):
            if not str(getattr(self, name)).strip():
                raise ConfigError(f"{name} must not be empty")

    @property
    def live(self) -> bool:
        return self.mode == "live"

    # Compatibility accessors keep existing step code source-compatible while
    # Fala flows migrate to the typed groups.
    @property
    def ready_label(self) -> str:
        return self.labels.ready

    def effective_triage_goal(self, repo: RepoEntry | None = None) -> str:
        return (repo.triage_goal if repo and repo.triage_goal else self.direction.repo_goal).strip()

    def effective_triage_context_paths(self, repo: RepoEntry | None = None) -> tuple[str, ...]:
        return repo.triage_context_paths if repo and repo.triage_context_paths else self.triage.context_paths

    def effective_auto_close_duplicates(self, repo: RepoEntry | None = None) -> bool:
        return repo.auto_close_duplicates if repo and repo.auto_close_duplicates is not None else self.triage.auto_close_duplicates

    def effective_auto_close_out_of_scope(self, repo: RepoEntry | None = None) -> bool:
        return repo.auto_close_out_of_scope if repo and repo.auto_close_out_of_scope is not None else self.triage.auto_close_out_of_scope
    @property
    def in_progress_label(self) -> str:
        return self.labels.in_progress

    @property
    def blocked_label(self) -> str:
        return self.labels.blocked

    @property
    def pr_opened_label(self) -> str:
        return self.labels.pr_opened

    @property
    def generated_label(self) -> str:
        return self.labels.generated

    @property
    def max_active_issues(self) -> int:
        return self.automation.max_active_issues

    @property
    def automerge(self) -> bool:
        return self.automation.automerge

    @property
    def require_human_approval(self) -> bool:
        return self.automation.require_human_approval

    @property
    def require_checks(self) -> bool:
        return self.automation.require_checks

    @property
    def require_test_evidence(self) -> bool:
        return self.automation.require_test_evidence

    @property
    def fixer_assignee(self) -> str:
        return self.automation.fixer_assignee

    @property
    def merge_method(self) -> str:
        return self.automation.merge_method


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _absolute_path(value: str) -> str:
    return str(Path(value).expanduser().absolute())

def _bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ConfigError(f"{name} must be a boolean")


def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"{name} must be an array of strings")
    return tuple(str(item) for item in value)


def _build_config(data: Mapping[str, Any], env: Mapping[str, str]) -> AgentConfig:
    del env
    github = _as_dict(data.get("github"))
    labels_data = _as_dict(data.get("labels"))
    automation_data = _as_dict(data.get("automation"))
    direction_data = _as_dict(data.get("direction"))
    executor_data = _as_dict(data.get("executor"))
    paths_data = _as_dict(data.get("paths"))
    triage_data = _as_dict(data.get("triage"))

    repos: list[RepoEntry] = []
    for index, item in enumerate(data.get("repos") or []):
        if not isinstance(item, Mapping):
            raise ConfigError(f"repos[{index}] must be a mapping")
        repo = str(item.get("repo") or "").strip()
        board = str(item.get("board") or "").strip()
        clone_path = str(item.get("clone_path") or "").strip()
        if not repo or not board or not clone_path:
            raise ConfigError(f"repos[{index}] requires non-empty repo, board, and clone_path")
        try:
            priority = int(item.get("priority", 50))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"repos[{index}].priority must be an integer") from exc
        repos.append(
            RepoEntry(
                repo=repo,
                board=board,
                clone_path=clone_path,
                priority=priority,
                triage_goal=str(item.get("triage_goal", "")),
                triage_context_paths=_context_paths(
                    item.get("triage_context_paths"),
                    f"repos[{index}].triage_context_paths",
                ),
                auto_close_duplicates=item.get("auto_close_duplicates"),
                auto_close_out_of_scope=item.get("auto_close_out_of_scope"),
            )
        )

    label_defaults = LabelConfig()
    labels = LabelConfig(
        ready=str(labels_data.get("ready", label_defaults.ready)),
        in_progress=str(labels_data.get("in_progress", label_defaults.in_progress)),
        blocked=str(labels_data.get("blocked", label_defaults.blocked)),
        pr_opened=str(labels_data.get("pr_opened", label_defaults.pr_opened)),
        generated=str(labels_data.get("generated", label_defaults.generated)),
        needs_feedback=str(labels_data.get("needs_feedback", label_defaults.needs_feedback)),
        duplicate=str(labels_data.get("duplicate", label_defaults.duplicate)),
        out_of_scope=str(labels_data.get("out_of_scope", label_defaults.out_of_scope)),
        frozen=str(labels_data.get("frozen", label_defaults.frozen)),
    )
    automation = AutomationConfig(
        max_active_issues=int(automation_data.get("max_active_issues", 1)),
        automerge=_bool(automation_data.get("automerge", False), "automation.automerge"),
        require_human_approval=_bool(
            automation_data.get("require_human_approval", True),
            "automation.require_human_approval",
        ),
        require_checks=_bool(automation_data.get("require_checks", True), "automation.require_checks"),
        require_test_evidence=_bool(
            automation_data.get("require_test_evidence", True),
            "automation.require_test_evidence",
        ),
        fixer_assignee=str(automation_data.get("fixer_assignee", "lokay-fixer")),
        merge_method=str(automation_data.get("merge_method", "merge")),
    )
    direction = DirectionConfig(
        repo_goal=str(direction_data.get("repo_goal", "")),
        require_keywords=_string_tuple(direction_data.get("require_keywords"), "direction.require_keywords"),
        deny_keywords=_string_tuple(direction_data.get("deny_keywords"), "direction.deny_keywords"),
        reject_labels=_string_tuple(
            direction_data.get("reject_labels"),
            "direction.reject_labels",
        )
        or DirectionConfig().reject_labels,
        min_goal_overlap=int(direction_data.get("min_goal_overlap", 1)),
    )
    executor = ExecutorConfig(
        enabled=_bool(executor_data.get("enabled", False), "executor.enabled"),
        command=str(executor_data.get("command", "omp")),
        model=str(executor_data.get("model", "omniroute/omp/default")),
        thinking=str(executor_data.get("thinking", "medium")),
        timeout_seconds=float(executor_data.get("timeout_seconds", 7200)),
    )
    paths = PathConfig(
        worktree_root=str(paths_data.get("worktree_root", "~/.hermes/worktrees/lokay")),
        dispatch_receipts=str(paths_data.get("dispatch_receipts", "~/.hermes/state/lokay-dispatch")),
        task_receipts=str(paths_data.get("task_receipts", "~/.hermes/state/lokay-receipts")),
        merge_receipts=str(paths_data.get("merge_receipts", "~/.hermes/state/lokay-merge")),
        active_issue=str(paths_data.get("active_issue", "~/.hermes/state/lokay-active")),
        triage_receipts=str(paths_data.get("triage_receipts", "~/.hermes/state/lokay-triage")),
    )
    triage = TriageConfig(
        enabled=_bool(triage_data.get("enabled", True), "triage.enabled"),
        context_paths=_context_paths(
            triage_data.get("context_paths"),
            "triage.context_paths",
            ("README.md",),
        ),
        context_max_bytes=triage_data.get("context_max_bytes", 131_072),
        auto_close_duplicates=_bool(
            triage_data.get("auto_close_duplicates", False),
            "triage.auto_close_duplicates",
        ),
        auto_close_out_of_scope=_bool(
            triage_data.get("auto_close_out_of_scope", True),
            "triage.auto_close_out_of_scope",
        ),
    )
    return AgentConfig(
        mode=str(data.get("mode", "dry-run")).strip().lower(),
        branch_prefix=str(data.get("branch_prefix", "ai/fix")),
        base_branch=str(data.get("base_branch", "main")),
        gh_cli=str(github.get("cli", "gh")),
        assignee=str(github.get("assignee", "mikolaj92")) or "mikolaj92",
        kanban_intake_assignee="lokay-intake",
        labels=labels,
        automation=automation,
        direction=direction,
        triage=triage,
        executor=executor,
        paths=paths,
        repos=tuple(repos),
        processes=tuple(data.get("processes", ())),
        raw=dict(data),
    )


def load_config(path: Path | str | None = None) -> AgentConfig:
    """Load the complete canonical TOML registry for runtime activation."""
    config_path = Path(path or DEFAULT_CONFIG).expanduser()
    try:
        registry = load_registry(config_path, env=os.environ)
    except RegistryConfigError as exc:
        raise ConfigError(str(exc)) from exc
    return _build_config(registry.data, os.environ)
