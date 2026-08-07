#!/usr/bin/env python3
"""Verify that launchd points at the deployed bytes from this checkout."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import subprocess
import re
import sys
from pathlib import Path
from typing import Iterable

from lokay.registry import PROCESS_IDS




FALA_PINNED_COMMIT = "b5f9a6d500a442a1c79060a862fe4b9da87bc98f"
FALA_TAG = "0.7.15"
FALA_GIT_URL = "https://github.com/mikolaj92/Fala.git"
POLICY_KEYS = frozenset({"automerge", "require_human_approval", "require_checks", "require_test_evidence", "executor_enabled"})
PROMOTION_POLICY = {
    "automerge": True,
    "require_human_approval": False,
    "require_checks": True,
    "require_test_evidence": True,
    "executor_enabled": True,
}


def is_promotion_policy(policy: object) -> bool:
    return isinstance(policy, dict) and set(policy) == POLICY_KEYS and policy == PROMOTION_POLICY
# Every shell entrypoint copied to ~/.hermes/scripts is part of the deployment
# contract. Keeping this list explicit makes a missing deployment fail closed.
DEPLOYED_SCRIPTS = (
    "lokay_health.sh",
    "lokay_status.sh",
    "lokay_hermes_update.sh",
    "lokay_repos.sh",
    "lokay_smoke.sh",
)
TEMPLATE_ENTRYPOINTS = {
    "lokay-health.plist.template": "lokay_health.sh",
    "lokay-hermes-update.plist.template": "lokay_hermes_update.sh",
}
AGGREGATE_FALA_LABEL = "com.mikolaj92.lokay.fala-tick-all"
AGGREGATE_FALA_MODULE = "lokay.tick_all"
PROCESS_MODULE = "lokay.process"
SUPERVISOR_MODULE = "lokay.supervisor"
SUPERVISOR_LABEL = "com.mikolaj92.lokay.supervisor"
SUPERVISOR_PLIST_RELATIVE = f"launchd/{SUPERVISOR_LABEL}.plist"
SUPERVISOR_TEMPLATE_NAME = "lokay-supervisor.plist.template"
PROCESS_TEMPLATE_NAME = "lokay-process.plist.template"
PROCESS_ENV_KEYS = (
    "HOME",
    "PYTHONPATH",
    "FALA_HOME",
    "FALA_EFFECTOR_ROOT",
    "FALA_CANDIDATE_ID",
    "HERMES_LOKAY_GENERATION",
    "HERMES_LOKAY_PROCESS_STATE_ROOT",
)
PROCESS_PATH_ENV_KEYS = (
    "HOME",
    "PYTHONPATH",
    "FALA_HOME",
    "FALA_EFFECTOR_ROOT",
    "HERMES_LOKAY_PROCESS_STATE_ROOT",
)
PROCESS_IDENTITY_KEYS = ("id", "enabled", "interval_seconds", "command")
RUNTIME_IDENTITY_KEYS = (
    "label",
    "program_arguments",
    "working_directory",
    "standard_out_path",
    "standard_error_path",
    "environment_variables",
    "start_interval",
    "run_at_load",
    "keep_alive",
    "process_type",
    "limit_load_to_session_type",
    "plist_sha256",
)



def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path) -> bool:
    """Return true only for an unlinked, non-symlink regular file."""
    try:
        stat = path.lstat()
    except OSError:
        return False
    return stat.st_nlink == 1 and path.is_file() and not path.is_symlink()


def _root_path(path: Path, label: str, errors: list[str]) -> Path:
    """Resolve a deployment root without allowing a symlinked/non-directory root."""
    path = path.expanduser()
    if path.is_symlink():
        errors.append(f"{label} root must not be a symlink: {path}")
        return path.resolve()
    if not path.is_dir():
        errors.append(f"{label} root must be a directory: {path}")
    return path.resolve()


def _validate_root_inventory(root: Path, label: str, errors: list[str]) -> None:
    """Reject unexpected, linked, or non-regular files in a deployment root."""
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if not (path.is_file() or path.is_symlink()):
            continue
        try:
            stat = path.lstat()
        except OSError:
            errors.append(f"unable to inspect {label} artifact: {path}")
            continue
        if path.is_symlink():
            errors.append(f"{label} artifact must not be a symlink: {path}")
        elif stat.st_nlink != 1:
            errors.append(f"{label} artifact must not be a hardlink: {path}")


def _validate_script_inventory(root: Path, label: str, errors: list[str]) -> None:
    """Reject files that are not explicitly part of the script deployment."""
    if root.is_symlink():
        errors.append(f"{label} script root must not be a symlink: {root}")
        return
    if not root.is_dir():
        return
    expected = set(DEPLOYED_SCRIPTS)
    for path in sorted(root.iterdir()):
        if not (path.is_file() or path.is_symlink()):
            continue
        if path.name not in expected:
            errors.append(f"unexpected {label} script: {path}")
            continue
        try:
            stat = path.lstat()
        except OSError:
            continue
        if path.is_symlink():
            errors.append(f"{label} script must not be a symlink: {path}")
        elif stat.st_nlink != 1:
            errors.append(f"{label} script must not be a hardlink: {path}")


def _validate_config_roots(roots: Iterable[Path], errors: list[str]) -> dict[str, str]:
    """Validate the exact active config inventory and return its hashes."""
    allowed = {"config.toml"}
    hashes: dict[str, str] = {}
    seen: set[Path] = set()
    for root in roots:
        root = root.expanduser()
        if root in seen:
            errors.append(f"duplicate active config root: {root}")
            continue
        seen.add(root)
        if root.is_symlink():
            errors.append(f"active config root must not be a symlink: {root}")
        root = root.resolve()
        if not root.is_dir():
            errors.append(f"required active config directory missing: {root}")
            continue
        files = sorted(path for path in root.iterdir() if path.is_file() or path.is_symlink())
        if not files:
            errors.append(f"no active config artifacts found: {root}")
        for path in files:
            if path.name not in allowed:
                errors.append(f"unexpected active config artifact: {path}")
                continue
            if not _regular_file(path):
                errors.append(f"active config artifact must be a private regular file: {path}")
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(f"invalid active config artifact {path}: {exc}")
                continue
            if "{{" in text or "}}" in text or "<config-path>" in text:
                errors.append(f"unresolved active config placeholder: {path}")
            hashes[str(path)] = sha256(path)
    return hashes

def _template_files(roots: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    seen_roots: set[Path] = set()
    for root in roots:
        root = root.expanduser().resolve()
        if root in seen_roots:
            raise ValueError(f"duplicate launchd template root: {root}")
        seen_roots.add(root)
        if root.name != "launchd" or root.parent.name != "templates":
            raise ValueError(f"non-canonical launchd template root: {root}; expected templates/launchd")
        if not root.is_dir():
            raise ValueError(f"required launchd template directory missing: {root}")
        files.extend(sorted(root.glob("*.plist.template")))
    if not files:
        raise ValueError("no launchd templates found")
    return files


def _render_template(raw: str, active_home: Path, active_root: Path | None = None) -> str:
    """Render compatibility markers used by canonical launchd fixtures."""
    rendered = raw
    active_root = active_root or (active_home / ".hermes" / "scripts")
    project = active_root.parent / "project"
    replacements = {
        "{{HOME}}": str(active_home),
        "{{ACTIVE_SCRIPTS}}": str(active_root),
        "{{PYTHON_PATH}}": str(project / ".venv" / "bin" / "python"),
        "{{PYTHONPATH}}": str(project / "src"),
        "{{FALA_HOME}}": str(project / "Fala"),
        "{{FALA_EFFECTOR_ROOT}}": str(project / "effectors"),
        "{{PROJECT_ROOT}}": str(project),
        "{{CONFIG_PATH}}": str(active_home / ".hermes" / "config.toml"),
        "{{DB_PATH}}": str(active_home / ".hermes" / "fala.sqlite"),
        "{{MODE_ARG}}": "--dry-run",
        "{{INTAKE_LIMIT}}": "10",
        "{{LOG_DIR}}": str(active_home / ".hermes" / "logs"),
        "<config-path>": str(active_home / ".hermes" / "config.toml"),
        # Supervisor template defaults for parity fixture rendering.
        "{{LABEL}}": SUPERVISOR_LABEL,
        "{{COMMAND}}": "lokay-process-repo_issue_poll",
        "{{START_INTERVAL}}": "60",
        "{{CANDIDATE_ID}}": "0" * 64,
        "{{GENERATION}}": "0" * 64,
        "{{PROCESS_STATE_ROOT}}": str(Path(active_home / ".hermes" / "fala.sqlite").parent / "process-state"),
    }
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, value)
    return rendered


def _plist_name(template: Path) -> str:
    suffix = ".plist.template"
    return template.name[:-len(suffix)] + ".plist" if template.name.endswith(suffix) else template.name


def _validate_rendered_roots(
    templates: list[Path],
    roots: Iterable[Path],
    *,
    label: str,
    contracts: dict[str, tuple[str, tuple[str, ...]]],
    errors: list[str],
    exact_inventory: bool = True,
) -> dict[str, Path]:
    """Validate one rendered plist inventory without merging root identities."""
    expected = {_plist_name(path): path for path in templates}
    parsed: dict[str, Path] = {}
    seen_roots: set[Path] = set()
    for root_input in roots:
        root = root_input.expanduser()
        if root in seen_roots:
            errors.append(f"duplicate {label} root: {root}")
            continue
        seen_roots.add(root)
        if root.is_symlink():
            errors.append(f"{label} root must not be a symlink: {root}")
        root = root.resolve()
        if not root.is_dir():
            errors.append(f"required {label} directory missing: {root}")
            continue
        root_names: set[str] = set()
        all_files = sorted(path for path in root.rglob("*") if path.is_file() or path.is_symlink())
        if not exact_inventory:
            all_files = [path for path in all_files if path.name in expected]
        for path in all_files:
            name = path.name
            root_names.add(name)
            if path.is_symlink():
                errors.append(f"{label} launchd artifact must not be a symlink: {path}")
                continue
            try:
                stat = path.lstat()
            except OSError:
                errors.append(f"unable to inspect {label} launchd artifact: {path}")
                continue
            if stat.st_nlink != 1:
                errors.append(f"{label} launchd artifact must not be a hardlink: {path}")
            if path.suffix != ".plist" or name not in expected:
                errors.append(f"unexpected {label} launchd artifact: {path}")
                continue
            try:
                document = plistlib.loads(path.read_bytes())
                raw = path.read_text(encoding="utf-8")
            except (OSError, plistlib.InvalidFileException, UnicodeDecodeError, ValueError) as exc:
                errors.append(f"invalid {label} launchd plist {path}: {exc}")
                continue
            if not isinstance(document, dict):
                errors.append(f"{label} launchd plist must be a dictionary: {path}")
                continue
            if re.search(r"\{\{[^}]+\}\}|<[A-Z][A-Z0-9_-]*>|<config-path>", raw):
                errors.append(f"unresolved {label} launchd template placeholder: {path}")
            contract = contracts.get(name)
            if contract is not None:
                expected_label, expected_args = contract
                if document.get("Label") != expected_label:
                    errors.append(f"{label} launchd Label mismatch: {path}")
                if document.get("ProgramArguments") != list(expected_args):
                    errors.append(f"{label} launchd ProgramArguments mismatch: {path}")
            if name in parsed:
                errors.append(f"duplicate {label} launchd artifact: {name}")
            parsed[name] = path
        missing = set(expected) - root_names
        errors.extend(f"missing {label} launchd artifact: {name}" for name in sorted(missing))
    return parsed


def _validate_active_plist_roots(
    templates: list[Path],
    roots: Iterable[Path],
    *,
    contracts: dict[str, tuple[str, tuple[str, ...]]],
    errors: list[str],
) -> None:
    # Active LaunchAgents only host script entrypoints plus installed process labels.
    # Catalog process templates are candidate-rendered under launchd/<label>.plist, not
    # as a single lokay-process.plist active artifact.
    expected = {
        name: contracts[name]
        for template in templates
        if template.name in TEMPLATE_ENTRYPOINTS and (name := _plist_name(template)) in contracts
    }
    for root_input in roots:
        root = root_input.expanduser()
        if root.is_symlink():
            errors.append(f"active root must not be a symlink: {root}")
        root = root.resolve()
        if not root.is_dir():
            errors.append(f"required active directory missing: {root}")
            continue
        aggregate = root / f"{AGGREGATE_FALA_LABEL}.plist"
        if aggregate.exists():
            errors.append(f"aggregate production launchd artifact is forbidden: {aggregate}")
        for name, (expected_label, expected_args) in expected.items():
            path = root / name
            if not path.exists():
                errors.append(f"missing active launchd artifact: {name}")
                continue
            if path.is_symlink():
                errors.append(f"active launchd artifact must not be a symlink: {path}")
                continue
            try:
                stat = path.lstat()
                document = plistlib.loads(path.read_bytes())
            except (OSError, plistlib.InvalidFileException, ValueError) as exc:
                errors.append(f"invalid active launchd plist {path}: {exc}")
                continue
            if stat.st_nlink != 1:
                errors.append(f"active launchd artifact must not be a hardlink: {path}")
            if not isinstance(document, dict):
                errors.append(f"active launchd plist must be a dictionary: {path}")
                continue
            if document.get("Label") != expected_label:
                errors.append(f"active launchd Label mismatch: {path}")
            if document.get("ProgramArguments") != list(expected_args):
                errors.append(f"active launchd ProgramArguments mismatch: {path}")


def validate(
    source_root: Path,
    active_root: Path,
    template_roots: Iterable[Path],
    *,
    active_plist_roots: Iterable[Path] | None = None,
    render_roots: Iterable[Path] | None = None,
    active_config_roots: Iterable[Path] | None = None,
) -> dict[str, object]:
    errors: list[str] = []
    source_root = _root_path(source_root, "source", errors)
    if (source_root / "scripts").is_dir():
        source_root = source_root / "scripts"
    active_root = _root_path(active_root, "active", errors)
    hashes: dict[str, str] = {}
    _validate_script_inventory(source_root, "source", errors)
    _validate_script_inventory(active_root, "active", errors)
    for name in DEPLOYED_SCRIPTS:
        source, active = source_root / name, active_root / name
        if not _regular_file(source):
            errors.append(f"missing source script: {source}")
            continue
        if not _regular_file(active):
            errors.append(f"missing active script: {active}")
            continue
        source_hash, active_hash = sha256(source), sha256(active)
        hashes[name] = source_hash
        if source_hash != active_hash:
            errors.append(f"deployment hash mismatch: {name} source={source_hash} active={active_hash}")
    home = Path(os.environ.get("HOME", str(Path.home()))).expanduser().resolve()
    active_home = active_root.parent.parent if active_root.name == "scripts" else home
    templates = _template_files(template_roots)
    contracts: dict[str, tuple[str, tuple[str, ...]]] = {}
    seen_labels: dict[str, Path] = {}
    seen_executors: dict[tuple[str, ...], Path] = {}
    seen_names: set[str] = set()
    expected_names = set(TEMPLATE_ENTRYPOINTS)
    for template in templates:
        if template.name in seen_names:
            errors.append(f"duplicate launchd template entry: {template.name}")
        seen_names.add(template.name)
        try:
            raw = template.read_text(encoding="utf-8")
            rendered = _render_template(raw, active_home, active_root)
            unresolved = re.findall(r"\{\{[^}]+\}\}|<[A-Z][A-Z0-9_-]*>|<config-path>", rendered)
            if unresolved:
                errors.append(f"unresolved launchd template placeholder: {template}: {', '.join(sorted(set(unresolved)))}")
            document = plistlib.loads(rendered.encode("utf-8"))
        except (OSError, plistlib.InvalidFileException, UnicodeDecodeError, ValueError) as exc:
            errors.append(f"invalid launchd template {template}: {exc}")
            continue
        if not isinstance(document, dict):
            errors.append(f"launchd plist must be a dictionary: {template}")
            continue
        label = document.get("Label")
        if not isinstance(label, str) or not label:
            errors.append(f"launchd Label missing: {template}")
        elif label in seen_labels:
            errors.append(f"duplicate launchd Label {label}: {template} and {seen_labels[label]}")
        else:
            seen_labels[label] = template
        arguments = document.get("ProgramArguments")
        if not isinstance(arguments, list) or not arguments or any(not isinstance(value, str) or not value for value in arguments):
            errors.append(f"launchd ProgramArguments missing or invalid: {template}")
            continue
        executor = tuple(arguments)
        if executor in seen_executors:
            errors.append(f"duplicate launchd ProgramArguments executor: {template} and {seen_executors[executor]}")
        else:
            seen_executors[executor] = template
        contracts[_plist_name(template)] = (label, tuple(arguments))
        executable = arguments[0]
        expected_name = TEMPLATE_ENTRYPOINTS.get(template.name)
        if expected_name:
            expected = active_root / expected_name
            if Path(executable).name != expected_name:
                errors.append(f"launchd entrypoint mismatch: {template} points to {Path(executable).name}; expected {expected_name}")
            elif Path(executable).expanduser().resolve() != expected:
                errors.append(f"launchd ProgramArguments path mismatch: {template} points to {executable}; expected {expected}")
        else:
            if label == AGGREGATE_FALA_LABEL or AGGREGATE_FALA_MODULE in arguments or template.name == "lokay-fala-tick-all.plist.template":
                errors.append(f"aggregate production launchd template is forbidden: {template}")
            elif template.name == PROCESS_TEMPLATE_NAME or PROCESS_MODULE in arguments:
                errors.append(f"per-process production launchd template is forbidden: {template}")
            elif template.name != SUPERVISOR_TEMPLATE_NAME and SUPERVISOR_MODULE not in arguments:
                errors.append(f"launchd executable is not a deployed script: {template}")
            else:
                if label != SUPERVISOR_LABEL:
                    errors.append(f"supervisor launchd Label mismatch: {template}")
                if (
                    document.get("ProcessType") != "Background"
                    or document.get("RunAtLoad") is not True
                    or document.get("KeepAlive") is not True
                    or "StartInterval" in document
                ):
                    errors.append(f"supervisor launchd schedule/process contract invalid: {template}")
                if document.get("LimitLoadToSessionType") not in (None, "Background"):
                    errors.append(f"supervisor launchd session contract invalid: {template}")
                env = document.get("EnvironmentVariables")
                project = active_root.parent / "project"
                expected_env = {
                    "HOME": str(active_home),
                    "PYTHONPATH": str(project / "src"),
                    "FALA_HOME": str(project / "Fala"),
                    "FALA_EFFECTOR_ROOT": str(project / "effectors"),
                    "FALA_CANDIDATE_ID": "0" * 64,
                    "HERMES_LOKAY_GENERATION": "0" * 64,
                    "HERMES_LOKAY_PROCESS_STATE_ROOT": str(
                        (active_home / ".hermes" / "fala.sqlite").parent / "process-state"
                    ),
                }
                if not isinstance(env, dict) or set(env) != set(PROCESS_ENV_KEYS):
                    errors.append(f"supervisor launchd environment must be exactly {sorted(PROCESS_ENV_KEYS)}: {template}")
                elif env != expected_env:
                    errors.append(f"supervisor launchd environment values mismatch: {template}")
                for key in ("StandardOutPath", "StandardErrorPath"):
                    if not isinstance(document.get(key), str) or not Path(document[key]).is_absolute():
                        errors.append(f"supervisor launchd {key} is invalid: {template}")
                mode_flags = [value for value in arguments if value in ("--dry-run", "--live")]
                if len(mode_flags) != 1:
                    errors.append(f"supervisor launchd mode flags are not exactly once: {template}")
                else:
                    _validate_supervisor_args(
                        arguments,
                        project=project,
                        config=active_home / ".hermes" / "config.toml",
                        db_path=str(active_home / ".hermes" / "fala.sqlite"),
                        mode=mode_flags[0][2:],
                        label="launchd template",
                        errors=errors,
                    )
                working_directory = document.get("WorkingDirectory")
                if not isinstance(working_directory, str) or not working_directory:
                    errors.append(f"supervisor launchd WorkingDirectory is missing: {template}")
                else:
                    project_path = Path(working_directory).expanduser()
                    if not project_path.is_absolute():
                        errors.append(f"supervisor launchd WorkingDirectory is not absolute: {template}")
                    if "candidates" in project_path.parts:
                        errors.append(f"supervisor launchd WorkingDirectory points at mutable candidates: {template}")
                    if working_directory != str(active_root.parent / "project"):
                        errors.append(f"supervisor launchd WorkingDirectory is not project-local: {template}")

    errors.extend(f"missing launchd template: {name}" for name in sorted(expected_names - seen_names))
    plist_roots = list(active_plist_roots or [])
    rendered_roots = list(render_roots or [])
    config_roots = list(active_config_roots or [])
    if active_plist_roots is not None:
        if not plist_roots or any(root is None for root in plist_roots):
            errors.append("active plist parity root is omitted")
        else:
            _validate_active_plist_roots(templates, plist_roots, contracts=contracts, errors=errors)
    if render_roots is not None:
        if not rendered_roots or any(root is None for root in rendered_roots):
            errors.append("rendered parity root is omitted")
        else:
            _validate_rendered_roots(templates, rendered_roots, label="rendered", contracts=contracts, errors=errors)
    if active_config_roots is not None:
        if not config_roots or any(root is None for root in config_roots):
            errors.append("active config parity root is omitted")
        else:
            config_hashes = _validate_config_roots(config_roots, errors)
    else:
        config_hashes = {}
    result: dict[str, object] = {"ok": not errors, "source_root": str(source_root), "active_root": str(active_root), "scripts": hashes, "configs": config_hashes, "templates": sorted(str(path) for path in templates), "errors": errors}
    if errors:
        raise DeploymentParityError(result)
    return result
def _relative_candidate_path(candidate: Path, value: object, label: str, errors: list[str]) -> Path | None:
    if not isinstance(value, str) or not value or Path(value).is_absolute() or "\x00" in value or ".." in Path(value).parts:
        errors.append(f"Fala {label} must be a safe relative candidate path")
        return None
    path = (candidate / value).resolve()
    try:
        path.relative_to(candidate)
    except ValueError:
        errors.append(f"Fala {label} escapes candidate root: {value}")
        return None
    return path



def _validate_supervisor_args(
    args: object,
    *,
    project: Path,
    config: Path,
    db_path: str,
    mode: object,
    label: str,
    errors: list[str],
) -> list[str] | None:
    if not isinstance(args, list) or any(not isinstance(value, str) for value in args):
        errors.append(f"Fala {label} ProgramArguments must be a string list")
        return None
    if mode not in {"dry-run", "live"}:
        errors.append(f"Fala {label} mode is invalid")
        return None
    if not args:
        errors.append(f"Fala {label} ProgramArguments must not be empty")
        return None
    if AGGREGATE_FALA_MODULE in args or any(AGGREGATE_FALA_LABEL in value for value in args):
        errors.append(f"Fala {label} ProgramArguments must not use aggregate tick_all")
    if PROCESS_MODULE in args:
        errors.append(f"Fala {label} ProgramArguments must not use per-process module")
    python = str(project / ".venv" / "bin" / "python")
    expected = [
        python,
        "-m",
        SUPERVISOR_MODULE,
        "--config",
        str(config),
        "--db",
        db_path,
        f"--{mode}",
        "--json",
    ]
    if args != expected:
        errors.append(f"Fala {label} ProgramArguments do not match canonical supervisor contract")
    if args.count("--config") != 1 or args.count("--db") != 1 or args.count("-m") != 1:
        errors.append(f"Fala {label} ProgramArguments flags are not exactly once")
    if not Path(args[0]).is_absolute():
        errors.append(f"Fala {label} python executable must be absolute")
    return args


def _validate_child_args(
    args: object,
    *,
    project: Path,
    config: Path,
    db_path: str,
    mode: object,
    command: str,
    label: str,
    errors: list[str],
) -> list[str] | None:
    if not isinstance(args, list) or any(not isinstance(value, str) for value in args):
        errors.append(f"Fala {label} dispatch command must be a string list")
        return None
    if mode not in {"dry-run", "live"}:
        errors.append(f"Fala {label} mode is invalid")
        return None
    if not args:
        errors.append(f"Fala {label} dispatch command must not be empty")
        return None
    if AGGREGATE_FALA_MODULE in args or any(AGGREGATE_FALA_LABEL in value for value in args):
        errors.append(f"Fala {label} dispatch command must not use aggregate tick_all")
    if SUPERVISOR_MODULE in args:
        errors.append(f"Fala {label} dispatch command must not use supervisor module")
    python = str(project / ".venv" / "bin" / "python")
    expected = [
        python,
        "-m",
        PROCESS_MODULE,
        command,
        "--config",
        str(config),
        "--db",
        db_path,
        f"--{mode}",
        "--json",
    ]
    if args != expected:
        errors.append(f"Fala {label} dispatch command does not match canonical process contract")
    if args.count("--config") != 1 or args.count("--db") != 1 or args.count("-m") != 1:
        errors.append(f"Fala {label} dispatch command flags are not exactly once")
    if not Path(args[0]).is_absolute():
        errors.append(f"Fala {label} python executable must be absolute")
    return args


def _expected_dispatch_commands(
    *,
    project: Path,
    config: Path,
    db_path: str,
    mode: object,
    process_rows: list[dict[str, object]],
) -> list[list[str]]:
    python = str(project / ".venv" / "bin" / "python")
    expected: list[list[str]] = []
    for process_id, row in zip(PROCESS_IDS, process_rows):
        command = str(row.get("command") or f"lokay-process-{process_id}")
        expected.append(
            [
                python,
                "-m",
                PROCESS_MODULE,
                command,
                "--config",
                str(config),
                "--db",
                db_path,
                f"--{mode}",
                "--json",
            ]
        )
    return expected


def _load_candidate_processes(config_path: Path, errors: list[str]) -> list[dict[str, object]]:
    """Load the exact catalog-derived process identity rows from candidate config."""
    try:
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
            import tomli as tomllib  # type: ignore

        embedded = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        errors.append(f"Fala candidate process catalog is unreadable: {exc}")
        return []
    processes = embedded.get("processes")
    if not isinstance(processes, list) or not processes:
        errors.append("Fala candidate process catalog is missing")
        return []
    if len(processes) != len(PROCESS_IDS):
        errors.append(f"Fala candidate must define exactly {len(PROCESS_IDS)} processes, found {len(processes)}")
    rows: list[dict[str, object]] = []
    for index, process in enumerate(processes):
        if not isinstance(process, dict):
            errors.append(f"Fala candidate process row is invalid at index {index}")
            continue
        if "launchd_label" in process:
            errors.append(f"Fala candidate process launchd_label is forbidden at index {index}")
        process_id = process.get("id")
        enabled = process.get("enabled")
        interval = process.get("interval_seconds")
        command = process.get("command")
        expected_id = PROCESS_IDS[index] if index < len(PROCESS_IDS) else None
        if not isinstance(process_id, str) or not process_id:
            errors.append(f"Fala candidate process id is invalid at index {index}")
            continue
        if expected_id is not None and process_id != expected_id:
            errors.append(
                f"Fala candidate process order mismatch at index {index}: expected {expected_id!r}, got {process_id!r}"
            )
        if not isinstance(enabled, bool):
            errors.append(f"Fala candidate process enabled is invalid for {process_id}")
        if not isinstance(interval, int) or isinstance(interval, bool) or interval < 30:
            errors.append(f"Fala candidate process interval_seconds is invalid for {process_id}")
        if not isinstance(command, str) or command != f"lokay-process-{process_id}":
            errors.append(f"Fala candidate process command is invalid for {process_id}")
        identity_keys = set(process) & set(PROCESS_IDENTITY_KEYS)
        # Accept full catalog rows in config; identity surface is only the four keys.
        rows.append(
            {
                "id": process_id,
                "enabled": enabled,
                "interval_seconds": interval,
                "command": command,
            }
        )
        del identity_keys
    return rows




def _git_head(path: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def validate_fala_candidate(candidate: Path, *, deployment_root: Path | None = None) -> dict[str, object]:
    """Validate an immutable Fala candidate without filesystem mutation."""
    candidate_input = candidate.expanduser()
    candidate_is_symlink = candidate_input.is_symlink()
    candidate = candidate_input.resolve()
    errors: list[str] = []
    if candidate_is_symlink:
        errors.append("Fala candidate must not be a symlink")
    if candidate.is_symlink():
        errors.append("Fala candidate must not be a symlink")
    if deployment_root is not None:
        root_errors: list[str] = []
        root = _root_path(deployment_root, "deployment", root_errors)
        if root_errors:
            errors.extend(root_errors)
            raise DeploymentParityError({"ok": False, "candidate": str(candidate), "errors": errors})
        allowed = ((root / "candidates").resolve(), (root / "versions").resolve())
        if candidate.parent.resolve() not in allowed:
            errors.append(f"Fala candidate must be a direct child of candidates or versions: {candidate}")
    manifest_path = candidate / "manifest.json"
    if not candidate.is_dir() or not _regular_file(manifest_path):
        errors.append(f"invalid Fala candidate: {candidate}")
        raise DeploymentParityError({"ok": False, "candidate": str(candidate), "errors": errors})
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentParityError({"ok": False, "candidate": str(candidate), "errors": [f"invalid Fala manifest: {exc}"]}) from exc
    if not isinstance(manifest, dict):
        raise DeploymentParityError({"ok": False, "candidate": str(candidate), "errors": ["Fala manifest must be an object"]})

    stable_keys = {
        "schema",
        "mode",
        "plugin_commit",
        "fala_tag",
        "fala_commit",
        "lock_hash",
        "config_path",
        "config_hash",
        "db_path",
        "metadata_path",
        "lock_path",
        "config_artifact_path",
        "revision_path",
        "policy",
        "repos",
        "processes",
    }
    manifest_required = stable_keys | {
        "candidate_id",
        "identity",
        "created_at",
        "program_arguments",
        "dispatch_commands",
        "artifacts",
        "runtime_identity",
    }
    if manifest.get("schema") != 1:
        errors.append("Fala manifest schema must be 1")
    if set(manifest) != manifest_required:
        errors.append("Fala manifest key set is invalid")
    identity = manifest.get("identity")
    if not isinstance(identity, dict) or set(identity) != stable_keys:
        errors.append("Fala manifest identity key set is invalid")
        identity = identity if isinstance(identity, dict) else {}
    candidate_id = manifest.get("candidate_id")
    if not isinstance(candidate_id, str) or len(candidate_id) != 64 or any(ch not in "0123456789abcdef" for ch in candidate_id):
        errors.append("Fala candidate_id must be a lowercase 64-hex string")
        candidate_id = str(candidate_id or "")
    if candidate_id != candidate.name:
        errors.append("Fala candidate_id does not match candidate directory")
    expected_id = hashlib.sha256((json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()
    if candidate_id != expected_id:
        errors.append("Fala candidate_id does not match canonical stable identity")
    for key in stable_keys:
        if manifest.get(key) != identity.get(key):
            errors.append(f"Fala candidate identity mismatch: {key}")

    mode = identity.get("mode")
    if mode not in {"dry-run", "live"}:
        errors.append("Fala candidate mode must be dry-run or live")
    for key in ("plugin_commit", "fala_commit", "lock_hash", "config_hash"):
        if not isinstance(identity.get(key), str) or not identity[key]:
            errors.append(f"Fala identity {key} must be a non-empty string")
    for key in ("config_path", "db_path"):
        value = identity.get(key)
        if not isinstance(value, str) or not value or not Path(value).is_absolute() or "\x00" in value:
            errors.append(f"Fala {key} must be an absolute path")
    repos_inventory = identity.get("repos")
    if not isinstance(repos_inventory, list) or any(
        not isinstance(r, dict)
        or set(r) != {"repo", "board", "clone_path", "priority"}
        or not isinstance(r["repo"], str)
        or not isinstance(r["board"], str)
        or not (isinstance(r["clone_path"], str) or r["clone_path"] is None)
        or not isinstance(r["priority"], int)
        or isinstance(r["priority"], bool)
        for r in repos_inventory
    ):
        errors.append("Fala repos inventory is missing or invalid")
    paths: dict[str, Path | None] = {}
    for key in ("metadata_path", "lock_path", "config_artifact_path", "revision_path"):
        paths[key] = _relative_candidate_path(candidate, identity.get(key), key, errors)
    project = candidate / "source" / "project"
    config = candidate / "source" / "config.toml"

    try:
        candidate_real = candidate.resolve()
        for path in candidate.rglob("*"):
            if path.is_symlink():
                errors.append(f"Fala candidate contains symlink: {path.relative_to(candidate)}")
                continue
            try:
                path.resolve().relative_to(candidate_real)
            except ValueError:
                errors.append(f"Fala candidate path escapes root: {path.relative_to(candidate)}")
            stat = path.stat()
            if path.is_dir() and stat.st_mode & 0o222:
                errors.append(f"Fala candidate directory is writable: {path.relative_to(candidate)}")
            if path.is_file() and (stat.st_nlink != 1 or stat.st_mode & 0o222):
                errors.append(f"Fala candidate file is writable or hardlinked: {path.relative_to(candidate)}")
    except OSError as exc:
        errors.append(f"unable to inspect Fala candidate tree: {exc}")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts or any(not isinstance(k, str) or not k for k in artifacts):
        errors.append("Fala manifest artifacts must be a non-empty object")
        artifacts = artifacts if isinstance(artifacts, dict) else {}
    aggregate_relative = f"launchd/{AGGREGATE_FALA_LABEL}.plist"
    if aggregate_relative in artifacts or (candidate / aggregate_relative).exists():
        errors.append("aggregate production launchd artifact is forbidden")
    launchd_dir = candidate / "launchd"
    if launchd_dir.is_dir():
        for path in sorted(launchd_dir.glob("*.plist")):
            relative = str(path.relative_to(candidate))
            if relative == aggregate_relative:
                continue
            if relative != SUPERVISOR_PLIST_RELATIVE:
                errors.append(f"per-process production launchd artifact is forbidden: {relative}")
        for path in sorted(launchd_dir.glob("*.plist.template")):
            errors.append(f"per-process production launchd template is forbidden: {path.relative_to(candidate)}")
    config_artifact_path = paths["config_artifact_path"]
    catalog_source = config_artifact_path if config_artifact_path and _regular_file(config_artifact_path) else config
    catalog = _load_candidate_processes(catalog_source, errors) if catalog_source and _regular_file(catalog_source) else []
    identity_processes = identity.get("processes")
    if not isinstance(identity_processes, list):
        errors.append("Fala identity processes inventory is missing or invalid")
        identity_processes = []
    if len(catalog) != len(PROCESS_IDS):
        errors.append(f"Fala candidate must define exactly {len(PROCESS_IDS)} processes, found {len(catalog)}")
    if len(identity_processes) != len(PROCESS_IDS):
        errors.append(
            f"Fala identity processes must contain exactly {len(PROCESS_IDS)} entries, found {len(identity_processes)}"
        )
    normalized_identity: list[dict[str, object]] = []
    for index, row in enumerate(identity_processes):
        if not isinstance(row, dict):
            errors.append(f"Fala identity process row is invalid at index {index}")
            continue
        if "launchd_label" in row:
            errors.append(f"Fala process launchd_label is forbidden for {row.get('id') or index}")
        if set(row) != set(PROCESS_IDENTITY_KEYS):
            errors.append(f"Fala process identity key set is invalid for {row.get('id') or index}")
        process_id = row.get("id")
        expected_id = PROCESS_IDS[index] if index < len(PROCESS_IDS) else None
        if not isinstance(process_id, str) or not process_id:
            errors.append(f"Fala identity process id is invalid at index {index}")
            continue
        if expected_id is not None and process_id != expected_id:
            errors.append(
                f"Fala identity process order mismatch at index {index}: expected {expected_id!r}, got {process_id!r}"
            )
        command = row.get("command")
        if not isinstance(command, str) or command != f"lokay-process-{process_id}":
            errors.append(f"Fala identity process command is invalid for {process_id}")
        enabled = row.get("enabled")
        if not isinstance(enabled, bool):
            errors.append(f"Fala identity process enabled is invalid for {process_id}")
        interval = row.get("interval_seconds")
        if not isinstance(interval, int) or isinstance(interval, bool) or interval < 30:
            errors.append(f"Fala identity process interval_seconds is invalid for {process_id}")
        normalized_identity.append(
            {
                "id": process_id,
                "enabled": enabled,
                "interval_seconds": interval,
                "command": command,
            }
        )
    if catalog and normalized_identity and normalized_identity != catalog:
        errors.append("Fala identity processes do not match candidate config catalog")
    process_rows = catalog if catalog else normalized_identity
    process_ids = [row.get("id") for row in process_rows if isinstance(row, dict)]
    if process_ids and process_ids != list(PROCESS_IDS[: len(process_ids)]):
        errors.append("Fala process catalog must match canonical PROCESS_IDS order")
    if len(process_ids) != len(set(process_ids)):
        errors.append("Fala process catalog contains duplicate process ids")
    supervisor_relative = SUPERVISOR_PLIST_RELATIVE
    supervisor_path = _relative_candidate_path(candidate, supervisor_relative, "supervisor plist path", errors)
    required_paths = {
        supervisor_relative,
        *(str(identity.get(key) or "") for key in ("metadata_path", "lock_path", "config_artifact_path", "revision_path")),
    }
    actual_artifacts = {str(path.relative_to(candidate)) for path in candidate.rglob("*") if path.is_file() and path != manifest_path}
    if set(artifacts) != actual_artifacts:
        unexpected = sorted(actual_artifacts - set(artifacts))
        missing = sorted(set(artifacts) - actual_artifacts)
        details = [*unexpected, *(f"missing:{item}" for item in missing)]
        errors.append(f"Fala artifact inventory does not match candidate files: {', '.join(details)}")
        if unexpected:
            errors.append(f"unmanifested candidate artifacts: {', '.join(unexpected)}")
    if not required_paths.issubset(set(artifacts)):
        errors.append("Fala artifact inventory is missing required artifacts")
    for relative, declared in artifacts.items():
        path = _relative_candidate_path(candidate, relative, "artifact path", errors)
        if path is None or not _regular_file(path):
            errors.append(f"missing or non-regular Fala candidate artifact: {relative}")
            continue
        if not isinstance(declared, dict) or set(declared) != {"sha256", "bytes"} or not isinstance(declared.get("sha256"), str) or len(declared["sha256"]) != 64 or not isinstance(declared.get("bytes"), int) or declared["bytes"] < 0:
            errors.append(f"Fala candidate artifact declaration is invalid: {relative}")
            continue
        if declared["sha256"] != sha256(path):
            errors.append(f"Fala candidate artifact hash mismatch: {relative}")
        if declared["bytes"] != path.stat().st_size:
            errors.append(f"Fala candidate artifact byte-size mismatch: {relative}")

    runtime_entries = manifest.get("runtime_identity")
    program_arguments = manifest.get("program_arguments")
    dispatch_commands = manifest.get("dispatch_commands")
    if not isinstance(runtime_entries, list) or len(runtime_entries) != 1:
        errors.append("Fala runtime_identity must be a list of exactly 1 supervisor entry")
        runtime_entries = runtime_entries if isinstance(runtime_entries, list) else []
    if not isinstance(program_arguments, list) or len(program_arguments) != 1:
        errors.append("Fala program_arguments must be a list of exactly 1 supervisor argv list")
        program_arguments = program_arguments if isinstance(program_arguments, list) else []
    if not isinstance(dispatch_commands, list) or len(dispatch_commands) != len(PROCESS_IDS):
        errors.append(
            f"Fala dispatch_commands must be a list of exactly {len(PROCESS_IDS)} child argv lists"
        )
        dispatch_commands = dispatch_commands if isinstance(dispatch_commands, list) else []
    python = project / ".venv" / "bin" / "python"
    if not _regular_file(python) or not os.access(python, os.X_OK):
        errors.append("Fala candidate python interpreter must be a regular non-symlink executable")
    db_path = str(identity.get("db_path") or "")
    pythonpath = str(project / "src")
    expected_env_keys = set(PROCESS_ENV_KEYS)
    expected_process_state_root = str(Path(db_path).expanduser().resolve().parent / "process-state") if db_path else ""
    supervisor_args = program_arguments[0] if program_arguments else None
    runtime = runtime_entries[0] if runtime_entries else None
    if not isinstance(runtime, dict) or set(runtime) != set(RUNTIME_IDENTITY_KEYS):
        errors.append("Fala runtime_identity key set is invalid for supervisor")
        runtime = runtime if isinstance(runtime, dict) else {}
    if runtime.get("label") != SUPERVISOR_LABEL:
        errors.append("Fala runtime_identity label mismatch for supervisor")
    if runtime.get("program_arguments") != supervisor_args:
        errors.append("Fala runtime ProgramArguments mismatch for supervisor")
    _validate_supervisor_args(
        supervisor_args,
        project=project,
        config=config,
        db_path=db_path,
        mode=mode,
        label="manifest:supervisor",
        errors=errors,
    )
    if runtime.get("working_directory") != str(project):
        errors.append("Fala runtime working directory is not version-local for supervisor")
    for key in ("standard_out_path", "standard_error_path"):
        value = runtime.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute() or "~" in value:
            errors.append(f"Fala runtime {key} is invalid for supervisor")
        elif Path(value).name != f"{SUPERVISOR_LABEL}.{ 'out' if key == 'standard_out_path' else 'err' }.log":
            errors.append(f"Fala runtime {key} basename is invalid for supervisor")
    env = runtime.get("environment_variables")
    if not isinstance(env, dict) or set(env) != expected_env_keys:
        errors.append(f"Fala runtime environment_variables must be exactly {sorted(expected_env_keys)} for supervisor")
    elif any(not isinstance(env.get(key), str) for key in expected_env_keys):
        errors.append("Fala runtime environment variable values must be strings for supervisor")
    elif any(not Path(env[key]).is_absolute() for key in PROCESS_PATH_ENV_KEYS):
        errors.append("Fala runtime environment variable paths must be absolute for supervisor")
    elif env.get("PYTHONPATH") != pythonpath:
        errors.append("Fala PYTHONPATH is not candidate-local for supervisor")
    elif env.get("FALA_CANDIDATE_ID") != candidate_id or env.get("HERMES_LOKAY_GENERATION") != candidate_id:
        errors.append(
            "Fala runtime FALA_CANDIDATE_ID/HERMES_LOKAY_GENERATION must equal 64-hex candidate_id for supervisor"
        )
    elif expected_process_state_root and env.get("HERMES_LOKAY_PROCESS_STATE_ROOT") != expected_process_state_root:
        errors.append(
            "Fala runtime HERMES_LOKAY_PROCESS_STATE_ROOT must be <db_parent>/process-state for supervisor"
        )
    else:
        try:
            if Path(env["FALA_HOME"]).resolve() != (project / "Fala").resolve():
                errors.append("Fala runtime source path is not version-local for supervisor")
            if Path(env["FALA_EFFECTOR_ROOT"]).resolve() != (project / "effectors").resolve():
                errors.append("Fala effector root is not candidate-local for supervisor")
        except OSError as exc:
            errors.append(f"Fala runtime environment paths are unreadable for supervisor: {exc}")
    if (
        runtime.get("start_interval") is not None
        or runtime.get("run_at_load") is not True
        or runtime.get("keep_alive") is not True
        or runtime.get("process_type") != "Background"
        or runtime.get("limit_load_to_session_type") not in (None, "Background")
    ):
        errors.append("Fala runtime schedule/process/session contract is invalid for supervisor")
    if supervisor_path is None or not _regular_file(supervisor_path):
        errors.append("missing or non-regular Fala supervisor plist")
    else:
        if runtime.get("plist_sha256") != sha256(supervisor_path):
            errors.append("Fala runtime plist hash mismatch for supervisor")
        try:
            document = plistlib.loads(supervisor_path.read_bytes())
            if not isinstance(document, dict) or document.get("Label") != SUPERVISOR_LABEL:
                errors.append("Fala plist Label is invalid for supervisor")
            arguments = document.get("ProgramArguments") if isinstance(document, dict) else None
            if arguments != supervisor_args:
                errors.append("Fala plist ProgramArguments mismatch for supervisor")
            if document.get("WorkingDirectory") != runtime.get("working_directory"):
                errors.append("Fala plist WorkingDirectory mismatch for supervisor")
            if (
                "StartInterval" in document
                or document.get("ProcessType") != "Background"
                or document.get("RunAtLoad") is not True
                or document.get("KeepAlive") is not True
            ):
                errors.append("Fala plist schedule/process contract is invalid for supervisor")
            if document.get("LimitLoadToSessionType") not in (None, "Background"):
                errors.append("Fala plist session contract is invalid for supervisor")
            env = document.get("EnvironmentVariables")
            if not isinstance(env, dict) or not isinstance(env.get("HOME"), str) or not Path(env["HOME"]).is_absolute():
                errors.append("Fala plist HOME is invalid for supervisor")
            if env != runtime.get("environment_variables"):
                errors.append("Fala plist EnvironmentVariables mismatch for supervisor")
            for key, runtime_key in (("StandardOutPath", "standard_out_path"), ("StandardErrorPath", "standard_error_path")):
                if document.get(key) != runtime.get(runtime_key):
                    errors.append(f"Fala plist {key} mismatch for supervisor")
            if AGGREGATE_FALA_MODULE in (arguments or []) or document.get("Label") == AGGREGATE_FALA_LABEL:
                errors.append("aggregate production launchd artifact is forbidden for supervisor")
            if PROCESS_MODULE in (arguments or []):
                errors.append("per-process production launchd artifact is forbidden for supervisor")
        except (OSError, plistlib.InvalidFileException, ValueError) as exc:
            errors.append(f"invalid Fala candidate plist for supervisor: {exc}")

    if process_rows and len(process_rows) == len(PROCESS_IDS):
        expected_dispatch = _expected_dispatch_commands(
            project=project,
            config=config,
            db_path=db_path,
            mode=mode,
            process_rows=process_rows,
        )
        if dispatch_commands != expected_dispatch:
            errors.append("Fala dispatch_commands do not match ordered PROCESS_IDS child argv contract")
        for index, (process_id, args) in enumerate(zip(PROCESS_IDS, dispatch_commands)):
            row = process_rows[index] if index < len(process_rows) else {}
            command = str(row.get("command") or f"lokay-process-{process_id}")
            _validate_child_args(
                args,
                project=project,
                config=config,
                db_path=db_path,
                mode=mode,
                command=command,
                label=f"dispatch:{process_id}",
                errors=errors,
            )
            if isinstance(args, list) and args and args[0] != str(python):
                errors.append(f"Fala dispatch command python is not candidate-local for {process_id}")
    elif dispatch_commands:
        errors.append("Fala dispatch_commands require a complete PROCESS_IDS process inventory")

    metadata_path = paths["metadata_path"]
    lock_path = paths["lock_path"]
    config_artifact_path = paths["config_artifact_path"]
    revision_path = paths["revision_path"]
    if config_artifact_path and _regular_file(config_artifact_path) and identity.get("config_hash") != sha256(config_artifact_path):
        errors.append("Fala config hash does not match candidate config bytes")
    policy = identity.get("policy")
    if not isinstance(policy, dict) or set(policy) != POLICY_KEYS:
        errors.append("Fala identity policy key set is invalid")
        policy = policy if isinstance(policy, dict) else {}
    else:
        for key in POLICY_KEYS:
            if not isinstance(policy.get(key), bool):
                errors.append(f"Fala identity policy {key} must be a bool")
        if not is_promotion_policy(policy):
            errors.append("Fala identity policy is unsafe for promotion")
    if config_artifact_path and _regular_file(config_artifact_path) and isinstance(policy, dict) and set(policy) == POLICY_KEYS:
        try:
            try:
                import tomllib
            except ModuleNotFoundError:
                import tomli as tomllib

            embedded = tomllib.loads(config_artifact_path.read_text(encoding="utf-8"))
            automation = embedded.get("automation") or {}
            executor = embedded.get("executor") or {}
            if not isinstance(automation, dict) or not isinstance(executor, dict):
                raise ValueError("policy table is invalid")
            expected_policy = {
                "automerge": embedded.get("automerge", automation.get("automerge", False)),
                "require_human_approval": embedded.get("require_human_approval", automation.get("require_human_approval", True)),
                "require_checks": embedded.get("require_checks", automation.get("require_checks", True)),
                "require_test_evidence": embedded.get("require_test_evidence", automation.get("require_test_evidence", True)),
                "executor_enabled": executor.get("enabled", False),
            }
            if policy != expected_policy:
                errors.append("Fala identity policy does not match embedded config")
        except (OSError, UnicodeDecodeError, ValueError, tomllib.TOMLDecodeError) as exc:
            errors.append(f"Fala embedded config policy is unreadable: {exc}")
    metadata: dict[str, object] | None = None
    if metadata_path and _regular_file(metadata_path):
        try:
            loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata = loaded if isinstance(loaded, dict) else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            metadata = None
        expected_lock_hash = sha256(lock_path) if lock_path and _regular_file(lock_path) else None
        if metadata is None or set(metadata) != {"plugin_commit", "fala_tag", "fala_commit", "lock_hash"} or metadata.get("plugin_commit") != identity.get("plugin_commit") or metadata.get("fala_tag") != FALA_TAG or metadata.get("fala_commit") != FALA_PINNED_COMMIT or metadata.get("lock_hash") != expected_lock_hash:
            errors.append("Fala metadata provenance is invalid")
    if revision_path and _regular_file(revision_path):
        try:
            revision = revision_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            revision = ""
        if revision != str(identity.get("plugin_commit") or ""):
            errors.append("Fala revision artifact does not match plugin commit")
    if identity.get("fala_tag") != FALA_TAG or identity.get("fala_commit") != FALA_PINNED_COMMIT:
        errors.append("Fala runtime provenance is missing or invalid")

    fala_root = project / "Fala"
    if (project / ".git").exists() or (fala_root / ".git").exists():
        errors.append("copied Fala/plugin .git metadata is forbidden")
    fala_pyproject = fala_root / "pyproject.toml"
    fala_src = fala_root / "python" / "fala"
    if not _regular_file(fala_pyproject) or not fala_src.is_dir() or fala_src.is_symlink():
        errors.append("bundled Fala source tree is incomplete")
    else:
        try:
            pyproject = (project / "pyproject.toml").read_text(encoding="utf-8")
            bundled_revision = fala_root / "revision.txt"
            if not _regular_file(bundled_revision):
                errors.append("bundled Fala revision marker is missing")
            elif bundled_revision.read_text(encoding="utf-8").strip() != FALA_PINNED_COMMIT:
                errors.append("bundled Fala revision marker is not pinned")
            fala_project = fala_pyproject.read_text(encoding="utf-8")
            lock_text = lock_path.read_text(encoding="utf-8") if lock_path and _regular_file(lock_path) else ""
        except (OSError, UnicodeDecodeError):
            pyproject = fala_project = lock_text = ""
        if 'name = "fala"' not in fala_project or f'version = "{FALA_TAG}"' not in fala_project:
            errors.append(f"bundled Fala metadata is not pinned to {FALA_TAG}")
        expected_git = f'fala = {{ git = "{FALA_GIT_URL}", tag = "v{FALA_TAG}" }}'
        if expected_git in pyproject or "../Fala" in pyproject:
            errors.append("bundled pyproject still references git/local Fala source")
        if 'fala = { path = "Fala", editable = true }' not in pyproject:
            errors.append("bundled Fala dependency path is invalid")
        if f'git = "{FALA_GIT_URL}' in lock_text or "../Fala" in lock_text:
            errors.append("bundled Fala lock still references git/local path")
        if 'editable = "Fala"' not in lock_text:
            errors.append("bundled Fala lock provenance is invalid")
    for required_relative in ("fala-package.toml", "src/lokay/effector.py"):
        required_file = project / required_relative
        if not _regular_file(required_file):
            errors.append(f"required Fala package artifact is missing: {required_relative}")

    result: dict[str, object] = {
        "ok": not errors,
        "candidate": str(candidate),
        "candidate_id": candidate_id,
        "manifest": str(manifest_path),
        "plists": [supervisor_relative],
        "supervisor_plist": supervisor_relative,
        "dispatch_commands": len(dispatch_commands) if isinstance(dispatch_commands, list) else 0,
        "metadata": str(metadata_path) if metadata_path else "",
        "errors": errors,
    }
    if errors:
        raise DeploymentParityError(result)
    return result

class DeploymentParityError(RuntimeError):
    def __init__(self, result: dict[str, object]):
        self.result = result
        super().__init__("deployment parity validation failed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--active-root", type=Path, required=True)
    parser.add_argument("--template-root", type=Path, action="append", required=True)
    parser.add_argument("--manifest", type=Path, help="write verified hashes as JSON")
    # Optional roots accepted by lokay_health.sh for forward compatibility.
    parser.add_argument("--active-plist-root", type=Path, default=None)
    parser.add_argument("--render-root", type=Path, default=None)
    parser.add_argument("--active-config-root", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        result = validate(
            args.source_root,
            args.active_root,
            args.template_root,
            active_plist_roots=[args.active_plist_root] if args.active_plist_root else None,
            render_roots=[args.render_root] if args.render_root else None,
            active_config_roots=[args.active_config_root] if args.active_config_root else None,
        )
    except DeploymentParityError as exc:
        print(json.dumps(exc.result, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        print(f"deployment parity validation failed: {exc}", file=sys.stderr)
        return 1

    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
