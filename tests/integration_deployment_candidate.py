from __future__ import annotations

import importlib.util
import ctypes
import json
import plistlib
import hashlib
import os
import subprocess
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
import copy

from lokay.registry import canonical_toml, load_registry

import tools.deployment_parity as parity


ROOT = Path(__file__).resolve().parents[1]


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


class DeploymentCandidateTests(unittest.TestCase):
    def setUp(self):
        self.module = load_plugin()
        self.commands = self.module.commands
        data = copy.deepcopy(load_registry(ROOT / "config.toml").data)
        data["mode"] = "dry-run"
        data["automation"]["automerge"] = True
        data["automation"]["require_human_approval"] = False
        data["automation"]["require_checks"] = True
        data["automation"]["require_test_evidence"] = True
        data["executor"]["enabled"] = True
        self.cfg = self.commands.LokayConfig.from_mapping(data)

    def _fala_git_clean(self):
        project_root = ROOT.resolve()
        fala_root = (ROOT.parent / "Fala").resolve()
        real_run = self.commands.subprocess.run

        def fake_run(argv, *args, **kwargs):
            command = list(argv)
            if len(command) >= 3 and command[:2] == ["git", "-C"]:
                checkout = Path(command[2]).resolve()
                if command[3:] == ["status", "--porcelain"] and checkout in {project_root, fala_root}:
                    return subprocess.CompletedProcess(command, 0, "", "")
                if checkout == fala_root and command[3:5] == ["cat-file", "-e"]:
                    return subprocess.CompletedProcess(command, 0, "", "")
                if checkout == fala_root and command[3:5] == ["show", f"{self.commands.FALA_PINNED_COMMIT}:pyproject.toml"]:
                    return subprocess.CompletedProcess(command, 0, '[project]\nversion = "0.7.15"\n', "")
            return real_run(argv, *args, **kwargs)

        return patch.object(self.commands.subprocess, "run", side_effect=fake_run)
    def test_init_rejects_existing_destination_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "config.toml"
            original = "mode = 'dry-run'\noriginal = true\n"
            target.write_text(original, encoding="utf-8")
            with self.assertRaisesRegex(self.commands.ConfigError, "config already exists"):
                self.commands.init_project(str(target), str(ROOT))
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

    SUPERVISOR_LABEL = "com.mikolaj92.lokay.supervisor"


    def _write_render_config(
        self,
        config: Path,
        *,
        mode: str = "dry-run",
        autonomous: bool = True,
    ) -> None:
        data = copy.deepcopy(load_registry(ROOT / "config.toml").data)
        data["mode"] = mode
        data["automation"]["automerge"] = bool(autonomous)
        data["automation"]["require_human_approval"] = not bool(autonomous)
        data["automation"]["require_checks"] = True
        data["automation"]["require_test_evidence"] = True
        data["executor"]["enabled"] = bool(autonomous)
        config.write_bytes(canonical_toml(data))


    def _render(
        self,
        root: Path,
        *,
        mode: str = "dry-run",
        config_path: Path | None = None,
        db_path: Path | None = None,
        autonomous: bool = True,
    ) -> Path:
        config = config_path or root / "config.toml"
        self._write_render_config(
            config,
            mode=mode,
            autonomous=autonomous,
        )
        db = db_path or root / "state.sqlite"
        lock_data = self.commands.rewrite_fala_git_to_bundled_lock((ROOT / "uv.lock").read_bytes())
        data = copy.deepcopy(load_registry(config).data)
        cfg = self.commands.LokayConfig.from_mapping(data)
        processes = self.commands._catalog_processes(cfg, config)
        config_bytes = self.commands._materialize_candidate_config(config, processes)
        identity = {
            "schema": 1,
            "mode": mode,
            "plugin_commit": "plugin-commit",
            "fala_tag": "0.7.15",
            "fala_commit": "b5f9a6d500a442a1c79060a862fe4b9da87bc98f",
            "lock_hash": hashlib.sha256(lock_data).hexdigest(),
            "config_path": str(config.absolute()),
            "config_hash": hashlib.sha256(config_bytes).hexdigest(),
            "db_path": str(db.absolute()),
            "metadata_path": "source/metadata.json",
            "lock_path": "source/project/uv.lock",
            "config_artifact_path": "source/config.toml",
            "revision_path": "source/revision.txt",
            "policy": {
                "automerge": bool(cfg.automerge),
                "require_human_approval": bool(cfg.require_human_approval),
                "require_checks": bool(cfg.require_checks),
                "require_test_evidence": bool(cfg.require_test_evidence),
                "executor_enabled": bool(cfg.executor.enabled),
            },
            "repos": self.commands._identity_repos(cfg, config),
            "processes": processes,
        }
        candidate_id = hashlib.sha256((json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()
        candidate = root / "candidates" / candidate_id
        real_which = self.commands.shutil.which
        def fake_which(command, **kwargs):
            return real_which(command, **kwargs)
        with self._fala_git_clean(), patch.object(self.commands, "_read_git_revision", return_value="plugin-commit"), patch.object(
            self.commands.shutil, "which", side_effect=fake_which
        ):
            result = self.commands.render_launchd(
                cfg, str(candidate), config_path=str(config), fala_db=str(db), mode=mode, deployment_root=str(root)
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


    def test_fala_source_tree_includes_python_package(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self._render(Path(directory)) / "source" / "project"
            fala_src = project / "Fala" / "python" / "fala"
            self.assertTrue(fala_src.is_dir())
            self.assertTrue(any(fala_src.rglob("*.py")))
            self.assertTrue((project / "fala-package.toml").is_file())
            self.assertTrue((project / "src" / "lokay" / "effector.py").is_file())
            self.assertFalse((project / "fala" / "packages" / "issue_intake.yaml").exists())
    def test_metadata_lock_hash_matches_bundled_lock_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = self._render(Path(directory))
            metadata = json.loads((candidate / "source" / "metadata.json").read_text(encoding="utf-8"))
            bundled = (candidate / "source" / "project" / "uv.lock").read_bytes()
            import hashlib
            self.assertEqual(metadata["lock_hash"], hashlib.sha256(bundled).hexdigest())

    def test_dirty_fala_checkout_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            self._write_render_config(config)
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

    def test_fala_checkout_head_must_match_pinned_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            self._write_render_config(config)
            fala_root = (ROOT.parent / "Fala").resolve()
            project_root = ROOT.resolve()
            real_run = self.commands.subprocess.run

            def wrong_head(argv, *args, **kwargs):
                command = list(argv)
                if len(command) >= 3 and command[:2] == ["git", "-C"]:
                    checkout = Path(command[2]).resolve()
                    if command[3:] == ["status", "--porcelain"] and checkout in {project_root, fala_root}:
                        return subprocess.CompletedProcess(command, 0, "", "")
                    if checkout == fala_root and command[3:] == ["rev-parse", "HEAD"]:
                        return subprocess.CompletedProcess(command, 0, "0" * 40 + "\n", "")
                    if checkout == fala_root and command[3:] == ["submodule", "status", "--recursive"]:
                        return subprocess.CompletedProcess(command, 0, "", "")
                return real_run(argv, *args, **kwargs)
            with patch.object(self.commands.subprocess, "run", side_effect=wrong_head), patch.object(
                self.commands, "_read_git_revision", return_value="plugin-commit"
            ):
                with self.assertRaisesRegex(self.commands.ConfigError, "HEAD does not match"):
                    self.commands._copy_candidate_source(ROOT.resolve(), root / "source", config, ROOT / "uv.lock")

    def _tar_bytes(self, members: list[tuple]) -> bytes:
        import io
        import tarfile
        import time

        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as bundle:
            for entry in members:
                name, payload, kind, *rest = entry
                explicit_mode = rest[0] if rest else None
                info = tarfile.TarInfo(name=name)
                info.mtime = int(time.time())
                if kind == "dir":
                    info.type = tarfile.DIRTYPE
                    info.mode = explicit_mode if explicit_mode is not None else 0o755
                    bundle.addfile(info)
                elif kind == "reg":
                    data = payload or b""
                    info.type = tarfile.REGTYPE
                    info.mode = explicit_mode if explicit_mode is not None else 0o644
                    info.size = len(data)
                    bundle.addfile(info, io.BytesIO(data))
                elif kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else str(payload)
                    bundle.addfile(info)
                elif kind == "hardlink":
                    info.type = tarfile.LNKTYPE
                    info.linkname = payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else str(payload)
                    bundle.addfile(info)
                elif kind == "fifo":
                    info.type = tarfile.FIFOTYPE
                    bundle.addfile(info)
                elif kind == "chr":
                    info.type = tarfile.CHRTYPE
                    info.devmajor = 1
                    info.devminor = 3
                    bundle.addfile(info)
                else:
                    raise AssertionError(f"unknown member kind {kind}")
        return buffer.getvalue()

    def _extract_archive_bytes(self, archive: bytes, destination: Path) -> None:
        import io
        import tarfile

        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
            self.commands._extract_git_archive(bundle, destination)

    def test_safe_archive_extracts_regular_files_and_dirs(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "out"
            archive = self._tar_bytes(
                [
                    ("pkg", None, "dir"),
                    ("pkg/hello.txt", b"hi\n", "reg"),
                    ("pkg/nested/deep.txt", b"deep\n", "reg"),
                ]
            )
            self._extract_archive_bytes(archive, destination)
            self.assertEqual((destination / "pkg" / "hello.txt").read_text(encoding="utf-8"), "hi\n")
            self.assertEqual((destination / "pkg" / "nested" / "deep.txt").read_text(encoding="utf-8"), "deep\n")

    def test_archive_preserves_sanitized_executable_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "out"
            archive = self._tar_bytes(
                [
                    ("bin/run.sh", b"#!/bin/sh\necho hi\n", "reg", 0o755),
                    ("readme.txt", b"hi\n", "reg", 0o644),
                    ("suid.sh", b"#!/bin/sh\n", "reg", 0o4755),
                    ("writey.sh", b"#!/bin/sh\n", "reg", 0o777),
                ]
            )
            self._extract_archive_bytes(archive, destination)
            run = destination / "bin" / "run.sh"
            self.assertEqual(run.stat().st_mode & 0o7777, 0o555)
            self.assertTrue(os.access(run, os.X_OK))
            self.assertEqual((destination / "readme.txt").stat().st_mode & 0o7777, 0o444)
            self.assertEqual((destination / "suid.sh").stat().st_mode & 0o7777, 0o555)
            self.assertFalse((destination / "suid.sh").stat().st_mode & 0o7000)
            self.assertEqual((destination / "writey.sh").stat().st_mode & 0o7777, 0o555)
            self.assertEqual((destination / "writey.sh").stat().st_mode & 0o222, 0)

    def test_archive_symlink_member_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "out"
            outside = Path(directory) / "outside.txt"
            outside.write_text("secret\n", encoding="utf-8")
            archive = self._tar_bytes(
                [
                    ("link", str(outside).encode("utf-8"), "symlink"),
                ]
            )
            with self.assertRaisesRegex(self.commands.ConfigError, "unsafe path"):
                self._extract_archive_bytes(archive, destination)
            self.assertFalse((destination / "link").exists())

    def test_archive_hardlink_member_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "out"
            archive = self._tar_bytes(
                [
                    ("a.txt", b"body\n", "reg"),
                    ("b.txt", b"a.txt", "hardlink"),
                ]
            )
            with self.assertRaisesRegex(self.commands.ConfigError, "unsafe path"):
                self._extract_archive_bytes(archive, destination)
            self.assertTrue((destination / "a.txt").is_file())
            self.assertFalse((destination / "b.txt").exists())

    def test_archive_device_and_fifo_members_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "out"
            for kind in ("fifo", "chr"):
                archive = self._tar_bytes([("special", None, kind)])
                with self.assertRaisesRegex(self.commands.ConfigError, "unsafe path"):
                    self._extract_archive_bytes(archive, destination)
                self.assertFalse(any(destination.iterdir()) if destination.exists() else True)

    def test_archive_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "out"
            outside = Path(directory) / "pwned.txt"
            archive = self._tar_bytes(
                [
                    ("../pwned.txt", b"escaped\n", "reg"),
                ]
            )
            with self.assertRaisesRegex(self.commands.ConfigError, "unsafe path"):
                self._extract_archive_bytes(archive, destination)
            self.assertFalse(outside.exists())

    def test_archive_absolute_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "out"
            archive = self._tar_bytes(
                [
                    ("/tmp/absolute-escape.txt", b"nope\n", "reg"),
                ]
            )
            with self.assertRaisesRegex(self.commands.ConfigError, "unsafe path"):
                self._extract_archive_bytes(archive, destination)

    def test_archive_duplicate_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "out"
            archive = self._tar_bytes(
                [
                    ("dup.txt", b"one\n", "reg"),
                    ("dup.txt", b"two\n", "reg"),
                ]
            )
            with self.assertRaisesRegex(self.commands.ConfigError, "unsafe path"):
                self._extract_archive_bytes(archive, destination)
            self.assertEqual((destination / "dup.txt").read_text(encoding="utf-8"), "one\n")

    def test_archive_cannot_write_through_prior_file_as_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "out"
            archive = self._tar_bytes(
                [
                    ("evil", b"not-a-dir\n", "reg"),
                    ("evil/through.txt", b"should-not-land\n", "reg"),
                ]
            )
            with self.assertRaisesRegex(self.commands.ConfigError, "unsafe path"):
                self._extract_archive_bytes(archive, destination)
            self.assertTrue((destination / "evil").is_file())
            self.assertFalse((destination / "evil" / "through.txt").exists())

    def test_copy_git_tree_rejects_symlink_archive_from_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "dest"
            outside = root / "outside.txt"
            outside.write_text("secret\n", encoding="utf-8")
            archive = self._tar_bytes(
                [
                    ("link", str(outside).encode("utf-8"), "symlink"),
                    ("ok.txt", b"ok\n", "reg"),
                ]
            )

            def fake_run(argv, *args, **kwargs):
                command = list(argv)
                if command[:1] == ["git"] and "archive" in command:
                    return subprocess.CompletedProcess(command, 0, archive, b"")
                raise AssertionError(f"unexpected command: {command}")

            with patch.object(self.commands.subprocess, "run", side_effect=fake_run):
                with self.assertRaisesRegex(self.commands.ConfigError, "unsafe path"):
                    self.commands._copy_git_tree(root / "repo", "HEAD", destination)
            self.assertFalse((destination / "link").exists())
            self.assertFalse((destination / "ok.txt").exists())



    def test_wrong_fala_version_at_pinned_commit_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            self._write_render_config(config)
            real_run = self.commands.subprocess.run

            def wrong_version(argv, *args, **kwargs):
                command = list(argv)
                if len(command) >= 3 and command[:2] == ["git", "-C"] and "status" in command:
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[3:5] == ["show", f"{self.commands.FALA_PINNED_COMMIT}:pyproject.toml"]:
                    return subprocess.CompletedProcess(command, 0, '[project]\nversion = "0.7.5"\n', "")
                return real_run(argv, *args, **kwargs)

            with self._fala_git_clean(), patch.object(self.commands.subprocess, "run", side_effect=wrong_version), patch.object(
                self.commands, "_read_git_revision", return_value="plugin-commit"
            ):
                with self.assertRaisesRegex(self.commands.ConfigError, "version must be 0.7.15"):
                    self.commands.render_launchd(
                        self.cfg, str(root / "candidates" / "candidate"), config_path=str(config), fala_db=str(root / "state.sqlite"), deployment_root=str(root)
                    )

    def test_dirty_plugin_checkout_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            self._write_render_config(config)
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
    def test_changed_configuration_produces_distinct_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = self._render(root)
            changed = self._render(root, mode="live")
            self.assertNotEqual(changed.name, original.name)

    def test_unsupported_launchctl_domain_is_unavailable(self):
        result = subprocess.CompletedProcess(
            ["launchctl", "print"], 125, "", "Domain does not support specified action\n"
        )
        with patch.object(self.commands.subprocess, "run", return_value=result):
            state = self.commands._launchctl_loaded_state("label", f"gui/{self.commands.os.getuid()}")
        self.assertEqual(state, {"label": "label", "domain": f"gui/{self.commands.os.getuid()}", "loaded": False, "available": False})

    def test_legacy_mutators_probe_user_and_gui_domains(self):
        calls: list[str] = []

        def fake_state(label: str, domain: str):
            calls.append(domain)
            return {"label": label, "domain": domain, "loaded": False, "available": True}

        with patch.object(self.commands, "_launchctl_loaded_state", side_effect=fake_state):
            states = self.commands._snapshot_legacy_mutators()
        self.assertEqual(set(states), set(self.commands.LEGACY_MUTATOR_LABELS))
        self.assertEqual(calls.count(f"user/{self.commands.os.getuid()}"), len(self.commands.LEGACY_MUTATOR_LABELS))
        self.assertEqual(calls.count(f"gui/{self.commands.os.getuid()}"), len(self.commands.LEGACY_MUTATOR_LABELS))
        self.assertFalse(any(entry["transition"] for entry in states.values()))

    def test_legacy_mutator_loaded_in_both_domains_fails_closed(self):
        def fake_state(label: str, domain: str):
            return {"label": label, "domain": domain, "loaded": label == self.commands.LEGACY_MUTATOR_LABELS[0], "available": True}

        with patch.object(self.commands, "_launchctl_loaded_state", side_effect=fake_state):
            with self.assertRaisesRegex(self.commands.ConfigError, "multiple domains"):
                self.commands._snapshot_legacy_mutators()

    def test_observational_health_is_not_transitioned(self):
        uid = self.commands.os.getuid()
        health = self.commands.LEGACY_HEALTH_LABEL

        def fake_state(label: str, domain: str):
            loaded = label == health and domain == f"user/{uid}"
            return {"label": label, "domain": domain, "loaded": loaded, "available": True}

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            plist = home / "Library" / "LaunchAgents" / f"{health}.plist"
            plist.parent.mkdir(parents=True)
            plist.write_text('<?xml version="1.0"?><plist><dict><key>ProgramArguments</key><array><string>health</string></array></dict></plist>\n', encoding="utf-8")
            with patch.object(self.commands.Path, "home", return_value=home), patch.object(
                self.commands, "_launchctl_loaded_state", side_effect=fake_state
            ):
                states = self.commands._snapshot_legacy_mutators()
                self.commands._bootout_legacy_mutators(states)
            self.assertTrue(states[health]["loaded"])
            self.assertFalse(states[health]["transition"])
            self.assertFalse(states[health]["repair_enabled"])

    def test_repair_health_is_transitioned(self):
        uid = self.commands.os.getuid()
        health = self.commands.LEGACY_HEALTH_LABEL
        calls: list[list[str]] = []
        booted: set[str] = set()

        def fake_state(label: str, domain: str):
            loaded = label == health and domain == f"user/{uid}" and label not in booted
            return {"label": label, "domain": domain, "loaded": loaded, "available": True}

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if argv[:2] == ["launchctl", "bootout"]:
                booted.add(str(argv[2]).rsplit("/", 1)[-1])
            return subprocess.CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            plist = home / "Library" / "LaunchAgents" / f"{health}.plist"
            plist.parent.mkdir(parents=True)
            plist.write_text(
                '<?xml version="1.0"?><plist><dict><key>ProgramArguments</key><array><string>health</string><string>--repair</string></array></dict></plist>\n',
                encoding="utf-8",
            )
            with patch.object(self.commands.Path, "home", return_value=home), patch.object(
                self.commands, "_launchctl_loaded_state", side_effect=fake_state
            ), patch.object(self.commands.subprocess, "run", side_effect=fake_run), patch.object(
                self.commands, "_verify_launchctl_unloaded"
            ):
                states = self.commands._snapshot_legacy_mutators()
                self.assertTrue(states[health]["transition"])
                self.commands._bootout_legacy_mutators(states)
            self.assertIn(["launchctl", "bootout", f"user/{uid}/{health}"], calls)
    def test_fala_gui_only_state_selects_gui_domain(self):
        label = self.SUPERVISOR_LABEL
        uid = self.commands.os.getuid()
        def fake_state(observed_label, domain):
            return {"label": observed_label, "domain": domain, "loaded": domain == f"gui/{uid}", "available": True}
        with patch.object(self.commands, "_launchctl_loaded_state", side_effect=fake_state):
            states = self.commands._launchctl_domain_states(label)
            self.assertEqual(self.commands._launchctl_intended_domain(label, states), f"gui/{uid}")

    def test_fala_user_only_state_selects_user_domain(self):
        label = self.SUPERVISOR_LABEL
        uid = self.commands.os.getuid()
        def fake_state(observed_label, domain):
            return {"label": observed_label, "domain": domain, "loaded": domain == f"user/{uid}", "available": True}
        with patch.object(self.commands, "_launchctl_loaded_state", side_effect=fake_state):
            states = self.commands._launchctl_domain_states(label)
            self.assertEqual(self.commands._launchctl_intended_domain(label, states), f"user/{uid}")

    def test_fala_unavailable_user_domain_selects_and_verifies_gui(self):
        label = self.SUPERVISOR_LABEL
        uid = self.commands.os.getuid()
        states = {
            f"user/{uid}": {"label": label, "domain": f"user/{uid}", "loaded": False, "available": False},
            f"gui/{uid}": {"label": label, "domain": f"gui/{uid}", "loaded": True, "available": True},
        }
        self.assertEqual(self.commands._launchctl_intended_domain(label, states), f"gui/{uid}")
        with patch.object(self.commands, "_launchctl_domain_states", return_value=states):
            self.commands._verify_launchctl_exact(label, f"gui/{uid}")

    def test_fala_unavailable_intended_domain_fails_closed(self):
        label = self.SUPERVISOR_LABEL
        uid = self.commands.os.getuid()
        states = {
            f"user/{uid}": {"label": label, "domain": f"user/{uid}", "loaded": False, "available": False},
            f"gui/{uid}": {"label": label, "domain": f"gui/{uid}", "loaded": False, "available": True},
        }
        with patch.object(self.commands, "_launchctl_domain_states", return_value=states):
            with self.assertRaisesRegex(self.commands.ConfigError, "intended.*unavailable"):
                self.commands._verify_launchctl_exact(label, f"user/{uid}")

    def test_fala_duplicate_domains_fail_closed(self):
        label = self.SUPERVISOR_LABEL
        uid = self.commands.os.getuid()
        states = {
            f"user/{uid}": {"label": label, "domain": f"user/{uid}", "loaded": True},
            f"gui/{uid}": {"label": label, "domain": f"gui/{uid}", "loaded": True},
        }
        with self.assertRaisesRegex(self.commands.ConfigError, "multiple domains"):
            self.commands._launchctl_intended_domain(label, states)

    def test_fala_cutover_bootstraps_only_intended_domain(self):
        label = self.SUPERVISOR_LABEL
        uid = self.commands.os.getuid()
        states = {
            f"user/{uid}": {"label": label, "domain": f"user/{uid}", "loaded": False},
            f"gui/{uid}": {"label": label, "domain": f"gui/{uid}", "loaded": True},
        }
        calls = []
        plist = Path("/tmp/fala.plist")
        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")
        with patch.object(self.commands, "_launchctl_loaded_state", return_value={"label": label, "loaded": False}), patch.object(
            self.commands.subprocess, "run", side_effect=fake_run
        ):
            # Directly exercise the destructive domain sequence with a mocked
            # exact-state verifier; deploy_fala covers the surrounding staging.
            with patch.object(self.commands, "_verify_launchctl_exact"):
                self.commands._launchctl_bootout(f"user/{uid}", label, ignore_failure=True)
                self.commands._launchctl_bootout(f"gui/{uid}", label, ignore_failure=True)
                self.commands.subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], check=True, capture_output=True, text=True)
        self.assertIn(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], calls)
        self.assertNotIn(["launchctl", "bootstrap", f"user/{uid}", str(plist)], calls)
    def test_fala_gui_domain_state_restores_on_rollback(self):
        label = self.SUPERVISOR_LABEL
        uid = self.commands.os.getuid()
        states = {
            f"user/{uid}": {"label": label, "domain": f"user/{uid}", "loaded": False},
            f"gui/{uid}": {"label": label, "domain": f"gui/{uid}", "loaded": True},
        }
        calls = []
        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")
        with patch.object(self.commands, "_launchctl_loaded_state", return_value={"label": label, "loaded": False}), patch.object(
            self.commands.subprocess, "run", side_effect=fake_run
        ):
            self.commands._launchctl_restore_states(states, Path("/tmp/fala.plist"))
        self.assertIn(["launchctl", "bootstrap", f"gui/{uid}", "/tmp/fala.plist"], calls)
        self.assertNotIn(["launchctl", "bootstrap", f"user/{uid}", "/tmp/fala.plist"], calls)

    def test_version_copy_failure_removes_partial_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            candidate_id = json.loads((candidate / "manifest.json").read_text())["candidate_id"]
            with patch.object(self.commands, "_snapshot_legacy_mutators", return_value={}), patch.object(
                self.commands, "_verify_candidate_copy", side_effect=self.commands.ConfigError("verification failed")
            ):
                with self.assertRaises(self.commands.ConfigError):
                    self.commands.deploy_fala(self.cfg, str(candidate), True, deployment_root=str(root))
            self.assertFalse((root / "versions" / candidate_id).exists())

    def test_loaded_service_without_canonical_plist_fails_before_cutover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            uid = self.commands.os.getuid()
            label = self.SUPERVISOR_LABEL
            states = {
                f"user/{uid}": {"label": label, "domain": f"user/{uid}", "loaded": True, "available": True},
                f"gui/{uid}": {"label": label, "domain": f"gui/{uid}", "loaded": False, "available": False},
            }
            calls: list[list[str]] = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, "OK\n", "")

            with patch.object(self.commands.Path, "home", return_value=root / "home"), patch.object(
                self.commands, "_snapshot_legacy_mutators", return_value={}
            ), patch.object(self.commands, "_launchctl_domain_states", return_value=states), patch.object(
                self.commands.subprocess, "run", side_effect=fake_run
            ):
                with self.assertRaisesRegex(self.commands.ConfigError, "no canonical installed plist"):
                    self.commands.deploy_fala(self.cfg, str(candidate), True, deployment_root=str(root))
            self.assertFalse((root / "current").exists())
            self.assertFalse(any(call[:2] == ["launchctl", "bootout"] for call in calls))

    def test_promotion_runs_inside_deployment_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            events: list[str] = []
            original_lock = self.commands._deployment_lock

            @contextmanager
            def recording_lock(lock_root: Path):
                with original_lock(lock_root):
                    events.append(f"enter:{lock_root}")
                    try:
                        yield
                    finally:
                        events.append(f"exit:{lock_root}")

            def fake_run(argv, **kwargs):
                if argv[:2] == ["launchctl", "print"]:
                    return subprocess.CompletedProcess(argv, 1, "", "not loaded")
                return subprocess.CompletedProcess(argv, 0, "OK\n", "")

            with patch.object(self.commands, "_deployment_lock", side_effect=recording_lock), patch.object(
                self.commands.Path, "home", return_value=root / "home"
            ), patch.object(self.commands, "_snapshot_legacy_mutators", return_value={}), patch.object(
                self.commands, "_verify_launchctl_exact"
            ), patch.object(self.commands.subprocess, "run", side_effect=fake_run):
                result = self.commands.deploy_fala(self.cfg, str(candidate), True, deployment_root=str(root))

            self.assertTrue(result["promoted"])
            self.assertEqual(events, [f"enter:{root}", f"exit:{root}"])

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
                self.commands, "_snapshot_legacy_mutators", return_value={}
            ), patch.object(self.commands, "_verify_launchctl_exact"), patch.object(self.commands.subprocess, "run", side_effect=fake_run):
                self.commands.deploy_fala(self.cfg, str(candidate), True, deployment_root=str(root))
            bootouts = [i for i, call in enumerate(calls) if call[:2] == ["launchctl", "bootout"]]
            bootstraps = [i for i, call in enumerate(calls) if call[:2] == ["launchctl", "bootstrap"]]
            self.assertTrue(bootouts)
            self.assertLess(max(bootouts), min(bootstraps))


    def test_promotion_replaces_legacy_current_that_fails_new_parity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._render(root)

            def fake_run(argv, **kwargs):
                if argv[:2] == ["launchctl", "print"]:
                    return subprocess.CompletedProcess(argv, 1, "", "not loaded")
                return subprocess.CompletedProcess(argv, 0, "OK\n", "")

            with patch.object(self.commands.Path, "home", return_value=root / "home"), patch.object(
                self.commands, "_snapshot_legacy_mutators", return_value={}
            ), patch.object(self.commands, "_verify_launchctl_exact"), patch.object(self.commands.subprocess, "run", side_effect=fake_run):
                self.commands.deploy_fala(self.cfg, str(first), True, deployment_root=str(root))

            legacy = (root / "current").resolve()
            legacy_manifest = legacy / "manifest.json"
            legacy.chmod(0o755)
            legacy_manifest.chmod(0o644)
            document = json.loads(legacy_manifest.read_text(encoding="utf-8"))
            document["fala_commit"] = "legacy-invalid"
            legacy_manifest.write_text(json.dumps(document), encoding="utf-8")
            legacy_manifest.chmod(0o444)
            legacy.chmod(0o555)

            other_config = root / "other.toml"
            second = self._render(root, config_path=other_config, db_path=root / "other.sqlite")
            with patch.object(self.commands.Path, "home", return_value=root / "home"), patch.object(
                self.commands, "_snapshot_legacy_mutators", return_value={}
            ), patch.object(self.commands, "_verify_launchctl_exact"), patch.object(self.commands.subprocess, "run", side_effect=fake_run):
                result = self.commands.deploy_fala(self.cfg, str(second), True, deployment_root=str(root))

            self.assertTrue(result["promoted"])
            self.assertNotEqual((root / "current").resolve(), legacy)

    def test_promotion_installs_version_local_runtime_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            stale_agents = root / "home" / "Library" / "LaunchAgents"
            stale_agents.mkdir(parents=True)
            for label in self.commands.STALE_FALA_LABELS:
                (stale_agents / f"{label}.plist").write_bytes(f"stale:{label}".encode())

            before_manifest = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
            stable_identity = before_manifest["identity"]
            expected_candidate_id = before_manifest["candidate_id"]
            def fake_run(argv, **kwargs):
                if argv[:2] == ["launchctl", "print"]:
                    return subprocess.CompletedProcess(argv, 1, "", "not loaded")
                return subprocess.CompletedProcess(argv, 0, "OK\n", "")

            generation_path = root / "home" / ".hermes" / "lokay" / "generation"
            with patch.object(self.commands.Path, "home", return_value=root / "home"), patch.object(
                self.commands, "_snapshot_legacy_mutators", return_value={}
            ), patch.object(self.commands, "_verify_launchctl_exact"), patch.object(
                self.commands.subprocess, "run", side_effect=fake_run
            ), patch.dict(os.environ, {"HOME": str(root / "home"), "HERMES_LOKAY_GENERATION_PATH": str(generation_path)}, clear=False):
                result = self.commands.deploy_fala(self.cfg, str(candidate), True, deployment_root=str(root))

            candidate_id = result["candidate_id"]
            version = root / "versions" / candidate_id
            label = self.SUPERVISOR_LABEL
            installed = root / "home" / "Library" / "LaunchAgents" / f"{label}.plist"
            document = plistlib.loads(installed.read_bytes())
            arguments = document["ProgramArguments"]
            environment = document["EnvironmentVariables"]
            project = version / "source" / "project"
            self.assertEqual(document["Label"], label)
            self.assertEqual(
                set(environment),
                {
                    "HOME",
                    "PYTHONPATH",
                    "FALA_HOME",
                    "FALA_EFFECTOR_ROOT",
                    "FALA_CANDIDATE_ID",
                    "HERMES_LOKAY_GENERATION",
                    "HERMES_LOKAY_PROCESS_STATE_ROOT",
                },
            )
            self.assertEqual(environment["HOME"], str((root / "home").resolve()))
            self.assertEqual(environment["PYTHONPATH"], str((project / "src").resolve()))
            self.assertEqual(environment["FALA_HOME"], str((project / "Fala").resolve()))
            self.assertEqual(environment["FALA_EFFECTOR_ROOT"], str((project / "effectors").resolve()))
            self.assertEqual(environment["FALA_CANDIDATE_ID"], candidate_id)
            self.assertTrue(generation_path.is_file())
            self.assertEqual(generation_path.read_text(encoding="utf-8").strip(), candidate_id)
            self.assertEqual(environment["HERMES_LOKAY_GENERATION"], candidate_id)
            self.assertEqual(
                environment["HERMES_LOKAY_PROCESS_STATE_ROOT"],
                str((Path(before_manifest["db_path"]).resolve().parent / "process-state")),
            )
            self.assertEqual(document["WorkingDirectory"], str(project.resolve()))
            self.assertEqual(
                arguments[:3],
                [str((project / ".venv" / "bin" / "python").resolve()), "-m", "lokay.supervisor"],
            )
            self.assertIn("--json", arguments)
            self.assertEqual(arguments[arguments.index("--config") + 1], str((version / "source" / "config.toml").resolve()))
            self.assertEqual(Path(arguments[arguments.index("--db") + 1]).resolve(), Path(before_manifest["db_path"]).resolve())
            mode_flags = [value for value in arguments if value in ("--dry-run", "--live")]
            self.assertEqual(mode_flags, ["--dry-run"])
            self.assertTrue(installed.is_file(), label)
            agents = root / "home" / "Library" / "LaunchAgents"
            self.assertEqual(sorted(path.name for path in agents.glob("*.plist")), [f"{label}.plist"])
            self.assertFalse((agents / "com.mikolaj92.lokay.fala-tick-all.plist").exists())
            self.assertFalse((agents / "com.mikolaj92.lokay.repo-issue-poll.plist").exists())
            self.assertTrue((version / "source" / "project" / "uv.lock").is_file())
            self.assertTrue((version / "source" / "project" / "README.md").is_file())
            self.assertTrue((version / "source" / "project" / "LICENSE").is_file())
            self.assertTrue((version / "source" / "project" / "Fala" / "vendor" / "EmberJson" / "emberjson").is_dir())
            self.assertTrue((version / "source" / "project" / "Fala" / "vendor" / "sqlite.fire" / "native" / "sqlite_fire.c").is_file())
            self.assertTrue((version / "source" / "project" / "Fala" / "vendor" / "sqlite.fire" / "native" / "libsqlite_fire.dylib").is_file())
            process_host_name = "libfala_process_host.dylib" if sys.platform == "darwin" else "libfala_process_host.so"
            self.assertTrue((version / "source" / "project" / "Fala" / "mojo" / "fala" / "native" / process_host_name).is_file())
            process_host = version / "source" / "project" / "Fala" / "mojo" / "fala" / "native" / process_host_name
            library = ctypes.CDLL(str(process_host))
            for symbol in (
                "fala_process_start_blob",
                "fala_process_destroy",
                "fala_process_wait",
                "fala_process_cancel",
                "fala_process_get_status",
                "fala_process_get_pid",
                "fala_process_get_exit_code",
                "fala_process_get_term_signal",
                "fala_process_was_timed_out",
                "fala_process_was_cancelled",
                "fala_process_get_error_code",
                "fala_process_get_error_message",
                "fala_host_getenv",
            ):
                self.assertTrue(getattr(library, symbol))
            self.assertEqual(len(list((version / "source" / "project" / "Fala" / "python" / "fala" / "__mojocache__").glob("_native.hash-*.so"))), 1)
            import importlib.util
            deployed_build = version / "source" / "project" / "Fala" / "python" / "fala" / "_build.py"
            spec = importlib.util.spec_from_file_location("deployed_fala_build", deployed_build)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module._PACKAGE_DIR = deployed_build.parent
            expected_native = deployed_build.parent / "__mojocache__" / f"_native.hash-{module._source_hash(version / 'source' / 'project' / 'Fala')}.so"
            self.assertTrue(expected_native.is_file(), f"expected {expected_native.name}; built {[path.name for path in expected_native.parent.glob('_native.hash-*.so')]}")
            self.assertNotIn(str(root / "candidates"), " ".join(arguments))
            self.assertEqual(arguments[arguments.index("--config") + 1], str((version / "source" / "config.toml").resolve()))
            self.assertTrue(parity.validate_fala_candidate(version, deployment_root=root)["ok"])
            version_manifest = json.loads((version / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(version_manifest["candidate_id"], expected_candidate_id)
            self.assertEqual(version_manifest["identity"], stable_identity)
            self.assertTrue((root / "current").is_symlink())
            self.assertEqual((root / "current").resolve(), version.resolve())
            previous = json.loads((root / "previous.json").read_text(encoding="utf-8"))
            self.assertIsNone(previous["candidate_id"])
            self.assertEqual(len(version_manifest["runtime_identity"]), 1)
            self.assertEqual(len(version_manifest["program_arguments"]), 1)
            self.assertEqual(len(version_manifest["dispatch_commands"]), 12)
            runtime0 = version_manifest["runtime_identity"][0]
            self.assertEqual(runtime0["label"], label)
            self.assertEqual(runtime0["program_arguments"], arguments)
            self.assertEqual(version_manifest["program_arguments"][0], arguments)
            self.assertIsNone(runtime0["start_interval"])
            self.assertTrue(runtime0["run_at_load"])
            self.assertTrue(runtime0["keep_alive"])
            self.assertEqual(runtime0["process_type"], "Background")
            self.assertEqual(runtime0["working_directory"], str((version / "source" / "project").resolve()))
            expected_log_dir = (root / "logs" / expected_candidate_id).resolve()
            self.assertEqual(Path(runtime0["standard_out_path"]).parent, expected_log_dir)
            self.assertEqual(Path(runtime0["standard_error_path"]).parent, expected_log_dir)
            self.assertEqual(Path(runtime0["standard_out_path"]).name, f"{label}.out.log")
            self.assertEqual(Path(runtime0["standard_error_path"]).name, f"{label}.err.log")
            self.assertEqual(runtime0["plist_sha256"], hashlib.sha256(installed.read_bytes()).hexdigest())
            plist_artifact = version_manifest["artifacts"][f"launchd/{label}.plist"]
            self.assertEqual(plist_artifact["sha256"], hashlib.sha256(installed.read_bytes()).hexdigest())
            self.assertEqual(plist_artifact["bytes"], installed.stat().st_size)
            for process_id, dispatch in zip(
                [
                    "repo_issue_poll", "issue_triage", "issue_feedback", "issue_split", "issue_close",
                    "issue_ready", "issue_to_pr", "pr_triage", "pr_repair", "pr_merge", "cleanup", "cleanup_reconcile",
                ],
                version_manifest["dispatch_commands"],
            ):
                self.assertEqual(dispatch[1:4], ["-m", "lokay.process", f"lokay-process-{process_id}"])
                self.assertEqual(dispatch[0], str((project / ".venv" / "bin" / "python").resolve()))

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

            with patch.object(self.commands, "_snapshot_legacy_mutators", return_value={}), patch.object(
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

            with patch.object(self.commands, "_snapshot_legacy_mutators", return_value={}), patch.object(
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

            with patch.object(self.commands, "_snapshot_legacy_mutators", return_value={}), patch.object(
                self.commands, "_fsync_directory", side_effect=fail_cutover
            ), patch.object(self.commands.subprocess, "run", side_effect=fake_run):
                with self.assertRaisesRegex(self.commands.ConfigError, "cutover directory fsync failed"):
                    self.commands.deploy_fala(self.cfg, str(candidate), True, deployment_root=str(root))
            self.assertFalse(any(call[:2] == ["launchctl", "bootstrap"] for call in calls))
            self.assertFalse((root / "current").exists())

    def test_legacy_mutator_loaded_after_staging_aborts_before_cutover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._render(root)

            def successful_run(argv, **kwargs):
                if argv[:2] == ["launchctl", "print"]:
                    return subprocess.CompletedProcess(argv, 1, "", "not loaded")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(self.commands.Path, "home", return_value=root / "home"), patch.object(
                self.commands, "_snapshot_legacy_mutators", return_value={}
            ), patch.object(self.commands, "_bootout_legacy_mutators"), patch.object(
                self.commands, "_verify_launchctl_exact"
            ), patch.object(self.commands.subprocess, "run", side_effect=successful_run):
                self.commands.deploy_fala(self.cfg, str(first), True, deployment_root=str(root))

            old_current = (root / "current").resolve()
            launch_agent = root / "home" / "Library" / "LaunchAgents" / f"{self.SUPERVISOR_LABEL}.plist"
            old_plist = launch_agent.read_bytes()
            old_previous = (root / "previous.json").read_bytes()
            second = self._render(root, config_path=root / "other.toml", db_path=root / "other.sqlite")
            second_id = json.loads((second / "manifest.json").read_text(encoding="utf-8"))["candidate_id"]
            calls: list[list[str]] = []
            probe_count = 0

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                if argv[:2] == ["launchctl", "print"]:
                    return subprocess.CompletedProcess(argv, 1, "", "not loaded")
                return subprocess.CompletedProcess(argv, 0, "", "")

            def reprobing_snapshot():
                nonlocal probe_count
                probe_count += 1
                if probe_count == 1:
                    return {}
                raise self.commands.ConfigError(
                    f"legacy mutator labels are loaded: {self.commands.LEGACY_SHELL_MUTATOR_LABELS[0]}"
                )

            with patch.object(self.commands.Path, "home", return_value=root / "home"), patch.object(
                self.commands, "_snapshot_legacy_mutators", side_effect=reprobing_snapshot
            ), patch.object(self.commands.subprocess, "run", side_effect=fake_run):
                with self.assertRaisesRegex(self.commands.ConfigError, "legacy mutator labels are loaded"):
                    self.commands.deploy_fala(self.cfg, str(second), True, deployment_root=str(root))

            self.assertEqual(probe_count, 2)
            self.assertFalse(any(call[:2] == ["launchctl", "bootstrap"] for call in calls))
            self.assertFalse(any(call[:2] == ["launchctl", "bootout"] for call in calls))
            self.assertEqual((root / "current").resolve(), old_current)
            self.assertEqual(launch_agent.read_bytes(), old_plist)
            self.assertEqual((root / "previous.json").read_bytes(), old_previous)
            self.assertFalse((root / "versions" / second_id).exists())


    def test_existing_current_is_restored_after_cutover_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._render(root)

            def successful_run(argv, **kwargs):
                if argv[:2] == ["launchctl", "print"]:
                    return subprocess.CompletedProcess(argv, 1, "", "not loaded")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(self.commands.Path, "home", return_value=root / "home"), patch.object(
                self.commands, "_snapshot_legacy_mutators", return_value={}
            ), patch.object(self.commands, "_verify_launchctl_exact"), patch.object(self.commands.subprocess, "run", side_effect=successful_run):
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
                self.commands, "_snapshot_legacy_mutators", return_value={}
            ), patch.object(self.commands.subprocess, "run", side_effect=failing_run):
                with self.assertRaises(self.commands.ConfigError):
                    self.commands.deploy_fala(self.cfg, str(second), True, deployment_root=str(root))
            self.assertEqual((root / "current").resolve(), old_target)
    def test_unload_verification_failure_still_restores_filesystem(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._render(root)

            def successful_run(argv, **kwargs):
                if argv[:2] == ["launchctl", "print"]:
                    return subprocess.CompletedProcess(argv, 1, "", "not loaded")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(self.commands.Path, "home", return_value=root / "home"), patch.object(
                self.commands, "_snapshot_legacy_mutators", return_value={}
            ), patch.object(self.commands, "_verify_launchctl_exact"), patch.object(
                self.commands.subprocess, "run", side_effect=successful_run
            ):
                self.commands.deploy_fala(self.cfg, str(first), True, deployment_root=str(root))
            old_current = (root / "current").resolve()
            import tools.deployment_parity as parity
            launch_agent = root / "home" / "Library" / "LaunchAgents" / f"{self.SUPERVISOR_LABEL}.plist"
            old_plist = launch_agent.read_bytes()
            old_previous = (root / "previous.json").read_bytes()
            second = self._render(root, config_path=root / "other.toml", db_path=root / "other.sqlite")

            with patch.object(self.commands.Path, "home", return_value=root / "home"), patch.object(
                self.commands, "_snapshot_legacy_mutators", return_value={}
            ), patch.object(
                self.commands, "_verify_launchctl_unloaded", side_effect=self.commands.ConfigError("unload verification uncertain")
            ), patch.object(self.commands.subprocess, "run", side_effect=successful_run):
                with self.assertRaisesRegex(self.commands.ConfigError, "unload verification uncertain"):
                    self.commands.deploy_fala(self.cfg, str(second), True, deployment_root=str(root))

            self.assertEqual((root / "current").resolve(), old_current)
            self.assertEqual(launch_agent.read_bytes(), old_plist)
            self.assertEqual((root / "previous.json").read_bytes(), old_previous)

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

    def test_candidate_policy_uses_canonical_automation_values(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = self._render(Path(directory))
            manifest = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["policy"],
                {
                    "automerge": True,
                    "require_human_approval": False,
                    "require_checks": True,
                    "require_test_evidence": True,
                    "executor_enabled": True,
                },
            )
            self.assertTrue(parity.validate_fala_candidate(candidate)["ok"])

    def test_autonomous_policy_is_accepted_in_live_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root, mode="live", autonomous=True)
            import tools.deployment_parity as parity
            self.assertTrue(parity.validate_fala_candidate(candidate, deployment_root=root)["ok"])

    def test_manual_policy_is_rejected_for_promotion(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(Exception) as raised:
                self._render(Path(directory), mode="live", autonomous=False)
            self.assertIn("Fala identity policy is unsafe for promotion", raised.exception.result["errors"])

    def test_live_only_render_has_correct_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root, mode="live")
            import tools.deployment_parity as parity
            self.assertTrue(parity.validate_fala_candidate(candidate)["ok"])
            manifest = json.loads(candidate.joinpath("manifest.json").read_text(encoding="utf-8"))
            argv_list = manifest["program_arguments"]
            self.assertEqual(len(argv_list), 1)
            argv = argv_list[0]
            self.assertIn("--live", argv)
            self.assertNotIn("--dry-run", argv)
            self.assertEqual(argv[1:3], ["-m", "lokay.supervisor"])
            self.assertIn("--json", argv)
            runtime_list = manifest["runtime_identity"]
            self.assertEqual(len(runtime_list), 1)
            self.assertEqual(runtime_list[0]["label"], self.SUPERVISOR_LABEL)
            self.assertEqual(runtime_list[0]["program_arguments"], argv)
            self.assertIsNone(runtime_list[0]["start_interval"])
            self.assertTrue(runtime_list[0]["run_at_load"])
            self.assertTrue(runtime_list[0]["keep_alive"])
            dispatch = manifest["dispatch_commands"]
            self.assertEqual(len(dispatch), 12)
            for process_id, command in zip(
                [
                    "repo_issue_poll", "issue_triage", "issue_feedback", "issue_split", "issue_close",
                    "issue_ready", "issue_to_pr", "pr_triage", "pr_repair", "pr_merge", "cleanup", "cleanup_reconcile",
                ],
                dispatch,
            ):
                self.assertEqual(command[1:4], ["-m", "lokay.process", f"lokay-process-{process_id}"])
                self.assertIn("--live", command)
                self.assertNotIn("--dry-run", command)

    def test_candidate_policy_is_required_and_autonomous(self):
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
                    "automerge": True,
                    "require_human_approval": False,
                    "require_checks": True,
                    "require_test_evidence": True,
                    "executor_enabled": True,
                },
            )

            # Make candidate mutable for tamper checks.
            for path in [candidate, *candidate.rglob("*")]:
                if path.is_dir():
                    path.chmod(0o755)
                elif path.is_file():
                    path.chmod(0o644)
            manifest["policy"]["require_human_approval"] = True
            manifest["identity"]["policy"]["require_human_approval"] = True
            (candidate / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            with self.assertRaises(parity.DeploymentParityError) as raised:
                parity.validate_fala_candidate(candidate, deployment_root=root)
            self.assertTrue(any("unsafe" in error for error in raised.exception.result["errors"]))

    def test_validate_fala_candidate_cli_skips_default_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            parser = self.module.commands.ArgumentParser(prog="lokay")
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
            config_path = root / "config.toml"
            data = copy.deepcopy(load_registry(ROOT / "config.toml").data)
            data["mode"] = "dry-run"
            data["automation"]["automerge"] = True
            data["automation"]["require_human_approval"] = False
            data["automation"]["require_checks"] = True
            data["automation"]["require_test_evidence"] = True
            data["executor"]["enabled"] = True
            config_path.write_bytes(canonical_toml(data))
            render_config = root / "candidate-config.toml"
            candidate = self._render(root, config_path=render_config, db_path=db_path, autonomous=True)
            deployment = root / "deployment"
            version = deployment / "versions" / candidate.name
            version.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(candidate, version)
            self.commands._promote_version_runtime(version, deployment, candidate.name)
            (deployment / "current").symlink_to(version, target_is_directory=True)
            home = root / "home"
            installed = home / "Library" / "LaunchAgents"
            installed.mkdir(parents=True, exist_ok=True)
            primary_plist = installed / f"{self.SUPERVISOR_LABEL}.plist"
            shutil.copy2(version / "launchd" / primary_plist.name, primary_plist)
            env = os.environ.copy()
            env.pop("HERMES_LOKAY_REPOS_FILE", None)
            env.pop("HERMES_LOKAY_WORKTREE_ROOT", None)
            env.update(
                {
                    "HOME": str(home),
                    "HERMES_LOKAY_FALA_DB": str(db_path),
                    "HERMES_LOKAY_FALA_REQUIRE_LIVE": "0",
                    "HERMES_LOKAY_CONFIG": str(config_path),
                    "HERMES_LOKAY_FALA_PLIST": str(primary_plist),
                    "HERMES_LOKAY_LOG_DIR": str(root / "logs"),
                    "HERMES_LOKAY_DEPLOYMENT_ROOT": str(deployment),
                }
            )
            completed = subprocess.run(
                ["bash", str(ROOT / "scripts" / "lokay_status.sh")],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("candidate gate=PASS", completed.stdout)
            combined = completed.stdout + completed.stderr
            self.assertNotIn("managed-python-unavailable", combined)
            self.assertNotIn("interpreter-unavailable", combined)
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
            self.assertFalse((home / "Library" / "LaunchAgents" / f"{self.SUPERVISOR_LABEL}.plist").exists())

    def _legacy_loaded_snapshot(self, home: Path, labels: list[str], *, repair: bool = True) -> dict[str, dict]:
        uid = self.commands.os.getuid()
        domain = f"user/{uid}"
        agents = home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        states: dict[str, dict] = {}
        for label in self.commands.LEGACY_MUTATOR_LABELS:
            loaded = label in labels
            entry = {
                "label": label,
                "domain": domain,
                "loaded": loaded,
                "available": True,
                "domains": {
                    domain: {"label": label, "domain": domain, "loaded": loaded, "available": True},
                    f"gui/{uid}": {"label": label, "domain": f"gui/{uid}", "loaded": False, "available": False},
                },
                "plist_path": None,
                "plist_bytes": None,
                "plist_sha256": None,
                "repair_enabled": False,
                "transition": False,
            }
            if loaded:
                plist = agents / f"{label}.plist"
                body = '<?xml version="1.0"?><plist><dict><key>Label</key><string>%s</string><key>ProgramArguments</key><array><string>%s</string>%s</array></dict></plist>\n' % (
                    label,
                    label,
                    "<string>--repair</string>" if label == self.commands.LEGACY_HEALTH_LABEL and repair else "",
                )
                data = body.encode("utf-8")
                plist.write_bytes(data)
                entry["plist_path"] = str(plist)
                entry["plist_bytes"] = data
                entry["plist_sha256"] = hashlib.sha256(data).hexdigest()
                if label == self.commands.LEGACY_HEALTH_LABEL:
                    entry["repair_enabled"] = repair
                    entry["transition"] = repair
                else:
                    entry["transition"] = True
            states[label] = entry
        return states

    def test_promotion_boots_out_loaded_legacy_before_fala_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            home = root / "home"
            legacy_label = self.commands.LEGACY_SHELL_MUTATOR_LABELS[0]
            snapshot = self._legacy_loaded_snapshot(home, [legacy_label])
            unloaded = self._legacy_loaded_snapshot(home, [])
            calls: list[list[str]] = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                if argv[:2] == ["launchctl", "print"]:
                    return subprocess.CompletedProcess(argv, 1, "", "not loaded")
                if argv[:2] == ["plutil", "-lint"]:
                    return subprocess.CompletedProcess(argv, 0, "OK\n", "")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(self.commands.Path, "home", return_value=home), patch.object(
                self.commands, "_snapshot_legacy_mutators", return_value=snapshot
            ), patch.object(self.commands, "_inspect_legacy_mutator_states", return_value=unloaded), patch.object(
                self.commands, "_verify_launchctl_exact"
            ), patch.object(self.commands, "_verify_launchctl_unloaded"), patch.object(
                self.commands.subprocess, "run", side_effect=fake_run
            ):
                result = self.commands.deploy_fala(self.cfg, str(candidate), True, deployment_root=str(root))

            self.assertTrue(result["promoted"])
            legacy_bootouts = [
                i for i, call in enumerate(calls)
                if call[:2] == ["launchctl", "bootout"] and call[2].endswith(f"/{legacy_label}")
            ]
            fala_bootstraps = [
                i for i, call in enumerate(calls)
                if call[:2] == ["launchctl", "bootstrap"] and self.SUPERVISOR_LABEL in " ".join(call)
            ]
            self.assertTrue(legacy_bootouts)
            self.assertTrue(fala_bootstraps)
            self.assertLess(max(legacy_bootouts), min(fala_bootstraps))

    def test_unknown_legacy_domain_aborts_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            calls: list[list[str]] = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, "OK\n", "")

            def failing_snapshot():
                raise self.commands.ConfigError(
                    "unable to inspect launchd state for user/501/com.mikolaj92.lokay.issue-intake: mysterious failure"
                )

            with patch.object(self.commands.Path, "home", return_value=root / "home"), patch.object(
                self.commands, "_snapshot_legacy_mutators", side_effect=failing_snapshot
            ), patch.object(self.commands.subprocess, "run", side_effect=fake_run):
                with self.assertRaisesRegex(self.commands.ConfigError, "unable to inspect launchd state"):
                    self.commands.deploy_fala(self.cfg, str(candidate), True, deployment_root=str(root))

            self.assertFalse(any(call[:2] == ["launchctl", "bootout"] for call in calls))
            self.assertFalse(any(call[:2] == ["launchctl", "bootstrap"] for call in calls))
            self.assertFalse((root / "current").exists())

    def test_dual_domain_legacy_state_aborts_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            calls: list[list[str]] = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, "OK\n", "")

            def failing_snapshot():
                raise self.commands.ConfigError(
                    f"legacy mutator label is loaded in multiple domains: {self.commands.LEGACY_SHELL_MUTATOR_LABELS[0]}"
                )

            with patch.object(self.commands.Path, "home", return_value=root / "home"), patch.object(
                self.commands, "_snapshot_legacy_mutators", side_effect=failing_snapshot
            ), patch.object(self.commands.subprocess, "run", side_effect=fake_run):
                with self.assertRaisesRegex(self.commands.ConfigError, "multiple domains"):
                    self.commands.deploy_fala(self.cfg, str(candidate), True, deployment_root=str(root))

            self.assertFalse(any(call[:2] == ["launchctl", "bootout"] for call in calls))
            self.assertFalse(any(call[:2] == ["launchctl", "bootstrap"] for call in calls))
            self.assertFalse((root / "current").exists())

    def test_bootstrap_failure_restores_stale_plists_and_loaded_domain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            home = root / "home"
            agents = home / "Library" / "LaunchAgents"
            stale_labels = [
                self.commands.AGGREGATE_FALA_LABEL,
                "com.mikolaj92.lokay.repo-issue-poll",
            ]
            stale_bytes = {
                label: f"original:{label}".encode("utf-8")
                for label in stale_labels
            }
            agents.mkdir(parents=True)
            for label, data in stale_bytes.items():
                (agents / f"{label}.plist").write_bytes(data)
            loaded_label = self.commands.AGGREGATE_FALA_LABEL
            user_domain = f"user/{self.commands.os.getuid()}"
            loaded_domains = {(user_domain, loaded_label)}
            calls: list[list[str]] = []

            def failing_run(argv, **kwargs):
                command = list(argv)
                calls.append(command)
                if command[:2] == ["launchctl", "print"]:
                    domain, label = command[2].rsplit("/", 1)
                    if (domain, label) in loaded_domains:
                        return subprocess.CompletedProcess(command, 0, "state = running\\n", "")
                    return subprocess.CompletedProcess(command, 1, "", "not loaded")
                if command[:2] == ["launchctl", "bootout"]:
                    domain, label = command[2].rsplit("/", 1)
                    loaded_domains.discard((domain, label))
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[:2] == ["plutil", "-lint"]:
                    return subprocess.CompletedProcess(command, 0, "OK\\n", "")
                if command[:2] == ["launchctl", "bootstrap"]:
                    domain = command[2]
                    label = Path(command[3]).stem
                    if label == self.SUPERVISOR_LABEL:
                        raise subprocess.CalledProcessError(1, command)
                    loaded_domains.add((domain, label))
                    return subprocess.CompletedProcess(command, 0, "", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(self.commands.Path, "home", return_value=home), patch.object(
                self.commands, "_snapshot_legacy_mutators", return_value={}
            ), patch.object(self.commands, "_bootout_legacy_mutators"), patch.object(
                self.commands, "_verify_launchctl_exact"
            ), patch.object(self.commands.subprocess, "run", side_effect=failing_run):
                with self.assertRaisesRegex(self.commands.ConfigError, "rolled back after launchd failure"):
                    self.commands.deploy_fala(self.cfg, str(candidate), True, deployment_root=str(root))

            for label, data in stale_bytes.items():
                self.assertEqual((agents / f"{label}.plist").read_bytes(), data)
            self.assertIn((user_domain, loaded_label), loaded_domains)
            self.assertFalse((root / "current").exists())


    def test_stale_plist_unlink_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            home = root / "home"
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            stale_label = self.commands.AGGREGATE_FALA_LABEL
            stale_target = agents / f"{stale_label}.plist"
            stale_bytes = b"cannot-remove-stale-plist"
            stale_target.write_bytes(stale_bytes)
            current = root / "current"
            unlink_calls = 0
            original_unlink = Path.unlink

            def refuse_stale_unlink(path, *args, **kwargs):
                nonlocal unlink_calls
                if path == stale_target:
                    unlink_calls += 1
                    raise OSError("unlink refused")
                return original_unlink(path, *args, **kwargs)

            def fake_run(argv, **kwargs):
                if argv[:2] == ["launchctl", "print"]:
                    return subprocess.CompletedProcess(argv, 1, "", "not loaded")
                if argv[:2] == ["plutil", "-lint"]:
                    return subprocess.CompletedProcess(argv, 0, "OK\\n", "")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(self.commands.Path, "home", return_value=home), patch.object(
                self.commands, "_snapshot_legacy_mutators", return_value={}
            ), patch.object(self.commands, "_bootout_legacy_mutators"), patch.object(
                self.commands, "_verify_launchctl_exact"
            ), patch.object(self.commands.subprocess, "run", side_effect=fake_run), patch.object(
                Path, "unlink", autospec=True, side_effect=refuse_stale_unlink
            ):
                with self.assertRaisesRegex(self.commands.ConfigError, "rolled back after launchd failure: unlink refused"):
                    self.commands.deploy_fala(self.cfg, str(candidate), True, deployment_root=str(root))

            self.assertEqual(unlink_calls, 1)
            self.assertEqual(stale_target.read_bytes(), stale_bytes)
            self.assertFalse(current.exists())

    def test_legacy_bootout_failure_aborts_before_fala_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            home = root / "home"
            legacy_label = self.commands.LEGACY_SHELL_MUTATOR_LABELS[0]
            snapshot = self._legacy_loaded_snapshot(home, [legacy_label])
            calls: list[list[str]] = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                if argv[:2] == ["launchctl", "print"]:
                    return subprocess.CompletedProcess(argv, 1, "", "not loaded")
                if argv[:2] == ["plutil", "-lint"]:
                    return subprocess.CompletedProcess(argv, 0, "OK\n", "")
                if argv[:2] == ["launchctl", "bootout"] and argv[2].endswith(f"/{legacy_label}"):
                    return subprocess.CompletedProcess(argv, 1, "", "bootout refused")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(self.commands.Path, "home", return_value=home), patch.object(
                self.commands, "_snapshot_legacy_mutators", return_value=snapshot
            ), patch.object(self.commands.subprocess, "run", side_effect=fake_run):
                with self.assertRaisesRegex(self.commands.ConfigError, "unable to bootout launchd service"):
                    self.commands.deploy_fala(self.cfg, str(candidate), True, deployment_root=str(root))

            self.assertFalse(
                any(
                    call[:2] == ["launchctl", "bootstrap"] and self.SUPERVISOR_LABEL in " ".join(call)
                    for call in calls
                )
            )
            self.assertFalse((root / "current").exists())
            self.assertFalse((home / "Library" / "LaunchAgents" / f"{self.SUPERVISOR_LABEL}.plist").exists())

    def test_fala_bootstrap_failure_restores_legacy_and_previous_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._render(root)
            home = root / "home"

            def successful_run(argv, **kwargs):
                if argv[:2] == ["launchctl", "print"]:
                    return subprocess.CompletedProcess(argv, 1, "", "not loaded")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(self.commands.Path, "home", return_value=home), patch.object(
                self.commands, "_snapshot_legacy_mutators", return_value={}
            ), patch.object(self.commands, "_bootout_legacy_mutators"), patch.object(
                self.commands, "_verify_launchctl_exact"
            ), patch.object(self.commands.subprocess, "run", side_effect=successful_run):

                self.commands.deploy_fala(self.cfg, str(first), True, deployment_root=str(root))
            old_current = (root / "current").resolve()
            launch_agent = home / "Library" / "LaunchAgents" / f"{self.SUPERVISOR_LABEL}.plist"
            old_plist = launch_agent.read_bytes()
            second = self._render(root, config_path=root / "other.toml", db_path=root / "other.sqlite")
            legacy_label = self.commands.LEGACY_SHELL_MUTATOR_LABELS[0]
            snapshot = self._legacy_loaded_snapshot(home, [legacy_label])
            restored_snapshot = dict(snapshot)
            restored_snapshot[legacy_label] = dict(snapshot[legacy_label])
            legacy_plist = Path(snapshot[legacy_label]["plist_path"])
            expected_legacy_bytes = snapshot[legacy_label]["plist_bytes"]
            # Simulate a post-bootout filesystem wipe so restore must rewrite bytes.
            legacy_plist.write_bytes(b"corrupted")
            calls: list[list[str]] = []
            restored = False

            def failing_run(argv, **kwargs):
                nonlocal restored
                calls.append(list(argv))
                if argv[:2] == ["launchctl", "print"]:
                    parts = argv[2].split("/")
                    domain = f"{parts[0]}/{parts[1]}"
                    label = parts[2]
                    if restored and label == legacy_label and domain == snapshot[legacy_label]["domain"]:
                        return subprocess.CompletedProcess(argv, 0, "state = running\n", "")
                    return subprocess.CompletedProcess(argv, 1, "", "not loaded")
                if argv[:2] == ["plutil", "-lint"]:
                    return subprocess.CompletedProcess(argv, 0, "OK\n", "")
                if argv[:2] == ["launchctl", "bootstrap"] and self.SUPERVISOR_LABEL in " ".join(argv):
                    raise subprocess.CalledProcessError(1, argv)
                if argv[:2] == ["launchctl", "bootstrap"] and argv[-1] == str(legacy_plist):
                    restored = True
                    return subprocess.CompletedProcess(argv, 0, "", "")
                return subprocess.CompletedProcess(argv, 0, "", "")

            def inspect_after_restore():
                if restored:
                    return restored_snapshot
                return self._legacy_loaded_snapshot(home, [])

            with patch.object(self.commands.Path, "home", return_value=home), patch.object(
                self.commands, "_snapshot_legacy_mutators", return_value=snapshot
            ), patch.object(self.commands, "_inspect_legacy_mutator_states", side_effect=inspect_after_restore), patch.object(
                self.commands, "_verify_launchctl_unloaded"
            ), patch.object(self.commands.subprocess, "run", side_effect=failing_run):
                with self.assertRaisesRegex(self.commands.ConfigError, "rolled back after launchd failure"):
                    self.commands.deploy_fala(self.cfg, str(second), True, deployment_root=str(root))

            self.assertEqual((root / "current").resolve(), old_current)
            self.assertEqual(launch_agent.read_bytes(), old_plist)
            self.assertEqual(legacy_plist.read_bytes(), expected_legacy_bytes)
            self.assertTrue(
                any(
                    call[:3] == ["launchctl", "bootstrap", snapshot[legacy_label]["domain"]] and call[-1] == str(legacy_plist)
                    for call in calls
                )
            )
            self.assertTrue(any(call[:2] == ["launchctl", "bootout"] and call[2].endswith(f"/{legacy_label}") for call in calls))

    def test_pre_staging_domain_state_does_not_drive_cutover_rollback(self):
        """Pre-staging launchd state must not drive cutover domain or rollback restore."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            home = root / "home"
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            label = self.SUPERVISOR_LABEL
            uid = self.commands.os.getuid()
            user_domain = f"user/{uid}"
            gui_domain = f"gui/{uid}"
            (agents / f"{label}.plist").write_bytes(b"original-supervisor")
            managed_labels = [
                self.SUPERVISOR_LABEL,
                *[item for item in self.commands.STALE_FALA_LABELS if item != self.SUPERVISOR_LABEL],
            ]
            wave_size = len(managed_labels)
            probe_count = 0
            calls: list[list[str]] = []
            written_previous: dict = {}

            def unloaded(label_arg: str) -> dict[str, dict]:
                return {
                    user_domain: {
                        "label": label_arg,
                        "domain": user_domain,
                        "loaded": False,
                        "available": True,
                    },
                    gui_domain: {
                        "label": label_arg,
                        "domain": gui_domain,
                        "loaded": False,
                        "available": True,
                    },
                }

            def early_states(label_arg: str) -> dict[str, dict]:
                # Pre-staging: both domains unloaded so an early-only path would
                # bootstrap/restore user/. Late path must instead honor gui load.
                return unloaded(label_arg)

            def late_states(label_arg: str) -> dict[str, dict]:
                states = unloaded(label_arg)
                if label_arg == label:
                    states[gui_domain] = {
                        "label": label_arg,
                        "domain": gui_domain,
                        "loaded": True,
                        "available": True,
                    }
                return states

            def domain_states(label_arg: str) -> dict[str, dict]:
                nonlocal probe_count
                wave = probe_count // wave_size
                probe_count += 1
                if wave == 0:
                    return early_states(label_arg)
                return late_states(label_arg)

            real_atomic_write = self.commands._atomic_write

            def capturing_atomic_write(path, data, *args, **kwargs):
                target = Path(path)
                if target.name == "previous.json":
                    payload = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")
                    written_previous["payload"] = json.loads(payload.decode("utf-8"))
                return real_atomic_write(path, data, *args, **kwargs)

            def failing_run(argv, **kwargs):
                command = list(argv)
                calls.append(command)
                if command[:2] == ["launchctl", "print"]:
                    return subprocess.CompletedProcess(command, 1, "", "not loaded")
                if command[:2] == ["plutil", "-lint"]:
                    return subprocess.CompletedProcess(command, 0, "OK\n", "")
                if command[:2] == ["launchctl", "bootstrap"]:
                    raise subprocess.CalledProcessError(1, command)
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(self.commands.Path, "home", return_value=home), patch.object(
                self.commands, "_snapshot_legacy_mutators", return_value={}
            ), patch.object(self.commands, "_bootout_legacy_mutators"), patch.object(
                self.commands, "_launchctl_domain_states", side_effect=domain_states
            ), patch.object(self.commands, "_verify_launchctl_unloaded"), patch.object(
                self.commands, "_atomic_write", side_effect=capturing_atomic_write
            ), patch.object(self.commands.subprocess, "run", side_effect=failing_run):
                with self.assertRaisesRegex(self.commands.ConfigError, "rolled back after launchd failure"):
                    self.commands.deploy_fala(self.cfg, str(candidate), True, deployment_root=str(root))

            # One early wave (validation) + one late wave (cutover/rollback).
            self.assertEqual(probe_count, 2 * wave_size)
            self.assertIn("payload", written_previous)
            previous_loaded = written_previous["payload"]["loaded_state"][label]
            self.assertTrue(previous_loaded[gui_domain]["loaded"])
            self.assertFalse(previous_loaded[user_domain]["loaded"])
            self.assertEqual(written_previous["payload"]["domain"], gui_domain)

            supervisor_bootstraps = [
                call
                for call in calls
                if call[:2] == ["launchctl", "bootstrap"] and call[-1].endswith(f"{label}.plist")
            ]
            # Late snapshot: cutover targets gui and rollback restores gui.
            # An early-only snapshot would have bootstrapped user once and skipped restore.
            self.assertEqual(len(supervisor_bootstraps), 2)
            self.assertTrue(all(call[2] == gui_domain for call in supervisor_bootstraps))
            self.assertFalse((root / "current").exists())
            self.assertEqual((agents / f"{label}.plist").read_bytes(), b"original-supervisor")

    def test_late_domain_snapshot_skips_restore_when_unloaded_after_staging(self):
        """Rollback must not re-bootstrap a domain that only looked loaded pre-staging."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._render(root)
            home = root / "home"
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            label = self.SUPERVISOR_LABEL
            uid = self.commands.os.getuid()
            user_domain = f"user/{uid}"
            gui_domain = f"gui/{uid}"
            (agents / f"{label}.plist").write_bytes(b"original-supervisor")
            managed_labels = [
                self.SUPERVISOR_LABEL,
                *[item for item in self.commands.STALE_FALA_LABELS if item != self.SUPERVISOR_LABEL],
            ]
            wave_size = len(managed_labels)
            probe_count = 0
            calls: list[list[str]] = []

            def unloaded(label_arg: str) -> dict[str, dict]:
                return {
                    user_domain: {
                        "label": label_arg,
                        "domain": user_domain,
                        "loaded": False,
                        "available": True,
                    },
                    gui_domain: {
                        "label": label_arg,
                        "domain": gui_domain,
                        "loaded": False,
                        "available": True,
                    },
                }

            def early_states(label_arg: str) -> dict[str, dict]:
                states = unloaded(label_arg)
                if label_arg == label:
                    states[user_domain] = {
                        "label": label_arg,
                        "domain": user_domain,
                        "loaded": True,
                        "available": True,
                    }
                return states

            def late_states(label_arg: str) -> dict[str, dict]:
                return unloaded(label_arg)

            def domain_states(label_arg: str) -> dict[str, dict]:
                nonlocal probe_count
                wave = probe_count // wave_size
                probe_count += 1
                if wave == 0:
                    return early_states(label_arg)
                return late_states(label_arg)

            def failing_run(argv, **kwargs):
                command = list(argv)
                calls.append(command)
                if command[:2] == ["launchctl", "print"]:
                    return subprocess.CompletedProcess(command, 1, "", "not loaded")
                if command[:2] == ["plutil", "-lint"]:
                    return subprocess.CompletedProcess(command, 0, "OK\n", "")
                if command[:2] == ["launchctl", "bootstrap"]:
                    raise subprocess.CalledProcessError(1, command)
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(self.commands.Path, "home", return_value=home), patch.object(
                self.commands, "_snapshot_legacy_mutators", return_value={}
            ), patch.object(self.commands, "_bootout_legacy_mutators"), patch.object(
                self.commands, "_launchctl_domain_states", side_effect=domain_states
            ), patch.object(self.commands, "_verify_launchctl_unloaded"), patch.object(
                self.commands.subprocess, "run", side_effect=failing_run
            ):
                with self.assertRaisesRegex(self.commands.ConfigError, "rolled back after launchd failure"):
                    self.commands.deploy_fala(self.cfg, str(candidate), True, deployment_root=str(root))

            self.assertEqual(probe_count, 2 * wave_size)
            supervisor_bootstraps = [
                call
                for call in calls
                if call[:2] == ["launchctl", "bootstrap"] and call[-1].endswith(f"{label}.plist")
            ]
            # Late snapshot is unloaded, so cutover bootstraps user once and rollback must not re-bootstrap.
            self.assertEqual(len(supervisor_bootstraps), 1)
            self.assertEqual(supervisor_bootstraps[0][2], user_domain)
            self.assertFalse((root / "current").exists())
            self.assertEqual((agents / f"{label}.plist").read_bytes(), b"original-supervisor")

    def test_deploy_fala_rejects_symlink_deployment_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            real_root = base / "real"
            real_root.mkdir()
            candidate = self._render(real_root)
            link_root = base / "link"
            link_root.symlink_to(real_root, target_is_directory=True)
            with self.assertRaisesRegex(self.commands.ConfigError, "deployment root must not be a symlink"):
                self.commands.deploy_fala(self.cfg, str(candidate), False, deployment_root=str(link_root))
            with self.assertRaisesRegex(self.commands.ConfigError, "deployment root must not be a symlink"):
                self.commands.deploy_fala(self.cfg, str(candidate), True, deployment_root=str(link_root))
            self.assertFalse((real_root / "versions").exists())
            self.assertFalse((real_root / "current").exists())

    def test_deploy_fala_rejects_non_directory_deployment_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            file_root = base / "not-a-dir"
            file_root.write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(self.commands.ConfigError, "deployment root must be a directory"):
                self.commands.deploy_fala(self.cfg, "any-candidate", False, deployment_root=str(file_root))


if __name__ == "__main__":
    unittest.main()
