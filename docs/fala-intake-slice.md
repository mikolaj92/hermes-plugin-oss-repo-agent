# Fala intake slice (v0) — **fala-runtime 0.7.15**

## Goal

Document the intake correlation path hosted by Fala 0.7.15. Production
scheduling runs the composed `auto_worker` path; this slice is a manual
diagnostic entrypoint only.

## Runtime

```text
fala == 0.7.15
```

Installed from Git via `[tool.uv.sources]` (`mikolaj92/Fala` tag `v0.7.15`).

## Run diagnostic

```bash
uv sync
uv run lokay-tick-intake --dry-run
```

For scheduled operation use only:

```bash
uv run lokay-tick-all --dry-run
```
