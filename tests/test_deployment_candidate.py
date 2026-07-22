from __future__ import annotations

import importlib.util
import json
import plistlib
import hashlib
import subprocess
import sys
import tempfile
import types
import unittest
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


class DeploymentCandidateTests(unittest.TestCase):
    def setUp(self):
        self.module = load_plugin()
        self.commands = self.module.commands
        self.cfg = self.commands.OssRepoAgentConfig.from_mapping({"repos": []})

    def _fala_git_clean(self):
        project_root = ROOT.resolve()
        fala_root = (ROOT.parent / "Fala").resolve()
        pinned = "b5f8085f418010a9290613b86671d435551411a9"
        real_run = self.commands.subprocess.run

        def fake_run(argv, *args, **kwargs):
            command = list(argv)
            if len(command) >= 3 and command[:2] == ["git", "-C"]:
                checkout = Path(command[2]).resolve()
                if "status" in command and checkout in {project_root, fala_root}:
                    return subprocess.CompletedProcess(command, 0, "", "")
                if checkout == fala_root and command[3:5] == ["rev-parse", "HEAD"]:
                    return subprocess.CompletedProcess(command, 0, pinned + "\n", "")
            return real_run(argv, *args, **kwargs)

        return patch.object(self.commands.subprocess, "run", side_effect=fake_run)
    def test_init_force_overwrite_preserves_original_on_fsync_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "config.toml"
            original = "mode = 'dry-run'\noriginal = true\n"
            target.write_text(original, encoding="utf-8")
            with patch.object(self.commands.os, "fsync", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    self.commands.init_project(str(target), "owner/repo", "owner-board", "/tmp/clones", "/tmp/worktrees", None, True)
            self.assertEqual(target.read_text(encoding="utf-8"), original)
            self.assertEqual(list(target.parent.glob(f".{target.name}.*")), [])

    def test_candidate_tree_fsync_failure_removes_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(self.commands, "_fsync_tree", side_effect=self.commands.ConfigError("candidate fsync failed")):
                with self.assertRaisesRegex(self.commands.ConfigError, "candidate fsync failed"):
                    self._render(root)
            candidates = root / "candidates"
            self.assertEqual(list(candidates.iterdir()) if candidates.exists() else [], [])

    def _render(self, root: Path, *, mode: str = "dry-run", config_path: Path | None = None, db_path: Path | None = None) -> Path:
        config = config_path or root / "config.toml"
        config.write_text(f"mode = '{mode}'\n", encoding="utf-8")
        db = db_path or root / "state.sqlite"
        lock_data = (ROOT / "uv.lock").read_bytes().replace(b'editable = "../Fala"', b'editable = "Fala"')
        identity = {
            "schema": 1,
            "mode": mode,
            "plugin_commit": "plugin-commit",
            "fala_tag": "0.2.1",
            "fala_commit": "b5f8085f418010a9290613b86671d435551411a9",
            "lock_hash": hashlib.sha256(lock_data).hexdigest(),
            "config_path": str(config.absolute()),
            "config_hash": hashlib.sha256(config.read_bytes()).hexdigest(),
            "db_path": str(db.absolute()),
            "metadata_path": "source/metadata.json",
            "lock_path": "source/uv.lock",
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
        candidate = root / "candidates" / candidate_id
        with self._fala_git_clean(), patch.object(self.commands, "_read_git_revision", return_value="plugin-commit"), patch.object(
            self.commands.shutil, "which", return_value="/usr/bin/uv"
        ):
            result = self.commands.render_launchd(
                self.cfg, str(candidate), config_path=str(config), fala_db=str(db), mode=mode, deployment_root=str(root)
            )
        self.assertTrue(result["ok"])
        return candidate
    def test_staging_directory_fsync_failure_cleans_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates_root = root / "candidates"
            original_fsync_directory = self.commands._fsync_directory

            def fail_staging(path: Path) -> None:
                if Path(path).name == "candidates":
                    raise self.commands.ConfigError("staging directory fsync failed")
                original_fsync_directory(path)

            with patch.object(self.commands, "_fsync_directory", side_effect=fail_staging):
                with self.assertRaisesRegex(self.commands.ConfigError, "staging directory fsync failed"):
                    self._render(root)
            self.assertFalse(any(candidates_root.iterdir()) if candidates_root.exists() else False)

    def test_bootstrap_apply_is_metadata_only(self):
        with patch.object(self.commands.subprocess, "run") as run:
            result = self.commands.bootstrap(self.cfg, True)
        self.assertTrue(result["ok"])
        self.assertFalse(result["effective_live"])
        run.assert_not_called()

    def test_render_launchd_is_metadata_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls: list[list[str]] = []
            real_run = self.commands.subprocess.run

            def record_run(argv, *args, **kwargs):
                calls.append(list(argv))
                return real_run(argv, *args, **kwargs)

            with patch.object(self.commands.subprocess, "run", side_effect=record_run):
                self._render(root)
            self.assertFalse(any(call[:2] == ["launchctl", "bootstrap"] for call in calls))
            self.assertFalse(any(call[:2] == ["launchctl", "bootout"] for call in calls))


    def test_fala_source_tree_includes_src(self):
        with tempfile.TemporaryDirectory() as directory:
            fala_src = self._render(Path(directory)) / "source" / "project" / "Fala" / "src"
            self.assertTrue(fala_src.is_dir())
            self.assertTrue(any(fala_src.rglob("*.py")))
    def test_metadata_lock_hash_matches_bundled_lock_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = self._render(Path(directory))
            metadata = json.loads((candidate / "source" / "metadata.json").read_text(encoding="utf-8"))
            bundled = (candidate / "source" / "uv.lock").read_bytes()
            import hashlib
            self.assertEqual(metadata["lock_hash"], hashlib.sha256(bundled).hexdigest())

    def test_dirty_fala_checkout_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text("mode = 'dry-run'\n", encoding="utf-8")
            fala_root = (ROOT.parent / "Fala").resolve()
            real_run = self.commands.subprocess.run

            def dirty_run(argv, *args, **kwargs):
                command = list(argv)
                if len(command) >= 3 and command[:2] == ["git", "-C"] and Path(command[2]).resolve() == fala_root and "status" in command:
                    return subprocess.CompletedProcess(command, 0, " M uv.lock\n", "")
                return real_run(argv, *args, **kwargs)

            with patch.object(self.commands.subprocess, "run", side_effect=dirty_run), patch.object(
                self.commands, "_read_git_revision", return_value="plugin-commit"
            ):
                with self.assertRaises(self.commands.ConfigError):
                    self.commands.render_launchd(
                        self.cfg, str(root / "candidates" / "candidate"), config_path=str(config), fala_db=str(root / "state.sqlite"), deployment_root=str(root)
                    )


    def test_dirty_plugin_checkout_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text("mode = 'dry-run'\n", encoding="utf-8")
            project_root = ROOT.resolve()
            real_run = self.commands.subprocess.run

            def dirty_run(argv, *args, **kwargs):
                command = list(argv)
                if len(command) >= 3 and command[:2] == ["git", "-C"] and Path(command[2]).resolve() == project_root and "status" in command:
                    return subprocess.CompletedProcess(command, 0, " M commands.py\n", "")
                return real_run(argv, *args, **kwargs)

            with patch.object(self.commands.subprocess, "run", side_effect=dirty_run), patch.object(
                self.commands, "_read_git_revision", return_value="plugin-commit"
            ):
                with self.assertRaisesRegex(self.commands.ConfigError, "plugin checkout is dirty"):
                    self.commands.render_launchd(
                        self.cfg,
                        str(root / "candidates" / "candidate"),
                        config_path=str(config),
                        fala_db=str(root / "state.sqlite"),
                        deployment_root=str(root),
                    )
    def test_existing_candidate_rejects_stale_requested_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._render(root)
            other = root / "other.toml"
            other.write_text("mode = 'live'\n", encoding="utf-8")
            with self.assertRaises(self.commands.ConfigError):
                self._render(root, mode="live", config_path=other, db_path=root / "other.sqlite")

    def test_legacy_mutators_probe_user_and_gui_domains(self):
        calls: list[str] = []

        def fake_state(label: str, domain: str):
            calls.append(domain)
            return {"label": label, "domain": domain, "loaded": False}

        with patch.object(self.commands, "_launchctl_loaded_state", side_effect=fake_state):
            states = self.commands._assert_legacy_mutators_unloaded()
        self.assertEqual(set(states), set(self.commands.LEGACY_MUTATOR_LABELS))
        self.assertEqual(calls.count(f"user/{self.commands.os.getuid()}"), len(self.commands.LEGACY_MUTATOR_LABELS))
        self.assertEqual(calls.count(f"gui/{self.commands.os.getuid()}"), len(self.commands.LEGACY_MUTATOR_LABELS))

    def test_legacy_mutator_loaded_in_both_domains_fails_closed(self):
        def fake_state(label: str, domain: str):
            return {"label": label, "domain": domain, "loaded": label == self.commands.LEGACY_MUTATOR_LABELS[0]}

        with patch.object(self.commands, "_launchctl_loaded_state", side_effect=fake_state):
            with self.assertRaisesRegex(self.commands.ConfigError, "multiple domains"):
                self.commands._assert_legacy_mutators_unloaded()

    def test_version_copy_failure_removes_partial_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            candidate_id = json.loads((candidate / "manifest.json").read_text())["candidate_id"]
            with patch.object(self.commands, "_assert_legacy_mutators_unloaded", return_value={}), patch.object(
                self.commands, "_verify_candidate_copy", side_effect=self.commands.ConfigError("verification failed")
            ):
                with self.assertRaises(self.commands.ConfigError):
                    self.commands.deploy_fala(self.cfg, str(candidate), True, deployment_root=str(root))
            self.assertFalse((root / "versions" / candidate_id).exists())

    def test_promotion_boots_out_fala_before_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            calls: list[list[str]] = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                if argv[:2] == ["launchctl", "print"]:
                    return subprocess.CompletedProcess(argv, 1, "", "not loaded")
                if argv[:2] == ["plutil", "-lint"]:
                    return subprocess.CompletedProcess(argv, 0, "OK\n", "")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(self.commands.Path, "home", return_value=root / "home"), patch.object(
                self.commands, "_assert_legacy_mutators_unloaded", return_value={}
            ), patch.object(self.commands.subprocess, "run", side_effect=fake_run):
                self.commands.deploy_fala(self.cfg, str(candidate), True, deployment_root=str(root))
            bootouts = [i for i, call in enumerate(calls) if call[:2] == ["launchctl", "bootout"]]
            bootstraps = [i for i, call in enumerate(calls) if call[:2] == ["launchctl", "bootstrap"]]
            self.assertTrue(bootouts)
            self.assertLess(max(bootouts), min(bootstraps))


    def test_promotion_installs_version_local_runtime_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)

            before_manifest = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
            stable_identity = before_manifest["identity"]
            expected_candidate_id = before_manifest["candidate_id"]
            def fake_run(argv, **kwargs):
                if argv[:2] == ["launchctl", "print"]:
                    return subprocess.CompletedProcess(argv, 1, "", "not loaded")
                return subprocess.CompletedProcess(argv, 0, "OK\n", "")

            with patch.object(self.commands.Path, "home", return_value=root / "home"), patch.object(
                self.commands, "_assert_legacy_mutators_unloaded", return_value={}
            ), patch.object(self.commands.subprocess, "run", side_effect=fake_run):
                result = self.commands.deploy_fala(self.cfg, str(candidate), True, deployment_root=str(root))

            candidate_id = result["candidate_id"]
            version = root / "versions" / candidate_id
            installed = root / "home" / "Library" / "LaunchAgents" / "com.mikolaj92.hermes.repo-agent-fala-tick-all.plist"
            document = plistlib.loads(installed.read_bytes())
            arguments = document["ProgramArguments"]
            self.assertNotIn(str(root / "candidates"), " ".join(arguments))
            self.assertEqual(arguments[arguments.index("--project") + 1], str((version / "source" / "project").resolve()))
            self.assertEqual(arguments[arguments.index("--config") + 1], str((version / "source" / "config.toml").resolve()))
            import tools.deployment_parity as parity
            self.assertTrue(parity.validate_fala_candidate(version, deployment_root=root)["ok"])
            version_manifest = json.loads((version / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(version_manifest["candidate_id"], expected_candidate_id)
            self.assertEqual(version_manifest["identity"], stable_identity)
            self.assertTrue((root / "current").is_symlink())
            self.assertEqual((root / "current").resolve(), version.resolve())
            previous = json.loads((root / "previous.json").read_text(encoding="utf-8"))
            self.assertIsNone(previous["candidate_id"])
            self.assertEqual(version_manifest["runtime_identity"]["working_directory"], str((version / "source" / "project").resolve()))
            expected_log_dir = (root / "logs" / expected_candidate_id).resolve()
            self.assertEqual(Path(version_manifest["runtime_identity"]["standard_out_path"]).parent, expected_log_dir)
            self.assertEqual(Path(version_manifest["runtime_identity"]["standard_error_path"]).parent, expected_log_dir)
            self.assertEqual(version_manifest["runtime_identity"]["plist_sha256"], hashlib.sha256(installed.read_bytes()).hexdigest())
            plist_artifact = version_manifest["artifacts"]["launchd/com.mikolaj92.hermes.repo-agent-fala-tick-all.plist"]
            self.assertEqual(plist_artifact["sha256"], hashlib.sha256(installed.read_bytes()).hexdigest())
            self.assertEqual(plist_artifact["bytes"], installed.stat().st_size)
    def test_durability_failure_prevents_cutover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            calls: list[list[str]] = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                if argv[:2] == ["launchctl", "print"]:
                    return subprocess.CompletedProcess(argv, 1, "", "not loaded")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(self.commands, "_assert_legacy_mutators_unloaded", return_value={}), patch.object(
                self.commands, "_fsync_tree", side_effect=self.commands.ConfigError("version fsync failed")
            ), patch.object(self.commands.subprocess, "run", side_effect=fake_run):
                with self.assertRaisesRegex(self.commands.ConfigError, "version fsync failed"):
                    self.commands.deploy_fala(self.cfg, str(candidate), True, deployment_root=str(root))
            self.assertFalse((root / "current").exists())
            self.assertFalse(any(call[:2] == ["launchctl", "bootout"] for call in calls))

    def test_version_directory_fsync_failure_prevents_cutover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            versions_root = root / "versions"
            original_fsync_directory = self.commands._fsync_directory
            failed = False

            def fail_version_directory(path: Path) -> None:
                nonlocal failed
                if Path(path) == versions_root and not failed:
                    failed = True
                    raise self.commands.ConfigError("version directory fsync failed")
                original_fsync_directory(path)

            calls: list[list[str]] = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                if argv[:2] == ["launchctl", "print"]:
                    return subprocess.CompletedProcess(argv, 1, "", "not loaded")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(self.commands, "_assert_legacy_mutators_unloaded", return_value={}), patch.object(
                self.commands, "_fsync_directory", side_effect=fail_version_directory
            ), patch.object(self.commands.subprocess, "run", side_effect=fake_run):
                with self.assertRaisesRegex(self.commands.ConfigError, "version directory fsync failed"):
                    self.commands.deploy_fala(self.cfg, str(candidate), True, deployment_root=str(root))
            self.assertFalse((root / "current").exists())
            self.assertFalse(any(call[:2] == ["launchctl", "bootout"] for call in calls))
            candidate_id = json.loads((candidate / "manifest.json").read_text())["candidate_id"]
            self.assertFalse((versions_root / candidate_id).exists())

    def test_candidate_parent_directory_fsync_failure_cleans_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate_parent = root / "candidates"
            original_fsync_directory = self.commands._fsync_directory
            failed = False

            def fail_candidate_parent(path: Path) -> None:
                nonlocal failed
                if Path(path).name == "candidates" and not failed:
                    failed = True
                    raise self.commands.ConfigError("candidate parent fsync failed")
                original_fsync_directory(path)

            with patch.object(self.commands, "_fsync_directory", side_effect=fail_candidate_parent):
                with self.assertRaisesRegex(self.commands.ConfigError, "candidate parent fsync failed"):
                    self._render(root)
            self.assertFalse(any(candidate_parent.iterdir()) if candidate_parent.exists() else False)
        
    def test_cutover_directory_fsync_failure_prevents_launchctl_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            calls: list[list[str]] = []
            original_fsync_directory = self.commands._fsync_directory
            failed = False

            def fail_cutover(path: Path) -> None:
                nonlocal failed
                if Path(path) == root and not failed:
                    failed = True
                    raise self.commands.ConfigError("cutover directory fsync failed")
                original_fsync_directory(path)

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                if argv[:2] == ["launchctl", "print"]:
                    return subprocess.CompletedProcess(argv, 1, "", "not loaded")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(self.commands, "_assert_legacy_mutators_unloaded", return_value={}), patch.object(
                self.commands, "_fsync_directory", side_effect=fail_cutover
            ), patch.object(self.commands.subprocess, "run", side_effect=fake_run):
                with self.assertRaisesRegex(self.commands.ConfigError, "cutover directory fsync failed"):
                    self.commands.deploy_fala(self.cfg, str(candidate), True, deployment_root=str(root))
            self.assertFalse(any(call[:2] == ["launchctl", "bootstrap"] for call in calls))
            self.assertFalse((root / "current").exists())

    def test_existing_current_is_restored_after_cutover_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._render(root)

            def successful_run(argv, **kwargs):
                if argv[:2] == ["launchctl", "print"]:
                    return subprocess.CompletedProcess(argv, 1, "", "not loaded")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(self.commands.Path, "home", return_value=root / "home"), patch.object(
                self.commands, "_assert_legacy_mutators_unloaded", return_value={}
            ), patch.object(self.commands.subprocess, "run", side_effect=successful_run):
                self.commands.deploy_fala(self.cfg, str(first), True, deployment_root=str(root))
            old_target = (root / "current").resolve()
            other_config = root / "other.toml"
            second = self._render(root, config_path=other_config, db_path=root / "other.sqlite")

            def failing_run(argv, **kwargs):
                if argv[:2] == ["launchctl", "print"]:
                    return subprocess.CompletedProcess(argv, 1, "", "not loaded")
                if argv[:2] == ["launchctl", "bootstrap"]:
                    raise subprocess.CalledProcessError(1, argv)
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(self.commands.Path, "home", return_value=root / "home"), patch.object(
                self.commands, "_assert_legacy_mutators_unloaded", return_value={}
            ), patch.object(self.commands.subprocess, "run", side_effect=failing_run):
                with self.assertRaises(self.commands.ConfigError):
                    self.commands.deploy_fala(self.cfg, str(second), True, deployment_root=str(root))
            self.assertEqual((root / "current").resolve(), old_target)

    def test_render_and_candidate_independent_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            # Validation must not require the source checkout or candidates parent.
            import tools.deployment_parity as parity
            import json
            result = parity.validate_fala_candidate(candidate)
            manifest = json.loads(candidate.joinpath("manifest.json").read_text())
            self.assertEqual(result["candidate_id"], manifest["candidate_id"])
            self.assertTrue(result["ok"])

    def test_candidate_policy_is_required_and_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            import tools.deployment_parity as parity

            result = parity.validate_fala_candidate(candidate, deployment_root=root)
            self.assertTrue(result["ok"])
            manifest = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["policy"],
                {
                    "automerge": False,
                    "require_human_approval": True,
                    "require_checks": True,
                    "require_test_evidence": True,
                    "executor_enabled": False,
                },
            )

            # Make candidate mutable for tamper checks.
            for path in [candidate, *candidate.rglob("*")]:
                if path.is_dir():
                    path.chmod(0o755)
                elif path.is_file():
                    path.chmod(0o644)
            manifest["policy"]["automerge"] = True
            manifest["identity"]["policy"]["automerge"] = True
            (candidate / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            with self.assertRaises(parity.DeploymentParityError) as raised:
                parity.validate_fala_candidate(candidate, deployment_root=root)
            self.assertTrue(any("unsafe" in error for error in raised.exception.result["errors"]))

    def test_validate_fala_candidate_cli_skips_default_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            parser = self.module.commands.ArgumentParser(prog="oss-repo-agent")
            self.module.commands.setup_parser(parser)
            args = parser.parse_args([
                "validate-fala-candidate",
                "--candidate",
                str(candidate),
                "--deployment-root",
                str(root),
            ])
            with patch.object(self.module.commands, "load_config", side_effect=AssertionError("default config must not load")):
                result = self.module.commands.run_from_args(args)
            self.assertTrue(result["ok"])
            self.assertEqual(result["candidate_id"], candidate.name)

    def test_unmanifested_candidate_artifact_fails_closed(self):

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            source_dir = candidate / "source"
            source_dir.chmod(0o755)
            extra = source_dir / "unexpected.txt"
            extra.write_text("unmanifested\n", encoding="utf-8")
            extra.chmod(0o444)
            import tools.deployment_parity as parity

            with self.assertRaises(parity.DeploymentParityError) as raised:
                parity.validate_fala_candidate(candidate)
            self.assertTrue(any("unmanifested" in error for error in raised.exception.result["errors"]))

    def test_status_blocks_historical_unsafe_runs(self):
        import os
        import sqlite3
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "fala.sqlite"
            with sqlite3.connect(db_path) as db:
                db.executescript(
                    """
                    CREATE TABLE schema_migrations (id TEXT PRIMARY KEY, version INTEGER NOT NULL, name TEXT NOT NULL, applied_at TEXT NOT NULL);
                    INSERT INTO schema_migrations VALUES ('v6', 6, 'latest', '2020-01-01T00:00:00Z');
                    CREATE TABLE runs (id TEXT PRIMARY KEY, status TEXT NOT NULL, title TEXT, package_id TEXT, package_version TEXT, package_digest TEXT, correlation_path_id TEXT, correlation_path_digest TEXT, runtime_version TEXT, backend_version TEXT, schema_version INTEGER NOT NULL, metadata TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT);
                    CREATE TABLE processes (run_id TEXT NOT NULL, id TEXT NOT NULL, process_type TEXT NOT NULL, impulse_id TEXT, status TEXT NOT NULL, priority INTEGER NOT NULL, attempt INTEGER NOT NULL, max_attempts INTEGER NOT NULL, available_at TEXT NOT NULL, lease_owner TEXT, lease_expires_at TEXT, input_json TEXT NOT NULL, output_json TEXT NOT NULL, error_json TEXT NOT NULL, metadata TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, output_schema_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY (run_id, id));
                    """
                )
                for status in ("failed", "created", "cancel_requested"):
                    db.execute(
                        "INSERT INTO runs VALUES (?, ?, '', '', '', '', '', '', '', '', 6, ?, ?, ?, NULL, NULL)",
                        (f"old-{status}", status, '{"mode":"dry-run"}', "2000-01-01T00:00:00Z", "2000-01-01T00:00:00Z"),
                    )
                now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                db.execute(
                    "INSERT INTO runs VALUES (?, 'completed', '', '', '', '', '', '', '', '', 6, ?, ?, ?, NULL, NULL)",
                    ("latest", '{"mode":"dry-run"}', now, now),
                )
                db.commit()
            repos = root / "repos.txt"
            repos.write_text("\n", encoding="utf-8")
            env = os.environ.copy()
            env.update(
                {
                    "HERMES_REPO_AGENT_FALA_DB": str(db_path),
                    "HERMES_REPO_AGENT_FALA_REQUIRE_LIVE": "0",
                    "HERMES_REPO_AGENT_REPOS_FILE": str(repos),
                    "HERMES_REPO_AGENT_LOG_DIR": str(root / "logs"),
                    "HERMES_REPO_AGENT_DEPLOYMENT_ROOT": str(root / "deployment"),
                }
            )
            completed = subprocess.run(
                ["bash", str(ROOT / "scripts" / "repo_agent_status.sh")],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("unresolved-runs:", completed.stdout)
            self.assertIn("old-failed", completed.stdout)
            self.assertIn("old-created", completed.stdout)
            self.assertIn("old-cancel_requested", completed.stdout)

    def test_promotion_bootout_restores_previously_unloaded_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            home = root / "home"
            calls: list[list[str]] = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                if argv[:2] == ["launchctl", "print"]:
                    return subprocess.CompletedProcess(argv, 1, "", "not loaded")
                if argv[:2] == ["plutil", "-lint"]:
                    return subprocess.CompletedProcess(argv, 0, "OK\n", "")
                if argv[:2] == ["launchctl", "bootstrap"]:
                    raise subprocess.CalledProcessError(1, argv)
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(self.commands.Path, "home", return_value=home), patch.object(
                self.commands.subprocess, "run", side_effect=fake_run
            ):
                with self.assertRaises(self.commands.ConfigError):
                    self.commands.deploy_fala(
                        self.cfg,
                        str(candidate),
                        True,
                        deployment_root=str(root),
                    )

            self.assertTrue(any(call[:2] == ["launchctl", "print"] for call in calls))
            self.assertTrue(any(call[:2] == ["launchctl", "bootout"] for call in calls))
            self.assertFalse((root / "current").exists())
            self.assertFalse((home / "Library" / "LaunchAgents" / "com.mikolaj92.hermes.repo-agent-fala-tick-all.plist").exists())


if __name__ == "__main__":
    unittest.main()
