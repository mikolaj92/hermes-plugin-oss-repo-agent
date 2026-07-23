from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repo_agent.flows.runtime import (
    RuntimeFacadeError,
    read_journal_processes,
    run_package_path,
)


_SCHEMA = """
CREATE TABLE processes (
    run_id TEXT NOT NULL,
    id TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    output_json TEXT NOT NULL,
    error_json TEXT NOT NULL
);
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    metadata TEXT NOT NULL
);
"""


class RuntimeFacadeTests(unittest.TestCase):
    def _db(self, root: Path, rows: list[tuple[object, ...]]) -> Path:
        db = root / "state.sqlite"
        with sqlite3.connect(db) as connection:
            connection.executescript(_SCHEMA)
            connection.executemany(
                "INSERT INTO processes VALUES (?,?,?,?,?,?,?)",
                rows,
            )
            connection.execute("INSERT INTO runs VALUES (?, ?)", ("run-1", "{}"))
        return db

    def test_normalizes_exact_journal_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [
                    ("run-1", "run-1:path:dependent", "cancelled", 0, 1, "{}", '{"reason":"dead_upstream"}'),
                    ("run-1", "run-1:path:fail", "failed", 1, 1, "{}", '{"reason":"semantic failure"}'),
                    ("run-1", "run-1:path:success", "succeeded", 1, 1, '{"value":1}', "{}"),
                ],
            )
            host = {
                "ok": True,
                "run_id": "run-1",
                "run_status": "failed",
                "replayed": False,
                "ticks": 2,
                "processes": [
                    {"id": "run-1:path:success", "status": "succeeded"},
                    {"id": "run-1:path:fail", "status": "failed"},
                    {"id": "run-1:path:dependent", "status": "cancelled"},
                ],
            }
            with patch("repo_agent.flows.runtime.host_run_package", return_value=host) as runner:
                result = run_package_path(
                    db_path=db,
                    package_path=Path(tmp) / "package.toml",
                    path_id="path",
                    run_id="run-1",
                )

        self.assertEqual(result.run_status, "failed")
        self.assertEqual(result.ticks, 2)
        self.assertEqual([process.step_id for process in result.failed], ["dependent", "fail"])
        failed = next(process for process in result.failed if process.step_id == "fail")
        self.assertEqual(failed.attempt, 1)
        self.assertEqual(failed.max_attempts, 1)
        self.assertEqual(failed.error, {"reason": "semantic failure"})
        runner.assert_called_once()

    def test_repo_agent_effectors_use_host_python_without_overriding_custom_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "package.toml"
            package.write_text(
                """
[[correlation_paths]]
id = "path"

[[correlation_paths.effectors]]
id = "repo_step"
adapter = { kind = "subprocess", command = ["python3", "-m", "repo_agent.effector", "extra"] }

[[correlation_paths.effectors]]
id = "repo_step_auto"
adapter = { kind = "subprocess", command = ["python3", "-m", "repo_agent.effector"] }

[[correlation_paths.effectors]]
id = "custom"
adapter = { kind = "subprocess", command = ["python3", "custom.py"] }
""",
                encoding="utf-8",
            )
            db = self._db(root, [("run-1", "run-1:path:repo_step", "succeeded", 1, 1, "{}", "{}")])
            host = {
                "ok": True,
                "run_id": "run-1",
                "run_status": "completed",
                "replayed": False,
                "ticks": 1,
                "processes": [{"id": "run-1:path:repo_step", "status": "succeeded"}],
            }
            with patch("repo_agent.flows.runtime.host_run_package", return_value=host) as runner:
                run_package_path(
                    db_path=db,
                    package_path=package,
                    path_id="path",
                    run_id="run-1",
                    command_overrides={"repo_step": ("explicit-python", "worker.py")},
                )

        self.assertEqual(
            runner.call_args.kwargs["command_overrides"],
            {
                "repo_step": ("explicit-python", "worker.py"),
                "repo_step_auto": (sys.executable, "-m", "repo_agent.effector"),
            },
        )

    def test_persists_run_mode_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [("run-1", "run-1:path:success", "succeeded", 1, 1, "{}", "{}")],
            )
            host = {
                "ok": True,
                "run_id": "run-1",
                "run_status": "completed",
                "replayed": False,
                "ticks": 1,
                "processes": [{"id": "run-1:path:success", "status": "succeeded"}],
            }
            with patch("repo_agent.flows.runtime.host_run_package", return_value=host):
                run_package_path(
                    db_path=db,
                    package_path=Path(tmp) / "package.toml",
                    path_id="path",
                    run_id="run-1",
                    run_metadata={"mode": "live"},
                )
            with sqlite3.connect(db) as connection:
                metadata = connection.execute("SELECT metadata FROM runs WHERE id='run-1'").fetchone()[0]
        self.assertEqual(json.loads(metadata), {"mode": "live"})

    def test_replay_ignores_invocation_metadata_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [("run-1", "run-1:path:success", "succeeded", 1, 1, "{}", "{}")],
            )
            with sqlite3.connect(db) as connection:
                connection.execute(
                    "UPDATE runs SET metadata=? WHERE id='run-1'",
                    (json.dumps({"mode": "dry-run", "host": "kept"}),),
                )
            host = {
                "ok": True,
                "run_id": "run-1",
                "run_status": "completed",
                "replayed": True,
                "ticks": 0,
                "processes": [{"id": "run-1:path:success", "status": "succeeded"}],
            }
            with patch("repo_agent.flows.runtime.host_run_package", return_value=host):
                result = run_package_path(
                    db_path=db,
                    package_path=Path(tmp) / "package.toml",
                    path_id="path",
                    run_id="run-1",
                    run_metadata={"mode": "live"},
                )
            with sqlite3.connect(db) as connection:
                metadata = connection.execute("SELECT metadata FROM runs WHERE id='run-1'").fetchone()[0]
        self.assertTrue(result.replayed)
        self.assertEqual(json.loads(metadata), {"mode": "dry-run", "host": "kept"})

    def test_matching_replay_metadata_preserves_host_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [("run-1", "run-1:path:success", "succeeded", 1, 1, "{}", "{}")],
            )
            with sqlite3.connect(db) as connection:
                connection.execute(
                    "UPDATE runs SET metadata=? WHERE id='run-1'",
                    (json.dumps({"mode": "dry-run", "host": "kept"}),),
                )
            host = {
                "ok": True,
                "run_id": "run-1",
                "run_status": "completed",
                "replayed": True,
                "ticks": 0,
                "processes": [{"id": "run-1:path:success", "status": "succeeded"}],
            }
            with patch("repo_agent.flows.runtime.host_run_package", return_value=host):
                result = run_package_path(
                    db_path=db,
                    package_path=Path(tmp) / "package.toml",
                    path_id="path",
                    run_id="run-1",
                    run_metadata={"mode": "dry-run"},
                )
            with sqlite3.connect(db) as connection:
                metadata = connection.execute("SELECT metadata FROM runs WHERE id='run-1'").fetchone()[0]
        self.assertTrue(result.replayed)
        self.assertEqual(json.loads(metadata), {"mode": "dry-run", "host": "kept"})

    def test_malformed_journal_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [("run-1", "run-1:path:fail", "failed", 1, 1, "{}", "not-json")],
            )
            with self.assertRaisesRegex(RuntimeFacadeError, "invalid JSON"):
                read_journal_processes(db, "run-1")

    def test_non_object_journal_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [("run-1", "run-1:path:fail", "failed", 1, 1, "[]", "{}")],
            )
            with self.assertRaisesRegex(RuntimeFacadeError, "must decode to an object"):
                read_journal_processes(db, "run-1")

    def test_host_and_journal_disagreement_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [("run-1", "run-1:path:work", "failed", 1, 1, "{}", "{}")],
            )
            host = {
                "ok": True,
                "run_id": "run-1",
                "run_status": "failed",
                "replayed": False,
                "ticks": 1,
                "processes": [{"id": "run-1:path:work", "status": "succeeded"}],
            }
            with (
                patch("repo_agent.flows.runtime.host_run_package", return_value=host),
                self.assertRaisesRegex(RuntimeFacadeError, "disagree"),
            ):
                run_package_path(
                    db_path=db,
                    package_path=Path(tmp) / "package.toml",
                    path_id="path",
                    run_id="run-1",
                )

    def test_secret_diagnostics_are_redacted(self) -> None:
        secret = "sentinel-secret"
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [
                    (
                        "run-1",
                        "run-1:path:fail",
                        "failed",
                        1,
                        1,
                        json.dumps({"authorization": secret}),
                        json.dumps({"message": f"token={secret}"}),
                    )
                ],
            )
            process = read_journal_processes(db, "run-1")[0]
        evidence = json.dumps({"output": process.output, "error": process.error})
        self.assertNotIn(secret, evidence)
        self.assertIn("<redacted>", evidence)

    def test_authorization_bearer_diagnostics_are_redacted(self) -> None:
        secret = "bearer-sentinel-secret"
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [("run-1", "run-1:path:fail", "failed", 1, 1, "{}", json.dumps({"message": "Authorization:" + " Bearer " + secret}))],
            )
            process = read_journal_processes(db, "run-1")[0]
        evidence = json.dumps(process.error)
        self.assertNotIn(secret, evidence)
        self.assertIn("<redacted>", evidence)


if __name__ == "__main__":
    unittest.main()
