import importlib
import importlib.util
import tomllib
from importlib import resources
import sys
import tempfile
import types
import unittest
from argparse import ArgumentParser
from pathlib import Path
from lokay.registry import canonical_toml


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    if sys.modules.get("hermes_plugins") is None:
        parent = types.ModuleType("hermes_plugins")
        parent.__path__ = []
        sys.modules["hermes_plugins"] = parent
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.lokay",
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_plugins.lokay"] = module
    spec.loader.exec_module(module)
    return module


def write_config(path):
    data = tomllib.loads((PLUGIN_ROOT / "config.toml").read_text(encoding="utf-8"))
    data["mode"] = "dry-run"
    data["github"]["assignee"] = "owner"
    data["automation"]["automerge"] = False
    data["automation"]["require_human_approval"] = True
    data["executor"]["enabled"] = False
    data["repos"] = [{
        "repo": "owner/example-repo",
        "board": "owner-example-repo",
        "clone_path": f"~/.lokay-test/product/{path.parent.name}/repos/example-repo",
        "priority": 50,
    }]
    path.write_bytes(canonical_toml(data))
    return path


class LokayInitAndDryRunTests(unittest.TestCase):
    def setUp(self):
        self.module = load_plugin()
        self.commands = self.module.commands
        self.config = importlib.import_module("hermes_plugins.lokay.config")

    def parser(self):
        parser = ArgumentParser()
        self.commands.setup_parser(parser)
        return parser

    def test_parser_registers_copy_only_init(self):
        args = self.parser().parse_args([
            "--config", "config.toml", "init", "--project-root", str(PLUGIN_ROOT)
        ])
        self.assertEqual(args.lokay_command, "init")
        self.assertEqual(args.project_root, str(PLUGIN_ROOT))

    def test_init_copies_checkout_config_without_loading_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.toml"
            args = self.parser().parse_args([
                "--config", str(target), "init", "--project-root", str(PLUGIN_ROOT)
            ])
            original = self.commands.load_config
            self.commands.load_config = lambda path: self.fail("init loaded config")
            try:
                result = self.commands.run_from_args(args)
            finally:
                self.commands.load_config = original
            self.assertTrue(result["ok"])
            self.assertEqual(target.read_bytes(), (PLUGIN_ROOT / "config.toml").read_bytes())
            self.assertEqual(
                result["sha256"],
                __import__("hashlib").sha256(target.read_bytes()).hexdigest(),
            )

    def test_init_rejects_invalid_source_without_destination_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            (root / "config.toml").write_text("mode = \"live\"\n", encoding="utf-8")
            target = Path(tmp) / "destination" / "config.toml"
            with self.assertRaises(self.commands.ConfigError):
                self.commands.init_project(str(target), str(root))
            self.assertFalse(target.exists())
            self.assertFalse(target.parent.exists())

    def test_init_rejects_existing_destination_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            (root / "config.toml").write_bytes((PLUGIN_ROOT / "config.toml").read_bytes())
            target = Path(tmp) / "target.toml"
            target.symlink_to(root / "config.toml")
            with self.assertRaises(self.commands.ConfigError):
                self.commands.init_project(str(target), str(root))
            self.assertTrue(target.is_symlink())

    def test_root_config_example_uses_safe_production_policy(self):
        example = PLUGIN_ROOT / "config.example.toml"
        loaded = self.config.load_config(str(example))
        self.assertEqual(loaded.mode, "live")
        self.assertTrue(loaded.automerge)
        self.assertFalse(loaded.require_human_approval)
        self.assertTrue(loaded.require_checks)
        self.assertTrue(loaded.require_test_evidence)
        self.assertTrue(loaded.executor.enabled)
        self.assertEqual(loaded.executor.command, "omp")
        self.assertEqual(len(loaded.processes), 12)
        self.assertTrue(loaded.repos)

    def test_packaged_config_resource_matches_checkout(self):
        resource = resources.files("lokay").joinpath("config.toml")
        self.assertEqual(resource.read_bytes(), (PLUGIN_ROOT / "config.toml").read_bytes())

    def test_intake_dry_run_returns_concrete_planned_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(Path(tmp) / "config.toml")
            args = self.parser().parse_args([
                "--config", str(config_path), "intake", "--limit", "2"
            ])
            result = self.commands.run_from_args(args)
            self.assertFalse(result["effective_live"])
            self.assertEqual(result["executed"], [False])
            self.assertEqual(result["planned_work"][0]["repo"], "owner/example-repo")
            self.assertFalse(result["planned_work"][0]["mutation"])
            self.assertIn("Kanban", result["planned_work"][0]["action"])
            self.assertEqual(self.commands.INTAKE_ASSIGNEE, "lokay-intake")
            self.assertTrue(result["safety_guards"])

    def test_dispatch_dry_run_reinforces_executor_and_merge_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(Path(tmp) / "config.toml")
            args = self.parser().parse_args([
                "--config", str(config_path), "dispatch", "--max", "2"
            ])
            result = self.commands.run_from_args(args)
            self.assertFalse(result["effective_live"])
            self.assertFalse(result["executor_runs"])
            self.assertEqual(result["planned_work"][0]["repo"], "owner/example-repo")
            self.assertFalse(result["planned_work"][0]["mutation"])
            self.assertIn("no PR merge support in v0", result["safety_guards"])

    def test_pr_triage_dry_run_plans_claim_without_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(Path(tmp) / "config.toml")
            args = self.parser().parse_args([
                "--config", str(config_path), "pr-triage"
            ])
            result = self.commands.run_from_args(args)
            self.assertFalse(result["effective_live"])
            self.assertEqual(result["merge_behavior"], "not-supported-in-v0")
            self.assertIn("claim", result["planned_work"][0]["action"])

    def test_pr_claim_filter_only_accepts_owner_ai_fix_branches(self):
        pr = {"number": 1, "author": {"login": "owner"}, "headRefName": "ai/fix/one"}
        external = {"number": 2, "author": {"login": "contributor"}, "headRefName": "ai/fix/two"}
        non_agent = {"number": 3, "author": {"login": "owner"}, "headRefName": "feature/two"}
        self.assertTrue(self.commands._claimable_pr("owner/repo", pr, "ai/fix"))
        self.assertFalse(self.commands._claimable_pr("owner/repo", external, "ai/fix"))
        self.assertFalse(self.commands._claimable_pr("owner/repo", non_agent, "ai/fix"))


if __name__ == "__main__":
    unittest.main()
