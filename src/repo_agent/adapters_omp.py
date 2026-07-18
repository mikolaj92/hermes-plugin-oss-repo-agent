"""Thin OMP CLI adapter."""

from __future__ import annotations

from pathlib import Path

from repo_agent.adapters_cli import run_cmd


def run_omp(
    *,
    prompt: str,
    cwd: str | Path,
    model: str | None,
    timeout: float,
    dry_run: bool,
) -> dict:
    cmd = ["omp", "-p", prompt, "--cwd", str(cwd), "--approval-mode", "yolo"]
    if model:
        cmd.extend(["--model", model])

    if dry_run:
        return {
            "status": "planned",
            "command": cmd,
            "prompt_len": len(prompt),
        }

    proc = run_cmd(
        cmd,
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
