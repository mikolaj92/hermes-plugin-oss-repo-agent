# Lokay
<!-- hermes-lokay: issue-5 closed-loop test 20260717 -->

Safe-by-default OSS maintainer automation for Hermes. Starter/library defaults remain dry-run and manual; autonomous production is an explicit guarded profile.

Production autonomy requires `mode=live`, `executor.enabled=true`, `automerge=true`, `require_human_approval=false`, `require_checks=true`, and `require_test_evidence=true`. A tick with idle intake still scans existing PRs across every configured repository; failed current-head checks can enter the existing-PR repair lane, while pending checks wait without another repair invocation.

GitHub remains a source of truth for public issues, pull requests, discussion,
labels, checks, and merge state. Hermes Kanban is the internal execution ledger:
task decomposition, agent assignment, worktrees, retries, blockers, and repair
tasks. This plugin only bridges the two where needed:

- claim eligible GitHub issues for the configured maintainer account,
- ensure one idempotent Kanban intake task per GitHub issue,
- claim owner-authored `ai/fix/*` pull requests during PR triage,
- let agents use `gh` for actual GitHub actions such as creating PRs,
  commenting evidence, labeling, or merging through the guarded triage gate.

It intentionally does not mirror every Kanban status back into GitHub.
The explicit mapping contract lives in
[`docs/github-kanban-mapping.md`](docs/github-kanban-mapping.md).
The process/lane/ownership scaffold lives in
[`docs/process-map.md`](docs/process-map.md).

## Install

```bash
hermes plugins install mikolaj92/lokay --enable
```

This repository is a standalone Hermes plugin: `plugin.yaml` and `__init__.py`
live at the repository root.

After install, Hermes may show [`after-install.md`](after-install.md). The short version is: copy the canonical TOML config into place, validate it, then dry-run the resident supervisor or one catalog process.

## Deployment

The deployment renderer creates an immutable Fala candidate and never installs
LaunchAgents or changes `deployment/current`:

```bash
hermes lokay --config ~/.hermes/lokay/config.toml render-launchd \
  --output ~/.hermes/lokay/deployment/candidates/<candidate-id> \
  --fala-db ~/.hermes/lokay/fala/state.sqlite --mode dry-run
```

Validate the candidate with parity and `plutil -lint` before separately
controlled promotion. Production promotion accepts only the guarded-autonomous
policy above. Production topology is exactly one resident LaunchAgent
(`com.mikolaj92.lokay.supervisor` via `python -m lokay.supervisor`). That
supervisor dispatches the twelve catalog process IDs (`lokay-process-<id>` via
`lokay.process`) as logical children only—never as separate LaunchAgents.
Retired aggregate aliases (`lokay-tick-all` / `auto_worker`, `issue_intake`,
`lifecycle_ok`) must not be installed as production LaunchAgents.


## 3-minute happy path

```bash
hermes lokay --config ~/.hermes/lokay/config.toml init
hermes lokay --config ~/.hermes/lokay/config.toml validate
uv run python -m lokay.process lokay-process-repo_issue_poll --dry-run
```

Expected dry-run signals:

- `effective_live: false` / dry-run mode
- no live mutations (`executed: false` or equivalent process payload)
- planned or idle catalog process output without side effects
- safety guards remaining no-merge, no-force-push, no-branch-deletion


The plugin registers:

- CLI namespace: `hermes lokay ...`
- Skills:
  - `lokay:repo-gh-cli-policy`
  - `lokay:repo-audit-finding-format`
  - `lokay:repo-fix-issue-pr`
  - `lokay:repo-review-agent-pr`

## Commands

```bash
hermes lokay --config <config.toml> init
hermes lokay --config <config.toml> validate
hermes lokay --config <config.toml> render-launchd --output <dir>
uv run python -m lokay.supervisor --config <config.toml> --db <fala.sqlite> --dry-run
uv run python -m lokay.process lokay-process-<id> --dry-run
```

One resident supervisor LaunchAgent is the only scheduled mutator. Use
`python -m lokay.process lokay-process-<id>` and retired tick CLIs only as
manual diagnostics while investigating one correlation path; they are not
production scheduling instructions. Legacy shell scripts, backfill, webhook,
and cron entrypoints are removed and are not runnable paths.


Operational health distinguishes mechanism from outcome: a completed idle run can prove a healthy scheduler but not successful issue resolution. The only end-to-end success is a real issue producing a code change whose guarded PR is merged to `main`, with the linked issue closed and immutable merge/cleanup receipts verified. Pending repair is active non-success; a terminal repair or merge failure is unhealthy and must name the failing run/process.

Runtime defaults:

- `HERMES_LOKAY_ASSIGNEE=mikolaj92`
- `HERMES_LOKAY_KANBAN_INTAKE_ASSIGNEE=lokay-intake`
- `HERMES_LOKAY_KANBAN_FIXER_ASSIGNEE=lokay-fixer`
- `HERMES_LOKAY_OMP_TIMEOUT_SECONDS=1800`
- `HERMES_LOKAY_ISSUE_TO_PR_OMP_MODEL=omniroute/omp/default`
- `HERMES_LOKAY_ISSUE_TO_PR_OMP_THINKING=medium`
- `HERMES_LOKAY_ISSUE_TO_PR_MAX_OMP_AGENTS=3`
- `HERMES_LOKAY_PR_REQUIRE_TEST_EVIDENCE=1`
- `HERMES_LOKAY_CLEANUP_DELETE_LOCAL_BRANCHES=1`
- `HERMES_LOKAY_UPDATE_DRY_RUN=1`
- `HERMES_LOKAY_STALE_LOCK_MINUTES=180`
- `HERMES_LOKAY_MIN_FREE_GB=5`

## Configuration

Default path: `~/.hermes/lokay/config.toml`

Override the path with `HERMES_LOKAY_CONFIG` or `--config`; the environment variable selects a TOML file and does not provide configuration content.

Start from [`config.example.toml`](config.example.toml). `init` copies the canonical checkout or packaged TOML config and never generates a starter file.

## v0 limitations

- The CLI facade is dry-run first; live mini runtime scripts include a guarded
  PR merge gate.
- No force-push or branch deletion behavior.
- Launchd output is template-only and macOS-specific.
- GitHub access goes through the `gh` CLI wrappers only.
- Local git commands are rendered with `GIT_MASTER=1` and executed with that
  environment variable set.

## Checks

```bash
uv run python -m unittest discover -s tests
uv run python tools/hygiene_check.py .
scripts/lokay_smoke.sh
```

The default suite excludes deployment integration fixtures and should finish
within two minutes. Run those explicitly when changing deployment or health
behavior:

```bash
uv run python -m unittest discover -s tests -p 'integration_*.py'
```

<!-- hermes e2e closed-loop test 20260717 -->
