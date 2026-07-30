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

After install, Hermes may show [`after-install.md`](after-install.md). The short version is: create a starter config, validate it, then run the Fala auto-worker in dry-run mode.

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
policy above. `lokay-tick-all` / `auto_worker` is the sole scheduled mutator.
Individual Fala ticks are manual diagnostics only and must not be installed as
separate scheduled jobs.

## 3-minute happy path

```bash
hermes lokay --config config.yaml init
hermes lokay --config config.yaml validate
uv run lokay-tick-all --dry-run
```

Expected dry-run signals:

- `effective_live: false`
- `executed: false`
- `planned_work` showing the composed auto-worker graph
- `safety_guards` showing the no-merge, no-force-push, no-branch-deletion policy

The plugin registers:

- CLI namespace: `hermes lokay ...`
- Skills:
  - `lokay:repo-gh-cli-policy`
  - `lokay:repo-audit-finding-format`
  - `lokay:repo-fix-issue-pr`
  - `lokay:repo-review-agent-pr`

## Commands

```bash
hermes lokay --config <config.json-or-yaml> init
hermes lokay --config <config.json-or-yaml> validate
hermes lokay --config <config> render-launchd --output <dir>
uv run lokay-tick-all --dry-run
uv run lokay-tick-all --live
```

`lokay-tick-all` / `auto_worker` is the only scheduled mutator. Use
`lokay-tick-intake`, `lokay-tick-dispatch`,
`lokay-tick-triage`, or `lokay-tick-cleanup` only as manual
diagnostic runs while investigating one correlation path; they are not
deployment or scheduling instructions. Legacy shell scripts, backfill,
webhook, and cron entrypoints are removed and are not runnable paths.

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
- `HERMES_LOKAY_REPOS_FILE` optional pipe-delimited repo registry override:
  `owner/repo|board|clone_path|priority`

## Configuration

Default path: `~/.hermes/lokay/config.yaml`

Override with `HERMES_LOKAY_CONFIG` or `--config`.

Start from [`config.example.yaml`](config.example.yaml), or let `init` create a local starter config.

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
