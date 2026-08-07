# Start here

Dry-run the resident supervisor or one catalog process first. Neither merges,
force pushes, deletes branches, nor runs an executor unless live mode is
explicitly authorized.

```bash
hermes lokay --config ~/.hermes/lokay/config.toml init
hermes lokay --config ~/.hermes/lokay/config.toml validate
uv run python -m lokay.process lokay-process-repo_issue_poll --dry-run
```

`init` copies the canonical `config.toml` from the checkout or packaged plugin into `~/.hermes/lokay/config.toml`; it does not generate or overwrite a starter file.

To use real repositories, edit the copied TOML configuration and keep running
dry-run process commands until the planned graph looks correct. Live mutation
requires the configured live mode and the explicit `--live` flag.

Production topology is exactly one resident LaunchAgent
(`com.mikolaj92.lokay.supervisor` via `python -m lokay.supervisor`). The twelve
catalog process IDs (`lokay-process-<id>` via `lokay.process`) are logical
children only and must never be installed as separate LaunchAgents. Retired
aggregate aliases (`lokay-tick-all` / `auto_worker`, `issue_intake`,
`lifecycle_ok`) and individual tick CLIs are manual diagnostics only, not
production LaunchAgent paths. Legacy shell intake/dispatch/triage/cleanup,
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
