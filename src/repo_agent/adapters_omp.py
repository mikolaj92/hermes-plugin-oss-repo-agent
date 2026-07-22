"""Thin OMP CLI adapter."""

from __future__ import annotations

from pathlib import Path

from repo_agent.adapters_cli import run_cmd


def run_omp(
    *,
    prompt: str,
    cwd: str | Path,
    command: str,
    model: str,
    thinking: str,
    timeout: float,
    dry_run: bool,
) -> dict:
    worktree = Path(cwd).resolve()
    args = [
        command,
        "--cwd",
        str(worktree),
        "--model",
        model,
        "--thinking",
        thinking,
        "--approval-mode",
        "yolo",
        "--no-session",
        "-p",
        prompt,
    ]
    if dry_run:
        return {
            "status": "planned",
            "command": args[:-1],
            "prompt_len": len(prompt),
        }
    proc = run_cmd(
        args,
        timeout=timeout,
        env=None,
        check=True,
        cwd=worktree,
    )
    return {
        "status": "completed",
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-1000:],
    }
