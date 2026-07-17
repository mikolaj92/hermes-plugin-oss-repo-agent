from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore

from repo_agent.runtime import DEFAULT_CONFIG


@dataclass(frozen=True)
class RepoEntry:
    repo: str
    board: str
    clone_path: str
    priority: int = 50


@dataclass(frozen=True)
class AgentConfig:
    mode: str = "dry-run"
    branch_prefix: str = "ai/fix"
    base_branch: str = "main"
    gh_cli: str = "gh"
    assignee: str = "mikolaj92"
    kanban_intake_assignee: str = "repo-agent-intake"
    ready_label: str = "ai:ready"
    max_active_issues: int = 1
    repos: tuple[RepoEntry, ...] = field(default_factory=tuple)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def live(self) -> bool:
        return self.mode.strip().lower() == "live"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def load_config(path: Path | str | None = None) -> AgentConfig:
    config_path = Path(path or DEFAULT_CONFIG).expanduser()
    if not config_path.is_file():
        # Minimal default so unit tests work without production config.
        return AgentConfig(
            mode=os.environ.get("HERMES_REPO_AGENT_MODE", "dry-run"),
            assignee=os.environ.get("HERMES_REPO_AGENT_ASSIGNEE", "mikolaj92"),
            kanban_intake_assignee=os.environ.get(
                "HERMES_KANBAN_INTAKE_ASSIGNEE", "repo-agent-intake"
            ),
        )

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    github = _as_dict(data.get("github"))
    labels = _as_dict(data.get("labels"))
    automation = _as_dict(data.get("automation"))

    repos: list[RepoEntry] = []
    for item in data.get("repos") or []:
        if not isinstance(item, dict):
            continue
        repo = str(item.get("repo") or "").strip()
        if not repo:
            continue
        board = str(item.get("board") or "").strip() or repo.replace("/", "-")
        clone_path = str(item.get("clone_path") or "").strip()
        priority = int(item.get("priority") or 50)
        repos.append(
            RepoEntry(
                repo=repo,
                board=board,
                clone_path=clone_path,
                priority=priority,
            )
        )

    # Optional pipe registry override used by legacy shell helpers.
    repos_file = os.environ.get("HERMES_REPO_AGENT_REPOS_FILE")
    if repos_file and Path(repos_file).is_file():
        repos = []
        for line in Path(repos_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 3:
                continue
            repos.append(
                RepoEntry(
                    repo=parts[0],
                    board=parts[1],
                    clone_path=parts[2],
                    priority=int(parts[3]) if len(parts) > 3 and parts[3] else 50,
                )
            )

    mode = str(data.get("mode") or "dry-run")
    if os.environ.get("HERMES_REPO_AGENT_MODE"):
        mode = os.environ["HERMES_REPO_AGENT_MODE"]

    return AgentConfig(
        mode=mode,
        branch_prefix=str(data.get("branch_prefix") or "ai/fix"),
        base_branch=str(data.get("base_branch") or "main"),
        gh_cli=str(github.get("cli") or "gh"),
        assignee=str(
            os.environ.get("HERMES_REPO_AGENT_ASSIGNEE")
            or github.get("assignee")
            or "mikolaj92"
        ),
        kanban_intake_assignee=str(
            os.environ.get("HERMES_KANBAN_INTAKE_ASSIGNEE")
            or "repo-agent-intake"
        ),
        ready_label=str(labels.get("ready") or "ai:ready"),
        max_active_issues=int(automation.get("max_active_issues") or 1),
        repos=tuple(repos),
        raw=data,
    )
