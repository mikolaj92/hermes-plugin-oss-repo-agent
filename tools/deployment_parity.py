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


FALA_PINNED_COMMIT = "9f10d58462b4e134d5b1cffe8ff9172909df70ea"
# Every shell entrypoint copied to ~/.hermes/scripts is part of the deployment
# contract. Keeping this list explicit makes a missing deployment fail closed.
DEPLOYED_SCRIPTS = (
    "repo_agent_health.sh",
    "repo_agent_status.sh",
    "repo_agent_hermes_update.sh",
    "repo_agent_repos.sh",
    "repo_agent_smoke.sh",
)
TEMPLATE_ENTRYPOINTS = {
    "oss-repo-agent-health.plist.template": "repo_agent_health.sh",
    "oss-repo-agent-hermes-update.plist.template": "repo_agent_hermes_update.sh",
}



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
    """Resolve a deployment root without allowing a symlinked root."""
    path = path.expanduser()
    if path.is_symlink():
        errors.append(f"{label} root must not be a symlink: {path}")
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
    allowed = {"config.toml", "config.yaml", "config.yml", "config.json"}
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
    replacements = {
        "{{HOME}}": str(active_home),
        "{{ACTIVE_SCRIPTS}}": str(active_root),
        "{{UV_BIN}}": "/usr/bin/uv",
        "{{PROJECT_ROOT}}": str(active_home / ".hermes" / "project"),
        "{{CONFIG_PATH}}": str(active_home / ".hermes" / "config.toml"),
        "{{DB_PATH}}": str(active_home / ".hermes" / "fala.sqlite"),
        "{{MODE_ARG}}": "--dry-run",
        "{{INTAKE_LIMIT}}": "10",
        "{{LOG_DIR}}": str(active_home / ".hermes" / "logs"),
        "<config-path>": str(active_home / ".hermes" / "config.toml"),
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
    expected_names = set(TEMPLATE_ENTRYPOINTS) | {"oss-repo-agent-fala-tick-all.plist.template"}
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
        elif template.name != "oss-repo-agent-fala-tick-all.plist.template":
            errors.append(f"launchd executable is not a deployed script: {template}")
        if template.name == "oss-repo-agent-fala-tick-all.plist.template":
            if label != "com.mikolaj92.hermes.repo-agent-fala-tick-all":
                errors.append(f"Fala launchd Label mismatch: {template}")
            if document.get("StartInterval") != 600 or document.get("ProcessType") != "Background" or document.get("RunAtLoad") is not False:
                errors.append(f"Fala launchd schedule/process contract invalid: {template}")
            if document.get("LimitLoadToSessionType") not in (None, "Background"):
                errors.append(f"Fala launchd session contract invalid: {template}")
            env = document.get("EnvironmentVariables")
            if not isinstance(env, dict) or not isinstance(env.get("HOME"), str):
                errors.append(f"Fala launchd HOME is missing: {template}")
            for key in ("StandardOutPath", "StandardErrorPath"):
                if not isinstance(document.get(key), str) or not Path(document[key]).is_absolute():
                    errors.append(f"Fala launchd {key} is invalid: {template}")
            mode_flags = [value for value in arguments if value in ("--dry-run", "--live")]
            if len(mode_flags) != 1:
                errors.append(f"Fala launchd mode flags are not exactly once: {template}")
            else:
                _validate_args(arguments, project=active_root.parent / "project", config=active_home / ".hermes" / "config.toml", db_path=str(active_home / ".hermes" / "fala.sqlite"), mode=mode_flags[0][2:], label="launchd template", errors=errors)
            project_index = arguments.index("--project") if "--project" in arguments else -1
            if project_index < 0 or project_index + 1 >= len(arguments):
                errors.append(f"Fala launchd project path is missing: {template}")
            else:
                project_path = Path(arguments[project_index + 1]).expanduser()
                if not project_path.is_absolute():
                    errors.append(f"Fala launchd project path is not absolute: {template}")
                if "candidates" in project_path.parts:
                    errors.append(f"Fala launchd project path points at mutable candidates: {template}")
                if document.get("WorkingDirectory") != arguments[project_index + 1]:
                    errors.append(f"Fala launchd WorkingDirectory is not project-local: {template}")

    errors.extend(f"missing launchd template: {name}" for name in sorted(expected_names - seen_names))
    plist_roots = list(active_plist_roots or [])
    rendered_roots = list(render_roots or [])
    config_roots = list(active_config_roots or [])
    if active_plist_roots is not None:
        if not plist_roots or any(root is None for root in plist_roots):
            errors.append("active plist parity root is omitted")
        else:
            _validate_rendered_roots(templates, plist_roots, label="active", contracts=contracts, errors=errors)
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


FALA_TAG = "0.7.6"




def _validate_args(
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
    expected = [args[0], "run", "--frozen", "--project", str(project), "repo-agent-tick-all", "--config", str(config), "--db", db_path, f"--{mode}", "--json"]
    if len(args) != len(expected) or args != expected:
        errors.append(f"Fala {label} ProgramArguments do not match canonical contract")
    if args.count("--project") != 1 or args.count("--config") != 1 or args.count("--db") != 1:
        errors.append(f"Fala {label} ProgramArguments flags are not exactly once")
    if not Path(args[0]).is_absolute():
        errors.append(f"Fala {label} uv executable must be absolute")
    return args


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
        root = deployment_root.expanduser().resolve()
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
    }
    manifest_required = stable_keys | {"candidate_id", "identity", "created_at", "program_arguments", "artifacts", "runtime_identity"}
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
    paths: dict[str, Path | None] = {}
    for key in ("metadata_path", "lock_path", "config_artifact_path", "revision_path"):
        paths[key] = _relative_candidate_path(candidate, identity.get(key), key, errors)
    plist_relative = "launchd/com.mikolaj92.hermes.repo-agent-fala-tick-all.plist"
    plist_path = _relative_candidate_path(candidate, plist_relative, "plist path", errors)
    project = candidate / "source" / "project"
    config = candidate / "source" / "config.toml"

    # Every path must remain contained, every directory immutable, and every file
    # a private regular file (an external hardlink is not immutable provenance).
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
    plist_relative = "launchd/com.mikolaj92.hermes.repo-agent-fala-tick-all.plist"
    required_paths = {plist_relative, *(str(identity.get(key) or "") for key in ("metadata_path", "lock_path", "config_artifact_path", "revision_path"))}
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

    runtime = manifest.get("runtime_identity")
    runtime_keys = {"program_arguments", "working_directory", "standard_out_path", "standard_error_path", "environment_variables", "start_interval", "run_at_load", "process_type", "limit_load_to_session_type", "plist_sha256"}
    if not isinstance(runtime, dict) or set(runtime) != runtime_keys:
        errors.append("Fala runtime_identity key set is invalid")
        runtime = runtime if isinstance(runtime, dict) else {}
    runtime_args = manifest.get("program_arguments")
    if not isinstance(runtime_args, list) or runtime.get("program_arguments") != runtime_args:
        errors.append("Fala manifest runtime ProgramArguments mismatch")
    _validate_args(runtime_args, project=project, config=config, db_path=str(identity.get("db_path") or ""), mode=mode, label="manifest", errors=errors)
    if runtime.get("working_directory") != str(project):
        errors.append("Fala runtime working directory is not version-local")
    for key in ("standard_out_path", "standard_error_path"):
        value = runtime.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute() or "~" in value:
            errors.append(f"Fala runtime {key} is invalid")
    env = runtime.get("environment_variables")
    expected_env_keys = {"HOME"} if candidate.parent.name == "candidates" else {"HOME", "UV_PROJECT_ENVIRONMENT", "UV_CACHE_DIR", "FALA_HOME", "PATH"}
    path_keys = expected_env_keys - {"PATH"}
    if not isinstance(env, dict) or set(env) != expected_env_keys or not isinstance(env.get("HOME"), str) or not Path(env["HOME"]).is_absolute():
        errors.append(f"Fala runtime environment_variables must be exactly {sorted(expected_env_keys)}")
    elif any(not isinstance(env[key], str) or not Path(env[key]).is_absolute() for key in path_keys):
        errors.append("Fala runtime environment variable paths must be absolute")
    elif "PATH" in env and (not isinstance(env["PATH"], str) or not all(Path(part).is_absolute() for part in env["PATH"].split(os.pathsep))):
        errors.append("Fala runtime PATH entries must be absolute")
    elif deployment_root is not None and "UV_PROJECT_ENVIRONMENT" in env:
        expected_runtime = (deployment_root.expanduser().resolve() / "runtime" / candidate_id).resolve()
        if Path(env["UV_PROJECT_ENVIRONMENT"]).parent.resolve() != expected_runtime or Path(env["UV_CACHE_DIR"]).parent.resolve() != expected_runtime:
            errors.append("Fala UV runtime paths are not candidate-local")
        if Path(env["FALA_HOME"]).resolve() != (project / "Fala").resolve():
            errors.append("Fala runtime source path is not version-local")
    if runtime.get("start_interval") != 600 or runtime.get("run_at_load") is not False or runtime.get("process_type") != "Background" or runtime.get("limit_load_to_session_type") not in (None, "Background"):
        errors.append("Fala runtime schedule/process/session contract is invalid")
    if plist_path is not None and runtime.get("plist_sha256") != sha256(plist_path):
        errors.append("Fala runtime plist hash mismatch")

    metadata_path = paths["metadata_path"]
    lock_path = paths["lock_path"]
    config_artifact_path = paths["config_artifact_path"]
    revision_path = paths["revision_path"]
    if config_artifact_path and _regular_file(config_artifact_path) and identity.get("config_hash") != sha256(config_artifact_path):
        errors.append("Fala config hash does not match candidate config bytes")
    policy = identity.get("policy")
    policy_keys = {
        "automerge",
        "require_human_approval",
        "require_checks",
        "require_test_evidence",
        "executor_enabled",
    }
    if not isinstance(policy, dict) or set(policy) != policy_keys:
        errors.append("Fala identity policy key set is invalid")
        policy = policy if isinstance(policy, dict) else {}
    else:
        for key in policy_keys:
            if not isinstance(policy.get(key), bool):
                errors.append(f"Fala identity policy {key} must be a bool")
        if (
            policy.get("automerge") is not False
            or policy.get("require_human_approval") is not True
            or policy.get("require_checks") is not True
            or policy.get("require_test_evidence") is not True
            or policy.get("executor_enabled") is not False
        ):
            errors.append("Fala identity policy is unsafe for promotion")
    if config_artifact_path and _regular_file(config_artifact_path) and isinstance(policy, dict) and set(policy) == policy_keys:
        try:
            try:
                import tomllib
            except ModuleNotFoundError:
                import tomli as tomllib

            embedded = tomllib.loads(config_artifact_path.read_text(encoding="utf-8"))
            automation = embedded.get("automation") or {}
            executor = embedded.get("executor") or {}
            expected_policy = {
                "automerge": bool(automation.get("automerge", False)),
                "require_human_approval": bool(automation.get("require_human_approval", True)),
                "require_checks": bool(automation.get("require_checks", True)),
                "require_test_evidence": bool(automation.get("require_test_evidence", True)),
                "executor_enabled": bool(executor.get("enabled", False)),
            }
            if policy != expected_policy:
                errors.append("Fala identity policy does not match embedded config")
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
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
        if 'name = "fala"' not in fala_project or 'version = "0.7.6"' not in fala_project:
            errors.append("bundled Fala metadata is not pinned to 0.7.6")
        if 'fala = { path = "Fala", editable = true }' not in pyproject or "../Fala" in pyproject or 'editable = "Fala"' not in lock_text or "../Fala" in lock_text:
            errors.append("bundled Fala dependency path or lock provenance is invalid")
    for required_relative in ("fala-package.toml", "src/repo_agent/effector.py"):
        required_file = project / required_relative
        if not _regular_file(required_file):
            errors.append(f"required Fala package artifact is missing: {required_relative}")

    try:
        document = plistlib.loads(plist_path.read_bytes() if plist_path else b"")
        if not isinstance(document, dict) or document.get("Label") != "com.mikolaj92.hermes.repo-agent-fala-tick-all":
            errors.append("Fala plist Label is invalid")
        arguments = document.get("ProgramArguments") if isinstance(document, dict) else None
        if arguments != runtime_args:
            errors.append("Fala plist ProgramArguments mismatch")
        if document.get("WorkingDirectory") != runtime.get("working_directory"):
            errors.append("Fala plist WorkingDirectory mismatch")
        if document.get("StartInterval") != 600 or document.get("ProcessType") != "Background" or document.get("RunAtLoad") is not False:
            errors.append("Fala plist schedule/process contract is invalid")
        if document.get("LimitLoadToSessionType") not in (None, "Background"):
            errors.append("Fala plist session contract is invalid")
        env = document.get("EnvironmentVariables")
        if not isinstance(env, dict) or not isinstance(env.get("HOME"), str) or not Path(env["HOME"]).is_absolute():
            errors.append("Fala plist HOME is invalid")
        if env != runtime.get("environment_variables"):
            errors.append("Fala plist EnvironmentVariables mismatch")
        for key, runtime_key in (("StandardOutPath", "standard_out_path"), ("StandardErrorPath", "standard_error_path")):
            if document.get(key) != runtime.get(runtime_key):
                errors.append(f"Fala plist {key} mismatch")
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        errors.append(f"invalid Fala candidate plist: {exc}")
    result: dict[str, object] = {"ok": not errors, "candidate": str(candidate), "candidate_id": candidate_id, "manifest": str(manifest_path), "plist": str(plist_path) if plist_path else "", "metadata": str(metadata_path) if metadata_path else "", "errors": errors}
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
    # Optional roots accepted by repo_agent_health.sh for forward compatibility.
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
