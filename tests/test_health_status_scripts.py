from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    parent = sys.modules.get("hermes_plugins")
    if parent is None:
        parent = types.ModuleType("hermes_plugins")
        parent.__path__ = []
        sys.modules["hermes_plugins"] = parent
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.oss_repo_agent",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["hermes_plugins.oss_repo_agent"] = module
    spec.loader.exec_module(module)
    return module


class HealthStatusScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_plugin()
        cls.commands = cls.module.commands
        cls.cfg = cls.commands.OssRepoAgentConfig.from_mapping({"mode": "dry-run", "repos": []})
        cls.holder = tempfile.TemporaryDirectory()
        cls.root = Path(cls.holder.name)
        cls.config = cls.root / "config.toml"
        cls.config.write_text("mode = 'dry-run'\n", encoding="utf-8")
        cls.base_db = cls.root / "base.sqlite"
        cls._write_db(cls.base_db, mode="dry-run")
        lock_data = (ROOT / "uv.lock").read_bytes().replace(b'editable = "../Fala"', b'editable = "Fala"')
        identity = {
            "schema": 1,
            "mode": "dry-run",
            "plugin_commit": "plugin-commit",
            "fala_tag": "0.2.1",
            "fala_commit": "b5f8085f418010a9290613b86671d435551411a9",
            "lock_hash": hashlib.sha256(lock_data).hexdigest(),
            "config_path": str(cls.config.absolute()),
            "config_hash": hashlib.sha256(cls.config.read_bytes()).hexdigest(),
            "db_path": str((cls.root / "state.sqlite").absolute()),
            "metadata_path": "source/metadata.json",
            "lock_path": "source/project/uv.lock",
            "config_artifact_path": "source/config.toml",
            "revision_path": "source/revision.txt",
            "policy": {
                "automerge": False,
                "require_human_approval": True,
                "require_checks": True,
                "require_test_evidence": True,
                "executor_enabled": False,
            },
        }
        candidate_id = hashlib.sha256((json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()
        candidate = cls.root / "deployment" / "candidates" / candidate_id
        project_root = ROOT.resolve()
        fala_root = (ROOT.parent / "Fala").resolve()
        real_run = cls.commands.subprocess.run

        def fake_git(argv, *args, **kwargs):
            command = list(argv)
            if len(command) >= 3 and command[:2] == ["git", "-C"]:
                checkout = Path(command[2]).resolve()
                if "status" in command and checkout in {project_root, fala_root}:
                    return subprocess.CompletedProcess(command, 0, "", "")
                if checkout == fala_root and command[3:5] == ["rev-parse", "HEAD"]:
                    return subprocess.CompletedProcess(command, 0, identity["fala_commit"] + "\n", "")
            return real_run(argv, *args, **kwargs)

        with patch.object(cls.commands.subprocess, "run", side_effect=fake_git), patch.object(
            cls.commands, "_read_git_revision", return_value=identity["plugin_commit"]
        ), patch.object(cls.commands.shutil, "which", return_value="/usr/bin/uv"):
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
  if [[ "$domain" == gui/* && "${FAKE_LAUNCHCTL_GUI_AVAILABLE:-0}" != 1 ]]; then printf 'Domain does not support specified action\n' >&2; exit 125; fi
  case ",${FAKE_LAUNCHCTL_LOADED:-}," in *,"$label",*) printf 'state = running\nruns = 1\nlast exit code = %s\n' "${FAKE_LAUNCHCTL_EXIT_CODE:-0}"; exit 0;; esac
  printf 'could not find service\n' >&2; exit 1
fi
exit 0
""",
            encoding="utf-8",
        )
        (fake / "gh").write_text(
            """#!/usr/bin/env bash
case \"$1\" in
  auth) exit 0;;
  api) printf 'offline-user\\n'; exit 0;;
  pr|issue) printf '0\\n'; exit 0;;
esac
exit 0
""",
            encoding="utf-8",
        )
        (fake / "hermes").write_text(
            """#!/usr/bin/env bash
if [[ \"$1\" == --version ]]; then printf 'hermes offline\\n'; exit 0; fi
if [[ \"$1\" == kanban ]]; then printf 'ready=0\\n'; exit 0; fi
exit 0
""",
            encoding="utf-8",
        )
        for command in (fake / "launchctl", fake / "gh", fake / "hermes"):
            command.chmod(0o755)
        return fake

    def _run(self, script: str, *, db: Path | None = None, deployment: Path | None = None, extra: dict[str, str] | None = None, args: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
        root = self.root / ("run-" + script.replace(".sh", ""))
        root.mkdir(parents=True, exist_ok=True)
        fake = self._fake_commands(root)
        home = ((deployment.parent / "home") if deployment is not None else (root / "home"))
        (home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "PATH": str(fake) + os.pathsep + env.get("PATH", ""),
                "HERMES_REPO_AGENT_REPOS_FILE": str(root / "repos.txt"),
                "HERMES_REPO_AGENT_LOG_DIR": str(root / "logs"),
                "HERMES_REPO_AGENT_HEALTH_LOG": str(root / "logs" / "health.log"),
                "HERMES_REPO_AGENT_DEPLOYMENT_ROOT": str(deployment or (root / "deployment")),
                "HERMES_REPO_AGENT_FALA_DB": str(db or (root / "missing.sqlite")),
                "HERMES_REPO_AGENT_FALA_PLIST": str(home / "Library" / "LaunchAgents" / "com.mikolaj92.hermes.repo-agent-fala-tick-all.plist"),
                "HERMES_REPO_AGENT_FALA_REQUIRE_LIVE": "0",
                "HERMES_REPO_AGENT_FALA_MAX_RUN_AGE_SECONDS": "1800",
                "HERMES_REPO_AGENT_MIN_FREE_GB": "0",
            }
        )
        (root / "repos.txt").write_text("offline/repo|offline-board|/tmp/offline-repo|1\n", encoding="utf-8")
        if extra:
            env.update(extra)
        return subprocess.run(["bash", str(ROOT / "scripts" / script), *args], env=env, capture_output=True, text=True, timeout=30)

    def _layout(self, *, db: Path, installed_copy: bool = True) -> Path:
        root = self.root / ("layout-" + db.stem)
        versions = root / "deployment" / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        version = versions / self.candidate.name
        shutil.copytree(self.candidate, version)
        self.commands._promote_version_runtime(version, root / "deployment", self.candidate.name)
        current = root / "deployment" / "current"
        current.symlink_to(version, target_is_directory=True)
        if installed_copy:
            installed = root / "home" / "Library" / "LaunchAgents"
            installed.mkdir(parents=True, exist_ok=True)
            shutil.copy2(version / "launchd" / "com.mikolaj92.hermes.repo-agent-fala-tick-all.plist", installed / "com.mikolaj92.hermes.repo-agent-fala-tick-all.plist")
        return root

    def test_health_rejects_malformed_environment(self):
        completed = self._run("repo_agent_health.sh", extra={"HERMES_STALE_LOCK_MINUTES": "not-a-number"})
        self.assertEqual(completed.returncode, 2)
        self.assertIn("invalid-env", completed.stderr)

    def test_status_rejects_malformed_environment(self):
        completed = self._run("repo_agent_status.sh", extra={"HERMES_REPO_AGENT_FALA_REQUIRE_LIVE": "maybe"})
        self.assertEqual(completed.returncode, 2)
        self.assertIn("invalid-env", completed.stderr)

    def test_health_marks_missing_current_and_db(self):
        completed = self._run("repo_agent_health.sh")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("fala-deployment invalid-current", completed.stdout)
        self.assertIn("fala-db missing", completed.stdout)

    def test_health_marks_invalid_current_candidate(self):
        db = self.root / "invalid-current.sqlite"
        self._write_db(db, mode="live")
        layout = self._layout(db=db)
        version_manifest = layout / "deployment" / "versions" / self.candidate.name / "manifest.json"
        version_manifest.chmod(0o644)
        version_manifest.write_text("{}\n", encoding="utf-8")
        completed = self._run("repo_agent_health.sh", db=db, deployment=layout / "deployment")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("fala-deployment candidate-invalid", completed.stdout)


    def _tamper_runtime_identity(self, layout: Path) -> None:
        manifest_path = layout / "deployment" / "versions" / self.candidate.name / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["runtime_identity"]["process_type"] = "Interactive"
        manifest_path.chmod(0o755)
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        manifest_path.chmod(0o444)

    def test_health_rejects_tampered_runtime_identity(self):
        db = self.root / "tampered-health.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        self._tamper_runtime_identity(layout)
        completed = self._run("repo_agent_health.sh", db=db, deployment=layout / "deployment")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("runtime-identity-process_type-mismatch", completed.stdout)

    def test_status_rejects_tampered_runtime_identity(self):
        db = self.root / "tampered-status.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        self._tamper_runtime_identity(layout)
        completed = self._run("repo_agent_status.sh", db=db, deployment=layout / "deployment")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("runtime-identity-process_type-mismatch", completed.stdout)
    def test_health_marks_non_live_production_gate(self):
        db = self.root / "non-live.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        completed = self._run(
            "repo_agent_health.sh",
            db=db,
            deployment=layout / "deployment",
            extra={"HERMES_REPO_AGENT_FALA_REQUIRE_LIVE": "1"},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("production-gate-requires-live", completed.stdout)

    def test_status_rejects_nonzero_fala_exit(self):
        db = self.root / "nonzero-fala.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        loaded = ",".join(("com.mikolaj92.hermes.repo-agent-fala-tick-all",))
        completed = self._run(
            "repo_agent_status.sh",
            db=db,
            deployment=layout / "deployment",
            extra={
                "FAKE_LAUNCHCTL_LOADED": loaded,
                "FAKE_LAUNCHCTL_EXIT_CODE": "1",
            },
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("fala-last-exit-invalid", completed.stdout)
    def test_status_rejects_loaded_legacy_mutator(self):
        db = self.root / "legacy-status.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        loaded = ",".join(
            (
                "com.mikolaj92.hermes.repo-agent-fala-tick-all",
                "com.mikolaj92.hermes.repo-issue-intake",
            )
        )
        completed = self._run(
            "repo_agent_status.sh",
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
        installed = layout / "home" / "Library" / "LaunchAgents" / "com.mikolaj92.hermes.repo-agent-fala-tick-all.plist"
        installed.chmod(0o644)
        installed.write_bytes(installed.read_bytes() + b"\n")
        completed = self._run("repo_agent_health.sh", db=db, deployment=layout / "deployment")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("installed-plist-not-current", completed.stdout)

    def test_status_marks_unresolved_historical_runs(self):
        db = self.root / "historical.sqlite"
        self._write_db(db, mode="dry-run", historical=True)
        completed = self._run("repo_agent_status.sh", db=db)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("unresolved-runs:", completed.stdout)
        for run_id in ("old-failed", "old-created", "old-cancel_requested"):
            self.assertIn(run_id, completed.stdout)

    def test_health_marks_dual_mutator_and_repair_blocked(self):
        db = self.root / "mutators.sqlite"
        self._write_db(db, mode="dry-run")
        layout = self._layout(db=db)
        loaded = ",".join(("com.mikolaj92.hermes.repo-agent-fala-tick-all", "com.mikolaj92.hermes.repo-issue-intake"))
        dual = self._run(
            "repo_agent_health.sh",
            db=db,
            deployment=layout / "deployment",
            extra={"FAKE_LAUNCHCTL_LOADED": loaded},
        )
        self.assertNotEqual(dual.returncode, 0)
        self.assertIn("dual-mutator active", dual.stdout + dual.stderr)
        repair = self._run(
            "repo_agent_health.sh",
            db=db,
            deployment=layout / "deployment",
            extra={"FAKE_LAUNCHCTL_LOADED": loaded},
            args=("--repair",),
        )
        self.assertNotEqual(repair.returncode, 0)
        self.assertIn("repair-blocked active-mutator", repair.stdout + repair.stderr)

if __name__ == "__main__":
    unittest.main()
