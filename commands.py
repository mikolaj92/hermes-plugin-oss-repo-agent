from __future__ import annotations

import hashlib
import io
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from argparse import ArgumentParser, Namespace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows development only
    fcntl = None  # type: ignore[assignment]
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib  # type: ignore
from . import github_cli, kanban
from .config import ConfigError, OssRepoAgentConfig, default_config_path, load_config
from .executor import CommandSpec, Runner, planned_command

INTAKE_ASSIGNEE = "repo-agent-intake"
FALA_PINNED_COMMIT = "69bc2ec9d4cdf61773114847c0c582fb2652296d"
FALA_PINNED_VERSION = "0.7.9"


def setup_parser(parser: ArgumentParser) -> None:
    parser.add_argument("--config", default=None)
    subparsers = parser.add_subparsers(dest="oss_repo_agent_command")
    subparsers.required = True
    init = subparsers.add_parser("init")
    init.add_argument("--repo", default="owner/example-repo")
    init.add_argument("--board", default="owner-example-repo")
    init.add_argument("--clone-root", default="./repos")
    init.add_argument("--worktree-root", default="./worktrees")
    init.add_argument("--assignee", default=None)
    init.add_argument("--force", action="store_true")
    subparsers.add_parser("validate")
    bootstrap = subparsers.add_parser("bootstrap")
    bootstrap.add_argument("--apply", action="store_true")
    intake = subparsers.add_parser("intake")
    intake.add_argument("--live", action="store_true")
    intake.add_argument("--limit", type=int, default=None)
    dispatch = subparsers.add_parser("dispatch")
    dispatch.add_argument("--live", action="store_true")
    dispatch.add_argument("--run-executor", action="store_true")
    dispatch.add_argument("--max", type=int, default=20)
    triage = subparsers.add_parser("pr-triage")
    triage.add_argument("--live", action="store_true")
    triage.add_argument("--comment", action="store_true")
    launchd = subparsers.add_parser("render-launchd")
    launchd.add_argument("--output", required=True)
    launchd.add_argument("--fala-db", default=None)
    launchd.add_argument("--mode", choices=("dry-run", "live"), default="dry-run")
    launchd.add_argument("--deployment-root", default=None)
    validate_candidate = subparsers.add_parser("validate-fala-candidate")
    validate_candidate.add_argument("--candidate", required=True)
    validate_candidate.add_argument("--deployment-root", default=None)
    deploy = subparsers.add_parser("deploy-fala")
    deploy.add_argument("--candidate", required=True)
    deploy.add_argument("--deployment-root", default=None)
    deploy.add_argument("--promote", action="store_true")


def handle_cli(args: Namespace) -> int:
    try:
        result = run_from_args(args)
    except ConfigError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        return 2
    except Exception as exc:
        from .tools.deployment_parity import DeploymentParityError
        if isinstance(exc, DeploymentParityError):
            print(json.dumps(exc.result, indent=2, sort_keys=True))
            return 1
        raise
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def run_from_args(args: Namespace, runner: Runner | None = None) -> dict[str, Any]:
    command = getattr(args, "oss_repo_agent_command")
    if command == "init":
        return init_project(
            getattr(args, "config", None),
            str(getattr(args, "repo", "owner/example-repo")),
            str(getattr(args, "board", "owner-example-repo")),
            str(getattr(args, "clone_root", "./repos")),
            str(getattr(args, "worktree_root", "./worktrees")),
            getattr(args, "assignee", None),
            bool(getattr(args, "force", False)),
        )
    if command == "validate-fala-candidate":
        from .tools.deployment_parity import validate_fala_candidate

        candidate_arg = Path(str(getattr(args, "candidate"))).expanduser()
        root_arg = getattr(args, "deployment_root", None)
        root_path = None
        if root_arg:
            root_path = Path(str(root_arg)).expanduser().absolute()
            if not candidate_arg.is_absolute():
                candidate_arg = root_path / "candidates" / candidate_arg
        return validate_fala_candidate(candidate_arg, deployment_root=root_path)
    cfg = load_config(getattr(args, "config", None))
    if command == "validate":
        return validate(cfg)
    if command == "bootstrap":
        return bootstrap(cfg, bool(getattr(args, "apply", False)))
    if command == "intake":
        limit = getattr(args, "limit", None) or cfg.github.default_limit
        return intake(cfg, bool(getattr(args, "live", False)), int(limit), runner or Runner())
    if command == "dispatch":
        return dispatch(cfg, bool(getattr(args, "live", False)), bool(getattr(args, "run_executor", False)), int(getattr(args, "max", 20)))
    if command == "pr-triage":
        return pr_triage(cfg, bool(getattr(args, "live", False)), bool(getattr(args, "comment", False)), runner or Runner())
    if command == "render-launchd":
        return render_launchd(
            cfg,
            str(getattr(args, "output")),
            fala_db=getattr(args, "fala_db", None),
            mode=str(getattr(args, "mode", "dry-run")),
            config_path=getattr(args, "config", None),
            deployment_root=getattr(args, "deployment_root", None),
        )
    if command == "deploy-fala":
        return deploy_fala(
            cfg,
            str(getattr(args, "candidate")),
            bool(getattr(args, "promote", False)),
            deployment_root=getattr(args, "deployment_root", None),
        )
    raise ConfigError(f"unknown command: {command}")


def init_project(config_path: str | None, repo: str, board: str, clone_root: str, worktree_root: str, assignee: str | None, force: bool) -> dict[str, Any]:
    target = Path(config_path).expanduser() if config_path else default_config_path()
    if target.exists() and not force:
        raise ConfigError(f"config already exists: {target}; pass --force to overwrite")
    target.parent.mkdir(parents=True, exist_ok=True)
    leaf = repo.split("/")[-1] or "example-repo"
    text = starter_config(repo, board, clone_root, worktree_root, leaf, assignee or repo.split("/", 1)[0])
    _atomic_write(target, text.encode("utf-8"))
    config_arg = str(target)
    return {
        "ok": True,
        "config": config_arg,
        "created": [config_arg],
        "next_commands": [
            f"hermes oss-repo-agent --config {config_arg} validate",
            f"hermes oss-repo-agent --config {config_arg} intake --limit 3",
            f"hermes oss-repo-agent --config {config_arg} dispatch --max 2",
        ],
        "safety": safety_guards(),
    }


def starter_config(repo: str, board: str, clone_root: str, worktree_root: str, leaf: str, assignee: str) -> str:
    clone_path = f"{clone_root.rstrip('/')}/{leaf}"
    return "\n".join([
        "version: 1",
        "mode: dry-run",
        f"clone_root: {clone_root}",
        f"worktree_root: {worktree_root}",
        "branch_prefix: ai/fix",
        "automerge: false",
        "github:",
        "  cli: gh",
        "  default_limit: 10",
        f"  assignee: {assignee}",
        "labels:",
        "  ready: ai:ready",
        "  in_progress: ai:in-progress",
        "  blocked: ai:blocked",
        "  pr_opened: ai:pr-opened",
        "  generated: ai:generated",
        "executor:",
        "  enabled: false",
        "  command: opencode",
        "  timeout_seconds: 1800",
        "repos:",
        f"  - repo: {repo}",
        f"    board: {board}",
        f"    clone_path: {clone_path}",
        "    trusted_authors: []",
        "    trusted_branch_prefixes: [ai/fix]",
        "    allowed_base_branches: [main]",
        "    external_pr_policy: block",
        "",
    ])


def safety_guards() -> list[str]:
    return [
        "dry-run unless config mode is live and --live or --apply is passed",
        "GitHub operations use gh CLI wrappers only",
        "GitHub issues are claimed before Kanban intake when github.assignee is configured",
        "GitHub content is treated as untrusted evidence",
        "no PR merge support in v0",
        "no force-push or branch deletion behavior",
    ]


def validate(cfg: OssRepoAgentConfig) -> dict[str, Any]:
    return {
        "ok": True,
        "mode": cfg.mode,
        "repos": [repo.repo for repo in cfg.repos],
        "automerge": cfg.automerge,
        "github_assignee": cfg.github.assignee,
        "safe_defaults": {
            "dry_run": cfg.mode == "dry-run",
            "automerge": cfg.automerge,
            "executor_enabled": cfg.executor.enabled,
        },
        "skills": [
            "oss-repo-agent:repo-gh-cli-policy",
            "oss-repo-agent:repo-audit-finding-format",
            "oss-repo-agent:repo-fix-issue-pr",
            "oss-repo-agent:repo-review-agent-pr",
        ],
    }


def bootstrap(cfg: OssRepoAgentConfig, apply: bool) -> dict[str, Any]:
    live = cfg.effective_live(apply)
    return {
        "ok": True,
        "effective_live": live,
        "planned_boards": [{"repo": repo.repo, "board": repo.board} for repo in cfg.repos],
        "message": "bootstrap renders board and label intent; mutation adapters are intentionally explicit",
    }


def _issue_labels(issue: dict[str, Any]) -> set[str]:
    return {str(label.get("name", "")) for label in issue.get("labels", []) if isinstance(label, dict)}


def _eligible_issue(issue: dict[str, Any], cfg: OssRepoAgentConfig) -> bool:
    labels = _issue_labels(issue)
    return not labels.intersection({cfg.labels.in_progress, cfg.labels.blocked, cfg.labels.pr_opened})


def _parse_rows(result_stdout: str, label: str) -> list[dict[str, Any]]:
    if not isinstance(result_stdout, str) or not result_stdout.strip():
        raise ValueError(f"{label} response is empty")
    try:
        data = json.loads(result_stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} response is not valid JSON") from exc
    if not isinstance(data, list):
        raise ValueError(f"{label} response must be a JSON array")
    if any(not isinstance(item, dict) for item in data):
        raise ValueError(f"{label} response must contain only JSON objects")
    return data


def _issue_rows(result_stdout: str) -> list[dict[str, Any]]:
    return _parse_rows(result_stdout, "issue list")


def _kanban_task_rows(result_stdout: str) -> list[dict[str, Any]]:
    return _parse_rows(result_stdout, "Kanban list")


def _existing_open_issue_work(tasks: list[dict[str, Any]], repo: str, number: int) -> bool:
    title_needle = f"{repo}#{number}"
    repo_line = f"Repository: {repo}"
    issue_line = f"Issue: #{number}"
    for task in tasks:
        if str(task.get("status") or "") == "done":
            continue
        title = str(task.get("title") or "")
        if not title.startswith(("[issue]", "[fix-pr]", "[fix-pr-review]")):
            continue
        body = str(task.get("body") or "")
        if title_needle in title or (repo_line in body and issue_line in body):
            return True
    return False


def _kanban_list_spec(board: str) -> CommandSpec:
    return CommandSpec(("hermes", "kanban", "--board", board, "list", "--json", "--sort", "created-desc"))


def intake(cfg: OssRepoAgentConfig, live_flag: bool, limit: int, runner: Runner) -> dict[str, Any]:
    live = cfg.effective_live(live_flag)
    list_commands = [github_cli.issue_list(repo.repo, limit) for repo in cfg.repos]
    list_results: list[Any] = []
    list_errors: list[Exception | None] = []
    for command in list_commands:
        try:
            list_results.append(runner.run(command, live=live))
            list_errors.append(None)
        except Exception as exc:
            list_results.append(None)
            list_errors.append(exc)
    inspection_commands: list[CommandSpec] = []
    inspection_results: list[Any] = []
    mutation_commands: list[CommandSpec] = []
    mutation_results: list[Any] = []
    ensured_tasks: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    repository_results: list[dict[str, Any]] = []

    def failure(repo_result: dict[str, Any], stage: str, result: Any = None, error: Exception | None = None) -> None:
        detail: dict[str, Any] = {"repo": repo_result["repo"], "stage": stage}
        if result is not None:
            detail["returncode"] = int(getattr(result, "returncode", 1))
            detail["stderr"] = str(getattr(result, "stderr", "") or "")
        if error is not None:
            detail["error"] = str(error)
        failures.append(detail)
        repo_result.setdefault("failures", []).append(detail)

    for index, repo in enumerate(cfg.repos):
        repo_result: dict[str, Any] = {"repo": repo.repo, "failures": []}
        repository_results.append(repo_result)
        result = list_results[index]
        if not live:
            repo_result["issue_list"] = {"status": "planned"}
            repo_result["board_list"] = {"status": "planned"}
            continue
        if list_errors[index] is not None:
            repo_result["issue_list"] = {"status": "failed"}
            failure(repo_result, "issue-list", error=list_errors[index])
            continue
        if result is None or result.returncode != 0:
            repo_result["issue_list"] = {"status": "failed"}
            failure(repo_result, "issue-list", result=result)
            continue
        try:
            issues = _issue_rows(result.stdout)
        except ValueError as exc:
            repo_result["issue_list"] = {"status": "failed"}
            failure(repo_result, "issue-list-response", result=result, error=exc)
            continue
        repo_result["issue_list"] = {"status": "ok", "rows": issues}
        board_list = _kanban_list_spec(repo.board)
        inspection_commands.append(board_list)
        try:
            board_result = runner.run(board_list, live=True)
            inspection_results.append(board_result)
        except Exception as exc:
            repo_result["board_list"] = {"status": "failed"}
            failure(repo_result, "board-list", error=exc)
            continue
        if board_result.returncode != 0:
            repo_result["board_list"] = {"status": "failed"}
            failure(repo_result, "board-list", result=board_result)
            continue
        try:
            existing_tasks = _kanban_task_rows(board_result.stdout)
        except ValueError as exc:
            repo_result["board_list"] = {"status": "failed"}
            failure(repo_result, "board-list-response", result=board_result, error=exc)
            continue
        repo_result["board_list"] = {"status": "ok", "rows": existing_tasks}
        for issue in issues:
            if not _eligible_issue(issue, cfg):
                continue
            number = issue.get("number")
            if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
                failure(repo_result, "issue-shape", error=ValueError("issue number must be a positive integer"))
                continue
            if _existing_open_issue_work(existing_tasks, repo.repo, number):
                ensured_tasks.append({"repo": repo.repo, "issue": number, "board": repo.board, "existing": True})
                continue
            title = str(issue.get("title") or "")
            body = f"GitHub issue: {issue.get('url') or ''}"
            if cfg.github.assignee:
                claim = github_cli.issue_claim(repo.repo, number, cfg.github.assignee)
                mutation_commands.append(claim)
                try:
                    claim_result = runner.run(claim, live=True)
                    mutation_results.append(claim_result)
                except Exception as exc:
                    failure(repo_result, "claim", error=exc)
                    continue
                if claim_result.returncode != 0:
                    failure(repo_result, "claim", result=claim_result)
                    continue
            draft = kanban.issue_task(repo.repo, repo.board, number, title, body, repo.clone_path)
            create = kanban.create_task_spec(draft, assignee=INTAKE_ASSIGNEE)
            mutation_commands.append(create)
            try:
                create_result = runner.run(create, live=True)
                mutation_results.append(create_result)
            except Exception as exc:
                failure(repo_result, "create-task", error=exc)
                continue
            if create_result.returncode != 0:
                failure(repo_result, "create-task", result=create_result)
                continue
            ensured_tasks.append({"repo": repo.repo, "issue": number, "board": repo.board, "idempotency_key": draft.idempotency_key})
    all_results = tuple(result for result in (*list_results, *inspection_results, *mutation_results) if result is not None)
    return {
        "ok": not failures,
        "effective_live": live,
        "planned_work": [
            {"repo": repo.repo, "action": "claim eligible GitHub issues and ensure idempotent Kanban intake tasks", "mutation": live}
            for repo in cfg.repos
        ],
        "safety_guards": safety_guards(),
        "commands": [planned_command(command) for command in (*list_commands, *inspection_commands, *mutation_commands)],
        "executed": [result.executed for result in all_results],
        "ensured_tasks": ensured_tasks,
        "repository_results": repository_results,
        "failures": failures,
    }


def dispatch(cfg: OssRepoAgentConfig, live_flag: bool, run_executor: bool, max_tasks: int) -> dict[str, Any]:
    live = cfg.effective_live(live_flag)
    executor_runs = cfg.executor_runs(live_flag, run_executor)
    return {
        "ok": True,
        "effective_live": live,
        "executor_runs": executor_runs,
        "max_tasks": max_tasks,
        "planned_work": [
            {
                "repo": repo.repo,
                "action": "draft guarded Kanban tasks for approved issues",
                "mutation": live,
                "executor_runs": executor_runs,
            }
            for repo in cfg.repos
        ],
        "safety_guards": safety_guards(),
        "message": "dispatch uses Kanban task drafts and requires explicit executor gates",
    }


def _pr_rows(result_stdout: str) -> list[dict[str, Any]]:
    return _parse_rows(result_stdout, "PR list")


def _claimable_pr(repo_name: str, pr: dict[str, Any], branch_prefix: str) -> bool:
    author = pr.get("author") if isinstance(pr.get("author"), dict) else {}
    owner = repo_name.split("/", 1)[0]
    head = str(pr.get("headRefName") or "")
    return str(author.get("login") or "").lower() == owner.lower() and head.startswith(f"{branch_prefix.rstrip('/')}/")


def pr_triage(cfg: OssRepoAgentConfig, live_flag: bool, comment: bool, runner: Runner) -> dict[str, Any]:
    live = cfg.effective_live(live_flag)
    list_commands = [github_cli.pr_list(repo.repo) for repo in cfg.repos]
    list_results: list[Any] = []
    list_errors: list[Exception | None] = []
    for command in list_commands:
        try:
            list_results.append(runner.run(command, live=live))
            list_errors.append(None)
        except Exception as exc:
            list_results.append(None)
            list_errors.append(exc)
    mutation_commands: list[CommandSpec] = []
    mutation_results: list[Any] = []
    claimed_prs: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    repository_results: list[dict[str, Any]] = []

    def failure(repo_result: dict[str, Any], stage: str, result: Any = None, error: Exception | None = None) -> None:
        detail: dict[str, Any] = {"repo": repo_result["repo"], "stage": stage}
        if result is not None:
            detail["returncode"] = int(getattr(result, "returncode", 1))
            detail["stderr"] = str(getattr(result, "stderr", "") or "")
        if error is not None:
            detail["error"] = str(error)
        failures.append(detail)
        repo_result.setdefault("failures", []).append(detail)

    for index, repo in enumerate(cfg.repos):
        repo_result: dict[str, Any] = {"repo": repo.repo, "failures": [], "claims": []}
        repository_results.append(repo_result)
        result = list_results[index]
        if not live:
            repo_result["pr_list"] = {"status": "planned"}
            continue
        if list_errors[index] is not None:
            repo_result["pr_list"] = {"status": "failed"}
            failure(repo_result, "pr-list", error=list_errors[index])
            continue
        if result is None or result.returncode != 0:
            repo_result["pr_list"] = {"status": "failed"}
            failure(repo_result, "pr-list", result=result)
            continue
        try:
            prs = _pr_rows(result.stdout)
        except ValueError as exc:
            repo_result["pr_list"] = {"status": "failed"}
            failure(repo_result, "pr-list-response", result=result, error=exc)
            continue
        repo_result["pr_list"] = {"status": "ok", "rows": prs}
        if not cfg.github.assignee:
            continue
        for pr in prs:
            if not _claimable_pr(repo.repo, pr, cfg.branch_prefix):
                continue
            number = pr.get("number")
            if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
                failure(repo_result, "pr-shape", error=ValueError("PR number must be a positive integer"))
                continue
            claim = github_cli.pr_claim(repo.repo, number, cfg.github.assignee)
            mutation_commands.append(claim)
            try:
                claim_result = runner.run(claim, live=True)
                mutation_results.append(claim_result)
            except Exception as exc:
                failure(repo_result, "claim", error=exc)
                continue
            repo_result["claims"].append({"pr": number, "assignee": cfg.github.assignee, "status": "ok" if claim_result.returncode == 0 else "failed"})
            if claim_result.returncode != 0:
                failure(repo_result, "claim", result=claim_result)
                continue
            claimed_prs.append({"repo": repo.repo, "pr": number, "assignee": cfg.github.assignee})
    all_results = tuple(result for result in (*list_results, *mutation_results) if result is not None)
    return {
        "ok": not failures,
        "effective_live": live,
        "comment_enabled": bool(comment) and live,
        "merge_behavior": "not-supported-in-v0",
        "planned_work": [
            {"repo": repo.repo, "action": "claim owner-authored agent PRs for triage; merge remains outside the CLI facade", "mutation": live}
            for repo in cfg.repos
        ],
        "commands": [planned_command(command) for command in (*list_commands, *mutation_commands)],
        "executed": [result.executed for result in all_results],
        "claimed_prs": claimed_prs,
        "repository_results": repository_results,
        "failures": failures,
    }
def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _read_git_revision(root: Path, fallback: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return fallback
def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise ConfigError(f"unable to open directory for fsync: {path}") from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise ConfigError(f"unable to fsync directory: {path}") from exc
    finally:
        os.close(fd)


def _fsync_tree(root: Path) -> None:
    """Durably persist every file and directory in a copied version tree."""
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_file():
            try:
                fd = os.open(path, os.O_RDONLY)
            except OSError as exc:
                raise ConfigError(f"unable to open version file for fsync: {path}") from exc
            try:
                os.fsync(fd)
            except OSError as exc:
                raise ConfigError(f"unable to fsync version file: {path}") from exc
            finally:
                os.close(fd)
        elif path.is_dir():
            _fsync_directory(path)
    _fsync_directory(root)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _remove_tree(path: Path) -> None:
    for child in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        try:
            child.chmod(0o755)
        except OSError:
            pass
    try:
        path.chmod(0o755)
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)

@contextmanager
def _deployment_lock(root: Path):
    """Serialize promotion and rollback across independent deploy invocations."""
    if fcntl is None:
        raise ConfigError("deployment promotion locking is unavailable")
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".promotion.lock"
    try:
        handle = lock_path.open("a+b")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError as exc:
        raise ConfigError(f"unable to acquire deployment promotion lock: {exc}") from exc
    try:
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _render_fala_plist(*, project_root: Path, config_path: Path, db_path: Path, mode: str, home: Path, uv_bin: str, log_dir: Path) -> bytes:
    template = project_root / "templates" / "launchd" / "oss-repo-agent-fala-tick-all.plist.template"
    if not template.is_file():
        raise ConfigError(f"Fala launchd template not found: {template}")
    values = {
        "{{UV_BIN}}": str(uv_bin),
        "{{PROJECT_ROOT}}": str(project_root),
        "{{REPO_ROOT}}": str(project_root),
        "{{CONFIG_PATH}}": str(config_path),
        "{{DB_PATH}}": str(db_path),
        "{{MODE_ARG}}": f"--{mode}",
        "{{HOME}}": str(home),
        "{{LOG_DIR}}": str(log_dir),
    }
    text = template.read_text(encoding="utf-8")
    for marker, value in values.items():
        text = text.replace(marker, value)
    if "{{" in text or "}}" in text:
        raise ConfigError("unresolved Fala launchd template placeholder")
    try:
        document = plistlib.loads(text.encode("utf-8"))
    except plistlib.InvalidFileException as exc:
        raise ConfigError(f"invalid Fala launchd template: {exc}") from exc
    arguments = document.get("ProgramArguments")
    required = [str(uv_bin), "run", "--frozen", "--project", str(project_root), "repo-agent-tick-all", "--config", str(config_path), "--db", str(db_path), f"--{mode}", "--json"]
    if arguments != required:
        raise ConfigError("Fala ProgramArguments do not match immutable candidate contract")
    if (
        document.get("Label") != "com.mikolaj92.hermes.repo-agent-fala-tick-all"
        or document.get("StartInterval") != 600
        or document.get("ProcessType") != "Background"
        or document.get("RunAtLoad") is not False
        or document.get("LimitLoadToSessionType") != "Background"
    ):
        raise ConfigError("Fala launchd schedule or session contract is invalid")
    return plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=False)
def _runtime_identity(document: dict[str, Any], plist_data: bytes) -> dict[str, Any]:
    return {
        "program_arguments": list(document.get("ProgramArguments") or []),
        "working_directory": document.get("WorkingDirectory"),
        "standard_out_path": document.get("StandardOutPath"),
        "standard_error_path": document.get("StandardErrorPath"),
        "environment_variables": dict(document.get("EnvironmentVariables") or {}),
        "start_interval": document.get("StartInterval"),
        "run_at_load": document.get("RunAtLoad"),
        "process_type": document.get("ProcessType"),
        "limit_load_to_session_type": document.get("LimitLoadToSessionType"),
        "plist_sha256": _sha256_bytes(plist_data),
    }
def _copy_git_tree(repo: Path, revision: str, destination: Path) -> None:
    try:
        archive = subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", revision],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ConfigError(f"unable to archive pinned source {repo}: {exc}") from exc
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as bundle:
            for member in bundle.getmembers():
                target = (destination / member.name).resolve()
                try:
                    target.relative_to(destination.resolve())
                except ValueError as exc:
                    raise ConfigError("pinned source archive contains an unsafe path") from exc
            bundle.extractall(destination)
    except (OSError, tarfile.TarError) as exc:
        raise ConfigError(f"unable to unpack pinned source {repo}: {exc}") from exc


def _copy_candidate_source(project_root: Path, destination: Path, config: Path, lock: Path) -> dict[str, bytes]:
    """Copy the runnable plugin and the complete pinned local Fala dependency."""
    project = destination / "project"
    project.mkdir(parents=True)
    for relative in ("src", "templates", "fala-package.toml", "pyproject.toml", "README.md", "LICENSE"):
        source = project_root / relative
        target = project / relative
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif source.is_file():
            shutil.copy2(source, target)
        else:
            raise ConfigError(f"Fala candidate source is missing: {source}")
    fala_root = (project_root.parent / "Fala").resolve()
    if not (fala_root / "python" / "fala").is_dir() or not (fala_root / "pyproject.toml").is_file():
        raise ConfigError(f"pinned Fala source is missing: {fala_root}")
    try:
        status = subprocess.run(
            ["git", "-C", str(fala_root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
        submodules = subprocess.run(
            ["git", "-C", str(fala_root), "submodule", "status", "--recursive"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ConfigError(f"unable to verify pinned Fala checkout: {exc}") from exc
    if status.stdout.strip():
        raise ConfigError("pinned Fala checkout is dirty")
    fala_target = project / "Fala"
    _copy_git_tree(fala_root, FALA_PINNED_COMMIT, project / "Fala")
    for line in submodules.stdout.splitlines():
        if not line or line[0] != " ":
            raise ConfigError("pinned Fala submodules are not initialized at recorded commits")
        fields = line[1:].split()
        if len(fields) < 2:
            raise ConfigError("unable to parse pinned Fala submodule status")
        commit, relative = fields[:2]
        submodule = (fala_root / relative).resolve()
        try:
            submodule.relative_to(fala_root)
        except ValueError as exc:
            raise ConfigError("pinned Fala submodule path is unsafe") from exc
        _copy_git_tree(submodule, commit, project / "Fala" / relative)
    (fala_target / "revision.txt").write_text(
        FALA_PINNED_COMMIT + "\n",
        encoding="utf-8",
    )
    shutil.copy2(config, destination / "config.toml")
    pyproject = (project / "pyproject.toml").read_text(encoding="utf-8")
    pyproject = pyproject.replace(
        'fala = { path = "../Fala", editable = true }',
        'fala = { path = "Fala", editable = true }',
    )
    (project / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    lock_data = lock.read_bytes().replace(b'editable = "../Fala"', b'editable = "Fala"')
    (project / "uv.lock").write_bytes(lock_data)
    return {"config.toml": config.read_bytes(), "uv.lock": lock_data}


def render_launchd(
    cfg: OssRepoAgentConfig,
    output: str,
    *,
    fala_db: str | None = None,
    mode: str = "dry-run",
    config_path: str | None = None,
    deployment_root: str | None = None,
) -> dict[str, Any]:
    if mode not in {"dry-run", "live"}:
        raise ConfigError("candidate mode must be dry-run or live")
    if mode == "live" and cfg.mode != "live":
        raise ConfigError("live candidate requires config mode='live'")
    if mode != cfg.mode:
        raise ConfigError(f"Fala candidate mode does not match config mode: {cfg.mode}")
    project_root = Path(__file__).resolve().parent
    candidate = Path(output).expanduser().resolve()
    root = Path(deployment_root).expanduser().absolute() if deployment_root else None
    if root is not None:
        candidates_root = (root / "candidates").resolve()
        try:
            candidate.resolve().relative_to(candidates_root)
        except ValueError as exc:
            raise ConfigError(f"candidate output must be inside deployment candidates root: {candidates_root}") from exc
    config = Path(config_path).expanduser().absolute() if config_path else default_config_path().expanduser().absolute()
    db = Path(fala_db).expanduser().absolute() if fala_db else Path.home() / ".hermes" / "oss-repo-agent" / "fala" / "state.sqlite"
    lock = project_root / "uv.lock"
    if not config.is_file() or not config.stat().st_size:
        raise ConfigError(f"Fala candidate source config is missing or empty: {config}")
    if not lock.is_file() or not lock.stat().st_size:
        raise ConfigError(f"Fala candidate source lock is missing or empty: {lock}")
    try:
        config_data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"invalid Fala candidate source config: {config}") from exc
    if not isinstance(config_data, dict):
        raise ConfigError("Fala candidate source config root must be a mapping")
    try:
        plugin_status = subprocess.run(
            ["git", "-C", str(project_root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ConfigError(f"unable to verify plugin checkout: {exc}") from exc
    if plugin_status.stdout.strip():
        raise ConfigError("plugin checkout is dirty")
    revision = _read_git_revision(project_root, "")
    fala_root = (project_root.parent / "Fala").resolve()
    try:
        pinned_present = subprocess.run(
            ["git", "-C", str(fala_root), "cat-file", "-e", f"{FALA_PINNED_COMMIT}^{{commit}}"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ConfigError(f"Fala candidate source does not contain pinned commit {FALA_PINNED_COMMIT}") from exc
    if pinned_present.returncode != 0:
        raise ConfigError(f"Fala candidate source does not contain pinned commit {FALA_PINNED_COMMIT}")
    try:
        pinned_pyproject = subprocess.run(
            ["git", "-C", str(fala_root), "show", f"{FALA_PINNED_COMMIT}:pyproject.toml"],
            check=True,
            capture_output=True,
            text=True,
        )
        pinned_metadata = tomllib.loads(pinned_pyproject.stdout)
        pinned_version = pinned_metadata.get("project", {}).get("version")
    except (OSError, subprocess.CalledProcessError, tomllib.TOMLDecodeError, AttributeError) as exc:
        raise ConfigError("unable to verify pinned Fala version") from exc
    if pinned_version != FALA_PINNED_VERSION:
        raise ConfigError(f"pinned Fala commit version must be {FALA_PINNED_VERSION}")
    fala_revision = FALA_PINNED_COMMIT
    lock_data = lock.read_bytes().replace(b'editable = "../Fala"', b'editable = "Fala"')
    lock_hash = _sha256_bytes(lock_data)
    policy = {
        "automerge": bool(cfg.automerge),
        "require_human_approval": bool(cfg.require_human_approval),
        "require_checks": bool(cfg.require_checks),
        "require_test_evidence": bool(cfg.require_test_evidence),
        "executor_enabled": bool(cfg.executor.enabled),
    }
    identity: dict[str, Any] = {
        "schema": 1,
        "mode": mode,
        "plugin_commit": revision,
        "fala_tag": FALA_PINNED_VERSION,
        "fala_commit": fala_revision,
        "lock_hash": lock_hash,
        "config_path": str(config),
        "config_hash": _sha256_file(config),
        "db_path": str(db),
        "metadata_path": "source/metadata.json",
        "lock_path": "source/project/uv.lock",
        "config_artifact_path": "source/config.toml",
        "revision_path": "source/revision.txt",
        "policy": policy,
    }
    candidate_id = _sha256_bytes(_canonical_json(identity))
    if candidate.name != candidate_id:
        raise ConfigError(f"candidate output directory must be named {candidate_id}")
    metadata = {"plugin_commit": revision, "fala_tag": FALA_PINNED_VERSION, "fala_commit": fala_revision, "lock_hash": lock_hash}
    source_data = _canonical_json(metadata)
    if candidate.exists():
        existing = candidate / "manifest.json"
        if not existing.is_file():
            raise ConfigError(f"candidate output already exists without manifest: {candidate}")
        try:
            old = json.loads(existing.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"invalid existing candidate manifest: {candidate}") from exc
        if old.get("identity") != identity or old.get("candidate_id") != candidate_id:
            raise ConfigError("existing candidate does not match requested mode/config/db/revision")
        from .tools.deployment_parity import validate_fala_candidate
        validate_fala_candidate(candidate, deployment_root=root)
        return {"ok": True, "candidate": str(candidate), "candidate_id": candidate_id, "created": False, "mode": mode}
    candidate.mkdir(parents=True)
    try:
        (candidate / "launchd").mkdir()
        (candidate / "source").mkdir()
        _copy_candidate_source(project_root, candidate / "source", config, lock)
        native_dir = candidate / "source" / "project" / "Fala" / "vendor" / "sqlite.fire" / "native"
        try:
            subprocess.run(
                ["make", "-C", str(native_dir)],
                check=True,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise ConfigError(f"unable to build candidate Fala native library: {exc}") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise ConfigError(f"unable to build candidate Fala native library: {detail}") from exc
        fala_root = candidate / "source" / "project" / "Fala"
        process_host_source = fala_root / "mojo" / "fala" / "native_process_host.c"
        process_host_dir = fala_root / "mojo" / "fala" / "native"
        process_host_dir.mkdir(parents=True)
        process_host_name = "libfala_process_host.dylib" if sys.platform == "darwin" else "libfala_process_host.so"
        process_host = process_host_dir / process_host_name
        process_host_command = ["cc", "-std=c11", "-Wall", "-Wextra"]
        process_host_command.extend(["-dynamiclib"] if sys.platform == "darwin" else ["-fPIC", "-shared"])
        process_host_command.extend(["-o", str(process_host), str(process_host_source)])
        try:
            subprocess.run(process_host_command, check=True, capture_output=True, text=True)
        except OSError as exc:
            raise ConfigError(f"unable to build candidate Fala process host: {exc}") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise ConfigError(f"unable to build candidate Fala process host: {detail}") from exc
        build_env = dict(os.environ)
        mojo = next(
            (
                value
                for value in (build_env.get("MODULAR_MOJO_MAX_DRIVER_PATH"), build_env.get("MOJO"))
                if value and Path(value).is_file()
            ),
            None,
        ) or shutil.which("mojo", path=build_env.get("PATH"))
        if not mojo:
            pixi_root = Path(build_env["CONDA_PREFIX"]) if build_env.get("CONDA_PREFIX") else project_root.parent / "Fala" / ".pixi" / "envs" / "default"
            pixi_mojo = pixi_root / "bin" / "mojo"
            pixi_import = pixi_root / "lib" / "mojo"
            if pixi_mojo.is_file() and pixi_import.is_dir():
                mojo = str(pixi_mojo)
                build_env.setdefault("MODULAR_MAX_PACKAGE_ROOT", str(pixi_root))
                build_env.setdefault("MODULAR_MOJO_MAX_PACKAGE_ROOT", str(pixi_root))
                build_env.setdefault("MODULAR_MOJO_MAX_DRIVER_PATH", mojo)
                build_env.setdefault("MODULAR_MOJO_MAX_IMPORT_PATH", str(pixi_import))
                build_env["PATH"] = str(pixi_root / "bin") + os.pathsep + build_env.get("PATH", "")
        if not mojo:
            raise ConfigError("unable to locate Mojo compiler for candidate runtime")
        mojo_cache = fala_root / "python" / "fala" / "__mojocache__"
        mojo_cache.mkdir()
        mojo_sources = sorted(
            list((fala_root / "python" / "fala").glob("*.mojo"))
            + list((fala_root / "mojo" / "fala").rglob("*.mojo"))
            + list((fala_root / "vendor" / "EmberJson").rglob("*.mojo"))
            + list((fala_root / "vendor" / "sqlite.fire").rglob("*.mojo"))
        )
        digest = hashlib.sha256()
        for path in mojo_sources:
            try:
                relative = str(path.relative_to(fala_root))
            except ValueError:
                relative = path.name
            digest.update(relative.encode())
            digest.update(path.read_bytes())
        mojo_output = mojo_cache / f"_native.hash-{digest.hexdigest()[:16]}.so"
        try:
            subprocess.run(
                [mojo, "build", str(fala_root / "python" / "fala" / "_native.mojo"), "--emit", "shared-lib", "-I", str(fala_root / "mojo"), "-I", str(fala_root / "vendor" / "EmberJson"), "-I", str(fala_root / "vendor" / "sqlite.fire" / "src"), "-o", str(mojo_output)],
                check=True,
                capture_output=True,
                text=True,
                env=build_env,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ConfigError(f"unable to build candidate Fala Mojo extension: {exc}") from exc
        candidate_project = candidate / "source" / "project"
        candidate_config = candidate / "source" / "config.toml"
        uv_bin = shutil.which("uv")
        if not uv_bin or not Path(uv_bin).is_absolute():
            raise ConfigError("unable to locate absolute uv executable")
        plist_data = _render_fala_plist(project_root=candidate_project, config_path=candidate_config, db_path=db, mode=mode, home=Path.home().resolve(), uv_bin=uv_bin, log_dir=((root or candidate.parent.parent) / "logs" / candidate_id).absolute())
        revision_data = (revision + "\n").encode()
        document = plistlib.loads(plist_data)
        runtime_identity = _runtime_identity(document, plist_data)
        _atomic_write(candidate / "launchd" / "com.mikolaj92.hermes.repo-agent-fala-tick-all.plist", plist_data)
        _atomic_write(candidate / "source" / "metadata.json", source_data)
        _atomic_write(candidate / "source" / "revision.txt", revision_data)
        artifacts: dict[str, dict[str, Any]] = {}
        for path in candidate.rglob("*"):
            if path.is_file():
                rel = str(path.relative_to(candidate))
                artifacts[rel] = {"sha256": _sha256_file(path), "bytes": path.stat().st_size}
        manifest_payload: dict[str, Any] = dict(identity)
        manifest_payload.update({
            "candidate_id": candidate_id,
            "identity": identity,
            "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "program_arguments": document["ProgramArguments"],
            "artifacts": artifacts,
            "runtime_identity": runtime_identity,
        })
        _atomic_write(candidate / "manifest.json", _canonical_json(manifest_payload))
        for path in candidate.rglob("*"):
            if path.is_file():
                path.chmod(0o444)
            elif path.is_dir():
                path.chmod(0o555)
        candidate.chmod(0o555)
        _fsync_tree(candidate)
        _fsync_directory(candidate.parent)
    except Exception:
        try:
            _remove_tree(candidate)
        except OSError:
            pass
        raise
    from .tools.deployment_parity import validate_fala_candidate
    validate_fala_candidate(candidate, deployment_root=root)
    return {"ok": True, "candidate": str(candidate), "candidate_id": candidate_id, "created": True, "mode": mode}


def _candidate_root(candidate: Path, deployment_root: str | None) -> Path:
    if deployment_root:
        return Path(deployment_root).expanduser().absolute()
    return candidate.parent.parent.absolute()


def _launchctl_absent(result: subprocess.CompletedProcess[str]) -> bool:
    text = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    return any(marker in text for marker in ("could not find service", "service not found", "no such process", "not loaded", "unknown service"))


def _launchctl_loaded_state(label: str, domain: str) -> dict[str, Any]:
    try:
        result = subprocess.run(["launchctl", "print", f"{domain}/{label}"], check=False, capture_output=True, text=True)
    except OSError as exc:
        raise ConfigError(f"unable to inspect launchd state: {exc}") from exc
    if result.returncode == 0:
        return {"label": label, "domain": domain, "loaded": True, "available": True}
    if "domain does not support specified action" in f"{result.stdout or ''}\n{result.stderr or ''}".lower():
        return {"label": label, "domain": domain, "loaded": False, "available": False}
    if _launchctl_absent(result):
        return {"label": label, "domain": domain, "loaded": False, "available": True}
    detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
    raise ConfigError(f"unable to inspect launchd state for {domain}/{label}: {detail}")


def _launchctl_bootout(domain: str, label: str, *, ignore_failure: bool = False) -> None:
    try:
        result = subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], check=False, capture_output=True, text=True)
    except OSError as exc:
        if ignore_failure:
            return
        raise ConfigError(f"unable to bootout launchd service {label}: {exc}") from exc
    if result.returncode != 0 and not _launchctl_absent(result):
        if ignore_failure:
            return
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        raise ConfigError(f"unable to bootout launchd service {domain}/{label}: {detail}")


def _verify_launchctl_unloaded(label: str, domain: str) -> None:
    if _launchctl_loaded_state(label, domain).get("loaded"):
        raise ConfigError(f"launchd service remains loaded: {domain}/{label}")
def _launchctl_domain_states(label: str) -> dict[str, dict[str, Any]]:
    """Inspect a label in both supported per-user launchd domains."""
    uid = os.getuid()
    return {
        domain: _launchctl_loaded_state(label, domain)
        for domain in (f"user/{uid}", f"gui/{uid}")
    }


def _launchctl_intended_domain(label: str, states: dict[str, dict[str, Any]]) -> str:
    loaded = [domain for domain, state in states.items() if state.get("loaded")]
    if len(loaded) > 1:
        raise ConfigError(f"Fala service is loaded in multiple domains: {label}")
    return loaded[0] if loaded else f"user/{os.getuid()}"


def _verify_launchctl_exact(label: str, intended_domain: str) -> None:
    states = _launchctl_domain_states(label)
    loaded = [domain for domain, state in states.items() if state.get("loaded")]
    if loaded != [intended_domain]:
        detail = ", ".join(loaded) if loaded else "none"
        raise ConfigError(f"Fala service domain verification failed: expected {intended_domain}, found {detail}")


def _launchctl_restore_states(states: dict[str, dict[str, Any]], plist: Path) -> None:
    """Restore every previously observed Fala domain, including unloaded domains."""
    label = str(next(iter(states.values()))["label"]) if states else plist.stem
    for domain, state in states.items():
        _launchctl_bootout(domain, label, ignore_failure=True)
        _verify_launchctl_unloaded(label, domain)
        if state.get("loaded"):
            try:
                subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True, capture_output=True, text=True)
                subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{label}"], check=True, capture_output=True, text=True)
            except (OSError, subprocess.CalledProcessError) as exc:
                raise ConfigError(f"unable to restore launchd service {domain}/{label}: {exc}") from exc









LEGACY_MUTATOR_LABELS = (
    "com.mikolaj92.hermes.repo-issue-intake",
    "com.mikolaj92.hermes.repo-issue-to-pr-dispatch",
    "com.mikolaj92.hermes.repo-pr-triage",
    "com.mikolaj92.hermes.repo-agent-cleanup",
    "com.mikolaj92.hermes.repo-agent-health",
)


def _assert_legacy_mutators_unloaded() -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for label in LEGACY_MUTATOR_LABELS:
        per_domain = {
            domain: _launchctl_loaded_state(label, domain)
            for domain in (f"user/{os.getuid()}", f"gui/{os.getuid()}")
        }
        loaded = [state for state in per_domain.values() if state.get("loaded")]
        if len(loaded) > 1:
            raise ConfigError(f"legacy mutator label is loaded in multiple domains: {label}")
        states[label] = loaded[0] if loaded else next(iter(per_domain.values()))
    health = states["com.mikolaj92.hermes.repo-agent-health"]
    if health.get("loaded"):
        health_plist = Path.home() / "Library" / "LaunchAgents" / "com.mikolaj92.hermes.repo-agent-health.plist"
        try:
            repair_enabled = "--repair" in health_plist.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"unable to inspect health launchd plist: {exc}") from exc
        if repair_enabled:
            raise ConfigError("legacy repair-enabled health label is loaded")
    loaded = [label for label, state in states.items() if state.get("loaded") and label != "com.mikolaj92.hermes.repo-agent-health"]
    if loaded:
        raise ConfigError(f"legacy mutator labels are loaded: {', '.join(loaded)}")
    return states


def _verify_candidate_copy(source: Path, version: Path) -> None:
    """Verify every copied candidate byte/hash and reject writable artifacts."""
    source_files = {path.relative_to(source) for path in source.rglob("*") if path.is_file()}
    version_files = {path.relative_to(version) for path in version.rglob("*") if path.is_file()}
    if source_files != version_files:
        raise ConfigError("deployment version file set differs from candidate")
    for relative in sorted(source_files):
        source_path = source / relative
        version_path = version / relative
        if source_path.read_bytes() != version_path.read_bytes() or _sha256_file(source_path) != _sha256_file(version_path):
            raise ConfigError(f"deployment version byte/hash verification failed: {relative}")
        if version_path.stat().st_mode & 0o222:
            raise ConfigError(f"deployment version artifact is writable: {relative}")


def _promote_version_runtime(version: Path, deployment_root: Path, candidate_id: str) -> None:
    """Rebind runtime paths to this immutable version before installation."""
    plist_path = version / "launchd" / "com.mikolaj92.hermes.repo-agent-fala-tick-all.plist"
    manifest_path = version / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        document = plistlib.loads(plist_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, plistlib.InvalidFileException) as exc:
        raise ConfigError(f"invalid immutable Fala version: {version}") from exc
    project = (version / "source" / "project").resolve()
    config = (version / "source" / "config.toml").resolve()
    args = list(document.get("ProgramArguments") or [])
    if "--project" not in args or "--config" not in args or "--db" not in args:
        raise ConfigError("Fala version plist is missing --project/--config/--db")
    uv_bin = shutil.which("uv")
    if not uv_bin or not Path(uv_bin).is_absolute():
        raise ConfigError("unable to locate absolute uv executable for promoted Fala version")
    args[0] = str(uv_bin)
    args[args.index("--project") + 1] = str(project)
    args[args.index("--config") + 1] = str(config)
    document["ProgramArguments"] = args
    environment = dict(document.get("EnvironmentVariables") or {})
    environment["HOME"] = str(Path.home().resolve())
    runtime_root = (deployment_root / "runtime" / candidate_id).resolve()
    environment["UV_PROJECT_ENVIRONMENT"] = str(runtime_root / ".venv")
    environment["UV_CACHE_DIR"] = str(runtime_root / "cache")
    environment["FALA_EFFECTOR_ROOT"] = str(runtime_root / "effectors")
    environment["FALA_HOME"] = str((project / "Fala").resolve())
    environment["PATH"] = os.pathsep.join(
        (
            str((Path.home() / ".local" / "share" / "mise" / "shims").resolve()),
            str((Path.home() / ".local" / "bin").resolve()),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        )
    )
    document["EnvironmentVariables"] = environment
    document["WorkingDirectory"] = str(project)
    runtime_root.mkdir(parents=True, exist_ok=True)
    Path(environment["UV_CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(environment["FALA_EFFECTOR_ROOT"]).mkdir(parents=True, exist_ok=True)
    log_dir = (deployment_root / "logs" / candidate_id).resolve()
    for key in ("StandardOutPath", "StandardErrorPath"):
        value = str(document.get(key) or "")
        if value:
            document[key] = str(log_dir / Path(value).name)
    plist_data = plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=False)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ConfigError("Fala version manifest artifacts are missing")
    plist_relative = "launchd/com.mikolaj92.hermes.repo-agent-fala-tick-all.plist"
    artifacts[plist_relative] = {"sha256": _sha256_bytes(plist_data), "bytes": len(plist_data)}
    manifest["program_arguments"] = args
    manifest["runtime_identity"] = _runtime_identity(document, plist_data)
    manifest["artifacts"] = artifacts
    manifest["candidate_id"] = candidate_id
    for directory in (version, version / "launchd"):
        directory.chmod(0o755)
    _atomic_write(plist_path, plist_data)
    _atomic_write(manifest_path, _canonical_json(manifest))
    for directory in (version, version / "launchd"):
        directory.chmod(0o555)
    plist_path.chmod(0o444)
    manifest_path.chmod(0o444)
    if document.get("ProgramArguments") != manifest.get("program_arguments"):
        raise ConfigError("promoted Fala plist arguments do not match version manifest")


def _verify_version_reuse(candidate: Path, version: Path) -> None:
    """Compare copied immutable bytes while allowing promotion-bound runtime metadata."""
    excluded = {Path("manifest.json"), Path("launchd/com.mikolaj92.hermes.repo-agent-fala-tick-all.plist")}
    candidate_files = {path.relative_to(candidate) for path in candidate.rglob("*") if path.is_file()} - excluded
    version_files = {path.relative_to(version) for path in version.rglob("*") if path.is_file()} - excluded
    if candidate_files != version_files:
        raise ConfigError("existing deployment version file set differs from candidate")
    for relative in sorted(candidate_files):
        source = candidate / relative
        installed = version / relative
        if source.read_bytes() != installed.read_bytes() or _sha256_file(source) != _sha256_file(installed):
            raise ConfigError(f"existing deployment version byte/hash mismatch: {relative}")

def deploy_fala(cfg: OssRepoAgentConfig, candidate_value: str, promote: bool, *, deployment_root: str | None = None) -> dict[str, Any]:
    candidate_arg = Path(candidate_value).expanduser()
    root = _candidate_root(candidate_arg, deployment_root)
    candidate = (candidate_arg if candidate_arg.is_absolute() else root / "candidates" / candidate_arg).absolute()
    candidates_root = (root / "candidates").resolve()
    try:
        candidate.resolve().relative_to(candidates_root)
    except ValueError as exc:
        raise ConfigError(f"candidate must be inside deployment candidates root: {candidate}") from exc
    from .tools.deployment_parity import validate_fala_candidate
    parity = validate_fala_candidate(candidate, deployment_root=root)
    result: dict[str, Any] = {"ok": True, "candidate": str(candidate), "candidate_id": parity["candidate_id"], "promoted": False, "parity": parity}
    if not promote:
        return result
    candidate_id = str(parity["candidate_id"])
    versions_root = root / "versions"
    versions_root.mkdir(parents=True, exist_ok=True)
    version = versions_root / candidate_id
    current = root / "current"
    old_current_target: Path | None = None
    if current.exists() or current.is_symlink():
        if not current.is_symlink():
            raise ConfigError(f"deployment current is not a symlink: {current}")
        try:
            old_current_target = current.resolve(strict=True)
        except OSError as exc:
            raise ConfigError(f"deployment current is dangling: {current}") from exc
        if old_current_target.parent != versions_root.resolve() or not re.fullmatch(r"[0-9a-f]{64}", old_current_target.name):
            raise ConfigError(f"deployment current points outside versions: {current}")
        if not old_current_target.is_dir():
            raise ConfigError(f"deployment current target is not a directory: {old_current_target}")
        try:
            old_manifest = json.loads((old_current_target / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"deployment current manifest is invalid: {old_current_target}") from exc
        if old_manifest.get("candidate_id") != old_current_target.name:
            raise ConfigError("deployment current manifest candidate_id mismatch")
        # Historical deployments may predate stricter provenance gates. Keep
        # the exact target for rollback without blocking a validated candidate.
    plist = candidate / "launchd" / "com.mikolaj92.hermes.repo-agent-fala-tick-all.plist"
    try:
        subprocess.run(["plutil", "-lint", str(plist)], check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ConfigError(f"Fala plist lint failed: {exc}") from exc
    _assert_legacy_mutators_unloaded()
    label = "com.mikolaj92.hermes.repo-agent-fala-tick-all"
    domain_states = _launchctl_domain_states(label)
    domain = _launchctl_intended_domain(label, domain_states)
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    target = launch_agents / plist.name
    old_agent_exists = target.is_file()
    old_agent_data = target.read_bytes() if old_agent_exists else None
    previous_path = root / "previous.json"
    previous_data = previous_path.read_bytes() if previous_path.is_file() else None
    previous = {
        "candidate_id": old_current_target.name if old_current_target else None,
        "path": str(old_current_target) if old_current_target else None,
        "loaded_state": domain_states,
        "label": label,
        "domain": domain,
    }
    version_created = False
    try:
        if version.exists():
            validate_fala_candidate(version, deployment_root=root)
            version_manifest = json.loads((version / "manifest.json").read_text(encoding="utf-8"))
            candidate_manifest = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
            if version_manifest.get("candidate_id") != candidate_id or version_manifest.get("identity") != candidate_manifest.get("identity"):
                raise ConfigError("existing deployment version identity differs from candidate")
            _verify_version_reuse(candidate, version)
            plist = version / "launchd" / plist.name
        else:
            shutil.copytree(candidate, version, copy_function=shutil.copy2)
            version_created = True
            for path in version.rglob("*"):
                if path.is_file():
                    path.chmod(0o444)
            for directory in (version, version / "launchd", version / "source"):
                if directory.is_dir():
                    directory.chmod(0o555)
            _verify_candidate_copy(candidate, version)
            _promote_version_runtime(version, root, candidate_id)
            validate_fala_candidate(version, deployment_root=root)
            plist = version / "launchd" / plist.name
            subprocess.run(["plutil", "-lint", str(plist)], check=True, capture_output=True, text=True)
            _fsync_tree(version)
            _fsync_directory(versions_root)
    except Exception:
        if version_created:
            for path in sorted(version.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                try:
                    path.chmod(0o755)
                except OSError:
                    pass
            try:
                version.chmod(0o755)
            except OSError:
                pass
            shutil.rmtree(version, ignore_errors=True)
            _fsync_directory(versions_root)
        raise
    try:
        _atomic_write(previous_path, _canonical_json(previous))
        tmp_link = root / ".current.next"
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
        tmp_link.symlink_to(version, target_is_directory=True)
        os.replace(tmp_link, current)
        _fsync_directory(root)
        for observed_domain in domain_states:
            _launchctl_bootout(observed_domain, label, ignore_failure=True)
            _verify_launchctl_unloaded(label, observed_domain)
        _atomic_write(target, plist.read_bytes())
        subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True, capture_output=True, text=True)
        subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{label}"], check=True, capture_output=True, text=True)
        _verify_launchctl_exact(label, domain)
    except (OSError, subprocess.CalledProcessError, ConfigError) as exc:
        for observed_domain in domain_states:
            _launchctl_bootout(observed_domain, label, ignore_failure=True)
            _verify_launchctl_unloaded(label, observed_domain)
        if current.exists() or current.is_symlink():
            current.unlink()
            _fsync_directory(root)
        if old_current_target is not None:
            current.symlink_to(old_current_target, target_is_directory=True)
            _fsync_directory(root)
        if old_agent_exists and old_agent_data is not None:
            _atomic_write(target, old_agent_data)
        elif target.exists():
            target.unlink()
            _fsync_directory(target.parent)
        if previous_data is not None:
            _atomic_write(previous_path, previous_data)
        elif previous_path.exists():
            previous_path.unlink()
            _fsync_directory(root)
        try:
            _launchctl_restore_states(domain_states, target)
        except (OSError, subprocess.CalledProcessError, ConfigError) as restore_exc:
            raise ConfigError(f"Fala promotion rollback could not restore launchd state: {restore_exc}") from restore_exc
        raise ConfigError(f"Fala promotion rolled back after launchd failure: {exc}") from exc
    result["promoted"] = True
    result["current"] = str(current)
    result["launch_agent"] = str(target)
    result["loaded_state"] = domain_states
    return result
