from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


class CommandError(RuntimeError):
    def __init__(self, cmd: list[str], returncode: int, stdout: str, stderr: str):
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"command failed ({returncode}): {' '.join(cmd)}: {stderr.strip() or stdout.strip()}"
        )


def run_cmd(
    cmd: list[str],
    *,
    timeout: float = 120.0,
    env: dict[str, str] | None = None,
    check: bool = True,
    cwd: str | Path | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = {**os.environ, **(env or {})}
    # Never leak through shell.
    proc = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=merged,
        cwd=cwd,
    )
    if check and proc.returncode != 0:
        raise CommandError(cmd, proc.returncode, proc.stdout, proc.stderr)
    return proc


def which(name: str) -> str | None:
    return shutil.which(name)


def gh_json(args: list[str], *, gh: str = "gh", timeout: float = 120.0) -> Any:
    proc = run_cmd([gh, *args], timeout=timeout)
    text = proc.stdout.strip()
    if not text:
        return None
    return json.loads(text)


def hermes_kanban_json(
    args: list[str],
    *,
    hermes: str = "hermes",
    timeout: float = 120.0,
) -> Any:
    proc = run_cmd([hermes, "kanban", *args], timeout=timeout)
    text = proc.stdout.strip()
    if not text:
        return None
    return json.loads(text)
