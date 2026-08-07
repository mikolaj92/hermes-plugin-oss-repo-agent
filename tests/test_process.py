from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import sys
import tempfile
import unittest

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

from lokay.process_contracts import (
    FORBIDDEN_PATH_ALIASES,
    PROCESS_CONTRACTS,
    PROCESS_CONTRACT_LIST,
    contract_for,
)
from lokay.registry import MIGRATION_DEFAULTS, PROCESS_GRAPH_CONTRACT, PROCESS_IDS, canonical_toml, process_defaults
from lokay.process_runtime import HealthRecord, ProcessRuntime, ProcessRuntimeError
from lokay.flows.runtime import HostPathRunResult, JournalProcess



def _host_result(
    *,
    process_id: str = "pr_merge",
    run_id: str = "run-test",
    run_status: str = "completed",
    effector_id: str = "merge_pr",
    head_oid: str = "head-1",
) -> HostPathRunResult:
    process_status = "succeeded" if run_status == "completed" else "failed"
    if process_id == "pr_merge" and run_status == "completed":
        specs = (
            ("build_merge_receipt", {"values": {"repo": "owner/repo", "number": 1, "head_oid": head_oid, "status": "merge_verified"}}),
            ("verify_merge_receipt", {"values": {"repo": "owner/repo", "number": 1, "head_oid": head_oid, "status": "finalization"}}),
        )
    else:
        specs = ((effector_id, {}),)
    processes = tuple(
        JournalProcess(
            id=f"{run_id}:{process_id}:{step_id}",
            status=process_status,
            attempt=1,
            max_attempts=1,
            output=output,
            error={},
            metadata={"correlation_path_id": process_id, "effector_id": step_id},
            correlation_path_id=process_id,
            effector_id=step_id,
        )
        for step_id, output in specs
    )
    return HostPathRunResult(
        run_id=run_id,
        path_id=process_id,
        run_status=run_status,
        replayed=False,
        ticks=1,
        processes=processes,
        package_id="lokay",
        package_version="0.0.0",
        package_digest="a" * 64,
        correlation_path_digest="b" * 64,
        runtime_version="0.7.15",
        backend_version="0.7.15",
        schema_version=1,
    )
def _cleanup_host_result(run_id: str) -> HostPathRunResult:
    process_row = JournalProcess(
        id=f"{run_id}:cleanup_reconcile:publish_reconcile_receipt",
        status="succeeded",
        attempt=1,
        max_attempts=1,
        output={
            "values": {
                "repo": "owner/repo",
                "number": 7,
                "status": "reconciled",
                "ok": True,
                "mutated": False,
                "mutation_status": "noop",
            }
        },
        error={},
        metadata={
            "correlation_path_id": "cleanup_reconcile",
            "effector_id": "publish_reconcile_receipt",
        },
        correlation_path_id="cleanup_reconcile",
        effector_id="publish_reconcile_receipt",
    )
    return HostPathRunResult(
        run_id=run_id,
        path_id="cleanup_reconcile",
        run_status="completed",
        replayed=False,
        ticks=1,
        processes=(process_row,),
        package_id="lokay",
        package_version="0.0.0",
        package_digest="a" * 64,
        correlation_path_digest="b" * 64,
        runtime_version="0.7.15",
        backend_version="0.7.15",
        schema_version=1,
    )

def _seed_predecessor(
    runtime: ProcessRuntime,
    *,
    generation: str,
    candidate_id: str,
    config_sha256: str,
    repo: str = "owner/repo",
    number: int = 1,
    head_oid: str = "head-1",
) -> None:
    subject = {"repo": repo, "number": number, "head_oid": head_oid}
    runtime.publish_receipt(
        process_id="pr_triage",
        receipt_kind="pr_decision",
        subject=subject,
        payload={"status": "decided", "ok": True, "action": "merge", **subject},
        generation=generation,
        candidate_id=candidate_id,
        config_sha256=config_sha256,
    )

def _cleanup_evidence_payload(subject: Mapping[str, Any]) -> dict[str, Any]:
    repo = str(subject["repo"])
    number = int(subject["number"])
    return {
        "repo": repo,
        "number": number,
        "issue": number,
        "pr_number": number + 100,
        "task_id": f"task-{number}",
        "branch": f"ai/fix/{number}",
        "clone_path": f"/tmp/{number}/clone",
        "worktree_path": f"/tmp/{number}/worktree",
        "task_receipt_path": f"/tmp/{number}/task.json",
        "claim_path": f"/tmp/{number}/claim.json",
        "merge_receipt_path": f"/tmp/{number}/merge.json",
        "receipt_path": f"/tmp/{number}/cleanup.json",
        "db_path": f"/tmp/{number}/fala.sqlite",
        "base_sha": "b" * 40,
        "head_oid": "c" * 40,
        "merge_oid": "d" * 40,
        "origin_main_sha": "e" * 40,
        "remote_retention_authorized": True,
    }



def _document(*, mode: str = "dry-run", enabled: dict[str, bool] | None = None) -> dict:
    enabled = enabled or {}
    processes = process_defaults()
    for row in processes:
        if row["id"] in enabled:
            row["enabled"] = enabled[row["id"]]
    return {
        "version": 1,
        "mode": mode,
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
        "processes": processes,
    }


def _write_config(directory: Path, document: dict) -> Path:
    path = directory / "config.toml"
    path.write_bytes(canonical_toml(document))
    return path


class ProcessDispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.process = importlib.import_module("lokay.process")

    def test_valid_catalog_dispatch_is_planned_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root, _document(mode="dry-run"))
            db = root / "state.sqlite"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = self.process.main(
                    [
                        "lokay-process-repo_issue_poll",
                        "--config",
                        str(config),
                        "--db",
                        str(db),
                        "--json",
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "planned")
            self.assertEqual(payload["process_id"], "repo_issue_poll")
            self.assertEqual(payload["command"], "lokay-process-repo_issue_poll")
            self.assertTrue(payload["dry_run"])
            self.assertFalse(payload["mutated"])
            self.assertFalse(db.exists())

    def test_state_root_fallback_matches_rendered_process_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "nested" / "state.sqlite"
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(self.process._state_root(db), db.parent.resolve() / "process-state")

    def test_each_canonical_process_has_independent_adapter(self) -> None:
        adapters = self.process.PROCESS_ADAPTERS
        self.assertEqual(set(adapters), set(PROCESS_IDS))
        self.assertEqual(len(adapters), 12)
        # No shared alias objects: each process owns a distinct callable.
        self.assertEqual(len(set(id(fn) for fn in adapters.values())), 12)
        for process_id, adapter in adapters.items():
            self.assertEqual(adapter.__name__, f"adapter_{process_id}")

    def test_unknown_command_rejects_before_adapter_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root, _document())
            db = root / "state.sqlite"
            side_effects: list[str] = []

            def boom(*_args, **_kwargs):
                side_effects.append("adapter")
                raise AssertionError("adapter must not run")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.dict(
                self.process.PROCESS_ADAPTERS,
                {process_id: boom for process_id in PROCESS_IDS},
            ):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = self.process.main(
                        [
                            "lokay-process-not_a_real_process",
                            "--config",
                            str(config),
                            "--db",
                            str(db),
                            "--json",
                        ]
                    )
            self.assertEqual(code, 2)
            self.assertEqual(side_effects, [])
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["ok"])
            self.assertIn("unknown process", payload["error"].lower() + payload["reason"].lower() + stdout.getvalue().lower() + stderr.getvalue().lower())

    def test_disabled_process_rejects_before_adapter_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(
                root,
                _document(enabled={"issue_triage": False}),
            )
            db = root / "state.sqlite"
            side_effects: list[str] = []

            def boom(*_args, **_kwargs):
                side_effects.append("adapter")
                raise AssertionError("adapter must not run")

            stdout = io.StringIO()
            with mock.patch.dict(
                self.process.PROCESS_ADAPTERS,
                {process_id: boom for process_id in PROCESS_IDS},
            ):
                with redirect_stdout(stdout):
                    code = self.process.main(
                        [
                            "lokay-process-issue_triage",
                            "--config",
                            str(config),
                            "--db",
                            str(db),
                            "--json",
                        ]
                    )
            self.assertEqual(code, 2)
            self.assertEqual(side_effects, [])
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["ok"])
            self.assertIn("disabled", payload["error"].lower())

    def test_command_identity_must_match_catalog_command_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            document = _document()
            poll = next(item for item in document["processes"] if item["id"] == "repo_issue_poll")
            poll["command"] = "lokay-process-issue_triage"
            config = _write_config(root, document)
            db = root / "state.sqlite"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = self.process.main(
                    [
                        "lokay-process-repo_issue_poll",
                        "--config",
                        str(config),
                        "--db",
                        str(db),
                        "--json",
                    ]
                )
            self.assertEqual(code, 2)
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["ok"])
            self.assertRegex(payload["error"].lower(), r"stale|mismatch|command")

    def test_live_against_dry_run_config_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root, _document(mode="dry-run"))
            db = root / "state.sqlite"
            side_effects: list[str] = []

            def boom(*_args, **_kwargs):
                side_effects.append("adapter")
                raise AssertionError("adapter must not run")

            stdout = io.StringIO()
            with mock.patch.dict(
                self.process.PROCESS_ADAPTERS,
                {process_id: boom for process_id in PROCESS_IDS},
            ):
                with redirect_stdout(stdout):
                    code = self.process.main(
                        [
                            "lokay-process-cleanup",
                            "--config",
                            str(config),
                            "--db",
                            str(db),
                            "--live",
                            "--json",
                        ]
                    )
            self.assertEqual(code, 2)
            self.assertEqual(side_effects, [])
            payload = json.loads(stdout.getvalue())
            self.assertIn("live", payload["error"].lower())

    def test_live_mode_fails_closed_without_runtime_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root, _document(mode="live"))
            db = root / "state.sqlite"
            state_root = root / "process-state"
            generation_path = root / "generation"
            stdout = io.StringIO()
            env = {
                "HERMES_LOKAY_PROCESS_STATE_ROOT": str(state_root),
                "HERMES_LOKAY_GENERATION_PATH": str(generation_path),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                for key in (
                    "HERMES_LOKAY_GENERATION",
                    "LOKAY_GENERATION",
                    "FALA_CANDIDATE_ID",
                    "HERMES_LOKAY_CANDIDATE_ID",
                    "LOKAY_CANDIDATE_ID",
                    "HERMES_LOKAY_CONFIG_SHA256",
                    "LOKAY_CONFIG_SHA256",
                ):
                    os.environ.pop(key, None)
                with redirect_stdout(stdout):
                    code = self.process.main(
                        [
                            "lokay-process-pr_merge",
                            "--config",
                            str(config),
                            "--db",
                            str(db),
                            "--live",
                            "--json",
                        ]
                    )
            self.assertEqual(code, 1)
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["reason"], "runtime_identity_missing")
            self.assertFalse(payload["mutated"])
            self.assertFalse(db.exists())
            self.assertFalse(state_root.exists())
            self.assertFalse(generation_path.exists())

    def test_live_mode_durable_lifecycle_with_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root, _document(mode="live"))
            db = root / "fala" / "state.sqlite"
            db.parent.mkdir(parents=True, exist_ok=True)
            state_root = root / "process-state"
            generation_path = root / "generation"
            generation_path.parent.mkdir(parents=True, exist_ok=True)
            generation = ProcessRuntime.open(
                state_root,
                dry_run=False,
                generation_path=generation_path,
            ).write_generation("gen-live-1")
            candidate = "c" * 64
            config_sha = hashlib.sha256(config.read_bytes()).hexdigest()
            _seed_predecessor(
                ProcessRuntime.open(state_root, dry_run=False, generation_path=generation_path),
                generation=generation,
                candidate_id=candidate,
                config_sha256=config_sha,
            )
            stdout = io.StringIO()
            env = {
                "HERMES_LOKAY_PROCESS_STATE_ROOT": str(state_root),
                "HERMES_LOKAY_GENERATION_PATH": str(generation_path),
                "FALA_CANDIDATE_ID": candidate,
            }
            host = _host_result(process_id="pr_merge", run_id="run-live-1")
            runner = mock.AsyncMock(return_value=host)
            with mock.patch.dict(os.environ, env, clear=False):
                for key in (
                    "HERMES_LOKAY_GENERATION",
                    "LOKAY_GENERATION",
                    "HERMES_LOKAY_CONFIG_SHA256",
                    "LOKAY_CONFIG_SHA256",
                ):
                    os.environ.pop(key, None)
                with mock.patch(
                    "lokay.flows.runtime.run_package_path_async",
                    new=runner,
                ):
                    with redirect_stdout(stdout):
                        code = self.process.main(
                            [
                                "lokay-process-pr_merge",
                                "--config",
                                str(config),
                                "--db",
                                str(db),
                                "--live",
                                "--json",
                            ]
                        )
            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["reason"], "completed")
            self.assertFalse(payload["mutated"])
            self.assertEqual(payload["process_id"], "pr_merge")
            self.assertEqual(payload["path_id"], "pr_merge")
            self.assertEqual(payload["generation"], generation)
            self.assertEqual(payload["candidate_id"], candidate)
            self.assertEqual(payload["config_sha256"], config_sha)
            self.assertEqual(payload["package_id"], "lokay")
            self.assertEqual(payload["fala"]["path_id"], "pr_merge")
            self.assertEqual(payload["fala"]["run_status"], "completed")
            self.assertEqual(
                {item["receipt_kind"] for item in payload["receipts"]},
                set(PROCESS_CONTRACTS["pr_merge"].output_receipts),
            )
            self.assertTrue(all(item["status"] == "written" for item in payload["receipts"]))
            contract = PROCESS_CONTRACTS["pr_merge"]
            self.assertEqual(payload["allowed_effectors"], list(contract.allowed_effectors))
            self.assertEqual(payload["required_inputs"], list(contract.required_inputs))
            self.assertEqual(
                payload["predecessor_groups"],
                [list(group) for group in contract.predecessor_groups],
            )
            self.assertEqual(payload["output_receipts"], list(contract.output_receipts))
            runner.assert_awaited_once()
            kwargs = runner.await_args.kwargs
            self.assertEqual(kwargs["path_id"], "pr_merge")
            self.assertEqual(kwargs["allowed_effectors"], list(contract.allowed_effectors))
            self.assertEqual(kwargs["max_ticks"], contract.max_ticks)
            self.assertEqual(Path(kwargs["package_path"]).name, "fala-package.toml")
            self.assertEqual(kwargs["inputs"]["path_id"], "pr_merge")
            self.assertEqual(kwargs["inputs"]["required_inputs"], list(contract.required_inputs))
            self.assertEqual(
                kwargs["inputs"]["predecessor_groups"],
                [list(group) for group in contract.predecessor_groups],
            )
            self.assertEqual(kwargs["inputs"]["output_receipts"], list(contract.output_receipts))
            self.assertIn("pr_decision", kwargs["inputs"])
            self.assertNotIn(kwargs["path_id"], FORBIDDEN_PATH_ALIASES)
            self.assertNotEqual(kwargs["path_id"], "tick_all")
            self.assertNotEqual(kwargs["path_id"], "auto_worker")
            self.assertNotEqual(kwargs["path_id"], "pr_triage")
            self.assertNotEqual(kwargs["path_id"], "pr_repair")
            runtime = ProcessRuntime.open(
                state_root,
                dry_run=False,
                generation_path=generation_path,
            )
            health = runtime.read_health("pr_merge")
            self.assertIsNotNone(health)
            assert health is not None
            self.assertEqual(health.status, "ok")
            self.assertEqual(health.generation, generation)
            self.assertTrue((state_root / "process-state.sqlite3").is_file())

    def test_live_mode_does_not_invoke_sibling_or_aggregate_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root, _document(mode="live"))
            db = root / "fala" / "state.sqlite"
            db.parent.mkdir(parents=True, exist_ok=True)
            state_root = root / "process-state"
            generation_path = root / "generation"
            generation_path.parent.mkdir(parents=True, exist_ok=True)
            generation = ProcessRuntime.open(
                state_root,
                dry_run=False,
                generation_path=generation_path,
            ).write_generation("gen-live-path")
            candidate = "e" * 64
            config_sha = hashlib.sha256(config.read_bytes()).hexdigest()
            _seed_predecessor(
                ProcessRuntime.open(state_root, dry_run=False, generation_path=generation_path),
                generation=generation,
                candidate_id=candidate,
                config_sha256=config_sha,
            )
            env = {
                "HERMES_LOKAY_PROCESS_STATE_ROOT": str(state_root),
                "HERMES_LOKAY_GENERATION_PATH": str(generation_path),
                "FALA_CANDIDATE_ID": candidate,
            }
            host = _host_result(process_id="pr_merge", run_id="run-path-1")
            runner = mock.AsyncMock(return_value=host)
            sibling = mock.AsyncMock(side_effect=AssertionError("sibling path invoked"))
            with mock.patch.dict(os.environ, env, clear=False):
                for key in (
                    "HERMES_LOKAY_GENERATION",
                    "LOKAY_GENERATION",
                    "HERMES_LOKAY_CONFIG_SHA256",
                    "LOKAY_CONFIG_SHA256",
                ):
                    os.environ.pop(key, None)
                with (
                    mock.patch("lokay.flows.runtime.run_package_path_async", new=runner),
                    mock.patch("lokay.tick_all.run_all", new=sibling),
                    mock.patch("lokay.flows.triage.run_package_path_async", new=sibling),
                    mock.patch("lokay.flows.cleanup.run_package_path_async", new=sibling),
                    redirect_stdout(io.StringIO()),
                ):
                    code = self.process.main(
                        [
                            "lokay-process-pr_merge",
                            "--config",
                            str(config),
                            "--db",
                            str(db),
                            "--live",
                            "--json",
                        ]
                    )
            self.assertEqual(code, 0)
            runner.assert_awaited_once()
            self.assertEqual(runner.await_args.kwargs["path_id"], "pr_merge")
            sibling.assert_not_awaited()



    def test_live_mode_fails_closed_when_health_not_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root, _document(mode="live"))
            db = root / "fala" / "state.sqlite"
            db.parent.mkdir(parents=True, exist_ok=True)
            state_root = root / "process-state"
            generation_path = root / "generation"
            generation_path.parent.mkdir(parents=True, exist_ok=True)
            generation = ProcessRuntime.open(
                state_root,
                dry_run=False,
                generation_path=generation_path,
            ).write_generation("gen-live-health")
            candidate = "d" * 64
            config_sha = hashlib.sha256(config.read_bytes()).hexdigest()
            _seed_predecessor(
                ProcessRuntime.open(state_root, dry_run=False, generation_path=generation_path),
                generation=generation,
                candidate_id=candidate,
                config_sha256=config_sha,
            )
            stdout = io.StringIO()
            bad_health = HealthRecord(
                process_id="pr_merge",
                status="stale_reclaimed",
                owner="lokay-process-pr_merge",
                lease_expires_at=None,
                last_exit=0,
                last_error=None,
                attempt=0,
                generation=generation,
                updated_at="2026-01-01T00:00:00Z",
                details={"subject": "lifecycle"},
            )
            env = {
                "HERMES_LOKAY_PROCESS_STATE_ROOT": str(state_root),
                "HERMES_LOKAY_GENERATION_PATH": str(generation_path),
                "FALA_CANDIDATE_ID": candidate,
            }
            host = _host_result(process_id="pr_merge", run_id="run-health")
            runner = mock.AsyncMock(return_value=host)
            with mock.patch.dict(os.environ, env, clear=False):
                for key in (
                    "HERMES_LOKAY_GENERATION",
                    "LOKAY_GENERATION",
                    "HERMES_LOKAY_CONFIG_SHA256",
                    "LOKAY_CONFIG_SHA256",
                ):
                    os.environ.pop(key, None)
                with mock.patch(
                    "lokay.flows.runtime.run_package_path_async",
                    new=runner,
                ), mock.patch.object(ProcessRuntime, "read_health", return_value=bad_health):
                    with redirect_stdout(stdout):
                        code = self.process.main(
                            [
                                "lokay-process-pr_merge",
                                "--config",
                                str(config),
                                "--db",
                                str(db),
                                "--live",
                                "--json",
                            ]
                        )
            self.assertEqual(code, 1)
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["reason"], "process_health_invalid")
            self.assertFalse(payload["mutated"])
            self.assertIn("stale_reclaimed", payload["error"])
            runner.assert_awaited_once()
            self.assertEqual(runner.await_args.kwargs["path_id"], "pr_merge")

    def test_live_mode_fails_closed_on_failed_fala_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root, _document(mode="live"))
            db = root / "fala" / "state.sqlite"
            db.parent.mkdir(parents=True, exist_ok=True)
            state_root = root / "process-state"
            generation_path = root / "generation"
            generation_path.parent.mkdir(parents=True, exist_ok=True)
            generation = ProcessRuntime.open(
                state_root,
                dry_run=False,
                generation_path=generation_path,
            ).write_generation("gen-live-fail")
            candidate = "f" * 64
            config_sha = hashlib.sha256(config.read_bytes()).hexdigest()
            _seed_predecessor(
                ProcessRuntime.open(state_root, dry_run=False, generation_path=generation_path),
                generation=generation,
                candidate_id=candidate,
                config_sha256=config_sha,
            )
            stdout = io.StringIO()
            host = _host_result(
                process_id="pr_merge",
                run_id="run-fail",
                run_status="failed",
            )
            runner = mock.AsyncMock(return_value=host)
            env = {
                "HERMES_LOKAY_PROCESS_STATE_ROOT": str(state_root),
                "HERMES_LOKAY_GENERATION_PATH": str(generation_path),
                "FALA_CANDIDATE_ID": candidate,
            }
            with mock.patch.dict(os.environ, env, clear=False):
                for key in (
                    "HERMES_LOKAY_GENERATION",
                    "LOKAY_GENERATION",
                    "HERMES_LOKAY_CONFIG_SHA256",
                    "LOKAY_CONFIG_SHA256",
                ):
                    os.environ.pop(key, None)
                with mock.patch(
                    "lokay.flows.runtime.run_package_path_async",
                    new=runner,
                ), redirect_stdout(stdout):
                    code = self.process.main(
                        [
                            "lokay-process-pr_merge",
                            "--config",
                            str(config),
                            "--db",
                            str(db),
                            "--live",
                            "--json",
                        ]
                    )
            self.assertEqual(code, 1)
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["reason"], "failed")
            self.assertFalse(payload["mutated"])
            runner.assert_awaited_once()

    def test_dry_run_does_not_call_fala(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root, _document(mode="dry-run"))
            db = root / "state.sqlite"
            runner = mock.AsyncMock(side_effect=AssertionError("Fala invoked in dry-run"))
            stdout = io.StringIO()
            with mock.patch(
                "lokay.flows.runtime.run_package_path_async",
                new=runner,
            ), redirect_stdout(stdout):
                code = self.process.main(
                    [
                        "lokay-process-pr_merge",
                        "--config",
                        str(config),
                        "--db",
                        str(db),
                        "--dry-run",
                        "--json",
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["status"], "planned")
            runner.assert_not_awaited()
    def test_dry_run_and_live_conflict(self) -> None:
        code = self.process.main(
            [
                "lokay-process-repo_issue_poll",
                "--config",
                "config.toml",
                "--db",
                "/tmp/x",
                "--dry-run",
                "--live",
            ]
        )
        self.assertEqual(code, 2)

    def test_validation_does_not_import_flows_or_tick_all(self) -> None:
        banned = {
            "lokay.tick_all",
            "lokay.flows.intake",
            "lokay.flows.issue_to_pr",
            "lokay.flows.triage",
            "lokay.flows.cleanup",
            "lokay.effector",
        }
        before = {name for name in sys.modules if name in banned}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root, _document())
            db = root / "state.sqlite"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = self.process.main(
                    [
                        "lokay-process-issue_ready",
                        "--config",
                        str(config),
                        "--db",
                        str(db),
                        "--json",
                    ]
                )
            self.assertEqual(code, 0)
        after = {name for name in sys.modules if name in banned}
        self.assertEqual(after, before)

    def test_twelve_explicit_process_contracts(self) -> None:
        self.assertEqual(tuple(PROCESS_CONTRACTS), PROCESS_IDS)
        self.assertEqual(len(PROCESS_CONTRACT_LIST), 12)
        self.assertEqual(len(set(id(item) for item in PROCESS_CONTRACT_LIST)), 12)
        for process_id in PROCESS_IDS:
            contract = contract_for(process_id)
            self.assertIs(PROCESS_CONTRACTS[process_id], contract)
            self.assertEqual(contract.process_id, process_id)
            self.assertEqual(contract.path_id, process_id)
            self.assertNotIn(contract.path_id, FORBIDDEN_PATH_ALIASES)
            self.assertTrue(contract.allowed_effectors)
            self.assertGreaterEqual(contract.max_ticks, len(contract.allowed_effectors))
            self.assertEqual(
                contract.output_receipts,
                tuple(PROCESS_GRAPH_CONTRACT[process_id]["output_receipts"]),
            )
            self.assertEqual(
                contract.predecessor_groups,
                tuple(
                    tuple(group)
                    for group in PROCESS_GRAPH_CONTRACT[process_id]["predecessor_groups"]
                ),
            )
            self.assertFalse(
                set(contract.allowed_effectors) & set(contract.forbidden_sibling_effectors)
            )
            adapter = self.process.PROCESS_ADAPTERS[process_id]
            self.assertIs(adapter.contract, contract)
            self.assertEqual(adapter.__name__, f"adapter_{process_id}")

    def test_sibling_effector_ownership_is_disjoint(self) -> None:
        issue_group = ("issue_feedback", "issue_split", "issue_close", "issue_ready")
        pr_group = ("pr_triage", "pr_repair", "pr_merge")
        for group in (issue_group, pr_group):
            for index, left in enumerate(group):
                left_set = set(PROCESS_CONTRACTS[left].allowed_effectors)
                for right in group[index + 1 :]:
                    right_set = set(PROCESS_CONTRACTS[right].allowed_effectors)
                    self.assertFalse(
                        left_set & right_set,
                        msg=f"ownership overlap {left}/{right}",
                    )
                    self.assertTrue(
                        set(PROCESS_CONTRACTS[left].forbidden_sibling_effectors)
                        >= right_set
                    )
                    self.assertTrue(
                        set(PROCESS_CONTRACTS[right].forbidden_sibling_effectors)
                        >= left_set
                    )

    def test_contracts_reject_aggregate_path_aliases(self) -> None:
        aliases = sorted(FORBIDDEN_PATH_ALIASES)
        self.assertIn("tick_all", aliases)
        self.assertIn("auto_worker", aliases)
        self.assertIn("issue_intake", aliases)
        for process_id, contract in PROCESS_CONTRACTS.items():
            self.assertNotIn(contract.path_id, FORBIDDEN_PATH_ALIASES)
            self.assertNotEqual(contract.path_id, "tick_all")
            self.assertNotEqual(contract.path_id, "auto_worker")
            self.assertNotEqual(contract.path_id, "issue_intake")
            self.assertNotIn("tick_all", contract.allowed_effectors)
            self.assertNotIn("auto_worker", contract.allowed_effectors)

    def test_adapters_are_not_generated_alias_map(self) -> None:
        source = Path(self.process.__file__).read_text(encoding="utf-8")
        self.assertIn('_bind_adapter(PROCESS_CONTRACTS["repo_issue_poll"])', source)
        self.assertIn('_bind_adapter(PROCESS_CONTRACTS["cleanup_reconcile"])', source)
        self.assertNotIn(
            "process_id: _make_adapter(process_id) for process_id in PROCESS_IDS",
            source,
        )
        self.assertNotIn(
            "process_id: _bind_adapter(PROCESS_CONTRACTS[process_id]) for process_id in PROCESS_IDS",
            source,
        )
        adapters = self.process.PROCESS_ADAPTERS
        self.assertEqual(tuple(adapters), PROCESS_IDS)
        # Distinct callables and distinct bound contracts.
        self.assertEqual(len(set(id(fn) for fn in adapters.values())), 12)
        self.assertEqual(len(set(id(fn.contract) for fn in adapters.values())), 12)

    def test_dry_run_planned_payload_exposes_contract_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root, _document())
            db = root / "state.sqlite"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = self.process.main(
                    [
                        "lokay-process-issue_feedback",
                        "--config",
                        str(config),
                        "--db",
                        str(db),
                        "--json",
                        "--dry-run",
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            contract = PROCESS_CONTRACTS["issue_feedback"]
            self.assertEqual(payload["path_id"], "issue_feedback")
            self.assertEqual(payload["allowed_effectors"], list(contract.allowed_effectors))
            self.assertEqual(
                payload["forbidden_sibling_effectors"],
                list(contract.forbidden_sibling_effectors),
            )
            self.assertEqual(payload["max_ticks"], contract.max_ticks)
            self.assertEqual(payload["lock_scope"], contract.lock_scope)
            self.assertEqual(payload["contract"]["path_id"], "issue_feedback")
            self.assertIn("split_mixed_triage_issue", payload["forbidden_sibling_effectors"])
            self.assertIn("close_triage_issue", payload["forbidden_sibling_effectors"])
            self.assertNotIn("ensure_triage_label", payload["forbidden_sibling_effectors"])

    def test_module_is_not_tick_all_alias(self) -> None:
        source = Path(self.process.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import lokay.tick_all", source)
        self.assertNotIn("from lokay.tick_all", source)
        self.assertNotIn("run_all", source)
        self.assertIn("PROCESS_ADAPTERS", source)
        self.assertIn("PROCESS_CONTRACTS", source)
        self.assertNotIn("tick_all", self.process.PROCESS_ADAPTERS)
        self.assertNotIn("auto_worker", self.process.PROCESS_ADAPTERS)
    def test_split_output_emits_all_declared_receipt_candidates(self) -> None:
        contract = PROCESS_CONTRACTS["issue_split"]
        output = {
            "values": {
                "status": "split_verified",
                "action": "split",
                "repo": "owner/one",
                "number": 10,
                "decision_digest": "d" * 64,
                "children": [
                    {"number": 11, "kind": "ready", "marker": "child-11"},
                    {"number": 12, "kind": "feedback", "marker": "child-12"},
                ],
                "parent_marker": "parent-10",
            }
        }
        mapped = self.process._OUTPUT_RECEIPT_EFFECTORS["issue_split"]["split_mixed_triage_issue"]
        self.assertIsInstance(mapped, tuple)
        candidates = self.process._receipt_candidates(
            output,
            set(contract.output_receipts),
            mapped_kinds=tuple(mapped),
        )
        self.assertEqual(
            [kind for kind, _payload, _subject, _action in candidates],
            ["split", "split_verified", "child_handoff", "child_handoff"],
        )
        self.assertEqual(candidates[0][2], {"repo": "owner/one", "number": 10})
        self.assertEqual(candidates[1][2], {"repo": "owner/one", "number": 10})
        self.assertEqual(candidates[2][2], {"repo": "owner/one", "number": 11})
        self.assertEqual(candidates[3][2], {"repo": "owner/one", "number": 12})
        self.assertTrue(all(action == "split" for _kind, _payload, _subject, action in candidates))

    def test_cleanup_external_input_resolves_into_effective_run_inputs(self) -> None:
        process = self.process
        contract = PROCESS_CONTRACTS["cleanup_reconcile"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generation_path = root / "generation"
            state_root = root / "process-state"
            runtime = ProcessRuntime.open(
                state_root,
                dry_run=False,
                generation_path=generation_path,
            )
            generation = runtime.write_generation("gen-cleanup-external")
            candidate = "a" * 64
            config_sha = "f" * 64
            subject = {"repo": "owner/repo", "number": 7}
            payload = _cleanup_evidence_payload(subject)
            record = runtime.publish_external_input(
                process_id="cleanup_reconcile",
                input_kind="unresolved_cleanup_evidence",
                subject=subject,
                payload=payload,
                generation=generation,
                candidate_id=candidate,
                config_sha256=config_sha,
            )
            self.assertIn(record.status, {"written", "exists"})
            resolved_subject, evidence = process._resolve_predecessor_evidence(
                runtime,
                contract=contract,
                generation=generation,
                candidate_id=candidate,
                config_sha256=config_sha,
            )
            self.assertEqual(resolved_subject, subject)
            self.assertEqual(evidence["unresolved_cleanup_evidence"], payload)
            effective = process.build_effective_run(
                contract=contract,
                process_id="cleanup_reconcile",
                run_id="run-cleanup-external",
                db_path=root / "fala.sqlite",
                cfg=None,
                generation=generation,
                candidate_id=candidate,
                config_sha256=config_sha,
                command="lokay-process-cleanup_reconcile",
                subject=resolved_subject,
                predecessor_evidence=evidence,
            )
            inputs = effective["inputs"]
            self.assertEqual(inputs["unresolved_cleanup_evidence"], payload)
            for field, value in payload.items():
                self.assertEqual(inputs["unresolved_cleanup_evidence"][field], value)
                for effector_inputs in effective["effector_inputs"].values():
                    if field == "db_path":
                        self.assertEqual(effector_inputs[field], str(root / "fala.sqlite"))
                    else:
                        self.assertEqual(effector_inputs[field], value)

    def test_cleanup_external_input_selects_earliest_valid_backlog_subject(self) -> None:
        process = self.process
        contract = PROCESS_CONTRACTS["cleanup_reconcile"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generation_path = root / "generation"
            runtime = ProcessRuntime.open(
                root / "process-state",
                dry_run=False,
                generation_path=generation_path,
            )
            generation = runtime.write_generation("gen-cleanup-order")
            candidate = "b" * 64
            config_sha = "e" * 64
            for number in (7, 8):
                subject = {"repo": "owner/repo", "number": number}
                runtime.publish_external_input(
                    process_id="cleanup_reconcile",
                    input_kind="unresolved_cleanup_evidence",
                    subject=subject,
                    payload=_cleanup_evidence_payload(subject),
                    generation=generation,
                    candidate_id=candidate,
                    config_sha256=config_sha,
                )
            selected_subject, evidence = process._resolve_predecessor_evidence(
                runtime,
                contract=contract,
                generation=generation,
                candidate_id=candidate,
                config_sha256=config_sha,
                cursor_key="cleanup__unresolved",
            )
            self.assertEqual(selected_subject, {"repo": "owner/repo", "number": 7})
            self.assertEqual(evidence["cursor_key"], "cleanup__unresolved")
            self.assertEqual(
                json.loads(evidence["cursor_value"]),
                [runtime.list_external_inputs(
                    process_id="cleanup_reconcile",
                    input_kind="unresolved_cleanup_evidence",
                )[0]["created_at"], process.subject_key(selected_subject)],
            )

    def test_cleanup_reconcile_adapter_publishes_reconciliation(self) -> None:
        process = self.process
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root, _document(mode="live"))
            db = root / "fala" / "state.sqlite"
            state_root = root / "process-state"
            generation_path = root / "generation"
            runtime = ProcessRuntime.open(
                state_root,
                dry_run=False,
                generation_path=generation_path,
            )
            generation = runtime.write_generation("gen-cleanup-adapter")
            candidate = "e" * 64
            config_sha = hashlib.sha256(config.read_bytes()).hexdigest()
            subject = {"repo": "owner/repo", "number": 7}
            evidence = _cleanup_evidence_payload(subject)
            runtime.publish_external_input(
                process_id="cleanup_reconcile",
                input_kind="unresolved_cleanup_evidence",
                subject=subject,
                payload=evidence,
                generation=generation,
                candidate_id=candidate,
                config_sha256=config_sha,
            )
            run_id = "run-cleanup-adapter"
            process_row = JournalProcess(
                id=f"{run_id}:cleanup_reconcile:publish_reconcile_receipt",
                status="succeeded",
                attempt=1,
                max_attempts=1,
                output={
                    "values": {
                        "repo": "owner/repo",
                        "number": 7,
                        "status": "reconciled",
                        "ok": True,
                        "mutated": False,
                        "mutation_status": "noop",
                    }
                },
                error={},
                metadata={
                    "correlation_path_id": "cleanup_reconcile",
                    "effector_id": "publish_reconcile_receipt",
                },
                correlation_path_id="cleanup_reconcile",
                effector_id="publish_reconcile_receipt",
            )
            host = HostPathRunResult(
                run_id=run_id,
                path_id="cleanup_reconcile",
                run_status="completed",
                replayed=False,
                ticks=1,
                processes=(process_row,),
                package_id="lokay",
                package_version="0.0.0",
                package_digest="a" * 64,
                correlation_path_digest="b" * 64,
                runtime_version="0.7.15",
                backend_version="0.7.15",
                schema_version=1,
            )
            runner = mock.AsyncMock(return_value=host)
            read_cursor_calls: list[tuple[str, str]] = []
            original_read_cursor = ProcessRuntime.read_cursor

            def capture_read_cursor(runtime_self: ProcessRuntime, process_id: str, cursor_key: str):
                read_cursor_calls.append((process_id, cursor_key))
                return original_read_cursor(runtime_self, process_id, cursor_key)

            stdout = io.StringIO()
            env = {
                "HERMES_LOKAY_PROCESS_STATE_ROOT": str(state_root),
                "HERMES_LOKAY_GENERATION_PATH": str(generation_path),
                "FALA_CANDIDATE_ID": candidate,
            }
            with mock.patch.dict(os.environ, env, clear=False):
                for key in (
                    "HERMES_LOKAY_GENERATION",
                    "LOKAY_GENERATION",
                    "HERMES_LOKAY_CONFIG_SHA256",
                    "LOKAY_CONFIG_SHA256",
                ):
                    os.environ.pop(key, None)
                with (
                    mock.patch("lokay.flows.runtime.run_package_path_async", new=runner),
                    mock.patch.object(ProcessRuntime, "read_cursor", new=capture_read_cursor),
                    redirect_stdout(stdout),
                ):
                    code = process.main(
                        [
                            "lokay-process-cleanup_reconcile",
                            "--config",
                            str(config),
                            "--db",
                            str(db),
                            "--live",
                            "--json",
                        ]
                    )
            self.assertEqual(code, 0, stdout.getvalue())
            result = json.loads(stdout.getvalue())
            self.assertTrue(result["ok"])
            self.assertEqual(result["process_id"], "cleanup_reconcile")
            self.assertEqual(result["receipts"][0]["receipt_kind"], "cleanup_reconciliation")
            self.assertEqual(result["receipts"][0]["status"], "written")
            self.assertEqual(result["action"], None)
            self.assertIn(("cleanup_reconcile", "cleanup__unresolved"), read_cursor_calls)
            runner.assert_awaited_once()
            self.assertEqual(
                runner.await_args.kwargs["inputs"]["unresolved_cleanup_evidence"],
                evidence,
            )
            cursor = runtime.read_cursor("cleanup_reconcile", "cleanup__unresolved")
            self.assertIsNotNone(cursor)
            assert cursor is not None
            self.assertEqual(json.loads(cursor.value), [
                runtime.list_external_inputs(
                    process_id="cleanup_reconcile",
                    input_kind="unresolved_cleanup_evidence",
                )[0]["created_at"], process.subject_key(subject),
            ])
    def test_cleanup_replay_reuses_receipt_after_cursor_failure(self) -> None:
        process = self.process
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root, _document(mode="live"))
            db = root / "fala" / "state.sqlite"
            state_root = root / "process-state"
            generation_path = root / "generation"
            runtime = ProcessRuntime.open(
                state_root,
                dry_run=False,
                generation_path=generation_path,
            )
            generation = runtime.write_generation("gen-cleanup-replay")
            candidate = "f" * 64
            config_sha = hashlib.sha256(config.read_bytes()).hexdigest()
            subject = {"repo": "owner/repo", "number": 7}
            runtime.publish_external_input(
                process_id="cleanup_reconcile",
                input_kind="unresolved_cleanup_evidence",
                subject=subject,
                payload=_cleanup_evidence_payload(subject),
                generation=generation,
                candidate_id=candidate,
                config_sha256=config_sha,
            )
            host_one = _cleanup_host_result("run-cleanup-replay-one")
            host_two = _cleanup_host_result("run-cleanup-replay-two")
            runner = mock.AsyncMock(side_effect=[host_one, host_two])
            env = {
                "HERMES_LOKAY_PROCESS_STATE_ROOT": str(state_root),
                "HERMES_LOKAY_GENERATION_PATH": str(generation_path),
                "FALA_CANDIDATE_ID": candidate,
            }

            def invoke() -> tuple[int, dict[str, Any]]:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = process.main(
                        [
                            "lokay-process-cleanup_reconcile",
                            "--config",
                            str(config),
                            "--db",
                            str(db),
                            "--live",
                            "--json",
                        ]
                    )
                return code, json.loads(stdout.getvalue())

            with mock.patch.dict(os.environ, env, clear=False):
                for key in (
                    "HERMES_LOKAY_GENERATION",
                    "LOKAY_GENERATION",
                    "HERMES_LOKAY_CONFIG_SHA256",
                    "LOKAY_CONFIG_SHA256",
                ):
                    os.environ.pop(key, None)
                with mock.patch("lokay.flows.runtime.run_package_path_async", new=runner):
                    with mock.patch.object(
                        ProcessRuntime,
                        "advance_cursor",
                        side_effect=ProcessRuntimeError("forced cursor failure"),
                    ):
                        first_code, first = invoke()
                    self.assertEqual(first_code, 1)
                    self.assertEqual(first["reason"], "receipt_gate_failed")
                    indexed = runtime.list_indexed_receipts(
                        process_id="cleanup_reconcile",
                        receipt_kind="cleanup_reconciliation",
                    )
                    self.assertEqual(len(indexed), 1)
                    first_digest = indexed[0]["digest"]
                    self.assertIsNone(runtime.read_cursor("cleanup_reconcile", "cleanup__unresolved"))

                    second_code, second = invoke()

            self.assertEqual(second_code, 0, second)
            self.assertTrue(second["ok"])
            self.assertNotEqual(first["run_id"], second["run_id"])
            self.assertEqual(second["receipts"][0]["digest"], first_digest)
            cursor = runtime.read_cursor("cleanup_reconcile", "cleanup__unresolved")
            self.assertIsNotNone(cursor)
            assert cursor is not None
            self.assertEqual(cursor.receipt_digest, first_digest)
            self.assertEqual(runner.await_count, 2)
    def test_catalog_cursor_key_rejects_unsafe_values(self) -> None:
        process = self.process
        self.assertEqual(
            process._catalog_cursor_key({"input_cursor": "cleanup/unresolved"}),
            "cleanup__unresolved",
        )
        for value in ("", " ", ".", "..", "cleanup\\unresolved"):
            with self.assertRaisesRegex(ProcessRuntimeError, "catalog input cursor"):
                process._catalog_cursor_key({"input_cursor": value})
        with self.assertRaisesRegex(ProcessRuntimeError, "catalog input cursor"):
            process._catalog_cursor_key({})
    def test_cleanup_external_input_cursor_filters_completed_backlog(self) -> None:
        process = self.process
        contract = PROCESS_CONTRACTS["cleanup_reconcile"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generation_path = root / "generation"
            runtime = ProcessRuntime.open(
                root / "process-state",
                dry_run=False,
                generation_path=generation_path,
            )
            generation = runtime.write_generation("gen-cleanup-cursor")
            candidate = "1" * 64
            config_sha = "2" * 64
            for number in (7, 8):
                subject = {"repo": "owner/repo", "number": number}
                runtime.publish_external_input(
                    process_id="cleanup_reconcile",
                    input_kind="unresolved_cleanup_evidence",
                    subject=subject,
                    payload=_cleanup_evidence_payload(subject),
                    generation=generation,
                    candidate_id=candidate,
                    config_sha256=config_sha,
                )
            rows = runtime.list_external_inputs(
                process_id="cleanup_reconcile",
                input_kind="unresolved_cleanup_evidence",
            )
            first = runtime.publish_receipt(
                process_id="cleanup_reconcile",
                receipt_kind="cleanup_reconciliation",
                subject={"repo": "owner/repo", "number": 7},
                payload={"repo": "owner/repo", "number": 7, "ok": True},
                generation=generation,
                candidate_id=candidate,
                config_sha256=config_sha,
            )
            runtime.advance_cursor(
                process_id="cleanup_reconcile",
                cursor_key="cleanup__unresolved",
                value=process._cleanup_cursor_value(rows[0]),
                receipt_digest=first.digest,
                receipt_path=first.path,
            )
            selected, _evidence = process._resolve_predecessor_evidence(
                runtime,
                contract=contract,
                generation=generation,
                candidate_id=candidate,
                config_sha256=config_sha,
                cursor_key="cleanup__unresolved",
            )
            self.assertEqual(selected, {"repo": "owner/repo", "number": 8})

    def test_cleanup_external_input_rejects_contradictory_nested_subject(self) -> None:
        process = self.process
        contract = PROCESS_CONTRACTS["cleanup_reconcile"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generation_path = root / "generation"
            runtime = ProcessRuntime.open(
                root / "process-state",
                dry_run=False,
                generation_path=generation_path,
            )
            generation = runtime.write_generation("gen-cleanup-conflict")
            candidate = "d" * 64
            config_sha = "c" * 64
            subject = {"repo": "owner/repo", "number": 7}
            payload = _cleanup_evidence_payload(subject)
            payload["subject"] = {"repo": "owner/repo", "number": 8}
            runtime.publish_external_input(
                process_id="cleanup_reconcile",
                input_kind="unresolved_cleanup_evidence",
                subject=subject,
                payload=payload,
                generation=generation,
                candidate_id=candidate,
                config_sha256=config_sha,
            )
            with self.assertRaisesRegex(ProcessRuntimeError, "missing or invalid"):
                process._resolve_predecessor_evidence(
                    runtime,
                    contract=contract,
                    generation=generation,
                    candidate_id=candidate,
                    config_sha256=config_sha,
                )


if __name__ == "__main__":
    unittest.main()
