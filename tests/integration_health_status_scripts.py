from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import time
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import copy

from lokay.registry import canonical_toml, load_registry



ROOT = Path(__file__).resolve().parents[1]


def _copy_fixture_tree(source: Path, destination: Path) -> None:
    """Copy a candidate tree without materializing its large immutable files."""
    if destination.exists() or destination.is_symlink():
        raise AssertionError(f"fixture destination already exists: {destination}")
    if sys.platform == "darwin":
        subprocess.run(["cp", "-cR", str(source), str(destination)], check=True, capture_output=True, text=True)
        return
    shutil.copytree(source, destination)

def load_plugin():
    parent = sys.modules.get("hermes_plugins")
    if parent is None:
        parent = types.ModuleType("hermes_plugins")
        parent.__path__ = []
        sys.modules["hermes_plugins"] = parent
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.lokay",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["hermes_plugins.lokay"] = module
    spec.loader.exec_module(module)
    return module


class HealthStatusScriptTests(unittest.TestCase):
    @classmethod
    def _strict_config_data(cls, *, mode: str = "dry-run", autonomous: bool = True) -> dict:
        data = copy.deepcopy(load_registry(ROOT / "config.toml").data)
        data["mode"] = mode
        data["automation"]["automerge"] = autonomous
        data["automation"]["require_human_approval"] = not autonomous
        data["automation"]["require_checks"] = True
        data["automation"]["require_test_evidence"] = True
        data["executor"]["enabled"] = autonomous
        return data

    @classmethod
    def _write_strict_config(cls, path: Path, *, mode: str = "dry-run", autonomous: bool = True) -> dict:
        data = cls._strict_config_data(mode=mode, autonomous=autonomous)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_toml(data))
        return data

    @classmethod
    def _install_registry_runtime(cls, deployment: Path, *, candidate_id: str | None = None) -> Path:
        """Install a complete candidate-local current version for shell bridge tests."""
        candidate_id = cls.candidate.name if candidate_id is None else candidate_id
        if candidate_id != cls.candidate.name:
            raise ValueError("fixture candidate id must match candidate manifest identity")
        version = deployment / "versions" / candidate_id
        version.parent.mkdir(parents=True, exist_ok=True)
        if version.is_symlink():
            version.unlink()
        elif version.exists():
            shutil.rmtree(version)
        _copy_fixture_tree(cls.candidate, version)
        cls.commands._promote_version_runtime(version, deployment, candidate_id)
        current = deployment / "current"
        if current.is_symlink():
            current.unlink()
        elif current.exists():
            shutil.rmtree(current)
        current.symlink_to(version, target_is_directory=True)
        return version

    @classmethod
    def setUpClass(cls):
        cls.module = load_plugin()
        cls.commands = cls.module.commands
        cls.holder = tempfile.TemporaryDirectory()
        cls.root = Path(cls.holder.name)
        cls.config = cls.root / "config.toml"
        data = cls._write_strict_config(cls.config, mode="dry-run", autonomous=True)
        cls.cfg = cls.commands.LokayConfig.from_mapping(data)
        cls.base_db = cls.root / "base.sqlite"
        cls._write_db(cls.base_db, mode="dry-run")
        lock_data = cls.commands.rewrite_fala_git_to_bundled_lock((ROOT / "uv.lock").read_bytes())
        processes = cls.commands._catalog_processes(cls.cfg, cls.config)
        cls.processes = processes
        config_bytes = cls.commands._materialize_candidate_config(cls.config, processes)
        identity = {
            "schema": 1,
            "mode": "dry-run",
            "plugin_commit": "plugin-commit",
            "fala_tag": "0.7.15",
            "fala_commit": "b5f9a6d500a442a1c79060a862fe4b9da87bc98f",
            "lock_hash": hashlib.sha256(lock_data).hexdigest(),
            "config_path": str(cls.config.absolute()),
            "config_hash": hashlib.sha256(config_bytes).hexdigest(),
            "db_path": str((cls.root / "state.sqlite").absolute()),
            "metadata_path": "source/metadata.json",
            "lock_path": "source/project/uv.lock",
            "config_artifact_path": "source/config.toml",
            "revision_path": "source/revision.txt",
            "policy": {
                "automerge": bool(cls.cfg.automerge),
                "require_human_approval": bool(cls.cfg.require_human_approval),
                "require_checks": bool(cls.cfg.require_checks),
                "require_test_evidence": bool(cls.cfg.require_test_evidence),
                "executor_enabled": bool(cls.cfg.executor.enabled),
            },
            "repos": cls.commands._identity_repos(cls.cfg, cls.config),
            "processes": processes,
        }
        candidate_id = hashlib.sha256((json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()
        candidate = cls.root / "deployment" / "candidates" / candidate_id
        project_root = ROOT.resolve()
        fala_root = (ROOT.parent / "Fala").resolve()
        real_run = cls.commands.subprocess.run
        real_which = cls.commands.shutil.which

        def fake_which(command, **kwargs):
            return real_which(command, **kwargs)

        def fake_git(argv, *args, **kwargs):
            command = list(argv)
            if len(command) >= 3 and command[:2] == ["git", "-C"]:
                checkout = Path(command[2]).resolve()
                if command[3:] == ["status", "--porcelain"] and checkout in {project_root, fala_root}:
                    return subprocess.CompletedProcess(command, 0, "", "")
                if checkout == fala_root and command[3:5] == ["rev-parse", "HEAD"]:
                    return subprocess.CompletedProcess(command, 0, identity["fala_commit"] + "\n", "")
            return real_run(argv, *args, **kwargs)

        with patch.object(cls.commands.subprocess, "run", side_effect=fake_git), patch.object(
            cls.commands, "_read_git_revision", return_value=identity["plugin_commit"]
        ), patch.object(cls.commands.shutil, "which", side_effect=fake_which):
            result = cls.commands.render_launchd(
                cls.cfg,
                str(candidate),
                config_path=str(cls.config),
                fala_db=str(cls.root / "state.sqlite"),
                mode="dry-run",
                deployment_root=str(cls.root / "deployment"),
            )
        assert result["ok"]
        cls.candidate = candidate
        cls.addClassCleanup(cls.holder.cleanup)

    SUPERVISOR_LABEL = "com.mikolaj92.lokay.supervisor"
    HERMES_UPDATE_LABEL = "com.mikolaj92.lokay.hermes-update"

    @classmethod
    def _default_loaded_labels(cls) -> list[str]:
        return [cls.SUPERVISOR_LABEL, cls.HERMES_UPDATE_LABEL]


    @staticmethod
    def _write_db(path: Path, *, mode: str, historical: bool = False) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with sqlite3.connect(path) as db:
            db.executescript(
                """
                CREATE TABLE schema_migrations (id TEXT PRIMARY KEY, version INTEGER NOT NULL, name TEXT NOT NULL, applied_at TEXT NOT NULL);
                INSERT INTO schema_migrations VALUES ('v6', 6, 'latest', '2020-01-01T00:00:00Z');
                CREATE TABLE runs (id TEXT PRIMARY KEY, status TEXT NOT NULL, title TEXT, package_id TEXT, package_version TEXT, package_digest TEXT, correlation_path_id TEXT, correlation_path_digest TEXT, runtime_version TEXT, backend_version TEXT, schema_version INTEGER NOT NULL, metadata TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT);
                CREATE TABLE processes (run_id TEXT NOT NULL, id TEXT NOT NULL, process_type TEXT NOT NULL, impulse_id TEXT, status TEXT NOT NULL, priority INTEGER NOT NULL, attempt INTEGER NOT NULL, max_attempts INTEGER NOT NULL, available_at TEXT NOT NULL, lease_owner TEXT, lease_expires_at TEXT, input_json TEXT NOT NULL, output_json TEXT NOT NULL, error_json TEXT NOT NULL, metadata TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, output_schema_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY (run_id, id));
                """
            )
            if historical:
                for status in ("failed", "created", "cancel_requested"):
                    db.execute(
                        "INSERT INTO runs VALUES (?, ?, '', '', '', '', '', '', '', '', 6, ?, ?, ?, NULL, NULL)",
                        (f"old-{status}", status, '{"mode":"dry-run"}', "2000-01-01T00:00:00Z", "2000-01-01T00:00:00Z"),
                    )
            db.execute(
                "INSERT INTO runs VALUES (?, 'completed', '', '', '', '', '', '', '', '', 6, ?, ?, ?, NULL, NULL)",
                ("latest", json.dumps({"mode": mode}), now, now),
            )
            db.commit()

    @classmethod
    def _fake_commands(cls, root: Path) -> Path:
        fake = root / "bin"
        fake.mkdir(parents=True, exist_ok=True)
        (fake / "launchctl").write_text(
            """#!/usr/bin/env bash
if [[ "$1" == print ]]; then
  domain="${2%/*}"
  label="${2##*/}"
  if [[ "$domain" == gui/* && "${FAKE_LAUNCHCTL_GUI_AVAILABLE:-1}" != 1 ]]; then printf 'Domain does not support specified action\\n' >&2; exit 125; fi
  if [[ "$domain" == user/* || "${FAKE_LAUNCHCTL_LOAD_GUI:-0}" == 1 ]]; then case ",${FAKE_LAUNCHCTL_LOADED:-}," in *,"$label",*)
    printf 'state = running\\nruns = 1\\nlast exit code = %s\\n' "${FAKE_LAUNCHCTL_EXIT_CODE:-0}"
    if [[ -n "${FAKE_LAUNCHCTL_PID:-}" ]]; then printf 'pid = %s\\n' "$FAKE_LAUNCHCTL_PID"; fi
    exit 0;;
  esac; fi
  printf 'could not find service\\n' >&2; exit 1
fi
exit 0
""",
            encoding="utf-8",
        )
        (fake / "gh").write_text(
            """#!/usr/bin/env bash
case "$1" in
  auth) exit 0;;
  api) printf 'offline-user\n'; exit 0;;
  pr|issue) printf '0\n'; exit 0;;
esac
exit 0
""",
            encoding="utf-8",
        )
        (fake / "hermes").write_text(
            """#!/usr/bin/env bash
if [[ "$1" == --version ]]; then printf 'hermes offline\n'; exit 0; fi
if [[ "$1" == kanban ]]; then printf 'ready=0\n'; exit 0; fi
exit 0
""",
            encoding="utf-8",
        )
        for command in (fake / "launchctl", fake / "gh", fake / "hermes"):
            command.chmod(0o755)
        return fake

    @staticmethod
    def _start_identity(pid: int) -> str:
        try:
            boot = str(int(os.stat("/").st_ctime_ns))
        except OSError:
            boot = "0"
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            check=False,
            capture_output=True,
            text=True,
        )
        started = (result.stdout or "").strip()
        if result.returncode == 0 and started:
            return f"{pid}:{boot}:ps:{started}"
        return f"{pid}:{boot}:unverified"

    def _status_document(
        self,
        *,
        candidate_id: str | None = None,
        generation: str | None = None,
        config_sha256: str | None = None,
        supervisor_pid: int | None = None,
        supervisor_start_identity: str | None = None,
        loop_timestamp: float | None = None,
        lease_state: str = "owned",
        schema_version: int = 1,
        slot_counts: dict | None = None,
        dispatch_slots: list | None = None,
        **overrides,
    ) -> dict:
        pid = int(os.getpid() if supervisor_pid is None else supervisor_pid)
        candidate = candidate_id or self.candidate.name
        generation_value = generation if generation is not None else candidate
        config_hash = config_sha256
        if config_hash is None:
            config_hash = hashlib.sha256(self.config.read_bytes()).hexdigest()
        identity = supervisor_start_identity or self._start_identity(pid)
        document = {
            "schema_version": schema_version,
            "event": "loop",
            "candidate_id": candidate,
            "generation": generation_value,
            "config_sha256": config_hash,
            "supervisor_pid": pid,
            "supervisor_start_identity": identity,
            "loop_timestamp": float(time.time() if loop_timestamp is None else loop_timestamp),
            "loop_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "loops": 1,
            "dispatches": 0,
            "lease_state": lease_state,
            "lease": {
                "owner_token": "test-owner",
                "owner_pid": pid,
                "start_identity": identity,
                "candidate_id": candidate,
                "generation": generation_value,
                "config_sha256": config_hash,
                "expires_at": time.time() + 90,
                "stale_after": time.time() + 180,
                "last_renewed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            },
            "slot_counts": {} if slot_counts is None else slot_counts,
            "dispatch_slots": [] if dispatch_slots is None else dispatch_slots,
        }
        document.update(overrides)
        return document

    def _write_status(
        self,
        state_root: Path,
        document: dict | None = None,
        *,
        as_symlink: bool = False,
        raw: str | None = None,
    ) -> Path:
        state_root.mkdir(parents=True, exist_ok=True)
        path = state_root / "status.json"
        if path.exists() or path.is_symlink():
            path.unlink()
        if as_symlink:
            target = state_root / "status-target.json"
            payload = raw if raw is not None else json.dumps(document if document is not None else self._status_document())
            target.write_text(payload if payload.endswith("\n") else payload + "\n", encoding="utf-8")
            path.symlink_to(target)
            return path
        if raw is not None:
            path.write_text(raw if raw.endswith("\n") else raw + "\n", encoding="utf-8")
        else:
            payload = document if document is not None else self._status_document()
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return path

    def _run(self, script: str, *, db: Path | None = None, deployment: Path | None = None, extra: dict[str, str] | None = None, args: tuple[str, ...] = (), install_runtime: bool = True, install_status: bool = True) -> subprocess.CompletedProcess[str]:
        root = self.root / ("run-" + script.replace(".sh", ""))
        root.mkdir(parents=True, exist_ok=True)
        fake = self._fake_commands(root)
        home = ((deployment.parent / "home") if deployment is not None else (root / "home"))
        (home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
        deployment_root = deployment or (root / "deployment")
        if install_runtime and not (deployment_root / "current").exists() and not (deployment_root / "current").is_symlink():
            version = self._install_registry_runtime(deployment_root)
            label = self.SUPERVISOR_LABEL
            shutil.copy2(version / "launchd" / f"{label}.plist", home / "Library" / "LaunchAgents" / f"{label}.plist")
        config_path = self.config if self.config.is_file() else (root / "config.toml")
        if not config_path.is_file():
            self._write_strict_config(config_path, mode="dry-run", autonomous=True)
        supervisor_plist = home / "Library" / "LaunchAgents" / f"{self.SUPERVISOR_LABEL}.plist"
        fala_db = db or (root / "missing.sqlite")
        state_root = root / "supervisor-state"
        env = os.environ.copy()
        env.pop("HERMES_LOKAY_REPOS_FILE", None)
        env.pop("HERMES_LOKAY_WORKTREE_ROOT", None)
        env.update(
            {
                "HOME": str(home),
                "PATH": str(fake) + os.pathsep + env.get("PATH", ""),
                "HERMES_LOKAY_CONFIG": str(config_path),
                "HERMES_LOKAY_LOG_DIR": str(root / "logs"),
                "HERMES_LOKAY_HEALTH_LOG": str(root / "logs" / "health.log"),
                "HERMES_LOKAY_DEPLOYMENT_ROOT": str(deployment_root),
                "HERMES_LOKAY_FALA_DB": str(fala_db),
                "HERMES_LOKAY_FALA_PLIST": str(supervisor_plist),
                "HERMES_LOKAY_FALA_REQUIRE_LIVE": "0",
                "HERMES_LOKAY_FALA_MAX_RUN_AGE_SECONDS": "1800",
                "HERMES_LOKAY_SUPERVISOR_STATUS_MAX_AGE_SECONDS": "180",
                "HERMES_LOKAY_SUPERVISOR_STATE_ROOT": str(state_root),
                "HERMES_LOKAY_MIN_FREE_GB": "0",
                "HERMES_LOKAY_PARITY_ENABLED": "0",
                "FAKE_LAUNCHCTL_LOADED": ",".join(self._default_loaded_labels()),
                "FAKE_LAUNCHCTL_PID": str(os.getpid()),
            }
        )
        if install_status and "HERMES_LOKAY_SUPERVISOR_STATE_ROOT" not in (extra or {}):
            candidate_id = self.candidate.name
            current = deployment_root / "current"
            if current.is_symlink() or current.exists():
                try:
                    candidate_id = current.resolve().name
                except OSError:
                    candidate_id = self.candidate.name
            self._write_status(
                state_root,
                self._status_document(
                    candidate_id=candidate_id,
                    generation=candidate_id,
                    config_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
                ),
            )
        if extra:
            env.update(extra)
            env.pop("HERMES_LOKAY_REPOS_FILE", None)
            env.pop("HERMES_LOKAY_WORKTREE_ROOT", None)
        return subprocess.run(["bash", str(ROOT / "scripts" / script), *args], env=env, capture_output=True, text=True, timeout=30)

    def _layout(self, *, db: Path, installed_copy: bool = True) -> Path:
        root = self.root / ("layout-" + db.stem)
        versions = root / "deployment" / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        version = versions / self.candidate.name
        _copy_fixture_tree(self.candidate, version)
        self.commands._promote_version_runtime(version, root / "deployment", self.candidate.name)
        current = root / "deployment" / "current"
        current.symlink_to(version, target_is_directory=True)
        if installed_copy:
            installed = root / "home" / "Library" / "LaunchAgents"
            installed.mkdir(parents=True, exist_ok=True)
            manifest = json.loads((version / "manifest.json").read_text(encoding="utf-8"))
            label = self.SUPERVISOR_LABEL
            shutil.copy2(version / "launchd" / f"{label}.plist", installed / f"{label}.plist")
        return root

    def test_health_rejects_malformed_environment(self):
        completed = self._run("lokay_health.sh", extra={"HERMES_LOKAY_STALE_LOCK_MINUTES": "not-a-number"})
        self.assertEqual(completed.returncode, 2)
        self.assertIn("invalid-env", completed.stderr)

    def test_status_rejects_malformed_environment(self):
        completed = self._run("lokay_status.sh", extra={"HERMES_LOKAY_FALA_REQUIRE_LIVE": "maybe"})
        self.assertEqual(completed.returncode, 2)
        self.assertIn("invalid-env", completed.stderr)

    def test_health_ignores_unsupported_secondary_launchctl_domain(self):
        loaded = ",".join(self._default_loaded_labels())
        completed = self._run("lokay_health.sh", extra={"FAKE_LAUNCHCTL_GUI_AVAILABLE": "0", "FAKE_LAUNCHCTL_LOADED": loaded})
        self.assertNotIn("launchd-query-failed", completed.stdout)
        self.assertNotIn("launchctl-domain-unavailable", completed.stdout)

    def test_status_ignores_unsupported_secondary_launchctl_domain(self):
        loaded = self.SUPERVISOR_LABEL
        completed = self._run("lokay_status.sh", extra={"FAKE_LAUNCHCTL_GUI_AVAILABLE": "0", "FAKE_LAUNCHCTL_LOADED": loaded})
        self.assertNotIn("launchctl-error", completed.stderr)
        self.assertNotIn("launchctl-unavailable", completed.stderr)

    def test_health_rejects_missing_current_before_db_validation(self):
        deployment = self.root / "missing-current-deployment"
        completed = self._run(
            "lokay_health.sh",
            deployment=deployment,
            install_runtime=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        combined = f"{completed.stdout}\n{completed.stderr}"
        self.assertIn("registry-error missing-current", combined)
    def test_health_marks_invalid_current_candidate(self):
        db = self.root / "invalid-current.sqlite"
        self._write_db(db, mode="live")
        layout = self._layout(db=db)
        version_manifest = layout / "deployment" / "versions" / self.candidate.name / "manifest.json"
        version_manifest.chmod(0o644)
        version_manifest.write_text("{}\n", encoding="utf-8")
        completed = self._run("lokay_health.sh", db=db, deployment=layout / "deployment")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("fala-deployment candidate-invalid", completed.stdout)


    def _tamper_runtime_identity(self, layout: Path) -> None:
        manifest_path = layout / "deployment" / "versions" / self.candidate.name / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["runtime_identity"][0]["process_type"] = "Interactive"
        manifest_path.chmod(0o755)
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        manifest_path.chmod(0o444)

    def test_health_rejects_tampered_runtime_identity(self):
        db = self.root / "tampered-health.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        self._tamper_runtime_identity(layout)
        completed = self._run("lokay_health.sh", db=db, deployment=layout / "deployment")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Fala runtime schedule/process/session contract is invalid", completed.stdout)

    def test_status_rejects_tampered_runtime_identity(self):
        db = self.root / "tampered-status.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        self._tamper_runtime_identity(layout)
        completed = self._run("lokay_status.sh", db=db, deployment=layout / "deployment")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Fala runtime schedule/process/session contract is invalid", completed.stdout)

    def test_status_rejects_policy_that_differs_from_embedded_config(self):
        db = self.root / "tampered-status-policy.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        manifest_path = layout / "deployment" / "versions" / self.candidate.name / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["policy"]["require_human_approval"] = True
        manifest["identity"]["policy"]["require_human_approval"] = True
        manifest_path.chmod(0o644)
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        manifest_path.chmod(0o444)
        completed = self._run("lokay_status.sh", db=db, deployment=layout / "deployment")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Fala identity policy is unsafe for promotion", completed.stdout)

    def test_status_rejects_safe_manifest_policy_with_unsafe_valid_toml(self):
        db = self.root / "unsafe-status-config-policy.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        version = layout / "deployment" / "versions" / self.candidate.name
        config = version / "source" / "config.toml"
        config.chmod(0o644)
        # Keep TOML valid, but diverge policy from the promotion-safe identity.
        config.write_text(
            "mode = 'dry-run'\nnote = 'literal # is data'\ntags = ['status', 'valid']\n\n"
            "[automation]\nautomerge = false\nrequire_human_approval = true\n"
            "require_checks = true\nrequire_test_evidence = true\n[executor]\nenabled = false\n",
            encoding="utf-8",
        )
        config.chmod(0o444)
        completed = self._run("lokay_status.sh", db=db, deployment=layout / "deployment")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Fala identity policy does not match embedded config", completed.stdout)

    def test_status_uses_top_level_policy_precedence(self):
        db = self.root / "status-policy-precedence.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        config = layout / "deployment" / "versions" / self.candidate.name / "source" / "config.toml"
        config.chmod(0o644)
        # Top-level keys win and must match the promotion-safe identity policy.
        config.write_text(
            "automerge = true\nrequire_human_approval = false\nrequire_checks = true\nrequire_test_evidence = true\n\n"
            "[automation]\nautomerge = false\nrequire_human_approval = true\nrequire_checks = false\nrequire_test_evidence = false\n"
            "[executor]\nenabled = true\n",
            encoding="utf-8",
        )
        config.chmod(0o444)
        completed = self._run("lokay_status.sh", db=db, deployment=layout / "deployment")
        self.assertNotIn("identity-policy-config-mismatch", completed.stdout)

    def test_health_uses_top_level_policy_precedence(self):
        db = self.root / "health-policy-precedence.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        version = layout / "deployment" / "versions" / self.candidate.name
        config = version / "source" / "config.toml"
        config.chmod(0o644)
        # Top-level keys win and must match the promotion-safe identity policy.
        config.write_text(
            "automerge = true\nrequire_human_approval = false\nrequire_checks = true\nrequire_test_evidence = true\n\n"
            "[automation]\nautomerge = false\nrequire_human_approval = true\nrequire_checks = false\nrequire_test_evidence = false\n"
            "[executor]\nenabled = true\n",
            encoding="utf-8",
        )
        config.chmod(0o444)
        manifest_path = version / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["config_hash"] = hashlib.sha256(config.read_bytes()).hexdigest()
        manifest["identity"]["config_hash"] = manifest["config_hash"]
        manifest["artifacts"]["source/config.toml"] = {"sha256": manifest["config_hash"], "bytes": config.stat().st_size}
        manifest_path.chmod(0o644)
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        manifest_path.chmod(0o444)
        completed = self._run("lokay_health.sh", db=db, deployment=layout / "deployment")
        self.assertNotIn("identity-policy-config-mismatch", completed.stdout)

    def test_health_rejects_latest_incomplete_run(self):
        db = self.root / "latest-incomplete.sqlite"
        self._write_db(db, mode="live")
        with sqlite3.connect(db) as connection:
            connection.execute("UPDATE runs SET status='created' WHERE id='latest'")
        layout = self._layout(db=db)
        completed = self._run(
            "lokay_health.sh",
            db=db,
            deployment=layout / "deployment",
            extra={"HERMES_LOKAY_FALA_REQUIRE_LIVE": "1"},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("latest-run-not-completed:created", completed.stdout)

    def test_status_rejects_latest_incomplete_run(self):
        db = self.root / "latest-incomplete-status.sqlite"
        self._write_db(db, mode="live")
        with sqlite3.connect(db) as connection:
            connection.execute("UPDATE runs SET status='active' WHERE id='latest'")
        layout = self._layout(db=db)
        completed = self._run(
            "lokay_status.sh",
            db=db,
            deployment=layout / "deployment",
            extra={"HERMES_LOKAY_FALA_REQUIRE_LIVE": "1"},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("latest-run-not-completed:active", completed.stdout)
    def test_health_rejects_every_unsafe_latest_status_without_process_failures(self):
        for status in ("waiting", "cancel_requested", "failed", "cancelled", "timed_out"):
            db = self.root / f"latest-{status}.sqlite"
            self._write_db(db, mode="live")
            with sqlite3.connect(db) as connection:
                connection.execute("UPDATE runs SET status=? WHERE id='latest'", (status,))
            layout = self._layout(db=db)
            completed = self._run(
                "lokay_health.sh",
                db=db,
                deployment=layout / "deployment",
                extra={"HERMES_LOKAY_FALA_REQUIRE_LIVE": "1"},
            )
            self.assertNotEqual(completed.returncode, 0, status)
            self.assertIn(f"latest-run-not-completed:{status}", completed.stdout)

    def test_health_marks_non_live_production_gate(self):
        db = self.root / "non-live.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        completed = self._run(
            "lokay_health.sh",
            db=db,
            deployment=layout / "deployment",
            extra={"HERMES_LOKAY_FALA_REQUIRE_LIVE": "1"},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("production-gate-requires-live", completed.stdout)

    def test_status_rejects_nonzero_fala_exit(self):
        db = self.root / "nonzero-fala.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        loaded = self.SUPERVISOR_LABEL
        completed = self._run(
            "lokay_status.sh",
            db=db,
            deployment=layout / "deployment",
            extra={
                "FAKE_LAUNCHCTL_LOADED": loaded,
                "FAKE_LAUNCHCTL_EXIT_CODE": "1",
            },
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("supervisor-last-exit-invalid", completed.stdout)
    def test_status_rejects_loaded_legacy_mutator(self):
        db = self.root / "legacy-status.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        loaded = ",".join([self.SUPERVISOR_LABEL, "com.mikolaj92.hermes.repo-issue-intake"])
        completed = self._run(
            "lokay_status.sh",
            db=db,
            deployment=layout / "deployment",
            extra={"FAKE_LAUNCHCTL_LOADED": loaded},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("legacy-mutator-unexpected-loaded", completed.stdout)
    def test_health_marks_installed_plist_mismatch(self):
        db = self.root / "plist-mismatch.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        label = self.SUPERVISOR_LABEL
        installed = layout / "home" / "Library" / "LaunchAgents" / f"{label}.plist"
        installed.chmod(0o644)
        installed.write_bytes(installed.read_bytes() + b"\n")
        completed = self._run("lokay_health.sh", db=db, deployment=layout / "deployment")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(f"installed-plist-not-current:{label}", completed.stdout)

    def test_status_marks_unresolved_historical_runs(self):
        db = self.root / "historical.sqlite"
        self._write_db(db, mode="dry-run", historical=True)
        completed = self._run("lokay_status.sh", db=db)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("unresolved-runs:", completed.stdout)
        for run_id in ("old-failed", "old-created", "old-cancel_requested"):
            self.assertIn(run_id, completed.stdout)

    def test_health_rejects_repair_argument(self):
        completed = self._run("lokay_health.sh", args=("--repair",))
        self.assertEqual(completed.returncode, 2)
        self.assertIn("unsupported argument: --repair", completed.stderr)

    def test_health_marks_dual_mutator(self):
        db = self.root / "mutators.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        loaded = ",".join([self.SUPERVISOR_LABEL, "com.mikolaj92.hermes.repo-issue-intake"])
        dual = self._run(
            "lokay_health.sh",
            db=db,
            deployment=layout / "deployment",
            extra={"FAKE_LAUNCHCTL_LOADED": loaded},
        )
        self.assertNotEqual(dual.returncode, 0)
        self.assertIn("dual-mutator active", dual.stdout + dual.stderr)

    def test_health_rejects_residual_process_production_plist(self):
        db = self.root / "residual-process-plist.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        residual_label = "com.mikolaj92.lokay.repo-issue-poll"
        residual_plist = layout / "home" / "Library" / "LaunchAgents" / f"{residual_label}.plist"
        residual_plist.write_text("residual\n", encoding="utf-8")
        completed = self._run(
            "lokay_health.sh",
            db=db,
            deployment=layout / "deployment",
            extra={"FAKE_LAUNCHCTL_LOADED": ",".join(self._default_loaded_labels())},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(f"process-production-plist-present:{residual_label}", completed.stdout)
        self.assertIn("supervisor-job-loaded count=1", completed.stdout)

    def test_health_rejects_loaded_residual_process_job(self):
        db = self.root / "residual-process-job.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        residual_label = "com.mikolaj92.lokay.issue-triage"
        loaded = ",".join([*self._default_loaded_labels(), residual_label])
        completed = self._run(
            "lokay_health.sh",
            db=db,
            deployment=layout / "deployment",
            extra={"FAKE_LAUNCHCTL_LOADED": loaded},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(f"process-production-job-loaded label={residual_label}", completed.stdout)
        self.assertIn("supervisor-job-loaded count=1", completed.stdout)

    def test_health_rejects_loaded_repair_enabled_health_agent(self):
        db = self.root / "repair-health.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        health_plist = layout / "home" / "Library" / "LaunchAgents" / "com.mikolaj92.hermes.repo-agent-health.plist"
        with health_plist.open("wb") as stream:
            plistlib.dump({"ProgramArguments": ["lokay_health.sh", "--repair"]}, stream)
        loaded = ",".join([self.SUPERVISOR_LABEL, "com.mikolaj92.hermes.repo-agent-health"])
        completed = self._run(
            "lokay_health.sh",
            db=db,
            deployment=layout / "deployment",
            extra={"FAKE_LAUNCHCTL_LOADED": loaded},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("health_repair_loaded=1", completed.stdout)

    def test_health_rejects_policy_that_differs_from_config(self):
        db = self.root / "tampered-health-policy.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        manifest_path = layout / "deployment" / "versions" / self.candidate.name / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["policy"]["require_human_approval"] = True
        manifest["identity"]["policy"]["require_human_approval"] = True
        manifest_path.chmod(0o644)
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        manifest_path.chmod(0o444)
        completed = self._run("lokay_health.sh", db=db, deployment=layout / "deployment")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Fala identity policy is unsafe for promotion", completed.stdout)

    def test_health_rejects_safe_manifest_policy_with_unsafe_valid_toml(self):
        db = self.root / "unsafe-config-policy.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        baseline = self._run("lokay_health.sh", db=db, deployment=layout / "deployment")
        self.assertNotIn("identity-policy-config-mismatch", baseline.stdout)
        version = layout / "deployment" / "versions" / self.candidate.name
        config = version / "source" / "config.toml"
        config.chmod(0o644)
        # Keep TOML valid, but diverge policy from the promotion-safe identity.
        config.write_text(
            "mode = 'dry-run'\nnote = 'literal # is data'\ntags = ['health', 'valid']\n\n"
            "[automation]\nautomerge = false\nrequire_human_approval = true\n"
            "require_checks = true\nrequire_test_evidence = true\n[executor]\nenabled = false\n",
            encoding="utf-8",
        )
        config.chmod(0o444)
        manifest_path = version / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["config_hash"] = hashlib.sha256(config.read_bytes()).hexdigest()
        manifest["identity"]["config_hash"] = manifest["config_hash"]
        manifest["artifacts"]["source/config.toml"] = {"sha256": manifest["config_hash"], "bytes": config.stat().st_size}
        manifest_path.chmod(0o644)
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        manifest_path.chmod(0o444)
        completed = self._run("lokay_health.sh", db=db, deployment=layout / "deployment")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Fala identity policy does not match embedded config", completed.stdout)

    def test_health_reports_unavailable_managed_toml_parser(self):
        db = self.root / "missing-toml-python.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        managed_python = layout / "deployment" / "versions" / self.candidate.name / "source" / "project" / ".venv" / "bin" / "python"
        managed_python.parent.chmod(0o755)
        managed_python.unlink()
        completed = self._run(
            "lokay_health.sh",
            db=db,
            deployment=layout / "deployment",
        )
        self.assertNotEqual(completed.returncode, 0)
        # Registry activation now runs before candidate validation and fails closed
        # when the managed interpreter is missing.
        combined = f"{completed.stdout}\n{completed.stderr}"
        self.assertTrue(
            "registry-error" in combined or "toml-parser-unavailable" in combined,
            msg=combined,
        )

    def test_health_parity_failure_increments_failures(self):
        db = self.root / "parity-health.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        completed = self._run(
            "lokay_health.sh",
            db=db,
            deployment=layout / "deployment",
            extra={
                "HERMES_LOKAY_PARITY_ENABLED": "1",
                "HERMES_LOKAY_PARITY_SOURCE_ROOT": str(ROOT / "scripts"),
                "HERMES_LOKAY_PARITY_ACTIVE_ROOT": str(self.root / "missing-active-scripts"),
                "HERMES_LOKAY_PARITY_TEMPLATE_ROOT": str(ROOT / "templates" / "launchd"),
                "HERMES_LOKAY_PARITY_ACTIVE_PLIST_ROOT": str(layout / "home" / "Library" / "LaunchAgents"),
                "HERMES_LOKAY_PARITY_CONFIG_ROOT": str(self.root / "missing-active-config"),
            },
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("deployment-parity mismatch", completed.stdout)

    def test_status_reports_fala_noop_activity(self):
        completed = self._run("lokay_status.sh", db=self.base_db)
        self.assertIn("Recent Fala Runs", completed.stdout)
        self.assertIn("run_id=latest", completed.stdout)
        self.assertIn("activity=noop", completed.stdout)

    def test_status_reports_fala_worked_activity(self):
        db = self.root / "worked.sqlite"
        self._write_db(db, mode="live")
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with sqlite3.connect(db) as connection:
            connection.execute(
                "INSERT INTO processes "
                "(run_id,id,process_type,status,priority,attempt,max_attempts,available_at,input_json,output_json,error_json,metadata,created_at,updated_at,output_schema_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "latest", "latest:auto_worker:intake_claim", "correlation.effector", "succeeded",
                    0, 1, 1, now, "{}", '{"values":{"mutated":true,"status":"claimed"}}',
                    "{}", "{}", now, now, "{}",
                ),
            )
        completed = self._run("lokay_status.sh", db=db)
        self.assertIn("run_id=latest", completed.stdout)
        self.assertIn("activity=worked", completed.stdout)

    def test_status_does_not_treat_action_without_mutation_as_worked(self):
        db = self.root / "action-only.sqlite"
        self._write_db(db, mode="live")
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with sqlite3.connect(db) as connection:
            connection.execute(
                "INSERT INTO processes "
                "(run_id,id,process_type,status,priority,attempt,max_attempts,available_at,input_json,output_json,error_json,metadata,created_at,updated_at,output_schema_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "latest", "latest:auto_worker:inspect", "correlation.effector", "succeeded",
                    0, 1, 1, now, "{}", '{"values":{"action":"inspect","mutated":false,"status":"ok"}}',
                    "{}", "{}", now, now, "{}",
                ),
            )
        completed = self._run("lokay_status.sh", db=db)
        self.assertIn("run_id=latest", completed.stdout)
        self.assertIn("activity=noop", completed.stdout)

    def test_health_uses_fala_plist_stdout_path(self):
        db = self.root / "runtime-log.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        label = self.SUPERVISOR_LABEL
        installed = layout / "home" / "Library" / "LaunchAgents" / f"{label}.plist"
        import plistlib
        with installed.open("rb") as stream:
            runtime_log = plistlib.load(stream)["StandardOutPath"]
        completed = self._run("lokay_health.sh", db=db, deployment=layout / "deployment")
        self.assertIn(f"missing-log label={label} path={runtime_log}", completed.stdout)

    def _status_case(self, name: str, *, install_status: bool = False):
        db = self.root / f"{name}.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        state_root = layout / "supervisor-state"
        return db, layout, state_root

    def test_health_accepts_valid_idle_supervisor_status(self):
        db, layout, state_root = self._status_case("status-healthy")
        self._write_status(
            state_root,
            self._status_document(
                candidate_id=self.candidate.name,
                generation=self.candidate.name,
                config_sha256=hashlib.sha256(self.config.read_bytes()).hexdigest(),
                slot_counts={},
                dispatch_slots=[],
            ),
        )
        completed = self._run(
            "lokay_health.sh",
            db=db,
            deployment=layout / "deployment",
            install_status=False,
            extra={"HERMES_LOKAY_SUPERVISOR_STATE_ROOT": str(state_root)},
        )
        self.assertIn("supervisor-status path=", completed.stdout)
        self.assertIn("lease_state=owned", completed.stdout)
        self.assertIn("dispatch_slots=0", completed.stdout)
        self.assertNotIn("supervisor-status-missing", completed.stdout)

    def test_status_accepts_valid_idle_supervisor_status(self):
        db, layout, state_root = self._status_case("status-healthy-status")
        self._write_status(
            state_root,
            self._status_document(
                candidate_id=self.candidate.name,
                generation=self.candidate.name,
                config_sha256=hashlib.sha256(self.config.read_bytes()).hexdigest(),
            ),
        )
        completed = self._run(
            "lokay_status.sh",
            db=db,
            deployment=layout / "deployment",
            install_status=False,
            extra={"HERMES_LOKAY_SUPERVISOR_STATE_ROOT": str(state_root)},
        )
        self.assertIn("Supervisor status", completed.stdout)
        self.assertIn("lease_state=owned", completed.stdout)
        self.assertNotIn("ERROR supervisor-status", completed.stdout)

    def test_health_rejects_missing_supervisor_status(self):
        db, layout, state_root = self._status_case("status-missing")
        completed = self._run(
            "lokay_health.sh",
            db=db,
            deployment=layout / "deployment",
            install_status=False,
            extra={"HERMES_LOKAY_SUPERVISOR_STATE_ROOT": str(state_root)},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("supervisor-status-missing", completed.stdout)

    def test_status_rejects_missing_supervisor_status(self):
        db, layout, state_root = self._status_case("status-missing-status")
        completed = self._run(
            "lokay_status.sh",
            db=db,
            deployment=layout / "deployment",
            install_status=False,
            extra={"HERMES_LOKAY_SUPERVISOR_STATE_ROOT": str(state_root)},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("supervisor-status-missing", completed.stdout)

    def test_health_rejects_malformed_supervisor_status(self):
        db, layout, state_root = self._status_case("status-malformed")
        self._write_status(state_root, raw="{not-json")
        completed = self._run(
            "lokay_health.sh",
            db=db,
            deployment=layout / "deployment",
            install_status=False,
            extra={"HERMES_LOKAY_SUPERVISOR_STATE_ROOT": str(state_root)},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("supervisor-status-malformed", completed.stdout)

    def test_health_rejects_symlink_supervisor_status(self):
        db, layout, state_root = self._status_case("status-symlink")
        self._write_status(state_root, self._status_document(), as_symlink=True)
        completed = self._run(
            "lokay_health.sh",
            db=db,
            deployment=layout / "deployment",
            install_status=False,
            extra={"HERMES_LOKAY_SUPERVISOR_STATE_ROOT": str(state_root)},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("supervisor-status-symlink", completed.stdout)

    def test_health_rejects_stale_supervisor_status(self):
        db, layout, state_root = self._status_case("status-stale")
        self._write_status(
            state_root,
            self._status_document(loop_timestamp=time.time() - 1000),
        )
        completed = self._run(
            "lokay_health.sh",
            db=db,
            deployment=layout / "deployment",
            install_status=False,
            extra={
                "HERMES_LOKAY_SUPERVISOR_STATE_ROOT": str(state_root),
                "HERMES_LOKAY_SUPERVISOR_STATUS_MAX_AGE_SECONDS": "180",
            },
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("supervisor-status-stale", completed.stdout)

    def test_health_rejects_future_loop_timestamp(self):
        db, layout, state_root = self._status_case("status-future")
        self._write_status(
            state_root,
            self._status_document(loop_timestamp=time.time() + 120),
        )
        completed = self._run(
            "lokay_health.sh",
            db=db,
            deployment=layout / "deployment",
            install_status=False,
            extra={"HERMES_LOKAY_SUPERVISOR_STATE_ROOT": str(state_root)},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("supervisor-status-loop-timestamp-future", completed.stdout)

    def test_health_rejects_unowned_lease_state(self):
        db, layout, state_root = self._status_case("status-lease")
        self._write_status(state_root, self._status_document(lease_state="present"))
        completed = self._run(
            "lokay_health.sh",
            db=db,
            deployment=layout / "deployment",
            install_status=False,
            extra={"HERMES_LOKAY_SUPERVISOR_STATE_ROOT": str(state_root)},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("supervisor-status-lease-unowned", completed.stdout)

    def test_health_rejects_dead_supervisor_pid(self):
        db, layout, state_root = self._status_case("status-dead-pid")
        self._write_status(
            state_root,
            self._status_document(
                supervisor_pid=2**30,
                supervisor_start_identity=f"{2**30}:0:unverified",
            ),
        )
        completed = self._run(
            "lokay_health.sh",
            db=db,
            deployment=layout / "deployment",
            install_status=False,
            extra={
                "HERMES_LOKAY_SUPERVISOR_STATE_ROOT": str(state_root),
                "FAKE_LAUNCHCTL_PID": str(2**30),
            },
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("supervisor-status-pid-dead", completed.stdout)

    def test_health_rejects_candidate_identity_mismatch(self):
        db, layout, state_root = self._status_case("status-candidate-mismatch")
        other = "a" * 64
        self._write_status(
            state_root,
            self._status_document(candidate_id=other, generation=other),
        )
        completed = self._run(
            "lokay_health.sh",
            db=db,
            deployment=layout / "deployment",
            install_status=False,
            extra={"HERMES_LOKAY_SUPERVISOR_STATE_ROOT": str(state_root)},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("supervisor-status-candidate-mismatch", completed.stdout)

    def test_health_rejects_retry_exhausted_slots(self):
        db, layout, state_root = self._status_case("status-retry-exhausted")
        self._write_status(
            state_root,
            self._status_document(
                slot_counts={"failed": 1},
                dispatch_slots=[
                    {
                        "process_id": "issue_to_pr",
                        "status": "failed",
                        "dispatch_id": "d1",
                        "pid": None,
                        "start_identity": None,
                        "attempt": 3,
                        "due_at": time.time(),
                        "deadline_at": None,
                        "exit_code": 1,
                        "details": {"retry_exhausted": True, "failure_class": "retryable_child_exit"},
                    }
                ],
            ),
        )
        completed = self._run(
            "lokay_health.sh",
            db=db,
            deployment=layout / "deployment",
            install_status=False,
            extra={"HERMES_LOKAY_SUPERVISOR_STATE_ROOT": str(state_root)},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("supervisor-status-retry-exhausted", completed.stdout)

    def test_health_rejects_live_orphan_recovery(self):
        db, layout, state_root = self._status_case("status-orphan")
        self._write_status(
            state_root,
            self._status_document(
                slot_counts={"orphaned": 1},
                dispatch_slots=[
                    {
                        "process_id": "pr_triage",
                        "status": "orphaned",
                        "dispatch_id": "d2",
                        "pid": 12345,
                        "start_identity": "12345:0:ps:fake",
                        "attempt": 1,
                        "due_at": time.time(),
                        "deadline_at": None,
                        "exit_code": None,
                        "details": {
                            "recovery_required": True,
                            "orphan_resolution": "live",
                            "fence_retained": True,
                        },
                    }
                ],
            ),
        )
        completed = self._run(
            "lokay_health.sh",
            db=db,
            deployment=layout / "deployment",
            install_status=False,
            extra={"HERMES_LOKAY_SUPERVISOR_STATE_ROOT": str(state_root)},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("supervisor-status-orphan-recovery", completed.stdout)

    def test_status_rejects_stale_supervisor_status(self):
        db, layout, state_root = self._status_case("status-stale-status")
        self._write_status(
            state_root,
            self._status_document(loop_timestamp=time.time() - 500),
        )
        completed = self._run(
            "lokay_status.sh",
            db=db,
            deployment=layout / "deployment",
            install_status=False,
            extra={"HERMES_LOKAY_SUPERVISOR_STATE_ROOT": str(state_root)},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("supervisor-status-stale", completed.stdout)

if __name__ == "__main__":
    unittest.main()
