from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .redaction import redact, redact_mapping


class SafetyError(ValueError):
    pass


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: int = 120

    def display(self) -> str:
        prefix = " ".join(f"{key}={value}" for key, value in sorted(self.env.items()) if key == "GIT_MASTER")
        command = " ".join(self.argv)
        return f"{prefix} {command}".strip()


@dataclass(frozen=True)
class CommandResult:
    spec: CommandSpec
    executed: bool
    returncode: int
    stdout: str = ""
    stderr: str = ""


def validate_command(spec: CommandSpec) -> None:
    if not spec.argv:
        raise SafetyError("empty command")
    lowered = tuple(part.lower() for part in spec.argv)
    if lowered[:3] == ("gh", "pr", "merge") and "--force" in lowered:
        raise SafetyError("force PR merge is forbidden")
    if lowered[:3] == ("gh", "repo", "delete"):
        raise SafetyError("repository deletion is forbidden")
    if lowered[:2] == ("gh", "auth"):
        raise SafetyError("auth inspection commands are forbidden")
    if lowered[0] == "git" and "push" in lowered and ("--force" in lowered or "-f" in lowered):
        raise SafetyError("force push is forbidden")
    if lowered[0] == "git" and "branch" in lowered and ("-d" in lowered or "-D" in spec.argv):
        raise SafetyError("branch deletion is forbidden")
    if any(part in {"curl", "wget"} for part in lowered):
        raise SafetyError("raw network clients are forbidden")


def git_spec(args: Sequence[str], cwd: str | None = None, timeout_seconds: int = 120) -> CommandSpec:
    return CommandSpec(argv=("git", *tuple(args)), cwd=cwd, env={"GIT_MASTER": "1"}, timeout_seconds=timeout_seconds)


def gh_spec(args: Sequence[str], timeout_seconds: int = 120) -> CommandSpec:
    return CommandSpec(argv=("gh", *tuple(args)), timeout_seconds=timeout_seconds)


class Runner:
    def run(self, spec: CommandSpec, live: bool) -> CommandResult:
        validate_command(spec)
        if not live:
            return CommandResult(spec=spec, executed=False, returncode=0)
        env = os.environ.copy()
        env.update(spec.env)
        cwd = str(Path(spec.cwd)) if spec.cwd else None
        completed = subprocess.run(
            list(spec.argv),
            cwd=cwd,
            env=env,
            shell=False,
            capture_output=True,
            text=True,
            timeout=spec.timeout_seconds,
            check=False,
        )
        return CommandResult(
            spec=spec,
            executed=True,
            returncode=completed.returncode,
            stdout=redact(completed.stdout),
            stderr=redact(completed.stderr),
        )


def planned_command(spec: CommandSpec) -> dict[str, object]:
    validate_command(spec)
    return {
        "argv": list(spec.argv),
        "display": spec.display(),
        "cwd": redact(spec.cwd or ""),
        "env": redact_mapping(spec.env),
        "timeout_seconds": spec.timeout_seconds,
    }
