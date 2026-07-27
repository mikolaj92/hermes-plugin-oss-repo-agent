# Make Lokay Resolve Real Issues End to End

## TL;DR
> Summary: Repair Lokay’s orchestration so every natural scheduler tick independently reconciles interrupted lifecycle state, scans intake and existing PRs across every configured repository, executes a fail-closed repair loop on the selected PR branch, and merges only after current-head checks and test evidence pass. Then promote an immutable autonomous candidate and recover `mikolaj92/lokay#10` / PR `#11` until that exact PR’s code is on `main` and cleanup is terminal.
> Deliverables:
> - One launchd-scheduled `auto_worker` with independent multi-repository intake and PR-triage lanes.
> - Existing-PR repair chain with remote-head anchoring, ownership/provenance, executor gating, OMP, no-force push, readback, CI wait/retry state, and re-triage.
> - Automated lifecycle reconciliation for stale claims, labels, tasks, worktrees, branches, and receipts.
> - Explicit autonomous production policy while starter/library defaults remain fail-closed.
> - Immutable candidate promotion and real end-to-end evidence for issue `#10` → existing PR `#11` → merge on `main` → issue closed → cleanup terminal.
> Effort: XL
> Risk: High - this deliberately enables code execution and guarded GitHub merge mutations; correctness depends on preserving exact provenance, current-head checks, receipt, and rollback contracts.

## Scope
### Must have
- Keep exactly one launchd scheduler and one scheduled mutator entrypoint: `lokay-tick-all`. Independent PR handling is in-tick Fala graph work, not a second launchd job.
- Scan every configured repository during each lane. An idle intake lane must not suppress PR triage; one repository’s noop/failure must not silently substitute another repository’s context.
- Reconcile interrupted work before selecting new work. GitHub is authoritative for issue/PR state and remote SHAs; local claims, Kanban tasks, worktrees, Fala rows, and receipts are execution evidence that must match before reuse or deletion.
- Repair an existing PR by checking out its exact remote head into a confined owned worktree, invoking OMP, requiring a new commit, pushing without force, and verifying `local_oid == remote_oid` for the same branch and PR.
- Persist the verified pushed head and its check state. Pending checks wait without a second OMP invocation. A later repair attempt is allowed only for a terminal failed/conflicting result attached to the current verified head.
- Merge only the selected owner-authored open/non-draft `ai/fix/*` PR targeting `main`, with no `ai:blocked`, authoritative mergeability, passing required checks, passing required test evidence, verified current head, and explicit autonomous policy.
- Keep `require_checks=true` and `require_test_evidence=true`. Production autonomy is exactly `executor_enabled=true`, `automerge=true`, `require_human_approval=false`; no missing/malformed value may default open.
- Preserve Fala `0.7.15` at peeled commit `b5f9a6d500a442a1c79060a862fe4b9da87bc98f`, the canonical atomic boundaries, immutable candidate identity, fsync/readback, no-clobber receipts, worktree confinement, rollback, cleanup ownership, multi-repository routing, and remote-branch retention policy.
- Recover the existing lifecycle without manually editing code, pushing the PR branch, changing GitHub labels, rerunning checks, merging, or closing the issue outside the orchestration. Before/after evidence must prove PR `#11` was updated rather than replaced.
- Continue fixing failures until the DoD is reached. A green test suite, healthy noop, launched service, generated review task, open PR, or successful OMP call is not success.

### Must NOT have
- No second scheduler, legacy mutator, manual `kickstart` used as end-to-end proof, or launchd template/argv change unless implementation proves the single tick entrypoint cannot express the graph (default decision: it can).
- No force push, branch reset, broad cleanup, foreign worktree/branch deletion, unverified claim deletion, or manual label repair.
- No second PR for issue `#10`; repair must retain PR number `11` and its head branch.
- No weakening checks, test evidence, owner/head/base, blocked-label, mergeability, current-head, provenance, receipt, issue-link, or cleanup gates.
- No OMP invocation when executor is disabled, in dry-run mode, when the selected PR head has pending checks, or when the same head has already been repaired and awaits fresh results.
- No cleanup conduction from a nonterminal repair/wait state. No merge receipt before verified merge readback. No claim release before cleanup evidence authorizes it.
- No changing safe library/config parser defaults or `starter_config()` to autonomous behavior. Autonomy is an explicit production profile only.
- No admission of Temida issues lacking `ai:ready`; Temida activity is not part of the primary DoD for this plan.
- No dirty Lokay/Fala candidate source and no deletion of `.omo/`, `.tmp/`, Fala `.fala-effector-*`, or the preserved sibling Fala stash.

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: tests-after using focused Python `unittest`, Fala path/integration tests, shell syntax checks, deployment candidate tests, health/status integration tests, and a real natural-cycle live proof.
- QA policy: each implementation todo includes a happy path and a failure/fail-closed scenario. Test observable contracts and process outputs, not source strings.
- Evidence: `.omo/evidence/task-<N>-<slug>.<ext>`. Live evidence must contain timestamps and immutable identifiers; secrets are redacted without removing correlation fields.
- Existing unit suites remain fast. Deployment/health/live scenarios remain explicit `integration_*.py` or operational evidence.
- Real DoD evidence must name: issue `mikolaj92/lokay#10`; PR `#11`; original remote head `ccc470458c0f4eb3cc96da7ea1cfcfc7915c98a7`; repaired remote head; fresh check run IDs and conclusions for that repaired head; Fala candidate/run/process IDs; OMP pre/post heads; merge SHA; proof merge SHA is reachable from current `origin/main`; issue closed state; merge receipt; cleanup receipt; absent/released owned claim and worktree.
- Natural-cycle observation is state-driven and bounded: observe scheduled runs until terminal success or until the configured OMP timeout plus CI completion allowance and at least two additional 600-second scheduler intervals expire. At timeout, record failure, diagnose, fix, redeploy if needed, and restart proof; never declare partial success.

## Execution strategy
### Parallel execution waves
> Target 5-8 todos per wave where dependencies permit. Graph contracts are established before independent implementation slices; live mutations remain serialized.
- Wave 1 (foundation, no live mutation): Todos 1-4. Todo 1 freezes evidence; Todos 2-4 may proceed in parallel after its inventory is shared.
- Wave 2 (graph integration): Todos 5-8. Wire the independent lanes, repair graph, lifecycle reconciliation, and state-driven retry using the Wave 1 contracts.
- Wave 3 (policy/deployment readiness): Todos 9-11 in parallel after graph behavior is integrated.
- Wave 4 (immutable rollout): Todos 12-13 sequentially build/promote and prove natural scheduler health.
- Wave 5 (real recovery/DoD): Todos 14-16 sequentially reconcile `#10/#11`, repair through natural cycles, merge/close/clean up, and verify `main`.
- Final wave: F1-F4 in parallel only after Todo 16 has direct DoD evidence.
- Critical path: 1 → 2/3/4 → 5/6/7/8 → 9/10/11 → 12 → 13 → 14 → 15 → 16 → F1-F4.

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
|---|---|---|---|
| 1 | None | 2-16 | None while freezing state |
| 2 | 1 | 5, 6, 8 | 3, 4 |
| 3 | 1 | 6, 7 | 2, 4 |
| 4 | 1 | 7, 8 | 2, 3 |
| 5 | 2 | 8, 11 | 6, 7 |
| 6 | 2, 3 | 8, 11 | 5, 7 |
| 7 | 3, 4 | 8, 11 | 5, 6 |
| 8 | 2-7 | 9-12 | None during graph integration |
| 9 | 8 | 12 | 10, 11 |
| 10 | 8 | 12 | 9, 11 |
| 11 | 5-8 | 12 | 9, 10 |
| 12 | 8-11 | 13 | None |
| 13 | 12 | 14 | None |
| 14 | 13 | 15 | None |
| 15 | 14 | 16 | None |
| 16 | 15 | F1-F4 | None |

## Todos
> Implementation and its contract tests are one todo. Every mutation boundary remains one Fala process: one external read, one external mutation, or one pure decision/transform.

- [ ] 1. Freeze current code, deployment, and `#10/#11` evidence
  - What to do: capture current Lokay/Fala commits and tracked cleanliness; preserve hashes for excluded local artifacts; capture promoted candidate identity, active config policy, exact launchd label/domain/argv, scheduler run baseline, production DB schema, active claims, Kanban tasks, owned worktrees/branches, receipts, and authoritative GitHub issue/PR/head/check/linkage state. Record the known starting PR head `ccc470458c0f4eb3cc96da7ea1cfcfc7915c98a7`. Inspect actual SQLite tables before querying; do not assume `process_runs`. Must NOT mutate GitHub, Kanban, Git branches, claims, deployment, or launchd.
  - Parallelization: Can parallel N | Wave 1 prerequisite | Blocks 2-16.
  - References: `/Users/mini-m4-main/.hermes/lokay/config.toml`; `/Users/mini-m4-main/.hermes/lokay/deployment/current/manifest.json`; `/Users/mini-m4-main/.hermes/lokay/fala/state.sqlite`; `scripts/lokay_status.sh`; `scripts/lokay_health.sh`; `src/lokay/config.py`; `fala-package.toml`.
  - Acceptance criteria: evidence contains exact local/source/candidate/config hashes; actual DB schema; GitHub issue `#10` labels/state; PR `#11` state/head/base/linkage/check runs; all matching local claims/tasks/worktrees/receipts; no state changed during collection.
  - QA scenarios: happy—read each authoritative source and store normalized evidence in `.omo/evidence/task-1-baseline.json`; failure—prove malformed/missing local evidence is reported as unknown/conflict rather than inferred in `.omo/evidence/task-1-baseline-failclosed.txt`.
  - Commit: N | Evidence only.

- [ ] 2. Define canonical multi-repository lane and selection contracts
  - What to do: implement a pure repository fanout/aggregation contract for `auto_worker`: each configured repo produces one intake read and one PR read result; candidate selection is deterministic by configured priority and stable tie-breaker; repository context (`repo`, `board`, `clone_path`, priority) travels with every selected issue/PR. One repo’s terminal read failure makes the lane fail closed with attribution; a noop in intake never suppresses PR reads. Avoid the current `repos[0]` behavior in `src/lokay/steps/poll.py::read_open_issues` and the exactly-one context restriction in `src/lokay/flows/triage.py::_resolve_repo_context` for composed execution.
  - Parallelization: Can parallel Y | Wave 1 | Blocks 5, 6, 8 | With 3, 4.
  - References: `src/lokay/steps/poll.py::{read_open_issues,normalize_issue_rows,filter_issue_eligibility,select_issue_candidate}`; `src/lokay/steps/triage.py::{read_open_prs,filter_fix_prs,select_fix_pr}`; `src/lokay/tick_all.py::_prefixed_inputs`; `src/lokay/flows/triage.py::_resolve_repo_context`; `fala-package.toml` paths `issue_intake`, `pr_triage`, `auto_worker`; `tests/test_path_composition.py`; `tests/test_fala_intake_flow.py`; `tests/test_fala_triage_router.py`.
  - Acceptance criteria: focused tests show two configured repos are both read; higher-priority eligible work wins deterministically; an idle issue lane still returns a selected failed PR; each output retains its exact repo/board/clone context; repo read failure is attributed and not replaced by another repo’s data.
  - QA scenarios: happy—run focused multi-repo atom/flow tests with one idle intake and a repairable PR in the second repo, evidence `.omo/evidence/task-2-multirepo.txt`; failure—inject malformed/failing GitHub output for one repo and prove terminal attribution/no cross-routing, evidence `.omo/evidence/task-2-multirepo-failclosed.txt`.
  - Commit: Y | `refactor(orchestration): separate repository intake and triage lanes` | `src/lokay/steps/poll.py`, `src/lokay/steps/triage.py`, flow helpers, focused tests.

- [ ] 3. Implement existing-PR repair ownership and worktree preparation
  - What to do: add repair-specific pure/read/mutation atoms that derive the selected PR’s exact repo, number, head branch, head OID, base branch, linked issue, clone, and deterministic worktree path; read remote head; inspect branch/worktree provenance; decide reuse/create/conflict; create or reuse a local branch anchored to the exact verified remote PR head; write/read back ownership metadata. Do not reuse `issue_to_pr.create_local_branch`’s `head == base` invariant unchanged. An existing owned worktree may be resumed only when branch, PR, repo, issue, receipt/run provenance, and remote head agree. Foreign/stale/conflicting ownership fails closed.
  - Parallelization: Can parallel Y | Wave 1 | Blocks 6, 7 | With 2, 4.
  - References: `src/lokay/steps/issue_to_pr.py::{read_clone_preconditions,fetch_clone_origin,read_base_ref,read_worktree_inventory,read_branch_provenance,create_local_branch,write_branch_provenance,add_worktree,verify_worktree_head}`; `src/lokay/adapters_git.py`; `src/lokay/steps/triage.py::load_pr_fields`; `tests/test_worktree_safety.py`; `tests/test_atomic_effectors.py`.
  - Acceptance criteria: test creates a repair worktree exactly at a mocked PR remote head different from base; reuse succeeds only for identical provenance/head; branch/worktree collision and changed remote head fail without mutation; no force/reset command is issued.
  - QA scenarios: happy—focused temporary-Git integration proves exact remote-head checkout and provenance readback, evidence `.omo/evidence/task-3-repair-worktree.txt`; failure—foreign branch/path and remote-head mismatch are rejected with terminal reasons and unchanged refs, evidence `.omo/evidence/task-3-repair-worktree-failclosed.txt`.
  - Commit: Y | `feat(repair): prepare owned worktrees from PR heads` | repair/worktree atoms, catalog registration, focused tests.

- [ ] 4. Add explicit fail-closed executor and repair-attempt decisions
  - What to do: add a pure executor decision immediately before OMP. Resolve `executor_enabled` using request input → effector config → `False`; require live mode/non-dry-run for mutation. Persist/derive repair-attempt identity from repo, PR, verified remote head, check-run identities/conclusions, and candidate/run provenance. Decide `invoke`, `wait_checks`, `already_repaired`, or terminal conflict. Pending/no fresh check data must wait; the same pushed head cannot invoke OMP twice; a new invocation requires a terminal failed/conflicting result for the current head.
  - Parallelization: Can parallel Y | Wave 1 | Blocks 7, 8 | With 2, 3.
  - References: `src/lokay/tick_all.py::_step_config`; `src/lokay/flows/triage.py::_step_config`; `src/lokay/steps/issue_to_pr.py::{read_omp_preconditions,invoke_omp,verify_omp_postconditions}`; `src/lokay/envelope.py::{cfg_of,input_of,terminal_upstream,upstream_noop}`; `src/lokay/config.py::ExecutorConfig`; `tests/test_atomic_effectors.py`; `tests/test_decide_matrix.py`.
  - Acceptance criteria: disabled live and enabled dry-run never call `run_omp`; enabled live with fresh failed checks invokes exactly once; pending checks and repeated same-head ticks produce non-mutating waits; malformed booleans/state fail terminally rather than enable execution.
  - QA scenarios: happy—mocked adapter counter proves one invocation for one actionable head and zero on repeat, evidence `.omo/evidence/task-4-executor-gate.txt`; failure—missing/malformed/disabled policy and pending checks produce no adapter call and no mutation, evidence `.omo/evidence/task-4-executor-gate-failclosed.txt`.
  - Commit: Y | `feat(executor): gate repair attempts by verified head state` | executor decision atoms, config plumbing, focused tests.

- [ ] 5. Make PR triage independent of intake and dispatch
  - What to do: restructure `auto_worker` into independent in-tick lanes under the same Fala correlation path. PR repository reads start from configured repos/reconciliation output, not `dispatch_verify_task_completed`. Preserve independent intake→dispatch conduction for new issues. Add an explicit pure lane join that reports each lane’s status and only passes terminal identity to cleanup. Do not create another launchd entrypoint or scheduler.
  - Parallelization: Can parallel Y | Wave 2 | Blocks 8, 11 | With 6, 7.
  - References: `fala-package.toml` `auto_worker` around `dispatch_verify_task_completed` → `triage_read_open_prs`; `src/lokay/tick_all.py::{_prefixed_inputs,run_all}`; `src/lokay/envelope.py` suffix resolution; `tests/test_path_composition.py`; `tests/integration_health_status_scripts.py` sole scheduler assumptions.
  - Acceptance criteria: composed Fala test with `no_selected_issue` still executes all PR read/filter/select atoms and chooses PR `#11`-shaped input; dispatch failure does not masquerade as PR noop; one tick still has one path/run and the launchd contract is unchanged.
  - QA scenarios: happy—run composed package path fixture with idle intake plus failing PR and save all process statuses/conduction in `.omo/evidence/task-5-independent-triage.json`; failure—inject dispatch terminal failure and prove triage result remains separately attributed while aggregate run fails, evidence `.omo/evidence/task-5-independent-triage-failclosed.json`.
  - Commit: Y | `refactor(fala): decouple PR triage from issue dispatch` | `fala-package.toml`, `tick_all.py`, path tests.

- [ ] 6. Build the complete atomic existing-PR repair chain
  - What to do: replace the current repair prompt → block-task tail with exact atoms for repair context/read remote head/ownership decision/worktree creation/provenance/readback/executor decision/OMP preconditions/invocation/postconditions/local head/push decision/push/remote readback/OID verification/existing PR readback. Reuse safe primitives where their invariants match; add repair-specific atoms where issue creation semantics do not. Push the selected head branch without force and never call PR creation. Register every handler in `src/lokay/catalog.py` and both standalone/composed graph IDs with exact declared predecessors and canonical/prefixed suffix conduction.
  - Parallelization: Can parallel Y | Wave 2 | Blocks 8, 11 | With 5, 7.
  - References: `src/lokay/steps/repair.py::_repair_decision_gate,build_repair_prompt`; `src/lokay/steps/issue_to_pr.py::{read_omp_preconditions,invoke_omp,verify_omp_postconditions,read_worktree_head,decide_branch_has_commits,read_push_head,push_branch,read_pushed_ref,verify_push_oid,read_open_pr_for_branch,decide_existing_pr}`; `src/lokay/catalog.py`; `fala-package.toml` paths `pr_triage` and `auto_worker`; `tests/test_atomic_effectors.py`; `tests/test_path_composition.py`.
  - Acceptance criteria: graph inventory contains every new atomic ID in standalone and `triage_` composed form; every atom consumes an exact predecessor; repair updates selected PR branch and verifies remote OID; no `create_pull_request` process is reachable from repair; OMP unchanged head/path escape/push mismatch fail at their exact atoms.
  - QA scenarios: happy—hermetic Fala repair path moves mocked PR head A→B, verifies B remotely, and retains PR number, evidence `.omo/evidence/task-6-repair-chain.json`; failure—run unchanged-head, escaped-path, foreign branch, and push-readback mismatch cases with no downstream merge/cleanup, evidence `.omo/evidence/task-6-repair-chain-failclosed.json`.
  - Commit: Y | `feat(fala): execute atomic PR repairs` | repair/issue_to_pr atoms, catalog, package graph, path tests.

- [ ] 7. Add state-driven CI wait, re-triage, and repair receipts
  - What to do: after verified push, publish an immutable repair receipt containing selected PR/head-before/head-after/check basis/OMP result/run/process/candidate/config/provenance. On later ticks, read PR/check state for `head_after`: pending waits; green continues through existing evidence and merge gates; failed permits another repair attempt anchored to that new head; stale check data for any other SHA is ignored/fails closed. Re-triage must be a later natural tick unless the same tick obtains authoritative fresh check data. Keep retry classification and no-clobber/fsync/readback behavior.
  - Parallelization: Can parallel Y | Wave 2 | Blocks 8, 11 | With 5, 6.
  - References: `src/lokay/steps/triage.py::{load_pr_fields,evaluate_checks,evaluate_test_evidence,decide_triage_action,read_merge_preconditions}`; receipt patterns in `src/lokay/steps/issue_to_pr.py::{build_dispatch_receipt,publish_dispatch_receipt,verify_dispatch_receipt}` and `src/lokay/steps/triage.py::{build_merge_receipt,publish_merge_receipt,verify_merge_receipt}`; `src/lokay/steps/cleanup.py`; `tests/test_receipt_durability.py`; `tests/test_decide_matrix.py`.
  - Acceptance criteria: receipt is immutable and binds exact before/after OIDs; repeat tick with pending checks performs zero OMP/push/merge mutations; green checks/evidence for exact repaired head route merge; failed checks for exact head create at most one next attempt; stale green checks cannot merge.
  - QA scenarios: happy—simulate failed@A → repair B → pending@B → green@B and prove exactly one OMP plus merge routing, evidence `.omo/evidence/task-7-repair-loop.json`; failure—supply green results for A while PR head is B and prove terminal/stale hold, evidence `.omo/evidence/task-7-repair-loop-failclosed.json`.
  - Commit: Y | `feat(repair): persist head-bound repair progress` | repair receipt/state atoms, graph, tests.

- [ ] 8. Integrate authoritative lifecycle reconciliation before selection
  - What to do: add preselection reconciliation atoms to `auto_worker`: read GitHub issue/PR/link/head state; read active claim; read Kanban task/process; read owned branch/worktree; read dispatch/repair/merge/cleanup receipts; make one pure transition decision; execute one mutation per atom; verify each readback. Valid transitions: resume matching open PR; wait matching pending checks; repair matching failed PR; finalize merged PR/closed issue; release only proven orphan state; hold on contradictions. GitHub identity/state and remote SHA take precedence, but local deletion requires matching ownership evidence. Explicitly handle `ai:ready + ai:in-progress` with open matching PR as resumable PR lifecycle, not new intake and not stale deletion.
  - Parallelization: Can parallel N | Wave 2 integration | Blocks 9-12 | Blocked by 2-7.
  - References: `src/lokay/steps/claim.py::{reserve_claim_file,read_issue_claim_state,verify_issue_claim}`; `src/lokay/steps/cleanup_reconcile.py`; `src/lokay/steps/cleanup.py::{read_claim_identity,verify_claim_release_evidence,release_claim_file,verify_claim_released}`; `src/lokay/steps/poll.py::filter_issue_eligibility`; label atoms in `src/lokay/steps/issue_to_pr.py`; `fala-package.toml` `cleanup_reconcile` and `auto_worker`; `tests/test_cleanup_reconcile.py`; `tests/test_tick_cleanup.py`; `tests/test_worktree_safety.py`.
  - Acceptance criteria: matching `#10/#11`-shaped state resumes triage despite intake exclusion; empty claim plus matching open PR is safe; stale owned claim with no remote lifecycle is released only after complete evidence; conflicting repo/issue/branch/OID/task/receipt holds terminally; reconciliation is idempotent across restart and no mutation repeats after verified completion.
  - QA scenarios: happy—Fala restart fixture resumes open failed PR from GitHub plus stale/missing local records and reaches repair selection, evidence `.omo/evidence/task-8-lifecycle-reconcile.json`; failure—conflicting claim/worktree/receipt identities remain untouched and block work, evidence `.omo/evidence/task-8-lifecycle-reconcile-failclosed.json`.
  - Commit: Y | `feat(lifecycle): reconcile interrupted issue and PR work` | reconciliation atoms, graph placement, state tests.

- [ ] 9. Enforce the explicit autonomous production policy everywhere
  - What to do: define one canonical Python policy predicate/value for production candidate validation: `{automerge:true, require_human_approval:false, require_checks:true, require_test_evidence:true, executor_enabled:true}`. Use it in `tools/deployment_parity.py`; align embedded Python in health/status to the same exact tuple and add drift tests. Preserve exact key/type validation, top-level/table precedence, candidate embedded config equality, active config equality/hash, identity hashing, mode, argv, artifact/runtime provenance. Keep `src/lokay/config.py` defaults and starter config disabled/manual. Update production examples only where they explicitly represent live autonomous deployment.
  - Parallelization: Can parallel Y | Wave 3 | Blocks 12 | With 10, 11.
  - References: `tools/deployment_parity.py::validate_fala_candidate`; `scripts/lokay_health.sh::validate_fala_current`; `scripts/lokay_status.sh` candidate validator; `config.example.toml`; `config.example.yaml`; `examples/config.example.yaml`; config generation in the command module containing `render_launchd`/`starter_config`; `tests/integration_deployment_candidate.py`; `tests/integration_health_status_scripts.py`; `tests/test_deployment_parity.py` if present.
  - Acceptance criteria: autonomous live candidate passes all three validators; every mixed/disabled/weakened tuple fails; config/candidate/active mismatch fails; dry-run starter/library defaults remain unchanged and valid outside promotion; policy tests detect divergence among parity, health, and status.
  - QA scenarios: happy—run focused candidate and health/status integration tests with exact autonomous tuple, evidence `.omo/evidence/task-9-autonomous-policy.txt`; failure—tamper each boolean and active config without recomputed identity and prove rejection, evidence `.omo/evidence/task-9-autonomous-policy-failclosed.txt`.
  - Commit: Y | `feat(deploy): require guarded autonomous policy` | parity, scripts, production examples, focused tests.

- [ ] 10. Align operator documentation and health semantics with outcome truth
  - What to do: document safe starter versus autonomous production profile, independent triage/repair behavior, current-head CI wait semantics, natural-cycle DoD, and exact failure disposition. Health/status must distinguish scheduler mechanism health, active work/wait/failure, and last verified end-to-end outcome; a noop may be healthy orchestration but cannot report outcome success. Do not claim Temida provenance without receipts.
  - Parallelization: Can parallel Y | Wave 3 | Blocks 12 | With 9, 11.
  - References: `README.md`; `docs/auto-worker.md`; `docs/github-kanban-mapping.md`; `scripts/lokay_health.sh`; `scripts/lokay_status.sh`; `tests/integration_health_status_scripts.py`.
  - Acceptance criteria: docs and script outputs state that only merged-main outcome is DoD; health can report healthy-idle without claiming worked; active pending repair is non-success but not falsely failed; terminal repair/merge failure is red with exact run/process.
  - QA scenarios: happy—fixture with healthy idle plus a prior verified success emits distinct mechanism/outcome fields, evidence `.omo/evidence/task-10-health-semantics.txt`; failure—fixture with open failed PR/no terminal receipt cannot emit success, evidence `.omo/evidence/task-10-health-semantics-failclosed.txt`.
  - Commit: Y | `docs(operations): define autonomous outcome contract` | operator docs, health/status semantics/tests.

- [ ] 11. Run complete hermetic graph, restart, and policy verification
  - What to do: run focused atom tests, decision matrix, path composition, multi-repo lane tests, repair chain, lifecycle reconciliation, receipt durability, worktree safety, Fala facade/router tests, candidate validation, health/status integration, syntax/compile/whitespace checks, then the complete default discovered unit suite. Add a restart scenario using a controlled SQLite DB that stops after verified push and resumes at pending checks without duplicate OMP/push. Keep long deployment tests explicit.
  - Parallelization: Can parallel Y | Wave 3 | Blocks 12 | With 9, 10 after code integration.
  - References: `tests/test_atomic_effectors.py`; `tests/test_decide_matrix.py`; `tests/test_path_composition.py`; `tests/test_fala_intake_flow.py`; `tests/test_fala_triage_router.py`; `tests/test_cleanup_reconcile.py`; `tests/test_receipt_durability.py`; `tests/test_worktree_safety.py`; `tests/integration_deployment_candidate.py`; `tests/integration_health_status_scripts.py`; `pyproject.toml`.
  - Acceptance criteria: all named focused and full suites pass; restart test records one OMP and one push; exact 0.7.15 runtime is used; no unregistered handler, missing path ID, waiting process, hidden test skip, or dirty tracked candidate input remains.
  - QA scenarios: happy—save exact commands, durations, counts, and exit codes in `.omo/evidence/task-11-hermetic-verification.txt`; failure—retain injected fail-closed scenario outputs showing no downstream mutation in `.omo/evidence/task-11-hermetic-failclosed.txt`.
  - Commit: Y only if verification reveals required product/test correction | `fix(orchestration): close verification gaps` | Narrow affected files.

- [ ] 12. Build and validate a clean immutable autonomous candidate
  - What to do: ensure Lokay and pinned Fala tracked trees are clean; update the production config atomically to exact autonomous policy without exposing secrets; render candidate under the authoritative hash-named candidates directory (use `render-launchd` candidate-name error to discover identity); build candidate-local runtime/native cache; validate manifest, config hash, repos, policy, Fala source hash, artifacts, plist, argv, environment containment, and deployment parity. Do not promote yet if any gate is red.
  - Parallelization: Can parallel N | Wave 4 | Blocks 13 | Blocked by 8-11.
  - References: command module implementing `render_launchd`; `tools/deployment_parity.py`; `templates/launchd/lokay-fala-tick-all.plist.template`; `/Users/mini-m4-main/.hermes/lokay/config.toml`; `/Users/mini-m4-main/.hermes/lokay/deployment/candidates`; pinned Fala checkout `/Users/mini-m4-main/Developer/Fala`.
  - Acceptance criteria: candidate path equals manifest candidate ID; policy is exact autonomous tuple; embedded and active config hashes match; runtime reports Fala `0.7.15` and peeled commit; native hash matches `_source_hash`; plist/argv remain the canonical single scheduler; parity and plutil pass; source trees remain clean.
  - QA scenarios: happy—store candidate ID, manifest/runtime hashes, parity/plutil outputs in `.omo/evidence/task-12-candidate.json`; failure—tampered policy/artifact/runtime hash candidate is rejected before promotion, evidence `.omo/evidence/task-12-candidate-failclosed.txt`.
  - Commit: N | Deployment artifact only; config/example source changes were committed earlier.

- [ ] 13. Atomically promote and observe the natural scheduler
  - What to do: record rollback candidate; promote the exact validated candidate using existing lock/copy/fsync/readback/current-symlink/launchctl transaction; verify exact installed plist bytes, label/domain/argv, no legacy mutators, current symlink, candidate-local runtime, health/status, and launchd last exit. Wait for at least two naturally elapsed 600-second intervals without manual kickstart. If activation or mechanism health fails, collect evidence, rollback with the existing transaction, fix source, rebuild, and retry.
  - Parallelization: Can parallel N | Wave 4 | Blocks 14 | Blocked by 12.
  - References: command module implementing `deploy_fala` and `_promote_version_runtime`; `scripts/lokay_health.sh`; `scripts/lokay_status.sh`; active plist `~/Library/LaunchAgents/com.mikolaj92.lokay.fala-tick-all.plist`; deployment logs/state.
  - Acceptance criteria: `current` resolves to candidate from Todo 12; launchd exact-state verification passes; two distinct natural run IDs use that candidate/config and finish with zero failed/waiting processes; rollback identity is recorded; idle results are labeled idle, not outcome success.
  - QA scenarios: happy—capture promotion transaction and two natural runs in `.omo/evidence/task-13-promotion.json`; failure—exercise candidate fixture rollback test and record restored identity/no mixed files in `.omo/evidence/task-13-promotion-rollback.txt`.
  - Commit: N | Operational deployment only.

- [ ] 14. Reconcile the live `lokay#10` / PR `#11` lifecycle automatically
  - What to do: without manual mutation, allow the next natural `auto_worker` run to read authoritative GitHub state and local stale/missing claim/task/worktree/receipt evidence. Verify reconciliation selects the existing PR repair lifecycle even though issue `#10` has `ai:in-progress`. Confirm selected PR is `#11`, branch is unchanged, original head is `ccc470...`, base is `main`, linkage closes `#10`, and no second PR is created. Any conflict must stop; fix reconciliation code, rebuild/promote, and repeat.
  - Parallelization: Can parallel N | Wave 5 | Blocks 15 | Blocked by 13.
  - References: live GitHub `mikolaj92/lokay#10` and PR `#11`; reconciliation atoms from Todo 8; Fala DB/logs; active claim/task/worktree/receipt roots from config.
  - Acceptance criteria: natural run contains reconciliation and triage selection outputs for exact repo/issue/PR/head; no manual label/claim/task edit; open PR count for the branch remains one and number remains 11; no OMP call occurs until exact failed-head and executor gates pass.
  - QA scenarios: happy—store authoritative before/read/decision outputs plus Fala process IDs in `.omo/evidence/task-14-live-reconcile.json`; failure—if any identity differs, store terminal conflict/no mutation evidence and return to implementation rather than proceeding, `.omo/evidence/task-14-live-reconcile-failclosed.json`.
  - Commit: Y only if live evidence exposes a code defect | `fix(lifecycle): reconcile live PR ownership` | Narrow correction plus regression test; then repeat Todos 11-14.

- [ ] 15. Repair PR `#11` through natural cycles until fresh checks pass
  - What to do: let the selected repair lane invoke OMP in its owned worktree, verify a new commit and confined diff, push the existing branch without force, read back exact remote OID, publish repair receipt, and wait for fresh checks tied to that OID. Diagnose every failure from exact OMP/process/check evidence; fix orchestration defects in source and redeploy, but never manually implement the PR fix. If fresh checks fail, allow the state machine’s next verified repair attempt; if pending, wait without duplicate OMP. Continue until required checks and test evidence are green for current PR head.
  - Parallelization: Can parallel N | Wave 5 | Blocks 16 | Blocked by 14.
  - References: live PR `#11`; repair graph/receipt from Todos 6-7; `src/lokay/steps/triage.py::{evaluate_checks,evaluate_test_evidence}`; GitHub Actions check runs; Fala runtime logs/DB and candidate manifest.
  - Acceptance criteria: repaired head differs from `ccc470...`; PR number/head branch unchanged; repair receipt binds before/after OIDs; remote OID equals verified local OID; current-head checks and test evidence are fresh and passing; pending ticks invoke neither OMP nor push; no manual code/push/check rerun occurred.
  - QA scenarios: happy—store OMP pre/post, diff paths, local/remote OIDs, check run IDs/conclusions, and Fala runs in `.omo/evidence/task-15-live-repair.json`; failure—each failed attempt records exact failure class, no unsafe downstream mutation, and corresponding regression/fix cycle in `.omo/evidence/task-15-live-repair-failures.json`.
  - Commit: Y only for Lokay orchestration defects discovered during live proof | `fix(repair): handle <observed failure>` | Product fix plus regression; OMP’s PR commit is not authored manually by supervisor.

- [ ] 16. Prove exact merge-to-main, issue closure, receipts, and cleanup
  - What to do: after green current-head gates, allow natural triage to verify merge preconditions/head, merge PR `#11`, read back GitHub merge provenance, verify linked issue identity, close/read back issue `#10`, publish/read back immutable merge receipt, then execute evidence-gated cleanup. Fetch `origin/main` read-only after merge and prove the receipt merge SHA is reachable and the PR’s code/acceptance file exists on main. Verify no open PR remains, issue is closed, owned claim/worktree/local branch are absent or retained exactly per policy, remote branch retention is recorded, and cleanup receipt is terminal. If any assertion fails, outcome is fail: repair the responsible code/state machine and repeat.
  - Parallelization: Can parallel N | Wave 5 | Blocks F1-F4 | Blocked by 15.
  - References: `src/lokay/steps/triage.py::{read_merge_preconditions,merge_pr,read_merge_postcondition,verify_merge_provenance,verify_linked_merge_provenance,read_linked_issue_state,close_linked_issue,verify_linked_issue_closed,build_merge_receipt,publish_merge_receipt,verify_merge_receipt}`; `src/lokay/steps/cleanup.py`; live GitHub issue/PR/main; merge/cleanup receipt roots.
  - Acceptance criteria: GitHub reports PR `#11` MERGED into `main`; merge receipt identifies PR 11, issue 10, repaired head, and merge SHA; `git merge-base --is-ancestor <merge_sha> origin/main` succeeds; issue `#10` is CLOSED; the intended PR change is present on `origin/main`; no open PR for branch; cleanup receipt terminal with all applicable postconditions true; natural Fala run IDs connect repair→merge→cleanup. This is the sole success condition.
  - QA scenarios: happy—write complete immutable outcome proof to `.omo/evidence/task-16-dod.json`; failure—any missing/mismatched identity, unreachable merge, open issue, missing code, receipt, or cleanup postcondition writes `.omo/evidence/task-16-dod-failure.json` and returns to the responsible todo instead of declaring completion.
  - Commit: Y only for defects found before DoD | `fix(<domain>): complete autonomous lifecycle` | Narrow code/test correction; repeat verification/deployment/live chain.

## Final verification wave (after ALL todos)
> Run in parallel only after Todo 16’s direct outcome proof exists. ALL reviewers must approve. Surface results and wait for the user’s explicit okay before calling the goal complete.

- [ ] F1. Plan compliance audit
  - Verify every Must have/Must NOT have against current source, candidate manifest, active config/plist, Fala process evidence, GitHub state, receipts, and main ancestry. Reject self-report or missing evidence.
  - Evidence: `.omo/evidence/final-plan-compliance.json`.

- [ ] F2. Code quality and fail-closed review
  - Review changed graph/atoms for exact predecessors, atomic boundaries, retry classification, idempotency, allocations/copies, path/branch ownership, current-head binding, no force-push, no silent aliases/dead handlers, and all exported-symbol callsites. Run LSP diagnostics and focused regression suites.
  - Evidence: `.omo/evidence/final-code-quality.json`.

- [ ] F3. Real outcome QA
  - Independently query GitHub and `origin/main`, read the exact merge/cleanup/repair receipts, inspect Fala runs/processes, and verify issue 10 → PR 11 → repaired SHA → merge SHA → main code → closed issue → cleanup. Do not trust prior summarized evidence without source readback.
  - Evidence: `.omo/evidence/final-real-outcome.json`.

- [ ] F4. Scope fidelity and deployment audit
  - Confirm no second scheduler/PR, no manual GitHub/Git/OMP mutation, no weakened policy, no foreign cleanup, no dirty candidate source, no unrequested Temida admission, preserved Fala pin/stash/artifacts, and a recorded usable rollback candidate.
  - Evidence: `.omo/evidence/final-scope-fidelity.json`.

## Commit strategy
- Commit by behavioral slice after its focused tests pass: repository lanes; repair ownership; executor/retry state; graph repair chain; lifecycle reconciliation; autonomous deployment policy; operations semantics.
- Use current branch only. Author is `mikolaj92`; no AI/co-author/generated trailers.
- Never include runtime DBs, logs, secrets, live receipts, deployment versions, `.omo/evidence`, `.tmp`, or Fala generated/cache artifacts in product commits.
- No force push. Live PR `#11` is changed only by Lokay’s OMP executor through the verified repair path.
- A live-discovered source correction requires focused regression, commit, clean candidate rebuild, promotion, and replay from the affected verification todo.

## Success criteria
- The active immutable candidate uses Fala `0.7.15` at peeled commit `b5f9a6d500a442a1c79060a862fe4b9da87bc98f`, exact autonomous guarded policy, one canonical launchd scheduler, and clean verified source/runtime identity.
- Idle intake cannot suppress independent PR triage; all configured repositories retain exact context.
- Existing PR repair is remote-head anchored, provenance-owned, executor-gated, confined, no-force, readback verified, receipt-backed, and idempotent while CI is pending.
- Interrupted lifecycle state reconciles automatically and conflicts fail closed.
- Required checks and test evidence remain mandatory and are bound to the current repaired head.
- PR `mikolaj92/lokay#11`—not a replacement—is merged into `main`; its verified merge SHA is reachable from current `origin/main`; its intended code exists on main; issue `#10` is closed; repair/merge/cleanup receipts and Fala process evidence correlate exactly; cleanup is terminal.
- If any preceding statement is unproven or false, the result is failure and work continues. No narrower completion state is accepted.
