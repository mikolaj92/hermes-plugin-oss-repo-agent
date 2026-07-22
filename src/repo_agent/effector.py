"""Strict subprocess boundary for allowlisted repo-agent effectors."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import re
import sys
from collections.abc import Mapping
from typing import Any

from fala import sdk

from repo_agent.catalog import EFFECTORS, resolve

_SECRET_KEY = re.compile(r"token|password|secret|api[_-]?key|authorization", re.I)
_AUTH_VALUE = re.compile(r"(?i)(authorization\s*[:=]\s*)[^\r\n,;]+")
_SECRET_VALUE = re.compile(
    r"(?i)((?:token|password|secret|api[_-]?key)\s*[:=]\s*)[^\s,;]+"
)


def _safe(value: Any, *, key: str = "", truncate: bool = False) -> Any:
    if _SECRET_KEY.search(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(k): _safe(v, key=str(k), truncate=truncate)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_safe(item, key=key, truncate=truncate) for item in value]
    if isinstance(value, str):
        redacted = _SECRET_VALUE.sub(r"\1<redacted>", _AUTH_VALUE.sub(r"\1<redacted>", value))
        return redacted[:2000] if truncate else redacted
    return value


def _handlers() -> dict[str, Any]:
    return {entry.ref: resolve(entry.ref) for entry in EFFECTORS}


def _request(manifest: Mapping[str, Any]) -> dict[str, Any]:
    inputs = sdk.declared_inputs(manifest)
    inputs["conduction"] = sdk.conduction(manifest)
    return {
        "input": inputs,
        "config": sdk.config(manifest),
        "process_id": manifest.get("process_id", ""),
        "impulse_id": manifest.get("impulse_id", ""),
    }

def _expected_dry_run(request: Mapping[str, Any]) -> bool | None:
    inputs = request["input"]
    config = request["config"]
    for source in (inputs, config):
        if "dry_run" in source and type(source["dry_run"]) is not bool:
            raise TypeError("dry_run must be a boolean")
    if "dry_run" in inputs:
        return inputs["dry_run"]
    if "dry_run" in config:
        return config["dry_run"]
    return None


def _normalize_result(raw: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise TypeError("handler result must be a JSON object")
    payload = dict(raw)
    if not payload:
        raise ValueError("handler result must not be empty")
    status = payload.get("status")
    if not isinstance(status, str) or not status:
        raise TypeError("handler result status must be a non-empty string")
    if type(payload.get("ok")) is not bool:
        raise TypeError("handler result ok must be a boolean")
    if type(payload.get("mutated")) is not bool:
        raise TypeError("handler result mutated must be a boolean")
    expected = _expected_dry_run(request)
    if "dry_run" in payload and type(payload["dry_run"]) is not bool:
        raise TypeError("result dry_run must be a boolean")
    if expected is not None:
        if "dry_run" in payload and payload["dry_run"] is not expected:
            raise ValueError("result dry_run conflicts with request")
        payload["dry_run"] = expected
    return payload


def main() -> int:
    try:
        manifest = sdk.load_manifest()
        handler_ref = sdk.config(manifest).get("handler")
        handler = _handlers().get(handler_ref) if isinstance(handler_ref, str) else None
        if handler is None:
            raise ValueError("unknown handler")
        request = _request(manifest)
        captured = StringIO()
        with redirect_stdout(captured), redirect_stderr(captured):
            raw = handler(request)
        payload = _safe(_normalize_result(raw, request))
        sdk.write_result(sdk.output(values=payload))
        if payload.get("ok") is False or payload.get("status") == "failed":
            print("repo-agent effector reported failure", file=sys.stderr)
            return 1
        return 0
    except Exception as exc:
        failure = {
            "status": "failed",
            "ok": False,
            "mutated": False,
            "reason": "effector_boundary_failed",
            "error": _safe(str(exc), truncate=True),
        }
        try:
            sdk.write_result(sdk.output(values=failure))
        except Exception:
            pass
        print(f"repo-agent effector failed: {failure['error']}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
