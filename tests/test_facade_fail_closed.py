from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


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
    sys.modules["hermes_plugins.oss_repo_agent"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ScriptedRunner:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def run(self, spec, live):
        self.calls.append(spec)
        key = tuple(spec.argv)
        response = self.responses.get(key)
        if response is None:
            response = (0, "[]", "")
        if isinstance(response, BaseException):
            raise response
        return SimpleNamespace(
            spec=spec,
            executed=live,
            returncode=response[0],
            stdout=response[1],
            stderr=response[2],
        )


class FacadeFailClosedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.commands = load_plugin().commands

    def config(self, *, assignee=None):
        return self.commands.OssRepoAgentConfig.from_mapping(
            {
                "mode": "live",
                "github": {"assignee": assignee} if assignee else {},
                "repos": [
                    {"repo": "owner/bad", "board": "bad-board"},
                    {"repo": "owner/good", "board": "good-board"},
                ],
            }
        )

    def test_row_parsers_reject_empty_malformed_and_mixed_json(self):
        for parser in (
            self.commands._issue_rows,
            self.commands._kanban_task_rows,
            self.commands._pr_rows,
        ):
            for payload in ("", "not-json", "{}", '[{"ok": true}, 3]'):
                with self.subTest(parser=parser.__name__, payload=payload):
                    with self.assertRaises(ValueError):
                        parser(payload)

    def test_intake_continues_after_repository_response_failure(self):
        cfg = self.config()
        issue_good = json.dumps([{"number": 4, "title": "Fix", "url": "https://example/4", "labels": []}])
        runner = ScriptedRunner(
            {
                ("gh", "issue", "list", "--repo", "owner/bad", "--state", "open", "--limit", "10", "--json", "number,title,url,labels,isLocked"): (0, "", ""),
                ("gh", "issue", "list", "--repo", "owner/good", "--state", "open", "--limit", "10", "--json", "number,title,url,labels,isLocked"): (0, issue_good, ""),
            }
        )
        result = self.commands.intake(cfg, True, 10, runner)
        self.assertFalse(result["ok"])
        self.assertEqual(result["ensured_tasks"][0]["issue"], 4)
        self.assertEqual([item["repo"] for item in result["repository_results"]], ["owner/bad", "owner/good"])
        self.assertEqual(result["failures"][0]["stage"], "issue-list-response")
        self.assertTrue(any(item["repo"] == "owner/good" and not item["failures"] for item in result["repository_results"]))

    def test_pr_triage_aggregates_list_and_claim_failures(self):
        cfg = self.config(assignee="bot")
        pr_good = json.dumps([{"number": 9, "author": {"login": "owner"}, "headRefName": "ai/fix/change"}])
        runner = ScriptedRunner(
            {
                ("gh", "pr", "list", "--repo", "owner/bad", "--state", "open", "--limit", "50", "--json", "number,title,author,headRefName,baseRefName,isDraft,labels,mergeStateStatus"): (2, "", "list failed"),
                ("gh", "pr", "list", "--repo", "owner/good", "--state", "open", "--limit", "50", "--json", "number,title,author,headRefName,baseRefName,isDraft,labels,mergeStateStatus"): (0, pr_good, ""),
                ("gh", "pr", "edit", "9", "--repo", "owner/good", "--add-assignee", "bot"): (1, "", "claim failed"),
            }
        )
        result = self.commands.pr_triage(cfg, True, False, runner)
        self.assertFalse(result["ok"])
        self.assertEqual(result["claimed_prs"], [])
        self.assertEqual({item["stage"] for item in result["failures"]}, {"pr-list", "claim"})
        self.assertEqual([item["repo"] for item in result["repository_results"]], ["owner/bad", "owner/good"])


if __name__ == "__main__":
    unittest.main()
