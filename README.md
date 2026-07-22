# oss-repo-agent
<!-- hermes-repo-agent: issue-5 closed-loop test 20260717 -->

Safe dry-run-first OSS maintainer automation for Hermes.

Use it to inspect open GitHub issues, draft guarded work, and keep agent work inside a no-merge/no-force-push policy before any human-approved execution.

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

## Install

```bash
hermes plugins install mikolaj92/hermes-plugin-oss-repo-agent --enable
```

This repository is a standalone Hermes plugin: `plugin.yaml` and `__init__.py`
live at the repository root.

After install, Hermes may show [`after-install.md`](after-install.md). The short version is: create a starter config, validate it, then run the Fala auto-worker in dry-run mode.

## Deployment

The deployment renderer creates an immutable Fala candidate and never installs
LaunchAgents or changes `deployment/current`:

```bash
hermes oss-repo-agent --config ~/.hermes/oss-repo-agent/config.toml render-launchd \
  --output ~/.hermes/oss-repo-agent/deployment/candidates/<candidate-id> \
  --fala-db ~/.hermes/oss-repo-agent/fala/state.sqlite --mode dry-run
```

Validate the candidate with parity and `plutil -lint` before separately
controlled promotion. `repo-agent-tick-all` / `auto_worker` is the sole
scheduled mutator. Individual Fala ticks are manual diagnostics only and must
not be installed as separate scheduled jobs.

## 3-minute happy path

```bash
hermes oss-repo-agent --config config.yaml init
hermes oss-repo-agent --config config.yaml validate
uv run repo-agent-tick-all --dry-run
```

Expected dry-run signals:

- `effective_live: false`
- `executed: false`
- `planned_work` showing the composed auto-worker graph
- `safety_guards` showing the no-merge, no-force-push, no-branch-deletion policy

The plugin registers:

- CLI namespace: `hermes oss-repo-agent ...`
- Skills:
  - `oss-repo-agent:repo-gh-cli-policy`
  - `oss-repo-agent:repo-audit-finding-format`
  - `oss-repo-agent:repo-fix-issue-pr`
  - `oss-repo-agent:repo-review-agent-pr`

## Commands

```bash
hermes oss-repo-agent --config <config.json-or-yaml> init
hermes oss-repo-agent --config <config.json-or-yaml> validate
hermes oss-repo-agent --config <config> render-launchd --output <dir>
uv run repo-agent-tick-all --dry-run
uv run repo-agent-tick-all --live
```

`repo-agent-tick-all` / `auto_worker` is the only scheduled mutator. Use
`repo-agent-tick-intake`, `repo-agent-tick-dispatch`,
`repo-agent-tick-triage`, or `repo-agent-tick-cleanup` only as manual
diagnostic runs while investigating one correlation path; they are not
deployment or scheduling instructions. Legacy shell scripts, backfill,
webhook, and cron entrypoints are removed and are not runnable paths.

Runtime defaults:

- `HERMES_REPO_AGENT_ASSIGNEE=mikolaj92`
- `HERMES_KANBAN_INTAKE_ASSIGNEE=repo-agent-intake`
- `HERMES_KANBAN_FIXER_ASSIGNEE=repo-agent-fixer`
- `HERMES_OMP_TIMEOUT_SECONDS=1800`
- `HERMES_ISSUE_TO_PR_OMP_MODEL=omniroute/omp/default`
- `HERMES_ISSUE_TO_PR_OMP_THINKING=medium`
- `HERMES_ISSUE_TO_PR_MAX_OMP_AGENTS=3`
- `HERMES_REPO_AGENT_MAX_TASK_ATTEMPTS=3`
- `HERMES_REPO_AGENT_RETRY_BACKOFF_SECONDS=1800`
- `HERMES_PR_REQUIRE_TEST_EVIDENCE=1`
- `HERMES_REPO_CLEANUP_DELETE_LOCAL_BRANCHES=1`
- `HERMES_REPO_AGENT_UPDATE_DRY_RUN=1`
- `HERMES_STALE_LOCK_MINUTES=180`
- `HERMES_REPO_AGENT_MIN_FREE_GB=5`
- `HERMES_REPO_AGENT_REPOS_FILE` optional pipe-delimited repo registry override:
  `owner/repo|board|clone_path|priority`

## Configuration

Default path: `~/.hermes/oss-repo-agent/config.yaml`

Override with `HERMES_OSS_REPO_AGENT_CONFIG` or `--config`.

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
python3 -m unittest discover -s tests
python3 tools/hygiene_check.py .
scripts/repo_agent_smoke.sh
```

<!-- hermes e2e closed-loop test 20260717 -->
