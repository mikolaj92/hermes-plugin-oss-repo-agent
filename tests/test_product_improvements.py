import importlib
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from argparse import ArgumentParser
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    if sys.modules.get("hermes_plugins") is None:
        parent = types.ModuleType("hermes_plugins")
        parent.__path__ = []
        sys.modules["hermes_plugins"] = parent
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.oss_repo_agent",
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_plugins.oss_repo_agent"] = module
    spec.loader.exec_module(module)
    return module


def write_config(path):
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "mode": "dry-run",
                "clone_root": "./repos",
                "worktree_root": "./worktrees",
                "branch_prefix": "ai/fix",
                "automerge": False,
                "github": {"cli": "gh", "default_limit": 10, "assignee": "owner"},
                "labels": {
                    "ready": "ai:ready",
                    "in_progress": "ai:in-progress",
                    "blocked": "ai:blocked",
                    "pr_opened": "ai:pr-opened",
                    "generated": "ai:generated",
                },
                "executor": {
                    "enabled": False,
                    "command": "opencode",
                    "timeout_seconds": 1800,
                },
                "repos": [
                    {
                        "repo": "owner/example-repo",
                        "board": "example-board",
                        "clone_path": "./repos/example-repo",
                        "trusted_authors": [],
                        "trusted_branch_prefixes": ["ai/fix"],
                        "allowed_base_branches": ["main"],
                        "external_pr_policy": "block",
                    }
                ],
            }
        )
    )
    return path


class OssInitAndDryRunTests(unittest.TestCase):
    def setUp(self):
        self.module = load_plugin()
        self.commands = self.module.commands
        self.config = importlib.import_module("hermes_plugins.oss_repo_agent.config")

    def parser(self):
        parser = ArgumentParser()
        self.commands.setup_parser(parser)
        return parser

    def test_parser_registers_init_command(self):
        args = self.parser().parse_args([
            "--config",
            "config.yaml",
            "init",
            "--repo",
            "owner/example-repo",
            "--board",
            "example-board",
        ])
        self.assertEqual(args.oss_repo_agent_command, "init")

    def test_init_bypasses_config_loading_and_writes_starter_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.yaml"
            args = self.parser().parse_args([
                "--config",
                str(target),
                "init",
                "--repo",
                "owner/example-repo",
                "--board",
                "example-board",
            ])
            original = self.commands.load_config
            self.commands.load_config = lambda path: self.fail("init loaded config")
            try:
                result = self.commands.run_from_args(args)
            finally:
                self.commands.load_config = original
            self.assertTrue(result["ok"])
            self.assertTrue(target.exists())
            loaded = self.config.load_config(str(target))
            self.assertEqual(loaded.mode, "dry-run")
            self.assertFalse(loaded.automerge)
            self.assertFalse(loaded.executor.enabled)
            self.assertEqual(loaded.repos[0].repo, "owner/example-repo")
            self.assertEqual(loaded.repos[0].board, "example-board")
            self.assertEqual(loaded.github.assignee, "owner")
            self.assertIn("validate", " ".join(result["next_commands"]))

    def test_init_refuses_to_overwrite_existing_config_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.yaml"
            target.write_text("version: 1\n")
            args = self.parser().parse_args(["--config", str(target), "init"])
            with self.assertRaises(self.config.ConfigError):
                self.commands.run_from_args(args)

    def test_root_config_example_is_production_policy(self):
        example = PLUGIN_ROOT / "config.example.yaml"
        self.assertTrue(example.exists())
        loaded = self.config.load_config(str(example))
        self.assertEqual(loaded.mode, "live")
        self.assertTrue(loaded.automerge)
        self.assertTrue(loaded.executor.enabled)
        self.assertEqual(loaded.executor.command, "omp")
        self.assertTrue(loaded.repos)

    def test_alternate_config_example_uses_production_policy(self):
        example = PLUGIN_ROOT / "examples" / "config.example.yaml"
        loaded = self.config.load_config(str(example))
        self.assertEqual(loaded.mode, "live")
        self.assertTrue(loaded.automerge)
        self.assertTrue(loaded.executor.enabled)
        self.assertEqual(loaded.executor.command, "omp")

    def test_docs_and_ci_are_present_for_three_minute_path(self):
        readme = (PLUGIN_ROOT / "README.md").read_text()
        after_install = PLUGIN_ROOT / "after-install.md"
        ci = PLUGIN_ROOT / ".github" / "workflows" / "ci.yml"
        self.assertTrue(after_install.exists())
        self.assertTrue(ci.exists())
        self.assertIn("init", readme)
        self.assertIn("install", readme.lower())
        self.assertIn("checks", ci.read_text().lower())

    def test_intake_dry_run_returns_concrete_planned_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(Path(tmp) / "config.json")
            args = self.parser().parse_args([
                "--config",
                str(config_path),
                "intake",
                "--limit",
                "2",
            ])
            result = self.commands.run_from_args(args)
            self.assertFalse(result["effective_live"])
            self.assertEqual(result["executed"], [False])
            self.assertEqual(result["planned_work"][0]["repo"], "owner/example-repo")
            self.assertFalse(result["planned_work"][0]["mutation"])
            self.assertIn("Kanban", result["planned_work"][0]["action"])
            self.assertEqual(self.commands.INTAKE_ASSIGNEE, "repo-agent-intake")
            self.assertTrue(result["safety_guards"])

    def test_dispatch_dry_run_reinforces_executor_and_merge_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(Path(tmp) / "config.json")
            args = self.parser().parse_args([
                "--config",
                str(config_path),
                "dispatch",
                "--max",
                "2",
            ])
            result = self.commands.run_from_args(args)
            self.assertFalse(result["effective_live"])
            self.assertFalse(result["executor_runs"])
            self.assertEqual(result["planned_work"][0]["repo"], "owner/example-repo")
            self.assertFalse(result["planned_work"][0]["mutation"])
            self.assertIn("no PR merge support in v0", result["safety_guards"])

    def test_pr_triage_dry_run_plans_claim_without_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(Path(tmp) / "config.json")
            args = self.parser().parse_args([
                "--config",
                str(config_path),
                "pr-triage",
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
