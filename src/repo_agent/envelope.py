"""Shared output envelope for mega-atomic Fala effectors (0.2.x)."""

from __future__ import annotations

from typing import Any

from fala.adapters import EffectorRunResult


def result(
    *,
    status: str,
    ok: bool = True,
    mutated: bool = False,
    dry_run: bool = False,
    reason: str | None = None,
    **extra: Any,
) -> EffectorRunResult:
    out: dict[str, Any] = {
        "status": status,
        "ok": ok,
        "mutated": mutated,
        "dry_run": dry_run,
    }
    if reason is not None:
        out["reason"] = reason
    out.update(extra)
    return EffectorRunResult(output=out)


def ok(status: str = "ok", **extra: Any) -> EffectorRunResult:
    return result(status=status, ok=True, **extra)


def planned(**extra: Any) -> EffectorRunResult:
    return result(status="planned", ok=True, dry_run=True, mutated=False, **extra)


def noop(reason: str, **extra: Any) -> EffectorRunResult:
    return result(status="noop", ok=True, mutated=False, reason=reason, **extra)


def fail(reason: str, **extra: Any) -> EffectorRunResult:
    mutated = bool(extra.pop("mutated", False))
    return result(status="failed", ok=False, mutated=mutated, reason=reason, **extra)


def cfg_of(request: Any) -> dict[str, Any]:
    return dict(getattr(request, "config", None) or {})


def input_of(request: Any) -> dict[str, Any]:
    return dict(getattr(request, "input", None) or {})


def conduction_of(request: Any) -> dict[str, Any]:
    return dict(input_of(request).get("conduction") or {})


def dry_run_flag(request: Any, default: bool = True) -> bool:
    data = input_of(request)
    if "dry_run" in data:
        return bool(data["dry_run"])
    cfg = cfg_of(request)
    if "dry_run" in cfg:
        return bool(cfg["dry_run"])
    return default


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def cond_get(
    request: Any,
    key: str,
    *effector_ids: str,
    default: Any = None,
    aliases: tuple[str, ...] = (),
) -> Any:
    """Resolve ``key`` from request.input, else first matching conduction output.

    ``aliases`` are alternate keys tried in both input and each conduction blob
    (e.g. number / pr_number). Prefer explicit input over conduction.
    """
    data = input_of(request)
    keys = (key, *aliases)
    for k in keys:
        if k in data and not _empty(data[k]):
            return data[k]
    cond = conduction_of(request)
    for eid in effector_ids:
        blob = cond.get(eid)
        if not isinstance(blob, dict):
            continue
        for k in keys:
            if k in blob and not _empty(blob[k]):
                return blob[k]
    return default


def cond_blob(request: Any, *effector_ids: str) -> dict[str, Any]:
    """First non-empty conduction dict among effector ids."""
    cond = conduction_of(request)
    for eid in effector_ids:
        blob = cond.get(eid)
        if isinstance(blob, dict) and blob:
            return dict(blob)
    return {}
def upstream_noop(request: Any, *effector_ids: str) -> dict[str, Any]:
    """Return the first upstream no-op output, if any."""
    for effector_id in effector_ids:
        blob = cond_blob(request, effector_id)
        if blob.get("status") == "noop":
            return blob
    return {}
