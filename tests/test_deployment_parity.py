from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from tools.deployment_parity import DeploymentParityError, DEPLOYED_SCRIPTS, validate


ROOT = Path(__file__).resolve().parents[1]


class DeploymentParityTests(unittest.TestCase):
    def make_deployment(self) -> tuple[tempfile.TemporaryDirectory[str], Path, Path, Path]:
        holder = tempfile.TemporaryDirectory()
        root = Path(holder.name)
        source = root / "source" / "scripts"
        active = root / "home" / ".hermes" / "scripts"
        templates = root / "templates"
        source.mkdir(parents=True)
        active.mkdir(parents=True)
        templates.mkdir()
        for name in DEPLOYED_SCRIPTS:
            source_file = ROOT / "scripts" / name
            shutil.copy2(source_file, source / name)
            shutil.copy2(source_file, active / name)
        # Keep the canonical production templates intact while making their active path explicit for this isolated deployment.
        destination = templates / "launchd"
        destination.mkdir()
        for template in (ROOT / "templates" / "launchd").glob("*.plist.template"):
            text = template.read_text(encoding="utf-8").replace(
                "/" + "Users/mini-m4-main/.hermes/scripts", str(active)
            )
            destination.joinpath(template.name).write_text(text, encoding="utf-8")
        return holder, source, active, templates

    def test_source_and_active_scripts_and_launchd_arguments_match(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        result = validate(source, active, [templates / "launchd"])
        self.assertTrue(result["ok"])
        self.assertEqual(set(result["scripts"]), set(DEPLOYED_SCRIPTS))

    def test_active_byte_drift_fails_closed(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        drifted = active / "repo_agent_smoke.sh"
        drifted.write_text(drifted.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
        with self.assertRaises(DeploymentParityError) as raised:
            validate(source, active, [templates / "launchd"])
        self.assertTrue(any("hash mismatch" in error for error in raised.exception.result["errors"]))

    def test_launchd_argument_drift_fails_closed(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        template = templates / "launchd" / "oss-repo-agent-dispatch.plist.template"
        text = template.read_text(encoding="utf-8").replace(
            str(active / "repo_issue_to_pr_dispatch.sh"), str(active / "repo_agent_health.sh")
        )
        template.write_text(text, encoding="utf-8")
        with self.assertRaises(DeploymentParityError) as raised:
            validate(source, active, [templates / "launchd"])
        self.assertTrue(any("entrypoint mismatch" in error for error in raised.exception.result["errors"]))
    def test_fala_template_requires_absolute_uv_and_canonical_arguments(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        template = templates / "launchd" / "oss-repo-agent-fala-tick-all.plist.template"
        template.write_text(template.read_text(encoding="utf-8").replace("{{UV_BIN}}", "uv"), encoding="utf-8")
        with self.assertRaises(DeploymentParityError) as raised:
            validate(source, active, [templates / "launchd"])
        self.assertTrue(any("uv executable must be absolute" in error for error in raised.exception.result["errors"]))

    def test_fala_template_rejects_mutable_candidate_paths(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        template = templates / "launchd" / "oss-repo-agent-fala-tick-all.plist.template"
        text = template.read_text(encoding="utf-8").replace("{{PROJECT_ROOT}}", str(active.parent / "candidates" / "candidate" / "source" / "project"))
        template.write_text(text, encoding="utf-8")
        with self.assertRaises(DeploymentParityError) as raised:
            validate(source, active, [templates / "launchd"])
        self.assertTrue(any("mutable candidates" in error for error in raised.exception.result["errors"]))

    def test_fala_template_requires_exactly_one_mode_flag(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        template = templates / "launchd" / "oss-repo-agent-fala-tick-all.plist.template"
        text = template.read_text(encoding="utf-8").replace(
            "    <string>{{MODE_ARG}}</string>",
            "    <string>--dry-run</string>\n    <string>--live</string>",
        )
        template.write_text(text, encoding="utf-8")
        with self.assertRaises(DeploymentParityError) as raised:
            validate(source, active, [templates / "launchd"])
        self.assertTrue(any("mode flags are not exactly once" in error for error in raised.exception.result["errors"]))


if __name__ == "__main__":
    unittest.main()
