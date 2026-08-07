from __future__ import annotations

import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from lokay.registry import (
    LIVE_REPO_INVENTORY,
    MIGRATION_DEFAULTS,
    PROCESS_FIELDS,
    PROCESS_GRAPH_CONTRACT,
    PROCESS_IDS,
    SECTION_FIELDS,
    TOP_LEVEL_FIELDS,
    ConfigError,
    canonical_toml,
    load_registry,
    migrate_config,
    process_defaults,
    validate_document,
)


def _two_repo_document() -> dict:
    return {
        "version": 1,
        "mode": "dry-run",
        "branch_prefix": "ai/fix",
        "base_branch": "main",
        "github": {"cli": "gh", "default_limit": 10, "assignee": ""},
        "labels": dict(MIGRATION_DEFAULTS["labels"]),
        "automation": dict(MIGRATION_DEFAULTS["automation"]),
        "direction": dict(MIGRATION_DEFAULTS["direction"]),
        "triage": dict(MIGRATION_DEFAULTS["triage"]),
        "executor": dict(MIGRATION_DEFAULTS["executor"]),
        "paths": {
            "worktree_root": "~/.hermes/worktrees/lokay",
            "dispatch_receipts": "~/.hermes/state/lokay-dispatch",
            "task_receipts": "~/.hermes/state/lokay-receipts",
            "merge_receipts": "~/.hermes/state/lokay-merge",
            "active_issue": "~/.hermes/state/lokay-active",
            "triage_receipts": "~/.hermes/state/lokay-triage",
        },
        "repos": [
            {
                "repo": "owner/one",
                "board": "owner-one",
                "clone_path": "~/Developer/one",
                "priority": 10,
            },
            {
                "repo": "owner/two",
                "board": "owner-two",
                "clone_path": "~/Developer/two",
                "priority": 5,
            },
        ],
        "processes": process_defaults(),
    }


class RegistryParserTests(unittest.TestCase):
    def test_validate_document_accepts_two_repo_fixture(self):
        document = validate_document(_two_repo_document())
        self.assertEqual(len(document["repos"]), 2)
        self.assertEqual(len(document["processes"]), 12)

    def test_checked_in_process_defaults_validate_in_canonical_order(self):
        document = _two_repo_document()
        self.assertEqual(
            [process["id"] for process in document["processes"]],
            list(PROCESS_IDS),
        )
        validate_document(document)

    def test_reordered_alternative_predecessors_fail_closed(self):
        data = _two_repo_document()
        triage = next(item for item in data["processes"] if item["id"] == "issue_triage")
        triage["predecessors"] = list(reversed(triage["predecessors"]))
        with self.assertRaisesRegex(ConfigError, "predecessor receipt order"):
            validate_document(data)

    def test_merge_method_schema_contract_is_shared_by_parser(self):
        data = _two_repo_document()
        data["automation"]["merge_method"] = "unsupported"
        with self.assertRaisesRegex(ConfigError, "merge_method: must be merge, squash, or rebase"):
            validate_document(data)

    def test_malformed_merge_method_types_fail_as_config_errors(self):
        for value in ([], {}):
            with self.subTest(value=value):
                data = _two_repo_document()
                data["automation"]["merge_method"] = value
                with self.assertRaisesRegex(ConfigError, "automation.merge_method: must be a string"):
                    validate_document(data)

    def test_schema_structural_contract_matches_canonical_toml(self):
        schema_path = Path(__file__).resolve().parents[1] / "schemas" / "config.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        canonical_path = schema_path.parents[1] / "config.toml"
        canonical = tomllib.loads(canonical_path.read_text(encoding="utf-8"))
        validate_document(canonical)
        self.assertEqual(set(schema["required"]), TOP_LEVEL_FIELDS)
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("parser is authoritative", schema["$comment"])
        for section, fields in SECTION_FIELDS.items():
            section_schema = schema["properties"][section]
            self.assertFalse(section_schema["additionalProperties"])
            self.assertEqual(set(section_schema["required"]), fields)
        repos_schema = schema["properties"]["repos"]
        self.assertGreaterEqual(len(canonical["repos"]), repos_schema["minItems"])
        self.assertEqual(
            set(repos_schema["items"]["required"]),
            {"repo", "board", "clone_path", "priority"},
        )
        processes_schema = schema["properties"]["processes"]
        self.assertEqual(len(canonical["processes"]), processes_schema["minItems"])
        self.assertEqual(len(canonical["processes"]), processes_schema["maxItems"])
        self.assertFalse(schema["$defs"]["processBase"]["additionalProperties"])
        self.assertEqual(set(schema["$defs"]["processBase"]["required"]), PROCESS_FIELDS)
        self.assertEqual(
            [item["id"] for item in canonical["processes"]],
            list(PROCESS_IDS),
        )
        for process_id in PROCESS_IDS:
            properties = schema["$defs"][f"{process_id}Graph"]["allOf"][1]["properties"]
            contract = PROCESS_GRAPH_CONTRACT[process_id]
            self.assertEqual(properties["output_receipts"]["const"], list(contract["output_receipts"]))
            self.assertEqual(
                properties["predecessors"]["const"],
                [receipt for group in contract["predecessor_groups"] for receipt in group],
            )
            self.assertEqual(properties["successors"]["const"], list(contract["successors"]))

    def test_parser_only_schema_constraints_fail_closed(self):
        cases = (
            ("leading whitespace", lambda data: data.__setitem__("branch_prefix", " ai/fix"), "leading or trailing whitespace"),
            (
                "duplicate strings",
                lambda data: data["direction"].__setitem__("require_keywords", ["same", "same"]),
                "must not contain duplicates",
            ),
            ("repository syntax", lambda data: data["repos"][0].__setitem__("repo", "invalid"), "owner/repository form"),
            ("portable path", lambda data: data["paths"].__setitem__("task_receipts", "/tmp/receipts"), "portable path rooted at ~"),
            (
                "lease relationship",
                lambda data: data["processes"][0].__setitem__("stale_owner_after_seconds", data["processes"][0]["lease_seconds"]),
                "must be at least",
            ),
        )
        for name, mutate, message in cases:
            with self.subTest(name=name):
                data = _two_repo_document()
                mutate(data)
                with self.assertRaisesRegex(ConfigError, message):
                    validate_document(data)

    def test_load_registry_shell_rows_preserve_declaration_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_bytes(canonical_toml(_two_repo_document()))
            document = load_registry(path)
            rows = document.shell_rows()
            self.assertEqual(len(rows), 2)
            self.assertTrue(rows[0].startswith("owner/one|owner-one|"))
            self.assertTrue(rows[1].startswith("owner/two|owner-two|"))
            self.assertEqual(document.sha256, __import__("hashlib").sha256(path.read_bytes()).hexdigest())
    def test_load_registry_rejects_absolute_section_path(self):
        data = _two_repo_document()
        data["paths"]["task_receipts"] = "/tmp/lokay-receipts"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_bytes(canonical_toml(data))
            with self.assertRaisesRegex(ConfigError, "portable path rooted at ~"):
                load_registry(path)

    def test_load_registry_rejects_absolute_repository_clone_path(self):
        data = _two_repo_document()
        data["repos"][0]["clone_path"] = "/tmp/lokay-clone"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_bytes(canonical_toml(data))
            with self.assertRaisesRegex(ConfigError, "portable path rooted at ~"):
                load_registry(path)

    def test_load_registry_rejects_portable_path_escape(self):
        data = _two_repo_document()
        data["paths"]["task_receipts"] = "~/../outside"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_bytes(canonical_toml(data))
            with self.assertRaisesRegex(ConfigError, "must not contain '..' path components"):
                load_registry(path)
    def test_load_registry_rejects_symlinked_path_component(self):
        with tempfile.TemporaryDirectory(prefix=".lokay-registry-", dir=Path.home()) as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            data = _two_repo_document()
            relative = root.resolve().relative_to(Path.home().resolve())
            data["paths"]["task_receipts"] = f"~/{relative.as_posix()}/link/receipts"
            config = root / "config.toml"
            config.write_bytes(canonical_toml(data))
            with self.assertRaisesRegex(ConfigError, "must not traverse symlinked paths"):
                load_registry(config)
    def test_load_registry_rejects_parent_component_before_normalization(self):
        with tempfile.TemporaryDirectory(prefix=".lokay-registry-", dir=Path.home()) as tmp:
            root = Path(tmp)
            target = root / "safe"
            target.mkdir()
            (root / "link").symlink_to(target, target_is_directory=True)
            relative = root.resolve().relative_to(Path.home().resolve()).as_posix()
            data = _two_repo_document()
            data["paths"]["task_receipts"] = f"~/{relative}/link/../safe"
            config = root / "config.toml"
            config.write_bytes(canonical_toml(data))
            with self.assertRaisesRegex(ConfigError, "must not contain '..' path components"):
                load_registry(config)

    def test_duplicate_repo_fails_closed(self):
        data = _two_repo_document()
        data["repos"][1]["repo"] = data["repos"][0]["repo"]
        with self.assertRaisesRegex(ConfigError, "duplicate repository"):
            validate_document(data)

    def test_recovery_table_rejected_on_activation(self):
        data = _two_repo_document()
        data["attempt_recovery"] = {
            "run_id": "r",
            "process_id": "p",
            "candidate": "c",
            "path_id": "path",
            "effector_id": "eff",
            "repo": "owner/one",
            "pr_number": 1,
            "verified_head": "abc",
        }
        with self.assertRaisesRegex(ConfigError, "recovery table"):
            validate_document(data)

    def test_retired_env_var_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_bytes(canonical_toml(_two_repo_document()))
            with self.assertRaisesRegex(ConfigError, "retired configuration environment"):
                load_registry(path, env={"HERMES_LOKAY_REPOS_FILE": "/tmp/repos"})

    def test_incomplete_process_catalog_fails(self):
        data = _two_repo_document()
        data["processes"] = data["processes"][:11]
        with self.assertRaisesRegex(ConfigError, "exactly twelve process IDs"):
            validate_document(data)

    def test_process_graph_rejects_missing_pr_merge_predecessor(self):
        data = _two_repo_document()
        merge = next(item for item in data["processes"] if item["id"] == "pr_merge")
        merge["predecessors"] = []
        with self.assertRaisesRegex(ConfigError, "pr_merge.predecessors|locked predecessor"):
            validate_document(data)

    def test_malformed_toml_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("mode = [\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "invalid TOML"):
                load_registry(path)


class RegistryMigrationTests(unittest.TestCase):
    def _migration_fixture(self, root: Path, *, external: bool = True, embedded: bool = False):
        data = json.loads(json.dumps(MIGRATION_DEFAULTS))
        data["repos"] = [
            {"repo": repo, "board": board, "clone_path": clone_path, "priority": priority}
            for repo, board, clone_path, priority in LIVE_REPO_INVENTORY
        ]
        data["processes"] = process_defaults()
        state_root = tempfile.TemporaryDirectory(prefix=".lokay-migration-", dir=Path.home())
        self.addCleanup(state_root.cleanup)
        state = Path(state_root.name) / "source-state"
        data["paths"]["task_receipts"] = str(state)
        source = root / "source.toml"
        source.write_bytes(canonical_toml(data))
        records = {
            "attempt_recovery": {
                "run_id": "run-1",
                "process_id": "process-1",
                "candidate": "candidate-1",
                "path_id": "path-1",
                "effector_id": "effector-1",
                "repo": "mikolaj92/lokay",
                "pr_number": 1,
                "verified_head": "head-1",
            },
            "repair_creation_recovery": {
                "run_id": "run-2",
                "process_id": "process-2",
                "candidate": "candidate-2",
                "path_id": "path-2",
                "effector_id": "effector-2",
            },
        }
        if external:
            recovery_dir = state / "recovery"
            recovery_dir.mkdir(parents=True)
            for name, record in records.items():
                payload = {"schema_version": 1, "kind": name, **record}
                (recovery_dir / f"{name}.json").write_text(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
        if embedded:
            embedded_text = "\n[attempt_recovery]\n" + "\n".join(
                f"{key} = {json.dumps(value)}" for key, value in records["attempt_recovery"].items()
            )
            embedded_text += "\n\n[repair_creation_recovery]\n" + "\n".join(
                f"{key} = {json.dumps(value)}" for key, value in records["repair_creation_recovery"].items()
            ) + "\n"
            source.write_bytes(source.read_bytes() + embedded_text.encode("utf-8"))
        return source, state, records

    def test_temp_migration_copies_exact_external_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, _, records = self._migration_fixture(Path(tmp))
            destination = Path(tmp) / "out" / "config.toml"
            recovery = Path(tmp) / "out" / "recovery"
            result = migrate_config(source, destination, recovery_root=recovery)
            self.assertEqual(result["migrated"], list(records))
            for name, record in records.items():
                expected = json.dumps(
                    {"schema_version": 1, "kind": name, **record},
                    sort_keys=True,
                    separators=(",", ":"),
                ) + "\n"
                self.assertEqual((recovery / f"{name}.json").read_text(encoding="utf-8"), expected)

    def test_temp_migration_without_recovery_files_writes_no_recovery_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, _, _ = self._migration_fixture(Path(tmp), external=False)
            destination = Path(tmp) / "out" / "config.toml"
            recovery = Path(tmp) / "out" / "recovery"
            result = migrate_config(source, destination, recovery_root=recovery)
            self.assertEqual(result["migrated"], [])
            self.assertTrue(destination.is_file())
            self.assertFalse(recovery.exists())


    def test_temp_migration_replaces_source_process_catalog_with_locked_defaults(self):
        """Migration owns the process topology; legacy process values are not preserved."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, _, _ = self._migration_fixture(root, external=False)
            source_document = tomllib.loads(source.read_text(encoding="utf-8"))
            source_document["processes"][0]["interval_seconds"] = 90
            source.write_bytes(canonical_toml(source_document))
            destination = root / "out" / "config.toml"
            result = migrate_config(source, destination)
            migrated = load_registry(destination)
            defaults = process_defaults()
            self.assertEqual(result["migrated"], [])
            self.assertEqual(migrated.processes, tuple(defaults))
            self.assertNotEqual(migrated.processes[0]["interval_seconds"], 90)
    def test_temp_migration_recovery_inputs_fail_before_outputs(self):
        cases = ("partial", "broken_symlink", "regular_file_dir", "unknown_key")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source, state, records = self._migration_fixture(root)
                recovery_dir = state / "recovery"
                if case == "partial":
                    (recovery_dir / "repair_creation_recovery.json").unlink()
                elif case == "broken_symlink":
                    (recovery_dir / "attempt_recovery.json").unlink()
                    (recovery_dir / "attempt_recovery.json").symlink_to(recovery_dir / "missing.json")
                elif case == "regular_file_dir":
                    for path in recovery_dir.iterdir():
                        path.unlink()
                    recovery_dir.rmdir()
                    recovery_dir.write_text("not a directory", encoding="utf-8")
                else:
                    path = recovery_dir / "attempt_recovery.json"
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    payload["unexpected"] = True
                    path.write_text(json.dumps(payload), encoding="utf-8")
                destination = root / "out" / "config.toml"
                recovery = root / "out" / "recovery"
                with self.assertRaises(ConfigError):
                    migrate_config(source, destination, recovery_root=recovery)
                self.assertFalse(destination.exists())
                self.assertFalse(destination.parent.exists())
                self.assertFalse(recovery.exists())

    def test_temp_migration_rejects_embedded_external_recovery_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, state, _ = self._migration_fixture(Path(tmp), embedded=True)
            path = state / "recovery" / "attempt_recovery.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["candidate"] = "different-candidate"
            path.write_text(json.dumps(payload), encoding="utf-8")
            destination = Path(tmp) / "out" / "config.toml"
            recovery = Path(tmp) / "out" / "recovery"
            with self.assertRaisesRegex(ConfigError, "embedded and external recovery state conflict"):
                migrate_config(source, destination, recovery_root=recovery)
            self.assertFalse(destination.exists())
            self.assertFalse(destination.parent.exists())
            self.assertFalse(recovery.exists())
    def test_temp_migration_rejects_destination_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, _, _ = self._migration_fixture(root)
            destination = root / "out" / "config.toml"
            destination.mkdir(parents=True)
            recovery = root / "out" / "recovery"
            with self.assertRaisesRegex(ConfigError, "canonical config conflict"):
                migrate_config(source, destination, recovery_root=recovery)
            self.assertTrue(destination.is_dir())
            self.assertFalse(recovery.exists())

    def test_temp_migration_rejects_recovery_root_symlink_or_file(self):
        for kind in ("symlink", "file"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source, _, _ = self._migration_fixture(root)
                destination = root / "out" / "config.toml"
                recovery = root / "out" / "recovery"
                recovery.parent.mkdir(parents=True)
                target = root / "outside-recovery"
                if kind == "symlink":
                    target.mkdir()
                    recovery.symlink_to(target, target_is_directory=True)
                else:
                    recovery.write_text("not a directory", encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, "destination parent is not a directory"):
                    migrate_config(source, destination, recovery_root=recovery)
                self.assertFalse(destination.exists())
                if kind == "symlink":
                    self.assertTrue(recovery.is_symlink())
                    self.assertEqual(recovery.readlink(), target)
                else:
                    self.assertTrue(recovery.is_file())
                    self.assertEqual(recovery.read_text(encoding="utf-8"), "not a directory")

    def test_live_migration_writes_portable_paths_and_recovery(self):
        source = Path.home() / ".hermes/lokay/config.toml"
        if not source.is_file():
            self.skipTest("live config unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "config.toml"
            recovery = Path(tmp) / "recovery"
            result = migrate_config(source, destination, recovery_root=recovery)
            text = destination.read_text(encoding="utf-8")
            self.assertNotIn("/Users/mini-m4-main", text)
            self.assertIn("~/Developer/hermes-repos/Fala-live", text)
            self.assertIn("~/Developer/hermes-repos/rnkstr-live", text)
            self.assertEqual(text.count("[[repos]]"), len(LIVE_REPO_INVENTORY))
            self.assertEqual(text.count("[[processes]]"), 12)
            self.assertIn("auto_close_duplicates = true", text)
            self.assertIn("task_receipts = \"~/.hermes/state/lokay-receipts-live\"", text)
            self.assertEqual(
                sorted(path.name for path in recovery.glob("*.json")),
                ["attempt_recovery.json", "repair_creation_recovery.json"],
            )
            document = load_registry(destination)
            self.assertEqual(document.sha256, result["config_sha256"])
            self.assertEqual(document.repos[0]["repo"], "mikolaj92/Fala")
            self.assertEqual(document.repos[-1]["repo"], "mikolaj92/rnkstr")

    def test_missing_repos_fails_before_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "src.toml"
            source.write_text('version = 1\nmode = "live"\n', encoding="utf-8")
            destination = Path(tmp) / "dest" / "config.toml"
            recovery = Path(tmp) / "recovery"
            with self.assertRaisesRegex(ConfigError, "requires a top-level repos array"):
                migrate_config(source, destination, recovery_root=recovery)
            self.assertFalse(destination.exists())
            self.assertFalse(recovery.exists())

    def test_migration_rolls_back_partial_commit(self):
        source = Path.home() / ".hermes/lokay/config.toml"
        if not source.is_file():
            self.skipTest("live config unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "out" / "config.toml"
            recovery = Path(tmp) / "state" / "recovery"
            real_replace = os.replace

            def fail_second_promotion(src, dst):
                # Staging replacements target dot-files. Fail only the second
                # real destination promotion, after config and first recovery
                # have already been installed.
                if Path(dst).name == "repair_creation_recovery.json":
                    raise OSError("simulated recovery replace failure")
                return real_replace(src, dst)

            with mock.patch("os.replace", side_effect=fail_second_promotion):
                with self.assertRaises(OSError):
                    migrate_config(source, destination, recovery_root=recovery)
            self.assertFalse(destination.exists())
            self.assertFalse(destination.parent.exists())
            self.assertFalse(recovery.exists())
            self.assertFalse(recovery.parent.exists())

    def test_incomplete_policy_section_fails_closed_on_load(self):
        data = _two_repo_document()
        del data["automation"]["automerge"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_bytes(canonical_toml(data))
            with self.assertRaisesRegex(ConfigError, "automation: missing required key"):
                load_registry(path)


if __name__ == "__main__":
    unittest.main()
