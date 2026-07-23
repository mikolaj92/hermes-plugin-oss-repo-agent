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

## Definition of Done

“The scheduler is healthy”, “the mechanics work”, and “the agent resolves issues” are separate claims.

### Scheduled runtime is healthy

- The installed `repo-agent-tick-all` launchd job runs naturally for at least two consecutive `StartInterval` windows; manual `kickstart` does not count.
- Its configured stdout log exists, is recent, and contains the same run IDs as the Fala journal.
- The latest live run is completed with no failed, waiting, or unresolved processes; health, status, candidate validation, and deployment parity exit zero.
- An empty queue is reported as `activity=noop`. A successful no-op proves scheduler health only, never issue resolution.

### End-to-end mechanics work

- A controlled canary is discovered by a naturally scheduled tick and proceeds through intake, claim, Kanban, implementation, branch, PR, triage, merge, issue closure, receipts, and cleanup.
- GitHub, Kanban, Fala DB, logs, receipts, and the deployed commit agree on the same issue and PR.
- Expected mutation steps record `mutated=true` and the run reports `worked=true`; tests alone, 36 succeeded no-ops, or `last_exit=0` are insufficient.

### The agent resolves issues

- In addition to the canary, at least one pre-existing, non-smoke, non-E2E, non-canary user issue with explicit acceptance criteria completes through the naturally scheduled flow.
- After merge, every acceptance criterion is verified against `main`, not only against the worker branch or PR checks.
- The final evidence names the issue, PR, merge commit, verification commands/results, cleanup receipt, Fala run IDs, and matching log entries.

Only the third gate permits the claim that the agent resolves real issues. A canary can satisfy the mechanics gate but can never satisfy the value gate by itself.

