# Runtime composition (Fala 0.7.15 package host)

Atomic effectors are composed into twelve catalog correlation paths. Production
scheduling is exactly one resident LaunchAgent:
`com.mikolaj92.lokay.supervisor` (`python -m lokay.supervisor`). That supervisor
dispatches the twelve process IDs as logical children via
`python -m lokay.process lokay-process-<id>`; those IDs are inventory only and
must never be installed as separate LaunchAgents.

Retired aggregate aliases (`auto_worker` / `lokay-tick-all`, `issue_intake`,
`lifecycle_ok`) are not production schedulers. Historical package composition
that joined multiple lanes in one host path remains documented in
[`process-map.md`](process-map.md) as retired graph evidence, not as the live
topology.

Canonical process/lane/ownership map: [`process-map.md`](process-map.md).

## Paths (twelve process IDs)

| Path id | Role | Effectors (high level) |
|---------|------|-------------------------|
| `repo_issue_poll` | live catalog process | poll open issues / snapshots |
| `issue_triage` | live catalog process | classify / mutate / feedback / close decisions + receipts |
| `issue_feedback` | live catalog process | needs-feedback mutations |
| `issue_split` | live catalog process | mixed-issue child handoffs |
| `issue_close` | live catalog process | authorized close |
| `issue_ready` | live catalog process | ready/eligibility handoff toward claim |
| `issue_to_pr` | live catalog process | load → parse → worktree → omp → push → pr → labels → receipt → complete |
| `pr_triage` | live catalog process | load PR → checks → evidence → decide → apply (merge/comment/repair gate) |
| `pr_repair` | live catalog process | head-bound repair OMP |
| `pr_merge` | live catalog process | claim_pr → merge → receipt → close_issue |
| `cleanup` | live catalog process | parse branch → verify closed/no PR → remove worktree → delete branch → release claim → receipt → maintenance task |
| `cleanup_reconcile` | live catalog process | validate identity → read GH+local evidence → decide no-target reconcile → receipts |

Diagnostic CLIs (`lokay-tick-*`, retired `lokay-tick-all`) remain manual entrypoints
only; they must not be restored as LaunchAgents.

Fala 0.7.15 package-host conduction passes each upstream effector result directly
to the next prefixed handler; effectors remain single-purpose subprocess adapters.

## Usage (mini-m4-0)

```bash
cd ~/Developer/lokay
uv sync
uv run python -m lokay.process lokay-process-repo_issue_poll --dry-run
uv run python -m lokay.supervisor --config ~/.hermes/lokay/config.toml --db ~/.hermes/lokay/fala/state.sqlite --dry-run
```

Default is **dry-run** unless `--live` is passed. Schedule only
`com.mikolaj92.lokay.supervisor`; run individual process modules manually when
diagnosing one path. Legacy shell intake/dispatch/triage/cleanup, backfill,
webhook, cron, aggregate tick-all, and per-process LaunchAgents are removed and
must not be restored as operational paths.

Safe starter/library defaults are dry-run, executor-disabled, and human-approved. An autonomous production candidate is explicit: `mode=live`, `executor.enabled=true`, `automerge=true`, `require_human_approval=false`, with both checks and test evidence required. Failed checks for the exact current PR head may authorize one durable repair attempt; pending checks wait and never invoke OMP twice for that head.

## Launchd

Template: `templates/launchd/lokay-supervisor.plist.template`
(label `com.mikolaj92.lokay.supervisor`, `RunAtLoad` + `KeepAlive`, no
`StartInterval`).

Promote one immutable Fala candidate and verify that the supervisor is the only
loaded mutator LaunchAgent. Health/status checks may report residual aggregate
or per-process labels solely to enforce their absence; those labels are not
runnable deployment paths.

## Definition of Done

“The scheduler is healthy”, “the mechanics work”, and “the agent resolves issues” are separate claims.

### Scheduled runtime is healthy

- The installed supervisor LaunchAgent stays loaded (`KeepAlive`) and continues dispatching enabled catalog processes; manual `kickstart` does not count as sustained health.
- Its configured stdout log exists, is recent, and contains the same run IDs as the Fala journal for child process work.
- The latest live child runs complete with no failed, waiting, or unresolved processes; health, status, candidate validation, and deployment parity exit zero.
- An empty queue is reported as idle/noop activity. A successful no-op proves scheduler health only, never issue resolution.

### End-to-end mechanics work

- A controlled canary is discovered by naturally scheduled supervisor dispatch and proceeds through intake, claim, Kanban, implementation, branch, PR, triage, merge, issue closure, receipts, and cleanup.
- GitHub, Kanban, Fala DB, logs, receipts, and the deployed commit agree on the same issue and PR.
- Expected mutation steps record `mutated=true` and the run reports worked activity; tests alone, many succeeded no-ops, or `last_exit=0` are insufficient.

### The agent resolves issues

- In addition to the canary, at least one pre-existing, non-smoke, non-E2E, non-canary user issue with explicit acceptance criteria completes through the naturally scheduled flow.
- After merge, every acceptance criterion is verified against `main`, not only against the worker branch or PR checks.
- The final evidence names the issue, PR, merge commit, verification commands/results, cleanup receipt, Fala run IDs, and matching log entries.

Only the third gate permits the claim that the agent resolves real issues. A canary can satisfy the mechanics gate but can never satisfy the value gate by itself.
