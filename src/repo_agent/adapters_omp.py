"""Thin OMP CLI adapter."""

from __future__ import annotations

from pathlib import Path

from repo_agent.adapters_cli import run_cmd


def run_omp(
    *,
    prompt: str,
    cwd: str | Path,
    model: str,
    timeout: float,
    dry_run: bool,
) -> dict:
    if dry_run:
        return {
            "status": "planned",
            "command": ["omp", "run", "--model", model, "--cwd", str(cwd)],
            "prompt_len": len(prompt),
        }
    proc = run_cmd(
        ["omp", "-p", prompt, "--model", model, "--cwd", str(cwd), "--approval-mode", "yolo"],
        timeout=timeout,
        env=None,
        check=True,
    )
    # Note: real omp flags may differ; this is the atomic adapter boundary.
    return {
        "status": "completed",
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-1000:],
    }
