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

After install, Hermes may show [`after-install.md`](after-install.md). The short version is: create a starter config, validate it, then run dry-run intake and dispatch.

## Deployment

The legacy `render-launchd` command currently reports deployment metadata; it does not write files or install LaunchAgents. Do not pass `--deploy`.

```bash
hermes oss-repo-agent --config ~/.hermes/oss-repo-agent/config.toml render-launchd --output <metadata-path>
```

The Fala scheduler is currently a source template only:
`templates/launchd/oss-repo-agent-fala-tick-all.plist.template`. Render its
`UV_BIN`, `REPO_ROOT`, and `HOME` placeholders explicitly, validate with
`plutil -lint`, and keep the existing shell jobs until dry-run and controlled
live validation authorize cutover.

## 3-minute happy path

```bash
hermes oss-repo-agent --config config.yaml init
hermes oss-repo-agent --config config.yaml validate
hermes oss-repo-agent --config config.yaml intake --limit 3
hermes oss-repo-agent --config config.yaml dispatch --max 2
```

Expected dry-run signals:

- `effective_live: false`
- `executed: false`
- `planned_work` showing the GitHub issue read or guarded Kanban task draft intent
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
hermes oss-repo-agent --config <config> bootstrap --apply
hermes oss-repo-agent --config <config> intake --live
hermes oss-repo-agent --config <config> dispatch --live --run-executor
hermes oss-repo-agent --config <config> pr-triage --live --comment
hermes oss-repo-agent --config <config> render-launchd --output <dir>
```

Live mutation requires both `mode: live` in config and an explicit live/apply CLI
flag. Executor runs require live mode, `--run-executor`, and
`executor.enabled: true`.

The mini dispatcher runs OMP workers only when live mode and `--run-opencode`
are enabled. Configure the OMP model, thinking mode, timeout, and worker cap
with `HERMES_ISSUE_TO_PR_OMP_MODEL`, `HERMES_ISSUE_TO_PR_OMP_THINKING`,
`HERMES_OMP_TIMEOUT_SECONDS`, and `HERMES_ISSUE_TO_PR_MAX_OMP_AGENTS`.

## Mini runtime harness

The production `mini-m4-0` automation is tracked in `scripts/`:

- `repo_issue_intake.sh` polls eligible GitHub issues, assigns them to the
  configured repo-agent account, and creates idempotent Hermes Kanban `[issue]`
  tasks.
- `repo_issue_to_pr_dispatch.sh` turns `[issue]` tasks into explicit
  `[fix-pr]` work, runs OMP workers with per-board locks and a hard timeout,
  finalizes Kanban tasks when an open PR appears, and handles `[fix-pr-review]`
  repair tasks from PR triage.
- `repo_pr_triage.sh` watches and claims owner-authored `ai/fix/*` PRs, requires
  labels, checks, mergeability, test evidence, and optional review approval
  before merge, comments on blocked PRs, and queues Kanban repair work for
  fixable failures.
- `repo_agent_cleanup.sh` removes clean controlled `ai/fix` worktrees, and local
  branches, after their GitHub issue is closed and no open PR remains.
- `repo_agent_health.sh` checks launchd, `gh auth`, disk space, logs, stale
  locks, active workers, GitHub queues, Kanban board stats, and Hermes update
  availability.
- `repo_agent_status.sh` prints a one-screen dashboard with launchd state,
  worker locks, queue counts, and recent dispatch/triage/cleanup decisions.
- `repo_agent_hermes_update.sh` checks for Hermes updates and can run
  `hermes update --backup --yes` only when no repo-agent worker lock is active.
- `repo_agent_backfill.sh` runs intake, dispatch, PR triage, and cleanup
  reconciliation without starting code workers.
- `repo_agent_webhook.sh` is an optional trusted event entrypoint that maps
  GitHub events to the same reconciliation scripts; it is not an HTTP listener
  and does not validate webhook signatures.
- `repo_agent_repos.sh` is the single runtime repo registry used by intake,
  dispatch, PR triage, cleanup, health, and status.
- `repo_agent_smoke.sh` runs local runtime regressions.

The launchd templates live in `templates/launchd/` and include
`LimitLoadToSessionType=Background`; without that macOS can reject SSH-driven
`launchctl bootstrap` with an opaque input/output error.

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
