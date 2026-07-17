# Auto-worker (Fala 0.2.x)

Mega-atomic effectors are composed into correlation paths. Ticks are CLI entrypoints
for launchd / manual ops.

## Paths

| Path id | CLI | Effectors (high level) |
|---------|-----|-------------------------|
| `issue_intake` | `repo-agent-tick-intake` | poll → claim → kanban |
| `issue_to_pr` | `repo-agent-tick-dispatch` | load → parse → worktree → omp → push → pr → labels → receipt → complete |
| `pr_triage` | `repo-agent-tick-triage` | load PR → checks → evidence → decide → apply (merge/comment/repair) |
| `cleanup_worktrees` | `repo-agent-tick-cleanup` | list worktrees → cleanup safe ones |
| all | `repo-agent-tick-all` | runs the four paths in sequence |

Bridges in `repo_agent.flows.bridges` remap `conduction` into atomic effectors
without embedding multi-stage logic inside atomics.

## Usage (mini-m4-0)

```bash
cd ~/Developer/hermes-plugin-oss-repo-agent
uv sync
uv run repo-agent-tick-all --dry-run
uv run repo-agent-tick-all --live   # only after dry-run looks good
```

Default is **dry-run** unless `--live` is passed.

## Launchd

Template: `templates/launchd/oss-repo-agent-fala-tick-all.plist.template`

Old shell launchd jobs remain until cutover; Fala ticks can run in parallel first.
