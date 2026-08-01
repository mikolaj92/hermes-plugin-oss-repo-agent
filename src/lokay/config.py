from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore

from lokay.runtime import DEFAULT_CONFIG


class ConfigError(ValueError):
    """Raised when the production Fala configuration is invalid."""


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


def _env_or(mapping: Mapping[str, Any], key: str, env: Mapping[str, str], env_key: str, default: Any) -> Any:
    return env.get(env_key) or mapping.get(key) or default


def _bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean")


def _build_config(data: Mapping[str, Any], env: Mapping[str, str]) -> AgentConfig:
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
        repos.append(RepoEntry(repo, board, clone_path, priority, str(item.get("triage_goal", "")), _context_paths(item.get("triage_context_paths"), f"repos[{index}].triage_context_paths"), item.get("auto_close_duplicates"), item.get("auto_close_out_of_scope")))

    repos_file = env.get("HERMES_LOKAY_REPOS_FILE")
    if repos_file and Path(repos_file).is_file():
        repos = []
        for line_number, line in enumerate(Path(repos_file).read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 3 or not all(part.strip() for part in parts[:3]):
                raise ConfigError(f"repos file line {line_number} requires repo|board|clone_path")
            try:
                priority = int(parts[3]) if len(parts) > 3 and parts[3] else 50
            except ValueError as exc:
                raise ConfigError(f"repos file line {line_number} has invalid priority") from exc
            repos.append(RepoEntry(parts[0].strip(), parts[1].strip(), parts[2].strip(), priority))

    mode = str(_env_or(data, "mode", env, "HERMES_LOKAY_MODE", "dry-run")).strip().lower()
    label_defaults = LabelConfig()
    labels = LabelConfig(
        ready=str(_env_or(labels_data, "ready", env, "HERMES_LOKAY_LABEL_READY", label_defaults.ready)),
        in_progress=str(_env_or(labels_data, "in_progress", env, "HERMES_LOKAY_LABEL_IN_PROGRESS", label_defaults.in_progress)),
        blocked=str(_env_or(labels_data, "blocked", env, "HERMES_LOKAY_LABEL_BLOCKED", label_defaults.blocked)),
        pr_opened=str(_env_or(labels_data, "pr_opened", env, "HERMES_LOKAY_LABEL_PR_OPENED", label_defaults.pr_opened)),
        generated=str(_env_or(labels_data, "generated", env, "HERMES_LOKAY_LABEL_GENERATED", label_defaults.generated)),
        needs_feedback=str(labels_data.get("needs_feedback", label_defaults.needs_feedback)),
        duplicate=str(labels_data.get("duplicate", label_defaults.duplicate)),
        out_of_scope=str(labels_data.get("out_of_scope", label_defaults.out_of_scope)),
        frozen=str(labels_data.get("frozen", label_defaults.frozen)),
    )
    automation = AutomationConfig(
        max_active_issues=int(automation_data.get("max_active_issues", 1)),
        automerge=_bool(automation_data.get("automerge", False), "automation.automerge"),
        require_human_approval=_bool(automation_data.get("require_human_approval", True), "automation.require_human_approval"),
        require_checks=_bool(automation_data.get("require_checks", True), "automation.require_checks"),
        require_test_evidence=_bool(automation_data.get("require_test_evidence", True), "automation.require_test_evidence"),
        fixer_assignee=str(automation_data.get("fixer_assignee", "lokay-fixer")),
        merge_method=str(automation_data.get("merge_method", "merge")),
    )
    reject_labels_raw = direction_data.get("reject_labels")
    if reject_labels_raw is None:
        reject_labels = DirectionConfig().reject_labels
    elif isinstance(reject_labels_raw, str):
        reject_labels = tuple(x.strip() for x in reject_labels_raw.split(",") if x.strip())
    else:
        reject_labels = tuple(str(x).strip() for x in reject_labels_raw if str(x).strip())
    require_raw = direction_data.get("require_keywords") or ()
    deny_raw = direction_data.get("deny_keywords") or ()
    if isinstance(require_raw, str):
        require_raw = [x.strip() for x in require_raw.split(",") if x.strip()]
    if isinstance(deny_raw, str):
        deny_raw = [x.strip() for x in deny_raw.split(",") if x.strip()]
    try:
        min_overlap = int(direction_data.get("min_goal_overlap", 1))
    except (TypeError, ValueError) as exc:
        raise ConfigError("direction.min_goal_overlap must be an integer") from exc
    direction = DirectionConfig(
        repo_goal=str(
            _env_or(direction_data, "repo_goal", env, "HERMES_LOKAY_GOAL", "")
        ),
        require_keywords=tuple(str(x) for x in require_raw),
        deny_keywords=tuple(str(x) for x in deny_raw),
        reject_labels=reject_labels,
        min_goal_overlap=min_overlap,
    )
    executor = ExecutorConfig(
        enabled=_bool(executor_data.get("enabled", False), "executor.enabled"),
        command=str(executor_data.get("command", "omp")),
        model=str(executor_data.get("model", "omniroute/omp/default")),
        thinking=str(executor_data.get("thinking", "medium")),
        timeout_seconds=float(executor_data.get("timeout_seconds", 7200)),
    )
    paths = PathConfig(
        worktree_root=str(_env_or(paths_data, "worktree_root", env, "HERMES_LOKAY_WORKTREE_ROOT", "~/.hermes/worktrees/lokay")),
        dispatch_receipts=str(_env_or(paths_data, "dispatch_receipts", env, "HERMES_LOKAY_RECEIPT_DIR", "~/.hermes/state/lokay-dispatch")),
        task_receipts=str(_env_or(paths_data, "task_receipts", env, "HERMES_LOKAY_TASK_RECEIPT_DIR", "~/.hermes/state/lokay-receipts")),
        merge_receipts=str(_env_or(paths_data, "merge_receipts", env, "HERMES_LOKAY_MERGE_RECEIPT_DIR", "~/.hermes/state/lokay-merge")),
        active_issue=str(_env_or(paths_data, "active_issue", env, "HERMES_LOKAY_ACTIVE_ISSUE_DIR", "~/.hermes/state/lokay-active")),
        triage_receipts=str(paths_data.get("triage_receipts", "~/.hermes/state/lokay-triage")),
    )
    triage = TriageConfig(
        enabled=_bool(triage_data.get("enabled", True), "triage.enabled"),
        context_paths=_context_paths(triage_data.get("context_paths"), "triage.context_paths", ("README.md",)),
        context_max_bytes=triage_data.get("context_max_bytes", 131_072),
        auto_close_duplicates=_bool(triage_data.get("auto_close_duplicates", False), "triage.auto_close_duplicates"),
        auto_close_out_of_scope=_bool(triage_data.get("auto_close_out_of_scope", True), "triage.auto_close_out_of_scope"),
    )
    return AgentConfig(
        mode=mode,
        branch_prefix=str(data.get("branch_prefix", "ai/fix")),
        base_branch=str(data.get("base_branch", "main")),
        gh_cli=str(github.get("cli", "gh")),
        assignee=str(_env_or(github, "assignee", env, "HERMES_LOKAY_ASSIGNEE", "mikolaj92")),
        kanban_intake_assignee=str(_env_or({}, "assignee", env, "HERMES_LOKAY_KANBAN_INTAKE_ASSIGNEE", "lokay-intake")),
        labels=labels,
        automation=automation,
        direction=direction,
        executor=executor,
        triage=triage,
        paths=paths,
        repos=tuple(repos),
        raw=dict(data),
    )


def load_config(path: Path | str | None = None) -> AgentConfig:
    config_path = Path(path or DEFAULT_CONFIG).expanduser()
    data: Mapping[str, Any] = {}
    if config_path.is_file():
        try:
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"unable to read TOML config {config_path}: {exc}") from exc
        if not isinstance(data, Mapping):
            raise ConfigError("config root must be a mapping")
    return _build_config(data, os.environ)
