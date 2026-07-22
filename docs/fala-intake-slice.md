# Fala intake slice (v0) — **fala-runtime 0.2.1**

## Goal

Orchestrate three atomic **effectors** with **Fala 0.2.x** correlation paths
(not the old 0.1.x flow API):

1. `poll` — read eligible GitHub issues (`gh` only)
2. `claim` — assign/label selected issue (dry-run by default)
3. `kanban` — ensure Hermes Kanban `[issue]` task (dry-run by default)

Fala owns: `create_run`, `instantiate_correlation_path`, claim/execute/advance
via `run_correlation_path` / `run_until_idle`, terminal run status.

## Requirement

```text
fala-runtime == 0.2.1   # or newer 0.2.x with correlation_paths
```

On mini-m4-0 the path dep is `../Fala` (must be checked out at tag `0.2.1+`).

## Run

```bash
cd ~/Developer/hermes-plugin-oss-repo-agent
# ensure sibling Fala is current
cd ../Fala && git fetch --tags && git checkout 0.2.1 && cd -

uv sync
uv run repo-agent-tick-intake --dry-run
uv run repo-agent-tick-intake --json
uv run repo-agent-tick-intake --live   # mutations

# Full auto-worker lifecycle ticks (all default dry-run)
uv run repo-agent-tick-dispatch --dry-run
uv run repo-agent-tick-triage --dry-run
uv run repo-agent-tick-triage --decide-only --dry-run
uv run repo-agent-tick-cleanup --branch 'ai/fix/1-example' --dry-run
```

DB: `~/.hermes/oss-repo-agent/fala/state.sqlite`

## Correlation paths

| Path id | Tick CLI | Effectors (condensed) |
|---------|----------|------------------------|
| `issue_intake` | `repo-agent-tick-intake` | poll → direction → comment → claim → kanban |
| `issue_to_pr` | `repo-agent-tick-dispatch` | load → parse → worktree → omp → verify → push → open_pr → labels → receipt → complete |
| `pr_triage` | `repo-agent-tick-triage` | list → load → checks → evidence → decide |
| `pr_merge` | (router follow-up) | claim_pr → merge → receipt → close_issue |
| `pr_comment_block` | (router follow-up) | comment_pr |
| `pr_repair` | (router follow-up) | review_task → prompt → worktree → omp → push |
| `cleanup` | `repo-agent-tick-cleanup` | parse → closed → no_open_pr → remove_wt → del_branch → release_claim |

Triage router: after `decide_triage_action`, `run_triage_with_router` runs the
matching follow-up path (`merge` | `comment_block` | `repair`) unless
`--decide-only` or action is `skip`.

## Tests

```bash
uv run python -m unittest tests.test_fala_intake_flow tests.test_path_composition -v
uv run python -m unittest discover -s tests -v
```

## Terminology (0.2.x)

| 0.1.x (legacy) | 0.2.x |
|----------------|-------|
| flow / step / needs | correlation_path / effector / conduction |
| StepRunRequest | EffectorRunRequest |
| instantiate_flow | instantiate_correlation_path |
| needs[step] | input.conduction[effector] |
