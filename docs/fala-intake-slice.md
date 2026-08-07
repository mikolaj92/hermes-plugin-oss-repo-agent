# Fala intake slice (v0) — **fala-runtime 0.7.15**

## Goal

Document the intake correlation path hosted by Fala 0.7.15. Production
scheduling is the resident supervisor LaunchAgent
(`com.mikolaj92.lokay.supervisor`), which dispatches catalog process IDs such as
`repo_issue_poll` and `issue_triage`. This slice is a manual diagnostic
entrypoint only.

## Runtime

```text
fala == 0.7.15
```

Installed from Git via `[tool.uv.sources]` (`mikolaj92/Fala` tag `v0.7.15`).

## Run diagnostic

```bash
uv sync
uv run lokay-tick-intake --dry-run
# preferred catalog process form:
uv run python -m lokay.process lokay-process-repo_issue_poll --dry-run
```

For scheduled operation use only the supervisor LaunchAgent template
`templates/launchd/lokay-supervisor.plist.template` (label
`com.mikolaj92.lokay.supervisor`). Do not schedule `lokay-tick-all`,
`auto_worker`, or per-process LaunchAgents.
