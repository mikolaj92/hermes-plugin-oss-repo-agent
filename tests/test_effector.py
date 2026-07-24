from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from repo_agent import effector


class EffectorBoundaryTests(unittest.TestCase):
    def run_effector(self, handler, *, input_data=None, config=None, handlers=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            output = root / "output"
            output.mkdir()
            manifest.write_text(
                json.dumps(
                    {
                        "input": input_data or {},
                        "config": {"handler": "allowed", **(config or {})},
                        "process_id": "process-1",
                        "impulse_id": "impulse-1",
                    }
                ),
                encoding="utf-8",
            )
            stderr = StringIO()
            env = {
                "FALA_EFFECTOR_MANIFEST": str(manifest),
                "FALA_EFFECTOR_OUTPUT_DIR": str(output),
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(effector, "_handlers", return_value={"allowed": handler} if handlers is None else handlers),
                redirect_stderr(stderr),
            ):
                code = effector.main()
            result = json.loads((output / "result.json").read_text(encoding="utf-8"))
            return code, result["values"], stderr.getvalue()

    def test_success_and_noop_exit_zero(self) -> None:
        for payload in (
            {"status": "ok", "ok": True, "mutated": False},
            {"status": "noop", "ok": True, "mutated": False},
        ):
            with self.subTest(status=payload["status"]):
                code, values, stderr = self.run_effector(lambda request: payload)
                self.assertEqual(code, 0)
                self.assertEqual(values, payload)
                self.assertEqual(stderr, "")

    def test_semantic_failure_exits_nonzero(self) -> None:
        code, values, stderr = self.run_effector(
            lambda request: {"status": "failed", "ok": False, "mutated": False, "reason": "denied"}
        )
        self.assertEqual(code, 1)
        self.assertFalse(values["ok"])
        self.assertIn("reported failure", stderr)

    def test_malformed_exception_and_unknown_handler_fail_closed(self) -> None:
        cases = (
            lambda request: "not-an-object",
            lambda request: {},
            lambda request: {"status": "ok", "ok": "false", "mutated": False},
            lambda request: {"status": "ok", "ok": True},
            lambda request: (_ for _ in ()).throw(RuntimeError("broken")),
        )
        for handler in cases:
            with self.subTest(handler=handler):
                code, values, _ = self.run_effector(handler)
                self.assertEqual(code, 1)
                self.assertEqual(values["reason"], "effector_boundary_failed")
        code, values, _ = self.run_effector(lambda request: {}, handlers={})
        self.assertEqual(code, 1)
        self.assertEqual(values["reason"], "effector_boundary_failed")

    def test_input_dry_run_overrides_config_and_conduction_is_preserved(self) -> None:
        observed = {}

        def handler(request):
            observed.update(request)
            return {"status": "ok", "ok": True, "mutated": False}

        code, values, _ = self.run_effector(
            handler,
            input_data={"dry_run": False, "conduction": {"prior": {"value": 1}}},
            config={"dry_run": True},
        )
        self.assertEqual(code, 0)
        self.assertFalse(values["dry_run"])
        self.assertNotIn("conduction", {"dry_run": observed["input"]["dry_run"]})
        self.assertEqual(observed["input"]["conduction"], {"prior": {"value": 1}})
        self.assertEqual(observed["process_id"], "process-1")
        self.assertEqual(observed["impulse_id"], "impulse-1")

    def test_secrets_are_redacted_from_result_and_stderr(self) -> None:
        secret = "sentinel-secret-value"

        def handler(request):
            raise RuntimeError(f"authorization={secret}")

        code, values, stderr = self.run_effector(handler)
        self.assertEqual(code, 1)
        evidence = json.dumps(values) + stderr
        self.assertNotIn(secret, evidence)
        self.assertIn("<redacted>", evidence)
        bearer = "bearer-sentinel-secret"

        def bearer_handler(request):
            raise RuntimeError("Authorization:" + " Bearer " + bearer)

        code, values, stderr = self.run_effector(bearer_handler)
        self.assertEqual(code, 1)
        self.assertNotIn(bearer, json.dumps(values) + stderr)

    def test_handler_output_is_not_leaked(self) -> None:
        secret = "unstructured-sentinel-value"

        def handler(request):
            print(secret)
            print(secret, file=__import__("sys").stderr)
            return {"status": "ok", "ok": True, "mutated": False}

        code, _, stderr = self.run_effector(handler)
        self.assertEqual(code, 0)
        self.assertNotIn(secret, stderr)


if __name__ == "__main__":
    unittest.main()
