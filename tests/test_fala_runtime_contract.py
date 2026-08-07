from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lokay.flows.runtime import (
    RuntimeFacadeError,
    read_journal_processes,
    read_journal_run,
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
    error_json TEXT NOT NULL,
    metadata TEXT NOT NULL
);
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    package_id TEXT,
    package_version TEXT,
    package_digest TEXT,
    correlation_path_id TEXT,
    correlation_path_digest TEXT,
    runtime_version TEXT,
    backend_version TEXT,
    schema_version INTEGER NOT NULL,
    metadata TEXT NOT NULL
);
"""


def _process_metadata(path_id: str, effector_id: str) -> str:
    return json.dumps(
        {
            "correlation_path_id": path_id,
            "correlation_path_spec_id": path_id,
            "effector_id": effector_id,
            "seq": 0,
        }
    )


def _process_row(
    run_id: str,
    path_id: str,
    effector_id: str,
    status: str,
    *,
    attempt: int = 1,
    max_attempts: int = 1,
    output: str = "{}",
    error: str = "{}",
    metadata: str | None = None,
) -> tuple[object, ...]:
    return (
        run_id,
        f"{run_id}:{path_id}:{effector_id}",
        status,
        attempt,
        max_attempts,
        output,
        error,
        metadata if metadata is not None else _process_metadata(path_id, effector_id),
    )


def _run_values(
    *,
    run_id: str = "run-1",
    status: str = "completed",
    package_id: str = "lokay",
    package_version: str = "1",
    package_digest: str = "pkg-digest",
    correlation_path_id: str = "path",
    correlation_path_digest: str = "path-digest",
    runtime_version: str = "0.7.15",
    backend_version: str = "native",
    schema_version: int = 6,
    metadata: str = "{}",
) -> tuple[object, ...]:
    return (
        run_id,
        status,
        package_id,
        package_version,
        package_digest,
        correlation_path_id,
        correlation_path_digest,
        runtime_version,
        backend_version,
        schema_version,
        metadata,
    )


def _host_result(
    *,
    run_id: str = "run-1",
    run_status: str = "completed",
    replayed: bool = False,
    ticks: int = 1,
    processes: list[dict[str, str]] | None = None,
    package_id: str = "lokay",
    package_version: str = "1",
    package_digest: str = "pkg-digest",
    correlation_path_id: str = "path",
    correlation_path_digest: str = "path-digest",
    runtime_version: str = "0.7.15",
    backend_version: str = "native",
    schema_version: int | None = 6,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": True,
        "run_id": run_id,
        "run_status": run_status,
        "replayed": replayed,
        "ticks": ticks,
        "package_id": package_id,
        "package_version": package_version,
        "package_digest": package_digest,
        "correlation_path_id": correlation_path_id,
        "correlation_path_digest": correlation_path_digest,
        "runtime_version": runtime_version,
        "backend_version": backend_version,
        "processes": processes if processes is not None else [],
    }
    if schema_version is not None:
        payload["schema_version"] = schema_version
    return payload



class RuntimeFacadeTests(unittest.TestCase):
    def _db(
        self,
        root: Path,
        rows: list[tuple[object, ...]],
        *,
        run: tuple[object, ...] | None = None,
    ) -> Path:
        db = root / "state.sqlite"
        with sqlite3.connect(db) as connection:
            connection.executescript(_SCHEMA)
            connection.executemany(
                "INSERT INTO processes VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
            connection.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                run if run is not None else _run_values(status="failed"),
            )
        return db

    def test_normalizes_exact_journal_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [
                    _process_row("run-1", "path", "dependent", "cancelled", attempt=0, error='{"reason":"dead_upstream"}'),
                    _process_row("run-1", "path", "fail", "failed", error='{"reason":"semantic failure"}'),
                    _process_row("run-1", "path", "success", "succeeded", output='{"value":1}'),
                ],
                run=_run_values(status="failed"),
            )
            host = _host_result(
                run_status="failed",
                ticks=2,
                processes=[
                    {"id": "run-1:path:success", "status": "succeeded"},
                    {"id": "run-1:path:fail", "status": "failed"},
                    {"id": "run-1:path:dependent", "status": "cancelled"},
                ],
            )
            with patch("lokay.flows.runtime.host_run_package", return_value=host) as runner:
                result = run_package_path(
                    db_path=db,
                    package_path=Path(tmp) / "package.toml",
                    path_id="path",
                    run_id="run-1",
                    allowed_effectors={"dependent", "fail", "success"},
                )

        self.assertEqual(result.run_status, "failed")
        self.assertEqual(result.path_id, "path")
        self.assertEqual(result.package_id, "lokay")
        self.assertEqual(result.package_digest, "pkg-digest")
        self.assertEqual(result.correlation_path_digest, "path-digest")
        self.assertEqual(result.runtime_version, "0.7.15")
        self.assertEqual(result.backend_version, "native")
        self.assertEqual(result.ticks, 2)
        self.assertEqual([process.step_id for process in result.failed], ["dependent", "fail"])
        failed = next(process for process in result.failed if process.step_id == "fail")
        self.assertEqual(failed.attempt, 1)
        self.assertEqual(failed.max_attempts, 1)
        self.assertEqual(failed.error, {"reason": "semantic failure"})
        self.assertEqual(failed.correlation_path_id, "path")
        self.assertEqual(failed.effector_id, "fail")
        runner.assert_called_once()

    def test_runtime_version_mismatch_fails_before_host_or_journal(self) -> None:
        with (
            patch("lokay.flows.runtime.metadata.version", return_value="0.7.9"),
            patch("lokay.flows.runtime.host_run_package") as runner,
            patch("lokay.flows.runtime.read_journal_processes") as journal,
            self.assertRaisesRegex(RuntimeFacadeError, "expected 0.7.15, observed 0.7.9"),
        ):
            run_package_path(
                db_path=Path("missing.sqlite"),
                package_path=Path("package.toml"),
                path_id="path",
                run_id="run-1",
                allowed_effectors={"success"},
            )
        runner.assert_not_called()
        journal.assert_not_called()

    def test_lokay_effectors_use_host_python_without_overriding_custom_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "package.toml"
            package.write_text(
                """
[[correlation_paths]]
id = "path"

[[correlation_paths.effectors]]
id = "repo_step"
adapter = { kind = "subprocess", command = ["python3", "-m", "lokay.effector", "extra"] }

[[correlation_paths.effectors]]
id = "repo_step_auto"
adapter = { kind = "subprocess", command = ["python3", "-m", "lokay.effector"] }

[[correlation_paths.effectors]]
id = "custom"
adapter = { kind = "subprocess", command = ["python3", "custom.py"] }
""",
                encoding="utf-8",
            )
            db = self._db(
                root,
                [_process_row("run-1", "path", "repo_step", "succeeded")],
                run=_run_values(status="completed"),
            )
            host = _host_result(
                processes=[{"id": "run-1:path:repo_step", "status": "succeeded"}],
            )
            with patch("lokay.flows.runtime.host_run_package", return_value=host) as runner:
                run_package_path(
                    db_path=db,
                    package_path=package,
                    path_id="path",
                    run_id="run-1",
                    command_overrides={"repo_step": ("explicit-python", "worker.py")},
                    allowed_effectors={"repo_step"},
                )

        self.assertEqual(
            runner.call_args.kwargs["command_overrides"],
            {
                "repo_step": ("explicit-python", "worker.py"),
                "repo_step_auto": (sys.executable, "-m", "lokay.effector"),
            },
        )

    def test_persists_run_mode_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [_process_row("run-1", "path", "success", "succeeded")],
                run=_run_values(status="completed"),
            )
            host = _host_result(
                processes=[{"id": "run-1:path:success", "status": "succeeded"}],
            )
            with patch("lokay.flows.runtime.host_run_package", return_value=host):
                run_package_path(
                    db_path=db,
                    package_path=Path(tmp) / "package.toml",
                    path_id="path",
                    run_id="run-1",
                    run_metadata={"mode": "live"},
                    allowed_effectors={"success"},
                )
            with sqlite3.connect(db) as connection:
                metadata = connection.execute("SELECT metadata FROM runs WHERE id='run-1'").fetchone()[0]
        self.assertEqual(json.loads(metadata), {"mode": "live"})

    def test_replay_ignores_invocation_metadata_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [_process_row("run-1", "path", "success", "succeeded")],
                run=_run_values(status="completed", metadata=json.dumps({"mode": "dry-run", "host": "kept"})),
            )
            host = _host_result(
                replayed=True,
                ticks=0,
                processes=[{"id": "run-1:path:success", "status": "succeeded"}],
            )
            with patch("lokay.flows.runtime.host_run_package", return_value=host):
                result = run_package_path(
                    db_path=db,
                    package_path=Path(tmp) / "package.toml",
                    path_id="path",
                    run_id="run-1",
                    run_metadata={"mode": "live"},
                    allowed_effectors={"success"},
                )
            with sqlite3.connect(db) as connection:
                metadata = connection.execute("SELECT metadata FROM runs WHERE id='run-1'").fetchone()[0]
        self.assertTrue(result.replayed)
        self.assertEqual(json.loads(metadata), {"mode": "dry-run", "host": "kept"})

    def test_matching_replay_metadata_preserves_host_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [_process_row("run-1", "path", "success", "succeeded")],
                run=_run_values(status="completed", metadata=json.dumps({"mode": "dry-run", "host": "kept"})),
            )
            host = _host_result(
                replayed=True,
                ticks=0,
                processes=[{"id": "run-1:path:success", "status": "succeeded"}],
            )
            with patch("lokay.flows.runtime.host_run_package", return_value=host):
                result = run_package_path(
                    db_path=db,
                    package_path=Path(tmp) / "package.toml",
                    path_id="path",
                    run_id="run-1",
                    run_metadata={"mode": "dry-run"},
                    allowed_effectors={"success"},
                )
            with sqlite3.connect(db) as connection:
                metadata = connection.execute("SELECT metadata FROM runs WHERE id='run-1'").fetchone()[0]
        self.assertTrue(result.replayed)
        self.assertEqual(json.loads(metadata), {"mode": "dry-run", "host": "kept"})

    def test_malformed_journal_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [_process_row("run-1", "path", "fail", "failed", error="not-json")],
            )
            with self.assertRaisesRegex(RuntimeFacadeError, "invalid JSON"):
                read_journal_processes(db, "run-1")

    def test_non_object_journal_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [_process_row("run-1", "path", "fail", "failed", output="[]")],
            )
            with self.assertRaisesRegex(RuntimeFacadeError, "must decode to an object"):
                read_journal_processes(db, "run-1")

    def test_host_and_journal_disagreement_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [_process_row("run-1", "path", "work", "failed")],
                run=_run_values(status="failed"),
            )
            host = _host_result(
                run_status="failed",
                processes=[{"id": "run-1:path:work", "status": "succeeded"}],
            )
            with (
                patch("lokay.flows.runtime.host_run_package", return_value=host),
                self.assertRaisesRegex(RuntimeFacadeError, "disagree"),
            ):
                run_package_path(
                    db_path=db,
                    package_path=Path(tmp) / "package.toml",
                    path_id="path",
                    run_id="run-1",
                    allowed_effectors={"work"},
                )

    def test_missing_allowed_effectors_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [_process_row("run-1", "path", "success", "succeeded")],
                run=_run_values(status="completed"),
            )
            host = _host_result(
                processes=[{"id": "run-1:path:success", "status": "succeeded"}],
            )
            with (
                patch("lokay.flows.runtime.host_run_package", return_value=host),
                self.assertRaisesRegex(RuntimeFacadeError, "allowed effectors are required"),
            ):
                run_package_path(
                    db_path=db,
                    package_path=Path(tmp) / "package.toml",
                    path_id="path",
                    run_id="run-1",
                )

    def test_missing_run_identity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [_process_row("run-1", "path", "success", "succeeded")],
                run=_run_values(package_id=None),  # type: ignore[arg-type]
            )
            with self.assertRaisesRegex(RuntimeFacadeError, "runs.package_id"):
                read_journal_run(db, "run-1")

    def test_null_path_identity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [_process_row("run-1", "path", "success", "succeeded")],
                run=_run_values(correlation_path_id=None),  # type: ignore[arg-type]
            )
            with self.assertRaisesRegex(RuntimeFacadeError, "runs.correlation_path_id"):
                read_journal_processes(db, "run-1")

    def test_malformed_run_metadata_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [_process_row("run-1", "path", "success", "succeeded")],
                run=_run_values(metadata="not-json"),
            )
            with self.assertRaisesRegex(RuntimeFacadeError, "invalid JSON"):
                read_journal_run(db, "run-1")

    def test_foreign_process_path_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [
                    _process_row(
                        "run-1",
                        "other_path",
                        "success",
                        "succeeded",
                        metadata=_process_metadata("other_path", "success"),
                    )
                ],
                run=_run_values(correlation_path_id="path"),
            )
            with sqlite3.connect(db) as connection:
                connection.execute(
                    "UPDATE processes SET id=? WHERE run_id=?",
                    ("run-1:other_path:success", "run-1"),
                )
            with self.assertRaisesRegex(RuntimeFacadeError, "foreign correlation_path_id"):
                read_journal_processes(db, "run-1", expected_path_id="path")

    def test_unknown_effector_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [_process_row("run-1", "path", "rogue", "succeeded")],
                run=_run_values(status="completed"),
            )
            with self.assertRaisesRegex(RuntimeFacadeError, "not in the allowed set"):
                read_journal_processes(
                    db,
                    "run-1",
                    expected_path_id="path",
                    allowed_effectors={"success"},
                )

    def test_malformed_process_metadata_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [_process_row("run-1", "path", "success", "succeeded", metadata="[]")],
            )
            with self.assertRaisesRegex(RuntimeFacadeError, "must decode to an object"):
                read_journal_processes(db, "run-1")

    def test_missing_process_metadata_fields_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [_process_row("run-1", "path", "success", "succeeded", metadata="{}")],
            )
            with self.assertRaisesRegex(RuntimeFacadeError, "metadata.correlation_path_id"):
                read_journal_processes(db, "run-1")

    def test_process_id_shape_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [
                    (
                        "run-1",
                        "run-1:path",
                        "succeeded",
                        1,
                        1,
                        "{}",
                        "{}",
                        _process_metadata("path", "success"),
                    )
                ],
            )
            with self.assertRaisesRegex(RuntimeFacadeError, "run_id:path_id:effector_id"):
                read_journal_processes(db, "run-1")

    def test_caller_path_disagreement_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [_process_row("run-1", "path", "success", "succeeded")],
                run=_run_values(status="completed", correlation_path_id="path"),
            )
            host = _host_result(
                correlation_path_id="other",
                processes=[{"id": "run-1:path:success", "status": "succeeded"}],
            )
            with (
                patch("lokay.flows.runtime.host_run_package", return_value=host),
                self.assertRaisesRegex(RuntimeFacadeError, "requested path_id disagrees"),
            ):
                run_package_path(
                    db_path=db,
                    package_path=Path(tmp) / "package.toml",
                    path_id="other",
                    run_id="run-1",
                    allowed_effectors={"success"},
                )

    def test_host_only_claim_without_journal_process_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [],
                run=_run_values(status="completed"),
            )
            host = _host_result(
                processes=[{"id": "run-1:path:success", "status": "succeeded"}],
            )
            with (
                patch("lokay.flows.runtime.host_run_package", return_value=host),
                self.assertRaisesRegex(RuntimeFacadeError, "disagree"),
            ):
                run_package_path(
                    db_path=db,
                    package_path=Path(tmp) / "package.toml",
                    path_id="path",
                    run_id="run-1",
                    allowed_effectors={"success"},
                )

    def test_package_digest_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [_process_row("run-1", "path", "success", "succeeded")],
                run=_run_values(status="completed"),
            )
            host = _host_result(
                package_digest="other-digest",
                processes=[{"id": "run-1:path:success", "status": "succeeded"}],
            )
            with (
                patch("lokay.flows.runtime.host_run_package", return_value=host),
                self.assertRaisesRegex(RuntimeFacadeError, "package_digest"),
            ):
                run_package_path(
                    db_path=db,
                    package_path=Path(tmp) / "package.toml",
                    path_id="path",
                    run_id="run-1",
                    allowed_effectors={"success"},
                )

    def test_correlation_path_digest_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [_process_row("run-1", "path", "success", "succeeded")],
                run=_run_values(status="completed"),
            )
            host = _host_result(
                correlation_path_digest="other-path-digest",
                processes=[{"id": "run-1:path:success", "status": "succeeded"}],
            )
            with (
                patch("lokay.flows.runtime.host_run_package", return_value=host),
                self.assertRaisesRegex(RuntimeFacadeError, "correlation_path_digest"),
            ):
                run_package_path(
                    db_path=db,
                    package_path=Path(tmp) / "package.toml",
                    path_id="path",
                    run_id="run-1",
                    allowed_effectors={"success"},
                )

    def test_runtime_backend_schema_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [_process_row("run-1", "path", "success", "succeeded")],
                run=_run_values(status="completed"),
            )
            host = _host_result(
                runtime_version="0.0.0",
                backend_version="other",
                schema_version=1,
                processes=[{"id": "run-1:path:success", "status": "succeeded"}],
            )
            with (
                patch("lokay.flows.runtime.host_run_package", return_value=host),
                self.assertRaisesRegex(RuntimeFacadeError, "runtime_version"),
            ):
                run_package_path(
                    db_path=db,
                    package_path=Path(tmp) / "package.toml",
                    path_id="path",
                    run_id="run-1",
                    allowed_effectors={"success"},
                )

    def test_missing_host_identity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [_process_row("run-1", "path", "success", "succeeded")],
                run=_run_values(status="completed"),
            )
            host = {
                "ok": True,
                "run_id": "run-1",
                "run_status": "completed",
                "replayed": False,
                "ticks": 1,
                "processes": [{"id": "run-1:path:success", "status": "succeeded"}],
            }
            with (
                patch("lokay.flows.runtime.host_run_package", return_value=host),
                self.assertRaisesRegex(RuntimeFacadeError, "Fala host package_id"),
            ):
                run_package_path(
                    db_path=db,
                    package_path=Path(tmp) / "package.toml",
                    path_id="path",
                    run_id="run-1",
                    allowed_effectors={"success"},
                )

    def test_run_metadata_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [_process_row("run-1", "path", "success", "succeeded")],
                run=_run_values(status="completed", metadata=json.dumps({"mode": "dry-run"})),
            )
            host = _host_result(
                processes=[{"id": "run-1:path:success", "status": "succeeded"}],
            )

            def _no_write(*_args: object, **_kwargs: object) -> None:
                return None

            with (
                patch("lokay.flows.runtime.host_run_package", return_value=host),
                patch("lokay.flows.runtime._write_run_metadata", side_effect=_no_write),
                self.assertRaisesRegex(RuntimeFacadeError, "run metadata"),
            ):
                run_package_path(
                    db_path=db,
                    package_path=Path(tmp) / "package.toml",
                    path_id="path",
                    run_id="run-1",
                    run_metadata={"mode": "live"},
                    allowed_effectors={"success"},
                )

    def test_secret_diagnostics_are_redacted(self) -> None:
        secret = "sentinel-secret"
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(
                Path(tmp),
                [
                    _process_row(
                        "run-1",
                        "path",
                        "fail",
                        "failed",
                        output=json.dumps({"authorization": secret}),
                        error=json.dumps({"message": f"token={secret}"}),
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
                [
                    _process_row(
                        "run-1",
                        "path",
                        "fail",
                        "failed",
                        error=json.dumps({"message": "Authorization:" + " Bearer " + secret}),
                    )
                ],
            )
            process = read_journal_processes(db, "run-1")[0]
        evidence = json.dumps(process.error)
        self.assertNotIn(secret, evidence)
        self.assertIn("<redacted>", evidence)


if __name__ == "__main__":
    unittest.main()
