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
            if template.name in {
                "lokay-fala-tick-all.plist.template",
                "lokay-process.plist.template",
            }:
                # Aggregate/per-process production templates are forbidden; keep supervisor + aux jobs.
                continue
            text = template.read_text(encoding="utf-8").replace(
                "/" + "Users/mini-m4-main/.hermes/scripts", str(active)
            )
            destination.joinpath(template.name).write_text(text, encoding="utf-8")
        return holder, source, active, templates

    def test_promotion_policy_requires_checks_to_be_enabled(self):
        from tools.deployment_parity import PROMOTION_POLICY, is_promotion_policy

        self.assertTrue(is_promotion_policy(PROMOTION_POLICY))
        self.assertFalse(is_promotion_policy({**PROMOTION_POLICY, "require_checks": False}))

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
    def test_active_config_inventory_is_toml_only(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        config_root = Path(holder.name) / "active-config"
        config_root.mkdir()
        (config_root / "config.toml").write_text("mode = 'dry-run'\n", encoding="utf-8")
        result = validate(source, active, [templates / "launchd"], active_config_roots=[config_root])
        self.assertTrue(result["ok"])
        for name in ("config.yaml", "config.yml", "config.json"):
            (config_root / name).write_text("retired = true\n", encoding="utf-8")
        with self.assertRaises(DeploymentParityError) as raised:
            validate(source, active, [templates / "launchd"], active_config_roots=[config_root])
        errors = raised.exception.result["errors"]
        for name in ("config.yaml", "config.yml", "config.json"):
            self.assertTrue(any("unexpected active config artifact" in error and name in error for error in errors))
    def test_active_plist_root_ignores_unrelated_launchagents(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        active_plist = Path(holder.name) / "active-launchd"
        active_plist.mkdir()
        from tools.deployment_parity import _render_template
        for template in (templates / "launchd").glob("*.plist.template"):
            if template.name not in {"lokay-health.plist.template", "lokay-hermes-update.plist.template"}:
                continue
            installed_name = template.name.removesuffix(".template")
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

    def test_fala_template_requires_candidate_runtime_environment(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        template = templates / "launchd" / "lokay-supervisor.plist.template"
        text = template.read_text(encoding="utf-8").replace(
            "    <key>PYTHONPATH</key>\n    <string>{{PYTHONPATH}}</string>\n",
            "    <key>PYTHONPATH</key>\n    <string>{{PYTHONPATH}}</string>\n    <key>PATH</key>\n    <string>/usr/bin</string>\n",
        )
        template.write_text(text, encoding="utf-8")
        with self.assertRaises(DeploymentParityError) as raised:
            validate(source, active, [templates / "launchd"])
        self.assertTrue(any("environment" in error.lower() for error in raised.exception.result["errors"]))

    def test_fala_template_rejects_mutable_candidate_paths(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        template = templates / "launchd" / "lokay-supervisor.plist.template"
        text = template.read_text(encoding="utf-8").replace("{{PROJECT_ROOT}}", str(active.parent / "candidates" / "candidate" / "source" / "project"))
        template.write_text(text, encoding="utf-8")
        with self.assertRaises(DeploymentParityError) as raised:
            validate(source, active, [templates / "launchd"])
        self.assertTrue(any("mutable candidates" in error for error in raised.exception.result["errors"]))

    def test_fala_template_requires_exactly_one_mode_flag(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        template = templates / "launchd" / "lokay-supervisor.plist.template"
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


    def test_aggregate_production_template_is_rejected(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        # Explicit forbidden aggregate fixture; production template may already be removed.
        destination = templates / "launchd" / "lokay-fala-tick-all.plist.template"
        destination.write_text(
            """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
  <key>Label</key>
  <string>com.mikolaj92.lokay.fala-tick-all</string>
  <key>ProgramArguments</key>
  <array>
    <string>{{PYTHON_PATH}}</string>
    <string>-m</string>
    <string>lokay.tick_all</string>
    <string>--config</string>
    <string>{{CONFIG_PATH}}</string>
    <string>--db</string>
    <string>{{DB_PATH}}</string>
    <string>{{MODE_ARG}}</string>
    <string>--json</string>
  </array>
  <key>WorkingDirectory</key>
  <string>{{PROJECT_ROOT}}</string>
</dict>
</plist>
""",
            encoding="utf-8",
        )
        with self.assertRaises(DeploymentParityError) as raised:
            validate(source, active, [templates / "launchd"])
        errors = raised.exception.result["errors"]
        self.assertTrue(any("aggregate production launchd template is forbidden" in error for error in errors), errors)

    def test_process_template_is_forbidden_in_production_inventory(self):
        holder, source, active, templates = self.make_deployment()
        self.addCleanup(holder.cleanup)
        process_template = ROOT / "templates" / "launchd" / "lokay-process.plist.template"
        destination = templates / "launchd" / "lokay-process.plist.template"
        if process_template.is_file():
            destination.write_text(process_template.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            # Explicit forbidden per-process fixture when production template is gone.
            destination.write_text(
                """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
  <key>Label</key>
  <string>com.mikolaj92.lokay.repo-issue-poll</string>
  <key>ProgramArguments</key>
  <array>
    <string>{{PYTHON_PATH}}</string>
    <string>-m</string>
    <string>lokay.process</string>
    <string>lokay-process-repo_issue_poll</string>
    <string>--config</string>
    <string>{{CONFIG_PATH}}</string>
    <string>--db</string>
    <string>{{DB_PATH}}</string>
    <string>{{MODE_ARG}}</string>
    <string>--json</string>
  </array>
  <key>WorkingDirectory</key>
  <string>{{PROJECT_ROOT}}</string>
</dict>
</plist>
""",
                encoding="utf-8",
            )
        with self.assertRaises(DeploymentParityError) as raised:
            validate(source, active, [templates / "launchd"])
        errors = raised.exception.result["errors"]
        self.assertTrue(
            any("per-process production launchd template is forbidden" in error for error in errors),
            errors,
        )

    def test_validate_fala_candidate_requires_supervisor_topology(self):
        from tools.deployment_parity import SUPERVISOR_LABEL, validate_fala_candidate

        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        root = Path(holder.name)
        versions = root / "versions"
        candidate = versions / ("a" * 64)
        launchd = candidate / "launchd"
        project = candidate / "source" / "project"
        (project / "src" / "lokay").mkdir(parents=True)
        (project / "effectors").mkdir(parents=True)
        (project / "Fala" / "python" / "fala").mkdir(parents=True)
        (project / ".venv" / "bin").mkdir(parents=True)
        python = project / ".venv" / "bin" / "python"
        python.write_text("#!/bin/sh\n", encoding="utf-8")
        python.chmod(0o755)
        (project / "src" / "lokay" / "effector.py").write_text("x=1\n", encoding="utf-8")
        (project / "fala-package.toml").write_text("name='fala'\n", encoding="utf-8")
        (project / "pyproject.toml").write_text('name = "lokay"\nfala = { path = "Fala", editable = true }\n', encoding="utf-8")
        (project / "Fala" / "pyproject.toml").write_text('name = "fala"\nversion = "0.7.15"\n', encoding="utf-8")
        (project / "Fala" / "revision.txt").write_text("b5f9a6d500a442a1c79060a862fe4b9da87bc98f\n", encoding="utf-8")
        config = candidate / "source" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        process_ids = [
            "repo_issue_poll", "issue_triage", "issue_feedback", "issue_split", "issue_close",
            "issue_ready", "issue_to_pr", "pr_triage", "pr_repair", "pr_merge", "cleanup", "cleanup_reconcile",
        ]
        rows = []
        catalog_toml = []
        for process_id in process_ids:
            interval = 300 if process_id == "cleanup_reconcile" else 60
            command = f"lokay-process-{process_id}"
            rows.append({"id": process_id, "enabled": True, "interval_seconds": interval, "command": command})
            catalog_toml.extend([
                "[[processes]]",
                f'id = "{process_id}"',
                "enabled = true",
                f"interval_seconds = {interval}",
                f'command = "{command}"',
                "",
            ])
        config.write_text("\n".join(catalog_toml), encoding="utf-8")
        launchd.mkdir(parents=True)
        # Forbidden residual process artifact plus incomplete supervisor topology.
        forbidden = launchd / "com.mikolaj92.lokay.repo-issue-poll.plist"
        forbidden.write_text(
            '<?xml version="1.0"?><plist version="1.0"><dict></dict></plist>',
            encoding="utf-8",
        )
        import json, hashlib
        identity = {
            "schema": 1,
            "mode": "dry-run",
            "plugin_commit": "x" * 40,
            "fala_tag": "0.7.15",
            "fala_commit": "b5f9a6d500a442a1c79060a862fe4b9da87bc98f",
            "lock_hash": "0" * 64,
            "config_path": str(root / "config.toml"),
            "config_hash": "0" * 64,
            "db_path": str(root / "fala.sqlite"),
            "metadata_path": "metadata.json",
            "lock_path": "uv.lock",
            "config_artifact_path": "source/config.toml",
            "revision_path": "revision.txt",
            "policy": {
                "automerge": True,
                "require_human_approval": False,
                "require_checks": True,
                "require_test_evidence": True,
                "executor_enabled": True,
            },
            "repos": [{"repo": "o/r", "board": "b", "clone_path": None, "priority": 1}],
            "processes": rows,
        }
        candidate_id = hashlib.sha256((json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()
        candidate.rename(versions / candidate_id)
        candidate = versions / candidate_id
        manifest = {
            **identity,
            "candidate_id": candidate_id,
            "identity": identity,
            "created_at": "2026-01-01T00:00:00Z",
            "program_arguments": [],
            "dispatch_commands": [],
            "artifacts": {},
            "runtime_identity": [],
        }
        (candidate / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(DeploymentParityError) as raised:
            validate_fala_candidate(candidate, deployment_root=root)
        errors = raised.exception.result["errors"]
        self.assertTrue(
            any("per-process production launchd artifact is forbidden" in error for error in errors),
            errors,
        )
        self.assertTrue(
            any("runtime_identity must be a list of exactly 1 supervisor entry" in error for error in errors),
            errors,
        )
        self.assertTrue(
            any("program_arguments must be a list of exactly 1 supervisor argv list" in error for error in errors),
            errors,
        )
        self.assertTrue(
            any("dispatch_commands must be a list of exactly 12 child argv lists" in error for error in errors),
            errors,
        )
        self.assertNotIn(f"launchd/{SUPERVISOR_LABEL}.plist", " ".join(errors))

    def test_validate_fala_candidate_rejects_aggregate_artifact(self):
        from tools.deployment_parity import AGGREGATE_FALA_LABEL, validate_fala_candidate

        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        root = Path(holder.name)
        versions = root / "versions"
        candidate = versions / ("b" * 64)
        launchd = candidate / "launchd"
        launchd.mkdir(parents=True)
        aggregate = launchd / f"{AGGREGATE_FALA_LABEL}.plist"
        aggregate.write_text("<?xml version=\"1.0\"?><plist version=\"1.0\"><dict></dict></plist>", encoding="utf-8")
        process_artifact = launchd / "com.mikolaj92.lokay.repo-issue-poll.plist"
        process_artifact.write_text("<?xml version=\"1.0\"?><plist version=\"1.0\"><dict></dict></plist>", encoding="utf-8")
        (candidate / "manifest.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(DeploymentParityError) as raised:
            validate_fala_candidate(candidate, deployment_root=root)
        errors = raised.exception.result["errors"]
        self.assertTrue(any("aggregate production launchd artifact is forbidden" in error for error in errors), errors)
        self.assertTrue(any("per-process production launchd artifact is forbidden" in error for error in errors), errors)



if __name__ == "__main__":
    unittest.main()
