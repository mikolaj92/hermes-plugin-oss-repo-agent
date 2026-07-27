# Start here

Run the composed Fala auto-worker in dry-run mode first. It does not merge,
force push, delete branches, or run an executor.

```bash
hermes lokay --config config.yaml init
hermes lokay --config config.yaml validate
uv run lokay-tick-all --dry-run
```

The first command writes a starter `config.yaml` with `mode: dry-run`,
`automerge: false`, and `executor.enabled: false`.

To use real repositories, edit `github.assignee` and `repos:` in the generated
config and keep running dry-run auto-worker commands until the planned graph
looks correct. Live mutation requires the configured live mode and the
explicit `--live` flag.

`lokay-tick-all` / `auto_worker` is the sole scheduled mutator. Individual
ticks (`lokay-tick-intake`, `lokay-tick-dispatch`,
`lokay-tick-triage`, and `lokay-tick-cleanup`) are manual diagnostics
only, not deployment paths. Legacy shell intake/dispatch/triage/cleanup,
backfill, webhook, and cron entrypoints are removed.

Fast checks (under two minutes):

```bash
uv run python -m unittest discover -s tests
uv run python tools/hygiene_check.py .
```

Deployment and health integration checks are intentionally separate:

```bash
uv run python -m unittest discover -s tests -p 'integration_*.py'
```
