# Start here

Run a safe dry-run first. It does not merge, force push, delete branches, or run an executor.

```bash
hermes oss-repo-agent --config config.toml init
hermes oss-repo-agent --config config.toml validate
hermes oss-repo-agent --config config.toml intake --limit 3
hermes oss-repo-agent --config config.toml dispatch --max 2
```

The first command writes a starter `config.toml` with `mode = "dry-run"`, `automerge = true`, and `executor.enabled = false`.

To use real repositories, edit `github.assignee` and `[[repos]]` in the generated
TOML config and keep running dry-run commands until the planned work looks correct.
The assignee is the public GitHub claim account; the active-issue claim is the
hard duplicate guard.

Live mutation requires both `mode = "live"` in config and an explicit CLI live flag. Executor runs also require `--run-executor` and `executor.enabled = true`.

The mini dispatcher runs OMP workers only when live mode and `--run-opencode`
are enabled. Configure the OMP model, thinking mode, worker timeout, optional
OMP process timeout, and worker cap with `HERMES_ISSUE_TO_PR_OMP_MODEL`,
`HERMES_ISSUE_TO_PR_OMP_THINKING`, `HERMES_OMP_TIMEOUT_SECONDS`,
`HERMES_ISSUE_TO_PR_OMP_MAX_TIME`, and `HERMES_ISSUE_TO_PR_MAX_OMP_AGENTS`.
The worker timeout defaults to 14400 seconds. `HERMES_OMP_TIMEOUT_SECONDS`
overrides the legacy `HERMES_ISSUE_TO_PR_WORKER_TIMEOUT_SECONDS` variable;
set it to `0` only to explicitly disable the supervisor timeout.
`HERMES_ISSUE_TO_PR_OMP_MAX_TIME` remains an independent optional
`omp --max-time` argument.

The six production jobs are scheduled by launchd only. Backfill and webhook
entrypoints may wake reconciliation but do not create a second scheduler.

CI-style checks:

```bash
python3 -m unittest discover -s tests
python3 tools/hygiene_check.py .
scripts/repo_agent_smoke.sh
```

The live mini runtime scripts are in `scripts/`. Keep changes there, commit and
push this repository, then deploy the immutable launchd bundle so config,
runtime, scripts, and plists switch together.
