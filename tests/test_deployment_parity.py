from __future__ import annotations

import shutil
import subprocess
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

    def test_extra_source_script_fails_closed(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        (source / "unexpected.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        with self.assertRaises(DeploymentParityError) as raised:
            validate(source, active, [templates / "launchd"])
        self.assertTrue(any("unexpected source script" in error for error in raised.exception.result["errors"]))

    def test_extra_active_script_fails_closed(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        (active / "unexpected.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        with self.assertRaises(DeploymentParityError) as raised:
            validate(source, active, [templates / "launchd"])
        self.assertTrue(any("unexpected active script" in error for error in raised.exception.result["errors"]))
    def test_active_plist_root_ignores_unrelated_launchagents(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        active_plist = Path(holder.name) / "active-launchd"
        active_plist.mkdir()
        from tools.deployment_parity import _render_template
        for template in (templates / "launchd").glob("*.plist.template"):
            installed_name = "com.mikolaj92.lokay.fala-tick-all.plist" if "fala-tick-all" in template.name else template.name.removesuffix(".template")
            active_plist.joinpath(installed_name).write_text(
                _render_template(template.read_text(encoding="utf-8"), active.parent.parent, active),
                encoding="utf-8",
            )
        (active_plist / "com.example.unrelated.plist").write_text("not even a plist", encoding="utf-8")
        result = validate(source, active, [templates / "launchd"], active_plist_roots=[active_plist])
        self.assertTrue(result["ok"])

    def test_active_plist_label_and_arguments_drift_fails_closed(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        active_plist = Path(holder.name) / "active-launchd"
        active_plist.mkdir()
        template = templates / "launchd" / "lokay-health.plist.template"
        from tools.deployment_parity import _render_template
        rendered = _render_template(template.read_text(encoding="utf-8"), active.parent.parent, active)
        rendered = rendered.replace("com.mikolaj92.lokay.health", "com.example.legacy").replace("lokay_health.sh", "lokay_status.sh")
        (active_plist / "lokay-health.plist").write_text(rendered, encoding="utf-8")
        with self.assertRaises(DeploymentParityError) as raised:
            validate(source, active, [templates / "launchd"], active_plist_roots=[active_plist])
        errors = raised.exception.result["errors"]
        self.assertTrue(any("active launchd Label mismatch" in error for error in errors))
        self.assertTrue(any("active launchd ProgramArguments mismatch" in error for error in errors))
    def test_invalid_template_with_active_root_reports_parity_error(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        active_plist = Path(holder.name) / "active-launchd"
        active_plist.mkdir()
        template = templates / "launchd" / "lokay-health.plist.template"
        template.write_text("not a plist", encoding="utf-8")
        with self.assertRaises(DeploymentParityError) as raised:
            validate(source, active, [templates / "launchd"], active_plist_roots=[active_plist])
        self.assertTrue(any("invalid launchd template" in error for error in raised.exception.result["errors"]))

    def test_active_byte_drift_fails_closed(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        drifted = active / "lokay_smoke.sh"
        drifted.write_text(drifted.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
        with self.assertRaises(DeploymentParityError) as raised:
            validate(source, active, [templates / "launchd"])
        self.assertTrue(any("hash mismatch" in error for error in raised.exception.result["errors"]))

    def test_fala_template_requires_absolute_uv_and_canonical_arguments(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        template = templates / "launchd" / "lokay-fala-tick-all.plist.template"
        template.write_text(template.read_text(encoding="utf-8").replace("{{UV_BIN}}", "uv"), encoding="utf-8")
        with self.assertRaises(DeploymentParityError) as raised:
            validate(source, active, [templates / "launchd"])
        self.assertTrue(any("uv executable must be absolute" in error for error in raised.exception.result["errors"]))

    def test_fala_template_rejects_mutable_candidate_paths(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        template = templates / "launchd" / "lokay-fala-tick-all.plist.template"
        text = template.read_text(encoding="utf-8").replace("{{PROJECT_ROOT}}", str(active.parent / "candidates" / "candidate" / "source" / "project"))
        template.write_text(text, encoding="utf-8")
        with self.assertRaises(DeploymentParityError) as raised:
            validate(source, active, [templates / "launchd"])
        self.assertTrue(any("mutable candidates" in error for error in raised.exception.result["errors"]))

    def test_fala_template_requires_exactly_one_mode_flag(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        template = templates / "launchd" / "lokay-fala-tick-all.plist.template"
        text = template.read_text(encoding="utf-8").replace(
            "    <string>{{MODE_ARG}}</string>",
            "    <string>--dry-run</string>\n    <string>--live</string>",
        )
        template.write_text(text, encoding="utf-8")
        with self.assertRaises(DeploymentParityError) as raised:
            validate(source, active, [templates / "launchd"])
        self.assertTrue(any("mode flags are not exactly once" in error for error in raised.exception.result["errors"]))

    def test_noncanonical_template_root_rejected(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        with self.assertRaises(ValueError):
            validate(source, active, [ROOT / "launchd"])

    def test_duplicate_template_identity_rejected(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        with self.assertRaises(ValueError):
            validate(source, active, [templates / "launchd", templates / "launchd"])

    def test_rendered_artifact_inventory_is_exact(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        rendered = Path(holder.name) / "rendered"
        rendered.mkdir()
        from tools.deployment_parity import _render_template
        for template in (templates / "launchd").glob("*.plist.template"):
            name = template.name.removesuffix(".template")
            rendered.joinpath(name).write_text(
                _render_template(template.read_text(encoding="utf-8"), active.parent.parent, active),
                encoding="utf-8",
            )
        (rendered / "unexpected.plist").write_text("<?xml version=\"1.0\"?><plist><dict/></plist>", encoding="utf-8")
        with self.assertRaises(DeploymentParityError) as raised:
            validate(source, active, [templates / "launchd"], render_roots=[rendered])
        self.assertTrue(any("unexpected rendered launchd artifact" in error for error in raised.exception.result["errors"]))

    def test_cli_accepts_optional_roots(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        command = [
            "python3", str(ROOT / "tools" / "deployment_parity.py"),
            "--source-root", str(source), "--active-root", str(active),
            "--template-root", str(templates / "launchd"),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_validate_fala_candidate_rejects_symlink_deployment_root(self):
        from tools.deployment_parity import validate_fala_candidate

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            real_root = base / "real"
            real_root.mkdir()
            candidates = real_root / "candidates"
            candidates.mkdir()
            candidate = candidates / "cafebabe"
            candidate.mkdir()
            (candidate / "manifest.json").write_text("{}", encoding="utf-8")
            link_root = base / "link"
            link_root.symlink_to(real_root, target_is_directory=True)
            with self.assertRaises(DeploymentParityError) as raised:
                validate_fala_candidate(candidate, deployment_root=link_root)
            self.assertTrue(
                any("deployment root must not be a symlink" in error for error in raised.exception.result["errors"])
            )

    def test_validate_fala_candidate_rejects_non_directory_deployment_root(self):
        from tools.deployment_parity import validate_fala_candidate

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            file_root = base / "not-a-dir"
            file_root.write_text("x", encoding="utf-8")
            candidate = base / "candidates" / "cafebabe"
            candidate.mkdir(parents=True)
            (candidate / "manifest.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(DeploymentParityError) as raised:
                validate_fala_candidate(candidate, deployment_root=file_root)
            self.assertTrue(
                any("deployment root must be a directory" in error for error in raised.exception.result["errors"])
            )


if __name__ == "__main__":
    unittest.main()
