# Fala intake slice (v0) — **fala-runtime 0.7.9**

## Goal

Document the intake correlation path hosted by Fala 0.7.9. Production
scheduling runs the composed `auto_worker` path; this slice is a manual
diagnostic entrypoint only.

## Runtime

```text
fala == 0.7.9
```

The local path dependency is `../Fala`; verify it is checked out at commit
`69bc2ec9d4cdf61773114847c0c582fb2652296d` before syncing.

## Run diagnostic

```bash
cd ../Fala && git fetch --tags && git checkout 69bc2ec9d4cdf61773114847c0c582fb2652296d && cd -
uv sync
uv run repo-agent-tick-intake --dry-run
```

For scheduled operation use only:

```bash
uv run repo-agent-tick-all --dry-run
```
