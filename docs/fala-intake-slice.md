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
`810671075b478c1cc5950eafe892826a17c068bf` before syncing.

## Run diagnostic

```bash
cd ../Fala && git fetch --tags && git checkout 810671075b478c1cc5950eafe892826a17c068bf && cd -
uv sync
uv run repo-agent-tick-intake --dry-run
```

For scheduled operation use only:

```bash
uv run repo-agent-tick-all --dry-run
```
