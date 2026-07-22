# Auto-worker (Fala 0.7.9 package host)

Mega-atomic effectors are composed into correlation paths. `auto_worker`,
invoked by `repo-agent-tick-all`, is the sole scheduled mutator. Individual
ticks are CLI entrypoints for manual diagnostics only.

## Paths

| Path id | Diagnostic CLI | Effectors (high level) |
|---------|----------------|-------------------------|
| `issue_intake` | `repo-agent-tick-intake` | poll → direction → comment → claim → kanban |
| `issue_to_pr` | `repo-agent-tick-dispatch` | load → parse → worktree → omp → push → pr → labels → receipt → complete |
| `pr_triage` | `repo-agent-tick-triage` | load PR → checks → evidence → decide → apply (merge/comment/repair) |
| `cleanup` | `repo-agent-tick-cleanup` | parse branch → verify closed/no PR → remove worktree → delete branch → release claim |
| `auto_worker` | `repo-agent-tick-all` | one package-host run containing the prefixed intake, dispatch, triage, and cleanup graph |

Fala 0.7.9 package-host conduction passes each upstream effector result directly
to the next prefixed handler; effectors remain single-purpose subprocess adapters.

## Usage (mini-m4-0)

```bash
cd ~/Developer/hermes-plugin-oss-repo-agent
uv sync
uv run repo-agent-tick-all --dry-run
uv run repo-agent-tick-all --live   # only after dry-run looks good
```

Default is **dry-run** unless `--live` is passed. Schedule only
`repo-agent-tick-all`; run the individual ticks above manually when diagnosing
one path. Legacy shell intake/dispatch/triage/cleanup, backfill, webhook, cron,
and separate launchd jobs are removed and must not be restored as operational
paths.

## Launchd

Template: `templates/launchd/oss-repo-agent-fala-tick-all.plist.template`.

Promote one immutable Fala candidate and verify that the auto-worker is the
only loaded mutator. Health/status checks may report historical legacy labels
to enforce their absence; those labels are not runnable deployment paths.
