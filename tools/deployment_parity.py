#!/usr/bin/env python3
"""Verify that launchd points at the deployed bytes from this checkout."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import sys
from pathlib import Path
from typing import Iterable


# Every shell entrypoint copied to ~/.hermes/scripts is part of the deployment
# contract. Keeping this list explicit makes a missing deployment fail closed.
DEPLOYED_SCRIPTS = (
    "repo_issue_intake.sh",
    "repo_issue_to_pr_dispatch.sh",
    "repo_pr_triage.sh",
    "repo_agent_health.sh",
    "repo_agent_cleanup.sh",
    "repo_agent_status.sh",
    "repo_agent_hermes_update.sh",
    "repo_agent_repos.sh",
    "repo_agent_backfill.sh",
    "repo_agent_webhook.sh",
    "cron_repo_issue_to_pr_dispatch.sh",
    "cron_repo_pr_triage.sh",
    "repo_agent_smoke.sh",
)
TEMPLATE_ENTRYPOINTS = {
    "oss-repo-agent-cleanup.plist.template": "repo_agent_cleanup.sh",
    "oss-repo-agent-dispatch.plist.template": "repo_issue_to_pr_dispatch.sh",
    "oss-repo-agent-health.plist.template": "repo_agent_health.sh",
    "oss-repo-agent-hermes-update.plist.template": "repo_agent_hermes_update.sh",
    "oss-repo-agent-pr-triage.plist.template": "repo_pr_triage.sh",
}



def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _template_files(roots: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if not root.is_dir():
            raise ValueError(f"required launchd template directory missing: {root}")
        files.extend(sorted(root.rglob("*.plist.template")))
    if not files:
        raise ValueError("no launchd templates found")
    return files


def validate(source_root: Path, active_root: Path, template_roots: Iterable[Path]) -> dict[str, object]:
    errors: list[str] = []
    source_root = source_root.expanduser().resolve()
    if (source_root / "scripts").is_dir():
        source_root = source_root / "scripts"
    active_root = active_root.expanduser().resolve()
    hashes: dict[str, str] = {}

    for name in DEPLOYED_SCRIPTS:
        source = source_root / name
        active = active_root / name
        if not source.is_file():
            errors.append(f"missing source script: {source}")
            continue
        if not active.is_file():
            errors.append(f"missing active script: {active}")
            continue
        source_hash = sha256(source)
        active_hash = sha256(active)
        hashes[name] = source_hash
        if source_hash != active_hash:
            errors.append(
                f"deployment hash mismatch: {name} source={source_hash} active={active_hash}"
            )

    home = Path(os.environ.get("HOME", str(Path.home()))).expanduser().resolve()
    for template in _template_files(template_roots):
        try:
            raw = template.read_text(encoding="utf-8")
            # A template's HOME marker is intentionally resolved relative to the
            # active root supplied to this check, so tests can use a temp deploy.
            active_home = active_root.parent.parent if active_root.name == "scripts" else home
            rendered = raw.replace("{{HOME}}", str(active_home))
            document = plistlib.loads(rendered.encode("utf-8"))
        except (OSError, plistlib.InvalidFileException, UnicodeDecodeError) as exc:
            errors.append(f"invalid launchd template {template}: {exc}")
            continue

        arguments = document.get("ProgramArguments")
        if not isinstance(arguments, list) or not arguments:
            errors.append(f"launchd ProgramArguments missing: {template}")
            continue
        executable = str(arguments[0])
        if not executable.endswith(".sh"):
            # The config-driven intake template intentionally invokes Hermes,
            # not a deployed shell entrypoint.
            continue
        name = Path(executable).name
        expected_name = TEMPLATE_ENTRYPOINTS.get(template.name)
        expected = active_root / (expected_name or name)
        if expected_name and name != expected_name:
            errors.append(
                f"launchd entrypoint mismatch: {template} points to {name}; "
                f"expected {expected_name}"
            )
        elif name not in DEPLOYED_SCRIPTS:
            errors.append(f"launchd references undeployed script {name}: {template}")
        elif Path(executable).expanduser().resolve() != expected:
            errors.append(
                f"launchd ProgramArguments path mismatch: {template} points to {executable}; "
                f"expected {expected}"
            )

    result: dict[str, object] = {
        "ok": not errors,
        "source_root": str(source_root),
        "active_root": str(active_root),
        "scripts": hashes,
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
    args = parser.parse_args(argv)
    try:
        result = validate(args.source_root, args.active_root, args.template_root)
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
