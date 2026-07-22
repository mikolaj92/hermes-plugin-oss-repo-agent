# Start here

Run the composed Fala auto-worker in dry-run mode first. It does not merge,
force push, delete branches, or run an executor.

```bash
hermes oss-repo-agent --config config.yaml init
hermes oss-repo-agent --config config.yaml validate
uv run repo-agent-tick-all --dry-run
```

The first command writes a starter `config.yaml` with `mode: dry-run`,
`automerge: false`, and `executor.enabled: false`.

To use real repositories, edit `github.assignee` and `repos:` in the generated
config and keep running dry-run auto-worker commands until the planned graph
looks correct. Live mutation requires the configured live mode and the
explicit `--live` flag.

`repo-agent-tick-all` / `auto_worker` is the sole scheduled mutator. Individual
ticks (`repo-agent-tick-intake`, `repo-agent-tick-dispatch`,
`repo-agent-tick-triage`, and `repo-agent-tick-cleanup`) are manual diagnostics
only, not deployment paths. Legacy shell intake/dispatch/triage/cleanup,
backfill, webhook, and cron entrypoints are removed.

CI-style checks:

```bash
python3 -m unittest discover -s tests
python3 tools/hygiene_check.py .
```
