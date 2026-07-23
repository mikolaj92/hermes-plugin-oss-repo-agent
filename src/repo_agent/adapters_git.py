"""Thin git CLI adapter (no shell=True)."""

from __future__ import annotations

from pathlib import Path

from repo_agent.adapters_cli import CommandError, run_cmd


def git(args: list[str], *, cwd: str | Path | None = None, timeout: float = 120.0) -> str:
    cmd = ["git", *args]
    # run_cmd does not take cwd — use env and -C
    if cwd is not None:
        cmd = ["git", "-C", str(cwd), *args]
    proc = run_cmd(cmd, timeout=timeout)
    return proc.stdout.strip()

def status_porcelain(worktree_path: str | Path) -> str:
    """Return full porcelain status, failing closed on git errors."""
    return git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=worktree_path)


def remote_ref(clone_path: str | Path, remote: str, branch: str) -> str:
    """Resolve one remote branch to its exact object id."""
    return git(["rev-parse", "--verify", f"refs/remotes/{remote}/{branch}"], cwd=clone_path)


def local_branch_head(clone_path: str | Path, branch: str) -> str:
    return git(["rev-parse", "--verify", f"refs/heads/{branch}"], cwd=clone_path)


def branch_config_get(clone_path: str | Path, branch: str, key: str) -> str:
    return git(["config", "--local", "--get", f"branch.{branch}.{key}"], cwd=clone_path)


def branch_config_set(clone_path: str | Path, branch: str, key: str, value: str) -> None:
    git(["config", "--local", f"branch.{branch}.{key}", value], cwd=clone_path)


def remote_url(clone_path: str | Path, remote: str = "origin") -> str:
    return git(["remote", "get-url", remote], cwd=clone_path)



def worktree_list(clone_path: str) -> str:
    return git(["worktree", "list", "--porcelain"], cwd=clone_path)


def worktree_add(clone_path: str, path: str, branch: str, *, create_branch: bool) -> None:
    args = ["worktree", "add"]
    if create_branch:
        args += ["-b", branch, path]
    else:
        args += [path, branch]
    git(args, cwd=clone_path)


def worktree_remove(clone_path: str, path: str, *, force: bool = False) -> None:
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(path)
    git(args, cwd=clone_path)


def branch_exists(clone_path: str, branch: str) -> bool:
    try:
        git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=clone_path)
        return True
    except CommandError:
        return False


def delete_local_branch(clone_path: str, branch: str, *, force: bool = False) -> None:
    flag = "-D" if force else "-d"
    git(["branch", flag, branch], cwd=clone_path)


def rev_parse(clone_path: str, rev: str = "HEAD") -> str:
    return git(["rev-parse", rev], cwd=clone_path)


def is_dirty(worktree_path: str) -> bool:
    """True if worktree has unstaged/staged/untracked changes."""
    try:
        out = git(["status", "--porcelain"], cwd=worktree_path)
    except CommandError:
        return True
    return bool(out.strip())


def parse_worktree_porcelain(text: str) -> list[dict[str, str]]:
    """Parse `git worktree list --porcelain` into path/branch/head rows."""
    rows: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in (text or "").splitlines():
        if not line.strip():
            if current.get("path"):
                rows.append(current)
            current = {}
            continue
        if line.startswith("worktree "):
            if current.get("path"):
                rows.append(current)
            current = {"path": line[len("worktree ") :].strip()}
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD ") :].strip()
        elif line.startswith("branch "):
            ref = line[len("branch ") :].strip()
            current["branch"] = ref.removeprefix("refs/heads/")
        elif line == "detached":
            current["branch"] = ""
        elif line == "bare":
            current["bare"] = "1"
        elif line == "locked":
            current["locked"] = "1"
        elif line.startswith("locked "):
            current["locked"] = line[len("locked ") :].strip() or "1"
    if current.get("path"):
        rows.append(current)
    return rows


def push_branch(worktree_path: str, branch: str, *, set_upstream: bool = True) -> str:
    args = ["push"]
    if set_upstream:
        args += ["-u", "origin", branch]
    else:
        args += ["origin", branch]
    # longer timeout for network push
    cmd = ["git", "-C", str(worktree_path), *args]
    proc = run_cmd(cmd, timeout=300.0)
    return (proc.stdout or proc.stderr or "").strip()
