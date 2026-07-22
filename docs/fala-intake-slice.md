# Fala intake slice (v0) — **fala-runtime 0.7.6**

## Goal

Document the intake correlation path hosted by Fala 0.7.6. Production
scheduling runs the composed `auto_worker` path; this slice is a manual
diagnostic entrypoint only.

## Runtime

```text
fala == 0.7.6
```

The local path dependency is `../Fala`; verify it is checked out at commit
`9f10d58462b4e134d5b1cffe8ff9172909df70ea` before syncing.

## Run diagnostic

```bash
cd ../Fala && git fetch --tags && git checkout 9f10d58462b4e134d5b1cffe8ff9172909df70ea && cd -
uv sync
uv run repo-agent-tick-intake --dry-run
```

For scheduled operation use only:

```bash
uv run repo-agent-tick-all --dry-run
```
