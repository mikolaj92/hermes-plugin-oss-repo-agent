# Fala intake slice (v0) — **fala-runtime 0.7.15**

## Goal

Document the intake correlation path hosted by Fala 0.7.15. Production
scheduling runs the composed `auto_worker` path; this slice is a manual
diagnostic entrypoint only.

## Runtime

```text
fala == 0.7.15
```

The local path dependency is `../Fala`; verify it is checked out at peeled commit
`b5f9a6d500a442a1c79060a862fe4b9da87bc98f` before syncing.

## Run diagnostic

```bash
cd ../Fala && git fetch --tags && git checkout b5f9a6d500a442a1c79060a862fe4b9da87bc98f && cd -
uv sync
uv run lokay-tick-intake --dry-run
```

For scheduled operation use only:

```bash
uv run lokay-tick-all --dry-run
```
