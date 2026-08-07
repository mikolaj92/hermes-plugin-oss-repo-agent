from __future__ import annotations
import base64

import hashlib
import io
import json
import os
import stat
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from argparse import ArgumentParser, Namespace
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows development only
    fcntl = None  # type: ignore[assignment]
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib  # type: ignore
from . import github_cli, kanban
from .config import ConfigError, LokayConfig, default_config_path, load_config
from .executor import CommandSpec, Runner, planned_command
from lokay.registry import (
    PROCESS_IDS,
    ConfigError as RegistryConfigError,
    load_registry,
    process_defaults,
)

AGGREGATE_FALA_LABEL = "com.mikolaj92.lokay.fala-tick-all"
SUPERVISOR_LABEL = "com.mikolaj92.lokay.supervisor"
STALE_FALA_LABELS = (
    AGGREGATE_FALA_LABEL,
    *(f"com.mikolaj92.lokay.{process_id.replace('_', '-')}" for process_id in PROCESS_IDS),
)
SUPERVISOR_MODULE = "lokay.supervisor"
DEFAULT_GENERATION_PATH = Path("~/.hermes/lokay/generation")
PROCESS_ENV_FENCE_KEYS = (
    "FALA_CANDIDATE_ID",
    "HERMES_LOKAY_GENERATION",
    "HERMES_LOKAY_PROCESS_STATE_ROOT",
)



INTAKE_ASSIGNEE = "lokay-intake"
FALA_PINNED_COMMIT = "b5f9a6d500a442a1c79060a862fe4b9da87bc98f"
FALA_PINNED_VERSION = "0.7.15"
FALA_PINNED_TAG = f"v{FALA_PINNED_VERSION}"
FALA_GIT_URL = "https://github.com/mikolaj92/Fala.git"
FALA_GIT_SOURCE = f'fala = {{ git = "{FALA_GIT_URL}", tag = "{FALA_PINNED_TAG}" }}'
FALA_BUNDLED_SOURCE = 'fala = { path = "Fala", editable = true }'
FALA_GIT_LOCK_SOURCE = (
    f'source = {{ git = "{FALA_GIT_URL}?tag={FALA_PINNED_TAG}#{FALA_PINNED_COMMIT}" }}'
)
FALA_BUNDLED_LOCK_SOURCE = 'source = { editable = "Fala" }'
FALA_GIT_REQUIRES_DIST = f'{{ name = "fala", git = "{FALA_GIT_URL}?tag={FALA_PINNED_TAG}" }}'
FALA_BUNDLED_REQUIRES_DIST = '{ name = "fala", editable = "Fala" }'
FALA_EMBER_JSON_COMMIT = "882acf141301db4ee797228016982ad6acc71a6f"
FALA_SQLITE_FIRE_COMMIT = "3d482362c863e769d018443045b27ca5db645b3c"


def rewrite_fala_git_to_bundled_pyproject(text: str) -> str:
    """Candidate-only rewrite: checkout git pin → vendored path dependency."""
    if FALA_GIT_SOURCE not in text:
        raise ConfigError("pyproject.toml missing pinned Fala git source")
    if "../Fala" in text or 'path = "Fala"' in text:
        raise ConfigError("pyproject.toml still references a local Fala path source")
    rewritten = text.replace(FALA_GIT_SOURCE, FALA_BUNDLED_SOURCE, 1)
    if FALA_GIT_SOURCE in rewritten or FALA_BUNDLED_SOURCE not in rewritten:
        raise ConfigError("failed to rewrite Fala git source to bundled path")
    return rewritten


def rewrite_fala_git_to_bundled_lock(data: bytes) -> bytes:
    """Candidate-only rewrite: lock git pin → vendored editable path."""
    if FALA_GIT_LOCK_SOURCE.encode() not in data:
        raise ConfigError("uv.lock missing pinned Fala git source")
    if b'editable = "../Fala"' in data or b'editable = "Fala"' in data:
        raise ConfigError("uv.lock still references a local Fala path source")
    rewritten = data.replace(FALA_GIT_LOCK_SOURCE.encode(), FALA_BUNDLED_LOCK_SOURCE.encode(), 1)
    rewritten = rewritten.replace(FALA_GIT_REQUIRES_DIST.encode(), FALA_BUNDLED_REQUIRES_DIST.encode(), 1)
    if FALA_GIT_LOCK_SOURCE.encode() in rewritten or FALA_BUNDLED_LOCK_SOURCE.encode() not in rewritten:
        raise ConfigError("failed to rewrite Fala git lock source to bundled path")
    if FALA_GIT_REQUIRES_DIST.encode() in rewritten or FALA_BUNDLED_REQUIRES_DIST.encode() not in rewritten:
        raise ConfigError("failed to rewrite Fala git requires-dist to bundled path")
    return rewritten


def setup_parser(parser: ArgumentParser) -> None:
    parser.add_argument("--config", default=None)
    subparsers = parser.add_subparsers(dest="lokay_command")
    subparsers.required = True
    init = subparsers.add_parser("init")
    init.add_argument("--project-root", default=None)
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
    command = getattr(args, "lokay_command")
    if command == "init":
        return init_project(
            getattr(args, "config", None),
            getattr(args, "project_root", None),
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




def _validated_init_source(project_root: str | None) -> tuple[bytes, str, str]:
    import importlib.resources

    if project_root is not None:
        source_path: Path | None = (Path(project_root).expanduser() / "config.toml").absolute()
        source_label = str(source_path)
        resource_context = None
    else:
        try:
            resource = importlib.resources.files("lokay").joinpath("config.toml")
            resource_context = importlib.resources.as_file(resource)
        except (ImportError, OSError, TypeError) as exc:
            raise ConfigError("packaged init source config is unavailable") from exc
        source_path = None
        source_label = "lokay/config.toml"

    def read_source(path: Path, label: str) -> tuple[bytes, str]:
        if path.suffix.lower() != ".toml":
            raise ConfigError(f"config must use .toml extension: {label}")
        try:
            source_stat = path.lstat()
        except OSError as exc:
            raise ConfigError(f"init source config is missing: {label}") from exc
        if path.is_symlink() or not path.is_file() or not stat.S_ISREG(source_stat.st_mode):
            raise ConfigError(f"init source config must be a regular file: {label}")
        try:
            source_bytes = path.read_bytes()
        except OSError as exc:
            raise ConfigError(f"unable to read init source config: {label}") from exc
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        try:
            registry = load_registry(path)
        except RegistryConfigError as exc:
            raise ConfigError(str(exc)) from exc
        except (OSError, TypeError, ValueError) as exc:
            raise ConfigError(f"invalid init source config: {label}") from exc
        if registry.raw_bytes != source_bytes or registry.sha256 != source_hash:
            raise ConfigError(f"init source config changed while reading: {label}")
        return source_bytes, source_hash

    if resource_context is None:
        assert source_path is not None
        source_bytes, source_hash = read_source(source_path, source_label)
    else:
        try:
            with resource_context as materialized:
                source_bytes, source_hash = read_source(Path(materialized), source_label)
        except (ImportError, OSError, TypeError) as exc:
            raise ConfigError("packaged init source config is unavailable") from exc
    return source_bytes, source_hash, source_label


def init_project(config_path: str | None, project_root: str | None = None) -> dict[str, Any]:
    target = Path(config_path).expanduser() if config_path else default_config_path()
    if target.suffix.lower() != ".toml":
        raise ConfigError(f"config must use .toml extension: {target}")
    if os.path.lexists(target):
        try:
            destination_stat = target.lstat()
        except OSError as exc:
            raise ConfigError(f"config destination is unavailable: {target}") from exc
        if target.is_symlink() or not stat.S_ISREG(destination_stat.st_mode):
            raise ConfigError(f"config destination already exists: {target}")
        raise ConfigError(f"config already exists: {target}")
    source_bytes, source_hash, source_name = _validated_init_source(project_root)
    _atomic_write(target, source_bytes)
    config_arg = str(target)
    return {
        "ok": True,
        "config": config_arg,
        "created": [config_arg],
        "source": source_name,
        "sha256": source_hash,
        "next_commands": [
            f"hermes lokay --config {config_arg} validate",
            f"hermes lokay --config {config_arg} intake --limit 3",
            f"hermes lokay --config {config_arg} dispatch --max 2",
        ],
        "safety": safety_guards(),
    }




def safety_guards() -> list[str]:
    return [
        "dry-run unless config mode is live and --live or --apply is passed",
        "GitHub operations use gh CLI wrappers only",
        "GitHub issues are claimed before Kanban intake when github.assignee is configured",
        "GitHub content is treated as untrusted evidence",
        "no PR merge support in v0",
        "no force-push or branch deletion behavior",
    ]


def validate(cfg: LokayConfig) -> dict[str, Any]:
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
            "lokay:repo-gh-cli-policy",
            "lokay:repo-audit-finding-format",
            "lokay:repo-fix-issue-pr",
            "lokay:repo-review-agent-pr",
        ],
    }


def bootstrap(cfg: LokayConfig, apply: bool) -> dict[str, Any]:
    live = cfg.effective_live(apply)
    return {
        "ok": True,
        "effective_live": live,
        "planned_boards": [{"repo": repo.repo, "board": repo.board} for repo in cfg.repos],
        "message": "bootstrap renders board and label intent; mutation adapters are intentionally explicit",
    }


def _issue_labels(issue: dict[str, Any]) -> set[str]:
    return {str(label.get("name", "")) for label in issue.get("labels", []) if isinstance(label, dict)}


def _eligible_issue(issue: dict[str, Any], cfg: LokayConfig) -> bool:
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


def intake(cfg: LokayConfig, live_flag: bool, limit: int, runner: Runner) -> dict[str, Any]:
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


def dispatch(cfg: LokayConfig, live_flag: bool, run_executor: bool, max_tasks: int) -> dict[str, Any]:
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


def pr_triage(cfg: LokayConfig, live_flag: bool, comment: bool, runner: Runner) -> dict[str, Any]:
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
def _seal_tree(root: Path) -> None:
    """Remove write bits from a tree while preserving read and execute bits."""
    try:
        for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                continue
            path.chmod(path.stat().st_mode & 0o555)
        if not root.is_symlink():
            root.chmod(root.stat().st_mode & 0o555)
    except OSError as exc:
        raise ConfigError(f"unable to seal immutable tree: {root}") from exc



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


def _toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_literal(item) for item in value) + "]"
    raise ConfigError(f"unsupported TOML literal type: {type(value).__name__}")


def _materialize_candidate_config(config_path: Path, processes: list[dict[str, Any]]) -> bytes:
    """Return candidate config bytes, injecting the process catalog when missing."""
    try:
        raw = config_path.read_bytes()
        document = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise ConfigError(f"unable to read candidate source config: {exc}") from exc
    existing = document.get("processes")
    if isinstance(existing, list) and existing:
        ordered_ids = [str(item.get("id") or "") for item in existing if isinstance(item, dict)]
        if ordered_ids != list(PROCESS_IDS):
            raise ConfigError("source config process catalog must match canonical PROCESS_IDS order")
        identity_rows = [_process_identity_row(item) for item in existing]
        if identity_rows != processes:
            raise ConfigError("source config process catalog does not match identity processes")
        return raw
    defaults_by_id = {str(item["id"]): dict(item) for item in process_defaults()}
    text = raw.decode("utf-8")
    if text and not text.endswith("\n"):
        text += "\n"
    for process in processes:
        process_id = str(process["id"])
        row = defaults_by_id.get(process_id)
        if row is None:
            raise ConfigError(f"missing process defaults for {process_id}")
        row = dict(row)
        row["enabled"] = bool(process["enabled"])
        row["interval_seconds"] = int(process["interval_seconds"])
        row["command"] = str(process["command"])
        # Stable identity no longer carries launchd_label; drop any defaults residue.
        row.pop("launchd_label", None)
        text += "\n[[processes]]\n"
        for key, value in row.items():
            text += f"{key} = {_toml_literal(value)}\n"
    return text.encode("utf-8")


def _process_identity_row(record: Mapping[str, Any]) -> dict[str, Any]:
    """Project a full process record onto the stable identity key set."""
    process_id = str(record.get("id") or "")
    if not process_id:
        raise ConfigError("process id is missing")
    command = str(record.get("command") or f"lokay-process-{process_id}")
    expected_command = f"lokay-process-{process_id}"
    if command != expected_command:
        raise ConfigError(f"process {process_id} command must be {expected_command!r}, got {command!r}")
    try:
        interval = int(record.get("interval_seconds"))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"process {process_id} interval_seconds must be an integer") from exc
    if interval < 1:
        raise ConfigError(f"process {process_id} interval_seconds must be >= 1")
    enabled = record.get("enabled")
    if not isinstance(enabled, bool):
        raise ConfigError(f"process {process_id} enabled must be a boolean")
    return {
        "id": process_id,
        "enabled": enabled,
        "interval_seconds": interval,
        "command": command,
    }


def _catalog_processes(cfg: LokayConfig, config_path: Path) -> list[dict[str, Any]]:
    """Resolve the ordered 12-process identity catalog, fail closed on mismatch."""
    del cfg
    try:
        raw_processes = [dict(item) for item in load_registry(config_path).processes]
    except (RegistryConfigError, OSError, TypeError, ValueError) as exc:
        raise ConfigError(f"unable to load canonical process catalog: {exc}") from exc
    if len(raw_processes) != len(PROCESS_IDS):
        raise ConfigError(
            f"process catalog must declare exactly {len(PROCESS_IDS)} processes, got {len(raw_processes)}"
        )
    ordered: list[dict[str, Any]] = []
    for expected_id, record in zip(PROCESS_IDS, raw_processes):
        process_id = str(record.get("id") or "")
        if process_id != expected_id:
            raise ConfigError(
                f"process catalog must declare process IDs in canonical order: expected {expected_id!r}, got {process_id!r}"
            )
        ordered.append(_process_identity_row(record))
    if len(ordered) != len(PROCESS_IDS):
        raise ConfigError(f"process catalog must declare exactly {len(PROCESS_IDS)} processes")
    return ordered


def _identity_repos(cfg: LokayConfig, config_path: Path) -> list[dict[str, Any]]:
    """Return the canonical ordered repository inventory."""
    del cfg
    try:
        return [dict(item) for item in load_registry(config_path).repos]
    except (RegistryConfigError, OSError, TypeError, ValueError, KeyError) as exc:
        raise ConfigError(f"unable to load canonical repository inventory: {exc}") from exc


def _process_state_root(db_path: Path) -> Path:
    """Durable process-state root co-located with the Fala database."""
    return Path(db_path).expanduser().resolve().parent / "process-state"


def _generation_pointer_path() -> Path:
    configured = (
        os.environ.get("HERMES_LOKAY_GENERATION_PATH")
        or os.environ.get("LOKAY_GENERATION_PATH")
        or ""
    ).strip()
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_GENERATION_PATH.expanduser()


def _process_environment(
    *,
    home: Path,
    project_root: Path,
    db_path: Path,
    candidate_id: str,
) -> dict[str, str]:
    """Build the exact launchd EnvironmentVariables for the supervisor job."""
    if not re.fullmatch(r"[0-9a-f]{64}", str(candidate_id or "")):
        raise ConfigError("process environment requires a 64-hex candidate_id")
    return {
        "HOME": str(Path(home).resolve()),
        "PYTHONPATH": str(Path(project_root) / "src"),
        "FALA_HOME": str(Path(project_root) / "Fala"),
        "FALA_EFFECTOR_ROOT": str(Path(project_root) / "effectors"),
        "FALA_CANDIDATE_ID": str(candidate_id),
        "HERMES_LOKAY_GENERATION": str(candidate_id),
        "HERMES_LOKAY_PROCESS_STATE_ROOT": str(_process_state_root(db_path)),
    }


def _publish_generation(candidate_id: str) -> Path:
    """Atomically publish ~/.hermes/lokay/generation (or configured path)."""
    if not re.fullmatch(r"[0-9a-f]{64}", str(candidate_id or "")):
        raise ConfigError("generation publish requires a 64-hex candidate_id")
    path = _generation_pointer_path()
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        parent.chmod(0o700)
    except OSError:
        pass
    fd, name = tempfile.mkstemp(prefix=".generation.", suffix=".tmp", dir=str(parent))
    temp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(str(candidate_id) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temp), str(path))
        try:
            dir_fd = os.open(str(parent), os.O_RDONLY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        path.chmod(0o600)
    except Exception as exc:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass
        raise ConfigError(f"unable to publish generation fence: {exc}") from exc
    return path


def _supervisor_program_arguments(
    *,
    python_path: Path,
    config_path: Path,
    db_path: Path,
    mode: str,
) -> list[str]:
    return [
        str(python_path),
        "-m",
        SUPERVISOR_MODULE,
        "--config",
        str(config_path),
        "--db",
        str(db_path),
        f"--{mode}",
        "--json",
    ]


def _process_program_arguments(
    *,
    python_path: Path,
    command: str,
    config_path: Path,
    db_path: Path,
    mode: str,
) -> list[str]:
    return [
        str(python_path),
        "-m",
        "lokay.process",
        command,
        "--config",
        str(config_path),
        "--db",
        str(db_path),
        f"--{mode}",
        "--json",
    ]


def _dispatch_commands(
    *,
    python_path: Path,
    config_path: Path,
    db_path: Path,
    mode: str,
    processes: list[dict[str, Any]],
) -> list[list[str]]:
    """Ordered twelve child argv lists in PROCESS_IDS order."""
    if len(processes) != len(PROCESS_IDS):
        raise ConfigError(f"dispatch_commands requires exactly {len(PROCESS_IDS)} processes")
    commands: list[list[str]] = []
    for expected_id, process in zip(PROCESS_IDS, processes):
        process_id = str(process.get("id") or "")
        if process_id != expected_id:
            raise ConfigError(
                f"dispatch_commands process order mismatch: expected {expected_id!r}, got {process_id!r}"
            )
        command = str(process.get("command") or "")
        expected_command = f"lokay-process-{process_id}"
        if command != expected_command:
            raise ConfigError(f"dispatch command for {process_id} must be {expected_command!r}")
        commands.append(
            _process_program_arguments(
                python_path=python_path,
                command=command,
                config_path=config_path,
                db_path=db_path,
                mode=mode,
            )
        )
    return commands


def _assert_no_forbidden_launchd_artifacts(launchd_dir: Path) -> None:
    """Reject aggregate and per-process launchd plists in candidate/version trees."""
    if not launchd_dir.is_dir():
        raise ConfigError(f"launchd directory is missing: {launchd_dir}")
    forbidden = launchd_dir / f"{AGGREGATE_FALA_LABEL}.plist"
    if forbidden.exists():
        raise ConfigError("aggregate production launchd artifact is forbidden")
    expected = launchd_dir / f"{SUPERVISOR_LABEL}.plist"
    for path in sorted(launchd_dir.glob("*.plist")):
        if path.resolve() != expected.resolve():
            raise ConfigError(f"per-process production launchd artifact is forbidden: {path.name}")
    if not expected.is_file():
        raise ConfigError(f"missing supervisor launchd artifact: {SUPERVISOR_LABEL}")


def _render_supervisor_plist(
    *,
    project_root: Path,
    config_path: Path,
    db_path: Path,
    mode: str,
    home: Path,
    log_dir: Path,
    candidate_id: str,
) -> bytes:
    template = project_root / "templates" / "launchd" / "lokay-supervisor.plist.template"
    if not template.is_file():
        raise ConfigError(f"Fala launchd supervisor template not found: {template}")
    python_path = project_root / ".venv" / "bin" / "python"
    pythonpath = project_root / "src"
    fala_home = project_root / "Fala"
    effector_root = project_root / "effectors"
    if not python_path.is_absolute() or python_path.is_symlink() or not python_path.is_file() or not os.access(python_path, os.X_OK):
        raise ConfigError(f"Fala candidate interpreter is unavailable: {python_path}")
    if not pythonpath.is_absolute() or not pythonpath.is_dir() or pythonpath.is_symlink():
        raise ConfigError(f"Fala candidate source is unavailable: {pythonpath}")
    if not fala_home.is_dir() or not effector_root.is_dir():
        raise ConfigError("Fala candidate runtime directories are missing")
    environment = _process_environment(
        home=home,
        project_root=project_root,
        db_path=db_path,
        candidate_id=candidate_id,
    )
    values = {
        "{{LABEL}}": SUPERVISOR_LABEL,
        "{{PYTHON_PATH}}": str(python_path),
        "{{PYTHONPATH}}": environment["PYTHONPATH"],
        "{{FALA_HOME}}": environment["FALA_HOME"],
        "{{FALA_EFFECTOR_ROOT}}": environment["FALA_EFFECTOR_ROOT"],
        "{{PROJECT_ROOT}}": str(project_root),
        "{{CONFIG_PATH}}": str(config_path),
        "{{DB_PATH}}": str(db_path),
        "{{MODE_ARG}}": f"--{mode}",
        "{{HOME}}": environment["HOME"],
        "{{LOG_DIR}}": str(log_dir),
        "{{CANDIDATE_ID}}": environment["FALA_CANDIDATE_ID"],
        "{{GENERATION}}": environment["HERMES_LOKAY_GENERATION"],
        "{{PROCESS_STATE_ROOT}}": environment["HERMES_LOKAY_PROCESS_STATE_ROOT"],
    }
    text = template.read_text(encoding="utf-8")
    for marker, value in values.items():
        text = text.replace(marker, value)
    unresolved = sorted(set(re.findall(r"\{\{[^}]+\}\}", text)))
    if unresolved:
        raise ConfigError(f"unresolved Fala launchd template placeholder: {', '.join(unresolved)}")
    try:
        document = plistlib.loads(text.encode("utf-8"))
    except plistlib.InvalidFileException as exc:
        raise ConfigError(f"invalid Fala launchd template: {exc}") from exc
    required = _supervisor_program_arguments(
        python_path=python_path,
        config_path=config_path,
        db_path=db_path,
        mode=mode,
    )
    if document.get("ProgramArguments") != required:
        raise ConfigError("Fala ProgramArguments do not match immutable supervisor contract")
    if document.get("WorkingDirectory") != str(project_root):
        raise ConfigError("Fala WorkingDirectory does not match immutable candidate project")
    if document.get("EnvironmentVariables") != environment:
        raise ConfigError("Fala launchd environment does not match immutable candidate contract")
    if (
        document.get("Label") != SUPERVISOR_LABEL
        or "StartInterval" in document
        or document.get("ProcessType") != "Background"
        or document.get("RunAtLoad") is not True
        or document.get("KeepAlive") is not True
        or document.get("LimitLoadToSessionType") != "Background"
    ):
        raise ConfigError("Fala launchd schedule or session contract is invalid")
    if document.get("StandardOutPath") != str(log_dir / f"{SUPERVISOR_LABEL}.out.log"):
        raise ConfigError("Fala StandardOutPath does not match supervisor label")
    if document.get("StandardErrorPath") != str(log_dir / f"{SUPERVISOR_LABEL}.err.log"):
        raise ConfigError("Fala StandardErrorPath does not match supervisor label")
    return plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=False)


def _runtime_identity(document: dict[str, Any], plist_data: bytes, *, label: str | None = None) -> dict[str, Any]:
    return {
        "label": label or document.get("Label"),
        "program_arguments": list(document.get("ProgramArguments") or []),
        "working_directory": document.get("WorkingDirectory"),
        "standard_out_path": document.get("StandardOutPath"),
        "standard_error_path": document.get("StandardErrorPath"),
        "environment_variables": dict(document.get("EnvironmentVariables") or {}),
        "start_interval": document.get("StartInterval"),
        "run_at_load": document.get("RunAtLoad"),
        "keep_alive": document.get("KeepAlive"),
        "process_type": document.get("ProcessType"),
        "limit_load_to_session_type": document.get("LimitLoadToSessionType"),
        "plist_sha256": _sha256_bytes(plist_data),
    }


def _provision_candidate_environment(project: Path) -> Path:
    """Install the bundled project into a genuine candidate-local environment."""
    uv_bin = shutil.which("uv")
    if not uv_bin or not Path(uv_bin).is_absolute():
        raise ConfigError("unable to locate absolute uv executable")
    venv = project / ".venv"
    if venv.exists() or venv.is_symlink():
        _remove_tree(venv)
    try:
        subprocess.run([sys.executable, "-m", "venv", "--copies", "--without-pip", str(venv)], check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ConfigError(f"unable to create candidate virtual environment: {exc}") from exc
    python = venv / "bin" / "python"
    if python.is_symlink() or not python.is_file() or not os.access(python, os.X_OK):
        raise ConfigError(f"candidate virtual environment interpreter is not a regular executable: {python}")
    if sys.platform == "darwin":
        library_name = f"libpython{sys.version_info.major}.{sys.version_info.minor}.dylib"
        source_library = Path(sys.base_prefix) / "lib" / library_name
        target_library = venv / "lib" / library_name
        if not source_library.is_file():
            raise ConfigError(f"candidate Python runtime library is unavailable: {source_library}")
        target_library.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_library, target_library)
    env = dict(os.environ)
    env["UV_PROJECT_ENVIRONMENT"] = str(venv)
    env.pop("VIRTUAL_ENV", None)
    smoke_env = dict(env)
    smoke_env["PYTHONPATH"] = str(project / "src")
    try:
        subprocess.run([uv_bin, "sync", "--frozen", "--project", str(project)], check=True, capture_output=True, text=True, env=env)
        subprocess.run([str(python), "-c", "import lokay, fala"], check=True, capture_output=True, text=True, env=smoke_env)
        with tempfile.TemporaryDirectory(prefix="lokay-candidate-smoke-") as smoke_root:
            smoke_db = Path(smoke_root) / "process.sqlite3"
            config_path = project.parent / "config.toml"
            for process_id in PROCESS_IDS:
                subprocess.run(
                    [
                        str(python),
                        "-m",
                        "lokay.process",
                        f"lokay-process-{process_id}",
                        "--config",
                        str(config_path),
                        "--db",
                        str(smoke_db),
                        "--dry-run",
                        "--json",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=smoke_env,
                )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise ConfigError(f"candidate runtime provisioning or smoke test failed: {detail}") from exc
    except OSError as exc:
        raise ConfigError(f"candidate runtime provisioning or smoke test failed: {exc}") from exc
    (project / "effectors").mkdir(parents=True, exist_ok=True)
    return python

def _safe_archive_member_path(destination: Path, member_name: str, *, allow_existing_dir: bool = False) -> Path:
    """Resolve a tar member to a path that cannot escape destination."""
    root = destination.resolve()
    name = member_name.replace("\\", "/")
    if not name or name.endswith("/") or name in {".", ".."} or Path(name).is_absolute():
        raise ConfigError("pinned source archive contains an unsafe path")
    parts = Path(name).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ConfigError("pinned source archive contains an unsafe path")
    target = root.joinpath(*parts)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ConfigError("pinned source archive contains an unsafe path") from exc
    # Reject writes that would traverse a previously materialized non-directory
    # or symlink (e.g. a link planted by an earlier member).
    current = root
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise ConfigError("pinned source archive contains an unsafe path")
        if current.exists():
            try:
                current.resolve().relative_to(root)
            except ValueError as exc:
                raise ConfigError("pinned source archive contains an unsafe path") from exc
    if target.is_symlink():
        raise ConfigError("pinned source archive contains an unsafe path")
    if target.exists():
        if not (allow_existing_dir and target.is_dir()):
            raise ConfigError("pinned source archive contains an unsafe path")
    return target


def _extract_git_archive(bundle: tarfile.TarFile, destination: Path) -> None:
    """Extract only regular files and directories; reject links/devices/traversal."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    for member in bundle.getmembers():
        if member.isdir():
            name = member.name.replace("\\", "/").rstrip("/")
            if not name:
                continue
            target = _safe_archive_member_path(destination, name, allow_existing_dir=True)
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not member.isreg():
            raise ConfigError("pinned source archive contains an unsafe path")
        target = _safe_archive_member_path(destination, member.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        source = bundle.extractfile(member)
        if source is None:
            raise ConfigError("pinned source archive contains an unsafe path")
        with source, target.open("wb") as handle:
            shutil.copyfileobj(source, handle)
        target.chmod(member.mode & 0o555)
        try:
            target.resolve().relative_to(root)
        except ValueError as exc:
            raise ConfigError("pinned source archive contains an unsafe path") from exc
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
            _extract_git_archive(bundle, destination)
    except (OSError, tarfile.TarError) as exc:
        raise ConfigError(f"unable to unpack pinned source {repo}: {exc}") from exc


def _copy_candidate_source(
    project_root: Path,
    destination: Path,
    config: Path,
    lock: Path,
    *,
    config_bytes: bytes | None = None,
) -> dict[str, bytes]:
    """Copy the runnable plugin and the complete pinned Fala dependency tree."""
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
        head = subprocess.run(
            ["git", "-C", str(fala_root), "rev-parse", "HEAD"],
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
    if head.stdout.strip() != FALA_PINNED_COMMIT:
        raise ConfigError("Fala checkout HEAD does not match pinned commit")
    if status.stdout.strip():
        raise ConfigError("pinned Fala checkout is dirty")
    fala_target = project / "Fala"
    _copy_git_tree(fala_root, FALA_PINNED_COMMIT, project / "Fala")
    for relative, commit in (
        ("vendor/EmberJson", FALA_EMBER_JSON_COMMIT),
        ("vendor/sqlite.fire", FALA_SQLITE_FIRE_COMMIT),
    ):
        dependency = fala_root / relative
        try:
            dependency_head = subprocess.run(
                ["git", "-C", str(dependency), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "-C", str(dependency), "diff", "--quiet", "HEAD"],
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ConfigError(f"unable to verify pinned Fala dependency {relative}: {exc}") from exc
        if dependency_head != commit:
            raise ConfigError(f"Fala dependency {relative} HEAD does not match pinned commit")
        _copy_git_tree(dependency, commit, fala_target / relative)
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
    config_target = destination / "config.toml"
    if config_bytes is None:
        shutil.copy2(config, config_target)
        config_payload = config.read_bytes()
    else:
        config_target.write_bytes(config_bytes)
        config_payload = config_bytes
    pyproject = rewrite_fala_git_to_bundled_pyproject((project / "pyproject.toml").read_text(encoding="utf-8"))
    (project / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    lock_data = rewrite_fala_git_to_bundled_lock(lock.read_bytes())
    (project / "uv.lock").write_bytes(lock_data)
    return {"config.toml": config_payload, "uv.lock": lock_data}


def render_launchd(
    cfg: LokayConfig,
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
    db = Path(fala_db).expanduser().absolute() if fala_db else Path.home() / ".hermes" / "lokay" / "fala" / "state.sqlite"
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
    lock_data = rewrite_fala_git_to_bundled_lock(lock.read_bytes())
    lock_hash = _sha256_bytes(lock_data)
    policy = {
        "automerge": bool(cfg.automerge),
        "require_human_approval": bool(cfg.require_human_approval),
        "require_checks": bool(cfg.require_checks),
        "require_test_evidence": bool(cfg.require_test_evidence),
        "executor_enabled": bool(cfg.executor.enabled),
    }
    catalog_processes = _catalog_processes(cfg, config)
    identity_repos = _identity_repos(cfg, config)
    config_bytes = _materialize_candidate_config(config, catalog_processes)
    identity: dict[str, Any] = {
        "schema": 1,
        "mode": mode,
        "plugin_commit": revision,
        "fala_tag": FALA_PINNED_VERSION,
        "fala_commit": fala_revision,
        "lock_hash": lock_hash,
        "config_path": str(config),
        "config_hash": _sha256_bytes(config_bytes),
        "db_path": str(db),
        "metadata_path": "source/metadata.json",
        "lock_path": "source/project/uv.lock",
        "config_artifact_path": "source/config.toml",
        "revision_path": "source/revision.txt",
        "policy": policy,
        "repos": identity_repos,
        "processes": catalog_processes,
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
        _copy_candidate_source(
            project_root,
            candidate / "source",
            config,
            lock,
            config_bytes=config_bytes,
        )
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
            + list((fala_root / "mojo" / "fala").glob("native_process_host.[ch]"))
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
        _provision_candidate_environment(candidate_project)
        log_dir = ((root or candidate.parent.parent) / "logs" / candidate_id).absolute()
        python_path = candidate_project / ".venv" / "bin" / "python"
        plist_data = _render_supervisor_plist(
            project_root=candidate_project,
            config_path=candidate_config,
            db_path=db,
            mode=mode,
            home=Path.home().resolve(),
            log_dir=log_dir,
            candidate_id=candidate_id,
        )
        supervisor_relative = f"launchd/{SUPERVISOR_LABEL}.plist"
        _atomic_write(candidate / supervisor_relative, plist_data)
        document = plistlib.loads(plist_data)
        runtime = _runtime_identity(document, plist_data, label=SUPERVISOR_LABEL)
        program_arguments = [list(runtime["program_arguments"])]
        runtime_identity = [runtime]
        dispatch_commands = _dispatch_commands(
            python_path=python_path,
            config_path=candidate_config,
            db_path=db,
            mode=mode,
            processes=catalog_processes,
        )
        _assert_no_forbidden_launchd_artifacts(candidate / "launchd")
        revision_data = (revision + "\n").encode()
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
            "program_arguments": program_arguments,
            "dispatch_commands": dispatch_commands,
            "artifacts": artifacts,
            "runtime_identity": runtime_identity,
        })
        _atomic_write(candidate / "manifest.json", _canonical_json(manifest_payload))
        _seal_tree(candidate)
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
        root = Path(deployment_root).expanduser().absolute()
    else:
        root = candidate.parent.parent.absolute()
    if root.is_symlink():
        raise ConfigError(f"deployment root must not be a symlink: {root}")
    if not root.is_dir():
        raise ConfigError(f"deployment root must be a directory: {root}")
    return root


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
    deadline = time.monotonic() + 5
    while True:
        state = _launchctl_loaded_state(label, domain)
        if state.get("available") is False or not state.get("loaded"):
            return
        if time.monotonic() >= deadline:
            raise ConfigError(f"launchd service remains loaded: {domain}/{label}")
        time.sleep(0.1)

def _launchctl_domain_states(label: str) -> dict[str, dict[str, Any]]:
    """Inspect a label in both supported per-user launchd domains."""
    uid = os.getuid()
    return {
        domain: _launchctl_loaded_state(label, domain)
        for domain in (f"user/{uid}", f"gui/{uid}")
    }


def _launchctl_intended_domain(label: str, states: dict[str, dict[str, Any]]) -> str:
    available = {domain: state for domain, state in states.items() if state.get("available") is not False}
    if not available:
        raise ConfigError("no supported Fala launchd domain is available")
    loaded = [domain for domain, state in available.items() if state.get("loaded")]
    if len(loaded) > 1:
        raise ConfigError(f"Fala service is loaded in multiple domains: {label}")
    if loaded:
        return loaded[0]
    user_domain = f"user/{os.getuid()}"
    return user_domain if user_domain in available else next(iter(available))


def _verify_launchctl_exact(label: str, intended_domain: str) -> None:
    states = _launchctl_domain_states(label)
    if states.get(intended_domain, {}).get("available") is False:
        raise ConfigError(f"intended Fala launchd domain is unavailable: {intended_domain}")
    loaded = [domain for domain, state in states.items() if state.get("available") is not False and state.get("loaded")]
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
    "com.mikolaj92.hermes.repo-agent-fala-tick-all",
)
LEGACY_SHELL_MUTATOR_LABELS = (
    "com.mikolaj92.hermes.repo-issue-intake",
    "com.mikolaj92.hermes.repo-issue-to-pr-dispatch",
    "com.mikolaj92.hermes.repo-pr-triage",
    "com.mikolaj92.hermes.repo-agent-cleanup",
    "com.mikolaj92.hermes.repo-agent-fala-tick-all",
)
LEGACY_HEALTH_LABEL = "com.mikolaj92.hermes.repo-agent-health"


def _legacy_plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _legacy_health_repair_enabled(plist_path: Path) -> bool:
    try:
        return "--repair" in plist_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"unable to inspect health launchd plist: {exc}") from exc


def _inspect_legacy_mutator_states() -> dict[str, dict[str, Any]]:
    """Probe both launchd domains for every legacy label and reject ambiguity."""
    states: dict[str, dict[str, Any]] = {}
    for label in LEGACY_MUTATOR_LABELS:
        per_domain = {
            domain: _launchctl_loaded_state(label, domain)
            for domain in (f"user/{os.getuid()}", f"gui/{os.getuid()}")
        }
        available = [state for state in per_domain.values() if state.get("available") is not False]
        if not available:
            raise ConfigError(f"no supported legacy launchd domain is available for {label}")
        loaded = [state for state in available if state.get("loaded")]
        if len(loaded) > 1:
            raise ConfigError(f"legacy mutator label is loaded in multiple domains: {label}")
        selected = loaded[0] if loaded else available[0]
        entry: dict[str, Any] = {
            "label": label,
            "domain": selected["domain"],
            "loaded": bool(selected.get("loaded")),
            "available": selected.get("available", True),
            "domains": per_domain,
            "plist_path": None,
            "plist_bytes": None,
            "plist_sha256": None,
            "repair_enabled": False,
            "transition": False,
        }
        if entry["loaded"]:
            plist_path = _legacy_plist_path(label)
            if not plist_path.is_file():
                raise ConfigError(f"loaded legacy label has no canonical installed plist: {label}")
            try:
                plist_bytes = plist_path.read_bytes()
            except OSError as exc:
                raise ConfigError(f"unable to snapshot legacy launchd plist {label}: {exc}") from exc
            entry["plist_path"] = str(plist_path)
            entry["plist_bytes"] = plist_bytes
            entry["plist_sha256"] = _sha256_bytes(plist_bytes)
            if label == LEGACY_HEALTH_LABEL:
                entry["repair_enabled"] = _legacy_health_repair_enabled(plist_path)
                entry["transition"] = bool(entry["repair_enabled"])
            else:
                entry["transition"] = True
        states[label] = entry
    return states


def _assert_legacy_mutators_unloaded() -> dict[str, dict[str, Any]]:
    """Fail closed when any transition-required legacy mutator remains loaded."""
    states = _inspect_legacy_mutator_states()
    health = states[LEGACY_HEALTH_LABEL]
    if health.get("loaded") and health.get("repair_enabled"):
        raise ConfigError("legacy repair-enabled health label is loaded")
    loaded = [label for label in LEGACY_SHELL_MUTATOR_LABELS if states[label].get("loaded")]
    if loaded:
        raise ConfigError(f"legacy mutator labels are loaded: {', '.join(loaded)}")
    return states


def _snapshot_legacy_mutators() -> dict[str, dict[str, Any]]:
    """Capture exact loaded legacy states and plist bytes needed for restoration."""
    return _inspect_legacy_mutator_states()


def _legacy_transition_entries(states: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [states[label] for label in LEGACY_MUTATOR_LABELS if states.get(label, {}).get("transition")]


def _legacy_state_summary(states: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        label_name: {
            "label": entry["label"],
            "domain": entry["domain"],
            "loaded": entry["loaded"],
            "available": entry.get("available", True),
            "plist_path": entry.get("plist_path"),
            "plist_sha256": entry.get("plist_sha256"),
            "repair_enabled": entry.get("repair_enabled", False),
            "transition": entry.get("transition", False),
        }
        for label_name, entry in states.items()
    }


def _bootout_legacy_mutators(states: dict[str, dict[str, Any]]) -> None:
    """Boot out transition-required legacy labels and verify they are absent."""
    if not states:
        # Empty maps are treated as "no legacy inventory" for disposable fixtures/mocks.
        return
    for entry in _legacy_transition_entries(states):
        label = str(entry["label"])
        domain = str(entry["domain"])
        _launchctl_bootout(domain, label, ignore_failure=False)
        for observed_domain, observed_state in entry.get("domains", {}).items():
            if observed_state.get("available") is False:
                continue
            _verify_launchctl_unloaded(label, observed_domain)
    # Fresh all-label/both-domain re-probe closes races after the sequenced bootouts.
    live = _inspect_legacy_mutator_states()
    residual = [
        label
        for label, entry in live.items()
        if (label in LEGACY_SHELL_MUTATOR_LABELS and entry.get("loaded"))
        or (label == LEGACY_HEALTH_LABEL and entry.get("loaded") and entry.get("repair_enabled"))
    ]
    if residual:
        raise ConfigError(f"legacy mutator labels remain loaded after bootout: {', '.join(residual)}")


def _restore_legacy_mutators(states: dict[str, dict[str, Any]]) -> None:
    """Restore previously loaded legacy labels from snapped canonical plist bytes."""
    if not states:
        return
    errors: list[str] = []
    for label in LEGACY_MUTATOR_LABELS:
        entry = states.get(label) or {}
        domains = entry.get("domains") or {}
        try:
            if not entry.get("transition"):
                # Leave unrelated / observational state alone; only prove unloaded labels stay unloaded.
                if entry.get("loaded"):
                    continue
                for observed_domain, observed_state in domains.items():
                    if observed_state.get("available") is False:
                        continue
                    _verify_launchctl_unloaded(label, observed_domain)
                continue
            domain = str(entry["domain"])
            plist_path = Path(str(entry["plist_path"]))
            plist_bytes = entry.get("plist_bytes")
            if not isinstance(plist_bytes, (bytes, bytearray)):
                raise ConfigError(f"missing legacy launchd plist snapshot for {label}")
            _atomic_write(plist_path, bytes(plist_bytes))
            for observed_domain, observed_state in domains.items():
                if observed_state.get("available") is False:
                    continue
                _launchctl_bootout(observed_domain, label, ignore_failure=True)
                _verify_launchctl_unloaded(label, observed_domain)
            subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True, capture_output=True, text=True)
            subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{label}"], check=True, capture_output=True, text=True)
            for observed_domain, observed_state in domains.items():
                if observed_state.get("available") is False:
                    continue
                state = _launchctl_loaded_state(label, observed_domain)
                if observed_domain == domain:
                    if not state.get("loaded"):
                        raise ConfigError(f"legacy launchd service not restored: {domain}/{label}")
                elif state.get("loaded"):
                    raise ConfigError(f"legacy launchd service loaded in unexpected domain after restore: {observed_domain}/{label}")
        except (OSError, subprocess.CalledProcessError, ConfigError, TypeError, ValueError) as exc:
            errors.append(f"{label}: {exc}")
    if errors:
        raise ConfigError("legacy launchd restore failed: " + "; ".join(errors))
    live = _inspect_legacy_mutator_states()
    for label, expected in states.items():
        actual = live.get(label) or {}
        if expected.get("transition"):
            if not actual.get("loaded") or actual.get("domain") != expected.get("domain"):
                raise ConfigError(
                    f"legacy launchd topology mismatch after restore for {label}: "
                    f"expected domain={expected.get('domain')} loaded=True, "
                    f"found domain={actual.get('domain')} loaded={actual.get('loaded')}"
                )
            continue
        if not expected.get("loaded") and actual.get("loaded") and (
            label in LEGACY_SHELL_MUTATOR_LABELS or actual.get("repair_enabled")
        ):
            raise ConfigError(f"legacy launchd service unexpectedly loaded after restore: {label}")


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
    """Bind the supervisor launchd job to the immutable version-local runtime."""
    manifest_path = version / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid immutable Fala version: {version}") from exc
    version = version.resolve()
    deployment_root = deployment_root.resolve()
    if not isinstance(manifest, dict):
        raise ConfigError(f"invalid immutable Fala version: {version}")
    identity = manifest.get("identity")
    processes = identity.get("processes") if isinstance(identity, dict) else manifest.get("processes")
    if not isinstance(processes, list) or len(processes) != len(PROCESS_IDS):
        raise ConfigError("Fala version process catalog must match PROCESS_IDS")
    for expected_id, process in zip(PROCESS_IDS, processes):
        if not isinstance(process, Mapping):
            raise ConfigError("Fala version process catalog entry is invalid")
        if str(process.get("id") or "") != expected_id:
            raise ConfigError(
                f"Fala version process catalog order mismatch: expected {expected_id!r}"
            )
        _process_identity_row(process)
    project = version / "source" / "project"
    config = version / "source" / "config.toml"
    python = project / ".venv" / "bin" / "python"
    mode = manifest.get("mode")
    db_path = manifest.get("db_path")
    if mode not in {"dry-run", "live"} or not isinstance(db_path, str) or not db_path:
        raise ConfigError("Fala version identity has invalid mode or database path")
    if not python.is_file() or python.is_symlink() or not os.access(python, os.X_OK):
        raise ConfigError(f"Fala version interpreter is unavailable: {python}")
    environment = _process_environment(
        home=Path.home(),
        project_root=project,
        db_path=Path(db_path),
        candidate_id=candidate_id,
    )
    log_dir = (deployment_root / "logs" / candidate_id).resolve()
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ConfigError("Fala version manifest artifacts are missing")
    version.chmod(0o755)
    (version / "launchd").chmod(0o755)
    _assert_no_forbidden_launchd_artifacts(version / "launchd")
    relative = f"launchd/{SUPERVISOR_LABEL}.plist"
    plist_path = version / relative
    try:
        document = plistlib.loads(plist_path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise ConfigError(f"invalid immutable Fala supervisor plist: {plist_path}") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"invalid immutable Fala supervisor plist: {plist_path}")
    expected_args = _supervisor_program_arguments(
        python_path=python,
        config_path=config,
        db_path=Path(db_path),
        mode=mode,
    )
    source_arguments = document.get("ProgramArguments")
    if (
        not isinstance(source_arguments, list)
        or len(source_arguments) != 9
        or source_arguments[1:3] != ["-m", SUPERVISOR_MODULE]
        or source_arguments[3] != "--config"
        or source_arguments[5] != "--db"
        or source_arguments[6] != db_path
        or source_arguments[7] != f"--{mode}"
        or source_arguments[8] != "--json"
        or not Path(str(source_arguments[0])).is_absolute()
    ):
        raise ConfigError(f"Fala version plist does not match supervisor command contract: {SUPERVISOR_LABEL}")
    document["ProgramArguments"] = expected_args
    document["EnvironmentVariables"] = environment
    document["WorkingDirectory"] = str(project)
    document["Label"] = SUPERVISOR_LABEL
    document["RunAtLoad"] = True
    document["KeepAlive"] = True
    if "StartInterval" in document:
        raise ConfigError(f"Fala version supervisor plist must not set StartInterval: {SUPERVISOR_LABEL}")
    for key in ("StandardOutPath", "StandardErrorPath"):
        value = document.get(key)
        if not isinstance(value, str) or not value:
            raise ConfigError(f"Fala version plist is missing {key}: {SUPERVISOR_LABEL}")
        document[key] = str(log_dir / Path(value).name)
    plist_data = plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=False)
    artifacts[relative] = {"sha256": _sha256_bytes(plist_data), "bytes": len(plist_data)}
    runtime = _runtime_identity(document, plist_data, label=SUPERVISOR_LABEL)
    program_arguments = [list(runtime["program_arguments"])]
    runtime_identity = [runtime]
    dispatch_commands = _dispatch_commands(
        python_path=python,
        config_path=config,
        db_path=Path(db_path),
        mode=mode,
        processes=list(processes),
    )
    manifest["program_arguments"] = program_arguments
    manifest["runtime_identity"] = runtime_identity
    manifest["dispatch_commands"] = dispatch_commands
    manifest["artifacts"] = artifacts
    manifest["candidate_id"] = candidate_id
    _atomic_write(plist_path, plist_data)
    _atomic_write(manifest_path, _canonical_json(manifest))
    _seal_tree(version)
    if len(manifest.get("program_arguments") or []) != 1:
        raise ConfigError("promoted Fala program_arguments must contain the supervisor only")
    if len(manifest.get("dispatch_commands") or []) != len(PROCESS_IDS):
        raise ConfigError("promoted Fala dispatch_commands must cover all catalog processes")
    if len(manifest.get("runtime_identity") or []) != 1 or any(
        runtime.get("program_arguments") != args
        for runtime, args in zip(manifest["runtime_identity"], manifest["program_arguments"])
    ):
        raise ConfigError("promoted Fala runtime identities do not match program_arguments")


def _verify_version_reuse(candidate: Path, version: Path) -> None:
    """Compare copied immutable bytes while allowing promotion-bound runtime metadata."""
    excluded = {
        Path("manifest.json"),
        Path(f"launchd/{SUPERVISOR_LABEL}.plist"),
    }
    candidate_files = {path.relative_to(candidate) for path in candidate.rglob("*") if path.is_file()} - excluded
    version_files = {path.relative_to(version) for path in version.rglob("*") if path.is_file()} - excluded
    if candidate_files != version_files:
        raise ConfigError("existing deployment version file set differs from candidate")
    for relative in sorted(candidate_files):
        source = candidate / relative
        installed = version / relative
        if source.read_bytes() != installed.read_bytes() or _sha256_file(source) != _sha256_file(installed):
            raise ConfigError(f"existing deployment version byte/hash mismatch: {relative}")


def _assert_deployment_root_inventory(root: Path) -> None:
    """Reject untracked pointer aliases before mutating deployment state."""
    if not root.exists():
        return
    unexpected = sorted(path.name for path in root.iterdir() if path.is_symlink() and path.name != "current")
    if unexpected:
        raise ConfigError(f"unexpected deployment root symlinks: {', '.join(unexpected)}")


def deploy_fala(cfg: LokayConfig, candidate_value: str, promote: bool, *, deployment_root: str | None = None) -> dict[str, Any]:
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
    with _deployment_lock(root):
        _assert_deployment_root_inventory(root)
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
        try:
            candidate_manifest = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"invalid candidate manifest: {candidate}") from exc
        identity = candidate_manifest.get("identity")
        processes = identity.get("processes") if isinstance(identity, dict) else candidate_manifest.get("processes")
        if not isinstance(processes, list) or len(processes) != len(PROCESS_IDS):
            raise ConfigError("candidate process catalog must match PROCESS_IDS")
        for expected_id, process in zip(PROCESS_IDS, processes):
            if not isinstance(process, Mapping) or str(process.get("id") or "") != expected_id:
                raise ConfigError("candidate process catalog must match PROCESS_IDS order")
            _process_identity_row(process)
        labels = [SUPERVISOR_LABEL]
        stale_labels = [label for label in STALE_FALA_LABELS if label != SUPERVISOR_LABEL]
        launchd_dir = candidate / "launchd"
        _assert_no_forbidden_launchd_artifacts(launchd_dir)
        plists = {SUPERVISOR_LABEL: launchd_dir / f"{SUPERVISOR_LABEL}.plist"}
        for label, plist in plists.items():
            try:
                subprocess.run(["plutil", "-lint", str(plist)], check=True, capture_output=True, text=True)
            except (OSError, subprocess.CalledProcessError) as exc:
                raise ConfigError(f"Fala plist lint failed for {label}: {exc}") from exc
        legacy_states = _snapshot_legacy_mutators()
        # Supervisor-only installation; transition out aggregate and per-process jobs.
        managed_labels = [SUPERVISOR_LABEL, *stale_labels]
        domain_states_by_label: dict[str, dict[str, dict[str, Any]]] = {
            label: _launchctl_domain_states(label) for label in managed_labels
        }
        primary_label = SUPERVISOR_LABEL
        domain = _launchctl_intended_domain(primary_label, domain_states_by_label[primary_label])
        launch_agents = Path.home() / "Library" / "LaunchAgents"
        launch_agents.mkdir(parents=True, exist_ok=True)
        installed_snapshot: dict[str, dict[str, Any]] = {}
        for label in [*labels, *stale_labels]:
            target = launch_agents / f"{label}.plist"
            if target.is_symlink():
                raise ConfigError(f"installed Fala LaunchAgent plist must be a regular file: {target}")
            if target.exists() and not target.is_file():
                raise ConfigError(f"installed Fala LaunchAgent path is not a regular file: {target}")
            old_agent_exists = target.is_file()
            old_agent_data = target.read_bytes() if old_agent_exists else None
            states = domain_states_by_label.get(label, {})
            if any(state.get("loaded") for state in states.values()) and not old_agent_exists:
                raise ConfigError(f"loaded Fala service has no canonical installed plist to preserve: {label}")
            installed_snapshot[label] = {
                "present": old_agent_exists,
                "sha256": _sha256_bytes(old_agent_data) if old_agent_data is not None else None,
                "data_base64": base64.b64encode(old_agent_data).decode("ascii") if old_agent_data is not None else None,
                "path": str(target),
            }
        previous_path = root / "previous.json"
        previous_data = previous_path.read_bytes() if previous_path.is_file() else None
        previous = {
            "candidate_id": old_current_target.name if old_current_target else None,
            "path": str(old_current_target) if old_current_target else None,
            "loaded_state": domain_states_by_label,
            "legacy_loaded_state": {},
            "labels": managed_labels,
            "domain": domain,
            "installed_plists": installed_snapshot,
        }
        version_created = False
        version_plists = dict(plists)
        try:
            if version.exists():
                validate_fala_candidate(version, deployment_root=root)
                version_manifest = json.loads((version / "manifest.json").read_text(encoding="utf-8"))
                if version_manifest.get("candidate_id") != candidate_id or version_manifest.get("identity") != candidate_manifest.get("identity"):
                    raise ConfigError("existing deployment version identity differs from candidate")
                _verify_version_reuse(candidate, version)
                version_plists = {SUPERVISOR_LABEL: version / "launchd" / f"{SUPERVISOR_LABEL}.plist"}
            else:
                shutil.copytree(candidate, version, copy_function=shutil.copy2)
                version_created = True
                _seal_tree(version)
                _verify_candidate_copy(candidate, version)
                _promote_version_runtime(version, root, candidate_id)
                validate_fala_candidate(version, deployment_root=root)
                version_plists = {SUPERVISOR_LABEL: version / "launchd" / f"{SUPERVISOR_LABEL}.plist"}
                for label, plist in version_plists.items():
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
        # Re-probe immediately before current/pointer and launchd cutover so a
        # legacy mutator that loaded during staging aborts before activation,
        # and so previous.json / bootout verification / rollback use launchd
        # state observed after staging rather than a stale pre-staging snapshot.
        try:
            legacy_states = _snapshot_legacy_mutators()
            previous["legacy_loaded_state"] = _legacy_state_summary(legacy_states)
            domain_states_by_label = {
                label: _launchctl_domain_states(label) for label in managed_labels
            }
            domain = _launchctl_intended_domain(primary_label, domain_states_by_label[primary_label])
            previous["loaded_state"] = domain_states_by_label
            previous["domain"] = domain
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

        installed_targets = {label: launch_agents / f"{label}.plist" for label in [*labels, *stale_labels]}
        try:
            _bootout_legacy_mutators(legacy_states)
            _atomic_write(previous_path, _canonical_json(previous))
            tmp_link = root / ".current.next"
            if tmp_link.exists() or tmp_link.is_symlink():
                tmp_link.unlink()
            tmp_link.symlink_to(version, target_is_directory=True)
            os.replace(tmp_link, current)
            _fsync_directory(root)
            for label, states in domain_states_by_label.items():
                for observed_domain, observed_state in states.items():
                    _launchctl_bootout(observed_domain, label, ignore_failure=True)
                    if observed_state.get("available") is not False:
                        _verify_launchctl_unloaded(label, observed_domain)
            for label in stale_labels:
                target = installed_targets[label]
                if target.exists() or target.is_symlink():
                    target.unlink()
            _fsync_directory(launch_agents)
            residual_installed = [
                label for label in stale_labels
                if installed_targets[label].exists() or installed_targets[label].is_symlink()
            ]
            if residual_installed:
                raise ConfigError(
                    "stale Fala LaunchAgent plist remains after removal: "
                    + ", ".join(residual_installed)
                )
            for label, plist in version_plists.items():
                target = installed_targets[label]
                _atomic_write(target, plist.read_bytes())
                subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True, capture_output=True, text=True)
                subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{label}"], check=True, capture_output=True, text=True)
                _verify_launchctl_exact(label, domain)
            _publish_generation(candidate_id)
        except (OSError, subprocess.CalledProcessError, ConfigError) as exc:
            rollback_errors: list[str] = []

            def restore_step(step_label: str, operation: Callable[[], None]) -> None:
                try:
                    operation()
                except (OSError, subprocess.CalledProcessError, ConfigError) as restore_exc:
                    rollback_errors.append(f"{step_label}: {restore_exc}")

            for label, states in domain_states_by_label.items():
                for observed_domain, observed_state in states.items():
                    restore_step(
                        f"unload {observed_domain}/{label}",
                        lambda observed_domain=observed_domain, label=label: _launchctl_bootout(observed_domain, label, ignore_failure=True),
                    )
                    if observed_state.get("available") is not False:
                        restore_step(
                            f"verify unload {observed_domain}/{label}",
                            lambda observed_domain=observed_domain, label=label: _verify_launchctl_unloaded(label, observed_domain),
                        )
            if current.exists() or current.is_symlink():
                restore_step("remove current", lambda: current.unlink())
                restore_step("sync deployment root", lambda: _fsync_directory(root))
            if old_current_target is not None:
                restore_step("restore current", lambda: current.symlink_to(old_current_target, target_is_directory=True))
                restore_step("sync deployment root", lambda: _fsync_directory(root))
            for label, snapshot in installed_snapshot.items():
                target = installed_targets[label]
                if snapshot.get("present") and snapshot.get("data_base64") is not None:
                    data = base64.b64decode(str(snapshot["data_base64"]))
                    restore_step(f"restore installed plist {label}", lambda target=target, data=data: _atomic_write(target, data))
                elif target.exists():
                    restore_step(f"remove installed plist {label}", lambda target=target: target.unlink())
                    restore_step("sync launch agents", lambda target=target: _fsync_directory(target.parent))
            if previous_data is not None:
                restore_step("restore previous.json", lambda: _atomic_write(previous_path, previous_data))
            elif previous_path.exists():
                restore_step("remove previous.json", lambda: previous_path.unlink())
                restore_step("sync deployment root", lambda: _fsync_directory(root))
            for label in managed_labels:
                target = installed_targets[label]
                states = domain_states_by_label.get(label) or {}
                if states:
                    restore_step(
                        f"restore launchd state {label}",
                        lambda states=states, target=target: _launchctl_restore_states(states, target),
                    )
            restore_step("restore legacy launchd state", lambda: _restore_legacy_mutators(legacy_states))
            if rollback_errors:
                detail = "; ".join(rollback_errors)
                raise ConfigError(
                    f"Fala promotion rolled back after launchd failure: {exc}; "
                    f"rollback warnings: {detail}"
                ) from exc
            raise ConfigError(f"Fala promotion rolled back after launchd failure: {exc}") from exc
        result["promoted"] = True
        result["current"] = str(current)
        result["launch_agents"] = [str(installed_targets[label]) for label in labels]
        result["launch_agent"] = result["launch_agents"][0]
        result["loaded_state"] = domain_states_by_label
        result["legacy_loaded_state"] = previous["legacy_loaded_state"]
        return result
