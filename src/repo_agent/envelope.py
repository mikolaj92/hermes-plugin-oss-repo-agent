"""Plain dictionary contract shared by repo-agent Fala effectors."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


Request = Mapping[str, Any]
Result = dict[str, Any]


def result(
    *,
    status: str,
    ok: bool = True,
    mutated: bool = False,
    dry_run: bool | None = None,
    reason: str | None = None,
    **extra: Any,
) -> Result:
    out: Result = {"status": status, "ok": ok, "mutated": mutated}
    # Keep absent unless explicit: the host validates and injects request dry-run metadata.
    if dry_run is not None:
        out["dry_run"] = dry_run
    if reason is not None:
        out["reason"] = reason
    out.update(extra)
    return out


def ok(status: str = "ok", **extra: Any) -> Result:
    return result(status=status, ok=True, **extra)


def planned(**extra: Any) -> Result:
    return result(status="planned", ok=True, dry_run=True, mutated=False, **extra)


def noop(reason: str, **extra: Any) -> Result:
    return result(status="noop", ok=True, mutated=False, reason=reason, **extra)


def fail(
    reason: str,
    *,
    failure_class: str = "terminal",
    retry_safe: bool = False,
    mutated: bool = False,
    **extra: Any,
) -> Result:
    return result(
        status="failed",
        ok=False,
        mutated=mutated,
        reason=reason,
        failure_class=failure_class,
        retry_safe=retry_safe,
        **extra,
    )


def cfg_of(request: Request) -> dict[str, Any]:
    value = request.get("config")
    return dict(value) if isinstance(value, Mapping) else {}


def input_of(request: Request) -> dict[str, Any]:
    value = request.get("input")
    return dict(value) if isinstance(value, Mapping) else {}


def conduction_of(request: Request) -> dict[str, Any]:
    value = input_of(request).get("conduction")
    return dict(value) if isinstance(value, Mapping) else {}


def dry_run_flag(request: Request, default: bool = True) -> bool:
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
    request: Request,
    key: str,
    *effector_ids: str,
    default: Any = None,
    aliases: tuple[str, ...] = (),
) -> Any:
    """Resolve a key from explicit input, then matching conduction outputs."""
    data = input_of(request)
    keys = (key, *aliases)
    for candidate in keys:
        if candidate in data and not _empty(data[candidate]):
            return data[candidate]
    cond = conduction_of(request)
    def matching_blobs(effector_id: str):
        exact = cond.get(effector_id)
        if exact is not None:
            yield exact
        suffix = f"_{effector_id}"
        yield from (blob for name, blob in cond.items() if name.endswith(suffix))
    for effector_id in effector_ids:
        for blob in matching_blobs(effector_id):
            if not isinstance(blob, Mapping):
                continue
            for candidate in keys:
                if candidate in blob and not _empty(blob[candidate]):
                    return blob[candidate]
    return default


def cond_blob(request: Request, *effector_ids: str) -> dict[str, Any]:
    """Return the first non-empty conduction dictionary."""
    cond = conduction_of(request)
    for effector_id in effector_ids:
        blob = cond.get(effector_id)
        if not isinstance(blob, Mapping) or not blob:
            suffix = f"_{effector_id}"
            blob = next((value for name, value in cond.items() if name.endswith(suffix) and isinstance(value, Mapping) and value), None)
        if isinstance(blob, Mapping) and blob:
            return dict(blob)
    return {}


def upstream_noop(request: Request, *effector_ids: str) -> dict[str, Any]:
    """Return the first upstream no-op output, if any."""
    for effector_id in effector_ids:
        blob = cond_blob(request, effector_id)
        if blob.get("status") == "noop":
            return blob
    return {}
