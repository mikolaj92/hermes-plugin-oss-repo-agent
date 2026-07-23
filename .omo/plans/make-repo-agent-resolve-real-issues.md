# Make Repo-Agent Resolve Real Issues

## TL;DR
> Summary: Finish and publish the existing Fala runtime work on `main`, close the remaining fail-open and multi-repository gaps, deploy one immutable rollback-safe live candidate, then prove through natural launchd ticks that the agent can complete both a disposable canary and the pre-existing `mikolaj92/Temida#3590` lifecycle without manual implementation or merge.
> Deliverables: reviewed and pushed `main` baseline; strict triage/repository/receipt/cleanup contracts; immutable candidate and rollback record; isolated Gate 2 canary evidence; independent Gate 3 Temida evidence; final provenance/outcome record.
> Effort: XL
> Risk: High - this intentionally performs guarded GitHub, Kanban, deployment, launchd, worktree, and merge mutations after fail-closed prerequisites pass.

## Scope
### Must have
- Work only on `main`, preserve every intended existing dirty-tree change, integrate it into reviewed commits authored only by `mikolaj92`, and push without force.
- Treat the current Fala migration as a prerequisite: the candidate source must be clean, runnable, internally version-consistent, and already present on `origin/main` before live activation.
- Enforce `require_human_approval` end to end and merge only authoritative `CLEAN`/`MERGEABLE` PRs; `UNKNOWN`, missing, malformed, stale, or contradictory review/mergeability data must hold fail closed.
- Keep checks, test evidence, owner author, `ai/fix/*` head, `main` base, open/non-draft state, and `ai:blocked` gates mandatory.
- Support the selected repository through intake, claim, dispatch, triage, repair, receipt, and cleanup; never fall back to `cfg.repos[0]` after a Temida issue/task/PR has been selected.
- Make dispatch, merge, and cleanup receipts immutable per run/entity and correlate them with the same Fala run/process IDs, candidate/config hashes, GitHub identities, and launch log event.
- Build and validate an immutable live candidate with exact source/Fala/config/package/plist provenance and an executable rollback to the previously active candidate.
- Use one registered Fala launchd job in one known domain with `StartInterval=600`, live arguments, `RunAtLoad=false`, and no legacy scheduler/mutator jobs.
- Run Gate 2 and Gate 3 only through naturally elapsed launchd intervals. A manual `kickstart`, direct tick command, manual implementation, manual push, or manual merge invalidates that gate.
- Gate 2 must use a uniquely identified disposable canary and finish intake through cleanup. Gate 3 must independently process pre-existing real issue `mikolaj92/Temida#3590` and prove every issue acceptance criterion on merged `main`.
- On ambiguity or failed activation/runtime/receipt/cleanup gates, fail closed, preserve evidence, restore the previous deployment/topology where safe, and report `INCOMPLETE - NOT PROMOTED` rather than manufacturing success.

### Must NOT have
- No force push, branch reset, broad `git clean`, destructive recovery, deletion of foreign worktrees/branches, or overwrite of unrelated user changes.
- No weakening or bypass of checks, test evidence, ownership, base/head, labels, mergeability, issue-direction, provenance, or cleanup guards.
- No `UNKNOWN`/empty mergeability merge and no implicit approval inferred from authorship, assignment, comments, or absent review data.
- No fixed shared receipt filename, overwriting conflicting receipts, receipt written for a dry run, or success-only cleanup journal that hides failed/partial attempts.
- No canary result counted as Gate 3, no Gate 2 identifier reused in Gate 3 evidence, and no unrelated queued Kanban task or repository processed as part of the proof.
- No manual code change in Temida by the supervising agent. If the worker cannot solve the issue, Gate 3 fails.
- No production activation from a dirty source checkout, unpushed commit, mismatched Fala version/commit, missing package asset, or candidate/config/plist parity mismatch.
- No secrets, credentials, tokens, machine-private payloads, or mutable runtime databases/logs committed to `main`.

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: tests-after using the repository's focused Python unittest/pytest conventions, shell syntax checks, real Fala package-host scenarios, and live state reconciliation only after the implementation under test exists.
- QA policy: every todo has agent-executed happy and failure scenarios; mocked/temp-fixture checks precede all live mutations.
- Evidence: `.omo/evidence/task-<N>-<slug>.<ext>` with commands, exit status, exact assertions, timestamps, and immutable IDs. Live evidence may redact secrets but may not omit correlation fields.
- A passing wrapper summary, grep hit, subagent report, or GitHub UI state alone is not proof. Validators must inspect the underlying receipt, Fala journal/process rows, launchd state, GitHub/Kanban readback, commit SHA, and command result.

## Execution strategy
### Parallel execution waves
> Target 5-8 todos per wave where dependencies permit. Live gates are deliberately serialized because each consumes and mutates the state established by the preceding gate.
- Wave 1 (no live mutations): Todos 1-5 in parallel after Todo 1 records the common baseline; coordinate ownership so no two workers edit the same file.
- Wave 2 (after Wave 1): Todos 6-8 in parallel on the integrated clean baseline.
- Wave 3 (after Wave 2): Todos 9-10 sequentially establish rollback state and activate the candidate.
- Wave 4 (after activation): Todos 11-12 sequentially execute and reconcile Gate 2.
- Wave 5 (after Gate 2): Todos 13-15 sequentially reserve, execute, and reconcile Gate 3.
- Final wave: F1-F4 in parallel after all implementation and live evidence todos.
- Critical path: 1 → 2/3/4/5 → 6/7/8 → 9 → 10 → 11 → 12 → 13 → 14 → 15 → F1-F4.

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
|---|---|---|---|
| 1 | None | 2-10 | None during baseline freeze |
| 2 | 1 | 6, 9 | 3, 4, 5 |
| 3 | 1 | 6, 9, 11 | 2, 4, 5 |
| 4 | 1 | 6, 8, 13 | 2, 3, 5 |
| 5 | 1 | 6, 8, 13 | 2, 3, 4 |
| 6 | 2-5 | 9 | 7, 8 |
| 7 | 1-5 | 9 | 6, 8 |
| 8 | 4-5 | 9, 13 | 6, 7 |
| 9 | 2-8 | 10 | None |
| 10 | 9 | 11 | None |
| 11 | 10 | 12 | None |
| 12 | 11 | 13 | None |
| 13 | 12 | 14 | None |
| 14 | 13 | 15 | None |
| 15 | 14 | F1-F4 | None |

## Todos
> Implementation + tests remain one todo. Tests are added/run after the corresponding behavior is implemented.

- [ ] 1. Freeze, reconcile, and publish the current baseline
  - What to do: inventory all modified/untracked paths, classify intended product/tests/docs/plans versus generated/runtime material, preserve byte hashes of foreign or excluded paths, and reconcile the existing Fala 0.7 migration against `.omo/plans/fala-07-process-migration.md`. Resolve internal version/provenance disagreement such as `pyproject.toml` versus prior `.omo/evidence` claims using actual dependency/package metadata, not by editing evidence to fit. Run the migration plan's focused verification, inspect the complete diff for accidental live data/secrets, commit intended changes atomically on `main`, and push `main` normally. Record local HEAD, `origin/main`, Fala source commit/version, and a clean candidate-source status. Must NOT reset, clean, overwrite, or silently discard current changes.
  - Parallelization: Can parallel N | Wave 1 prerequisite | Blocks 2-10.
  - References: `.omo/plans/fala-07-process-migration.md:141-193`; `.omo/plans/hermes-repo-agent-remediation.md:41-53`; `pyproject.toml`; `fala-package.toml`; `tools/deployment_parity.py`; `.github/workflows/ci.yml`.
  - Acceptance criteria: intended code is committed on `main`; `git rev-parse HEAD` equals `git rev-parse origin/main`; the candidate source inputs are clean; focused Python, shell, package-host, candidate, and parity checks named by the Fala migration plan pass; excluded path hashes match the frozen inventory; no credential pattern or runtime DB/log is included in the pushed commit.
  - QA scenarios: happy—run the exact migration verification suite and record commit/remote equality in `.omo/evidence/task-1-baseline.txt`; failure—inject/use a temp fixture with mismatched Fala manifest metadata and prove validation rejects it in `.omo/evidence/task-1-baseline-failure.txt`.
  - Commit: Yes | `refactor(fala): complete process runtime migration` plus narrowly scoped follow-up commits if required | Existing intended product/test/config/docs files only.

- [ ] 2. Make triage approval and mergeability fail closed
  - What to do: pass `require_human_approval` and authoritative `reviewDecision` through config → tick inputs → flow/package conduction → `decide_triage_action`. When approval is required, merge only the explicitly accepted review state defined by the existing GitHub contract; missing, unknown, changes-requested, or malformed review state must block. When approval is disabled, do not require a review, but retain every other gate. Merge only authoritative `CLEAN`/`MERGEABLE`; hold `UNKNOWN`, empty, and unrecognized mergeability rather than treating them as ready. Preserve repair behavior for known conflicts/check failures and comment/hold behavior for policy failures.
  - Parallelization: Can parallel Y | Wave 1 | Blocks 6, 9 | With 3, 4, 5.
  - References: `src/repo_agent/steps/triage.py:368-447`; `src/repo_agent/tick_all.py:86-100`; `src/repo_agent/flows/triage.py:150-235`; `src/repo_agent/config.py:284-292`; `docs/github-kanban-mapping.md:49-57`; existing triage decision tests under `tests/`.
  - Acceptance criteria: a table-driven test covers approval required/disabled × approved/missing/changes-requested × clean/unknown/conflicting, and only disabled+clean or required+approved+clean reaches `action=merge`; current checks/evidence/owner/head/base/draft/blocked-label cases remain passing.
  - QA scenarios: happy—focused triage tests with an approved clean PR and approval-disabled clean PR, evidence `.omo/evidence/task-2-triage.txt`; failure—UNKNOWN mergeability and missing required approval both produce a non-mutation decision, evidence `.omo/evidence/task-2-triage-fail-closed.txt`.
  - Commit: Yes | `fix(triage): enforce approval and clean merge state` | triage config/input/conduction code and focused tests.

- [ ] 3. Correlate immutable lifecycle receipts with Fala runs
  - What to do: replace shared `auto-worker-dispatch.json` and `auto-worker-merge.json` paths with deterministic immutable per-run/per-entity paths. Carry `run_id`, `path_id`, relevant Fala process IDs, candidate ID, config hash, repo/issue/task/branch/PR, base/head/merge SHAs, and timestamp through dispatch and merge receipts. Add a terminal cleanup receipt effector after all cleanup guards/actions, recording every live cleanup attempt—success, safe no-op, retryable/terminal failure, cancellation, and partial mutation—with per-step status/mutated/failure fields. Dry runs remain planned and write no file. Use atomic no-clobber write, parent/file fsync, identical-payload idempotence, conflicting-payload rejection, and authoritative readback. Migrate or quarantine old fixed receipt files without treating them as current evidence.
  - Parallelization: Can parallel Y | Wave 1 | Blocks 6, 9, 11 | With 2, 4, 5.
  - References: `src/repo_agent/tick_all.py:56-109`; `src/repo_agent/steps/issue_to_pr.py:758-837`; `src/repo_agent/steps/triage.py:744-831`; `src/repo_agent/flows/runtime.py:128-217`; `src/repo_agent/flows/cleanup.py:113-136`; `src/repo_agent/catalog.py`; cleanup/receipt conductors in `fala-package.toml`; `tests/test_receipt_durability.py:42-126`.
  - Acceptance criteria: two sequential lifecycle mutations create distinct immutable receipts; each receipt's run/process/candidate/config/entity IDs match journal and log records; conflicting reuse fails; dead/cancelled upstream cannot emit a false success receipt; every live cleanup attempt emits one truthful terminal receipt; dry run emits none.
  - QA scenarios: happy—two temp Fala runs produce non-clobbering dispatch/merge/cleanup bundles, evidence `.omo/evidence/task-3-receipts.json`; failure—conflict, fsync/readback failure, cancelled dependency, and partial cleanup retain accurate mutation/error state, evidence `.omo/evidence/task-3-receipts-failure.json`.
  - Commit: Yes | `fix(receipts): bind lifecycle evidence to immutable runs` | tick/flow/step/catalog/package graph and receipt tests.

- [ ] 4. Preserve selected repository context end to end
  - What to do: remove first-repository fallbacks after selection. Propagate the selected repo entry's repo, board, clone path, priority, and policy from intake/claim/task identity through dispatch, triage/repair, and cleanup. Ensure auto-worker can inspect multiple configured repos while performing each lifecycle step only against the selected entity. Reject mismatched task/repo/board/clone context and ambiguous duplicate claims instead of using `cfg.repos[0]`.
  - Parallelization: Can parallel Y | Wave 1 | Blocks 6, 8, 13 | With 2, 3, 5.
  - References: `src/repo_agent/tick_all.py:60-109`; `src/repo_agent/flows/issue_to_pr.py:69-140`; `src/repo_agent/flows/triage.py:157-180`; `src/repo_agent/flows/cleanup.py:49-78`; `src/repo_agent/steps/claim.py:165-200`; `src/repo_agent/steps/issue_to_pr.py:123-170`; config repo parsing at `src/repo_agent/config.py:244-273`.
  - Acceptance criteria: with repo A first and Temida second, a selected Temida issue uses only Temida's board/clone/repo in every step and receipt; a mismatched Kanban task or claim fails before filesystem/GitHub mutation; repo A state remains byte-for-byte and API-state unchanged.
  - QA scenarios: happy—multi-repo temp fixture selects the second repo and completes a mocked lifecycle, evidence `.omo/evidence/task-4-repo-context.json`; failure—cross-wired board/clone/task is rejected, evidence `.omo/evidence/task-4-repo-context-failure.json`.
  - Commit: Yes | `fix(runtime): carry selected repository context` | tick/flows/steps and multi-repo tests.

- [ ] 5. Refuse stale branches, dirty clones, and foreign worktrees
  - What to do: before creating/reusing an agent worktree, record clone status, worktree porcelain, local/remote branch refs, exact `origin/main`, and intended branch identity. Create a new issue branch only from the freshly fetched exact origin base. Reuse only when receipt/task/issue provenance and expected head match; reject a collision, stale commits, dirty clone/worktree, unknown lock, or foreign ownership. Cleanup may remove only the exact proven worktree/branch after closed-issue/no-open-PR guards and must never reset/delete foreign state.
  - Parallelization: Can parallel Y | Wave 1 | Blocks 6, 8, 13 | With 2, 3, 4.
  - References: `src/repo_agent/steps/issue_to_pr.py:359-468`; `src/repo_agent/adapters_git.py:19-54`; `src/repo_agent/steps/cleanup.py:132-198`; `docs/agents/temida-github-workflow.md:1-22,56-60`.
  - Acceptance criteria: clean fresh branch begins at exact `origin/main`; branch collision, dirty clone, stale head, and foreign worktree all fail before mutation; before/after inventories prove unrelated refs/worktrees unchanged; exact owned cleanup still succeeds.
  - QA scenarios: happy—temp clone creates and cleans an owned worktree, evidence `.omo/evidence/task-5-worktree.txt`; failure—seed stale branch and foreign dirty worktree and prove both are preserved/rejected, evidence `.omo/evidence/task-5-worktree-failure.txt`.
  - Commit: Yes | `fix(worktrees): enforce branch provenance isolation` | git adapter/worktree/cleanup code and temp-repo tests.

- [ ] 6. Verify the complete runtime contract before live work
  - What to do: integrate Todos 2-5, update package graph/assets and all affected tests, then run focused unit/integration tests, real Fala temporary-SQLite scenarios, shell syntax checks, and a no-network dry-run auto-worker. Verify semantic failures become durable failed processes, dependents cancel, idle/noop is not reported as useful work, and the new receipt/repository/triage contracts agree between source, packaged candidate inputs, and runtime output. Commit and push this code to `main` before candidate construction.
  - Parallelization: Can parallel Y | Wave 2 | Blocks 9 | With 7, 8.
  - References: `fala-package.toml`; `src/repo_agent/flows/runtime.py:128-217`; `.omo/plans/fala-07-process-migration.md:159-176`; `tests/test_fala_runtime_contract.py`; `tests/test_fala_triage_router.py`; `scripts/repo_agent_smoke.sh`.
  - Acceptance criteria: focused suites and real-host scenarios pass from the exact pushed commit; a semantic failure has a durable error/process ID and suppresses dependents; two-repo/receipt/approval/worktree failure matrices pass; local and `origin/main` SHAs agree.
  - QA scenarios: happy—real two-step, empty, triage, multi-repo, and receipt runs, evidence `.omo/evidence/task-6-runtime.json`; failure—middle semantic failure and cancelled downstream receipt, evidence `.omo/evidence/task-6-runtime-failure.json`.
  - Commit: Yes | `test(runtime): cover guarded multi-repo lifecycle` | focused tests/fixtures plus any package registration adjustments.

- [ ] 7. Bind candidate, config, and launchd provenance
  - What to do: extend candidate manifest/validation so it binds exact plugin commit/tree, Fala package version and source commit, Python dependency lock, package asset hashes, active config hash and normalized policy, repo/board/clone inventory, candidate/deployment roots, rendered plist hash/label/domain/arguments/schedule, and expected runtime/database/log paths. Validator must reject any source-versus-active disagreement, including the observed Fala version evidence mismatch. Render live mode only: argv contains exactly `--live` as the mode flag, contains no `--dry-run`, `StartInterval=600`, and `RunAtLoad=false`; unresolved or contradictory mode is invalid.
  - Parallelization: Can parallel Y | Wave 2 | Blocks 9 | With 6, 8.
  - References: `commands.py:102-140`; candidate/deploy implementation later in `commands.py`; `tools/deployment_parity.py:101-135`; `scripts/repo_agent_health.sh:60-89`; `templates/launchd/oss-repo-agent-fala-tick-all.plist.template:30-45`; `tests/test_deployment_candidate.py`.
  - Acceptance criteria: a clean candidate validates only when every source/Fala/config/plist/package identity matches; each single-field mismatch fails closed; rendered plist is live, 600-second, non-RunAtLoad, and references only immutable candidate paths.
  - QA scenarios: happy—build and validate a temp immutable candidate, evidence `.omo/evidence/task-7-candidate.json`; failure—tamper each critical manifest/plist/config field and prove rejection, evidence `.omo/evidence/task-7-candidate-failure.json`.
  - Commit: Yes | `fix(deploy): bind runtime policy provenance` | commands/parity/health/template/tests.

- [ ] 8. Prepare Temida clone, board, and safe repository configuration
  - What to do: create or verify a controlled clean local clone for `mikolaj92/Temida`, a dedicated Hermes Kanban board, absolute live paths, trusted owner/head/base policy, and priority that does not permit unrelated work to jump the controlled gates. Add Temida to source deployment configuration while retaining the repo-agent canary repository. Validate GitHub CLI access and required label/assignee visibility read-only. Do not label `Temida#3590` yet and do not process existing unrelated board tasks.
  - Parallelization: Can parallel Y | Wave 2 | Blocks 9, 13 | With 6, 7.
  - References: active/source config shape in `config.example.toml:49-53` and `config.example.yaml:31-36`; `src/repo_agent/config.py:244-273`; `src/repo_agent/steps/poll.py:53-90`; `src/repo_agent/steps/claim.py:165-200`.
  - Acceptance criteria: config validation reports both repositories with distinct correct board/clone identities; Temida clone is clean at authoritative `origin/main`; board query is successful and unrelated tasks are inventoried/excluded; issue #3590 remains unmutated.
  - QA scenarios: happy—read-only config/clone/board/GitHub preflight, evidence `.omo/evidence/task-8-temida-preflight.json`; failure—temp mismatched board/clone mapping fails validation, evidence `.omo/evidence/task-8-temida-preflight-failure.json`.
  - Commit: Yes | `chore(config): add guarded Temida repository` | source config/example/schema/tests only; machine-specific secrets remain outside git.

- [ ] 9. Build candidate and freeze an executable rollback snapshot
  - What to do: from exact clean `origin/main`, build the immutable candidate and validate it before promotion. Snapshot prior current target/manifest, active config/plist hashes, launchd domain/job state, database/log/receipt paths, legacy job inventory, and foreign-path hashes. Exercise rollback in a non-live/temp deployment fixture, including failure after pointer swap and launchd load, and produce a rollback receipt schema. Stop before live promotion if restoration cannot be proven.
  - Parallelization: Can parallel N | Wave 3 | Blocks 10 | Blocked by 2-8.
  - References: deployment code in `commands.py`; `tests/test_deployment_candidate.py:532-562,695-726`; `tools/deployment_parity.py`; `launchd/`; `.omo/plans/hermes-repo-agent-completion.md` deployment/rollback requirements.
  - Acceptance criteria: candidate validator and parity pass; source commit equals pushed `origin/main`; rollback fixture restores exact previous pointer/files/job and records truthful receipt; frozen foreign hashes match; candidate ID/config hash/plist hash are recorded for later receipt correlation.
  - QA scenarios: happy—candidate build plus temp promote/restore, evidence `.omo/evidence/task-9-rollback.json`; failure—forced post-swap and post-load failures restore prior state or explicitly quarantine without false success, evidence `.omo/evidence/task-9-rollback-failure.json`.
  - Commit: No | Runtime candidate and evidence only; any product defect returns to Todos 6-7 and a new pushed commit/candidate.

- [ ] 10. Promote and observe the live natural scheduler
  - What to do: under the deployment lock, atomically promote the validated candidate, install only canonical regular-file artifacts/plist, load exactly one Fala job in the intended launchd domain, and remove/disable legacy mutator jobs only where their ownership is proven. Read back current pointer, manifest, config, plist, `launchctl print`, health/parity, logs, and process journal. Prove the job has live args, `StartInterval=600`, `RunAtLoad=false`, no manual trigger, and that at least one naturally elapsed idle tick completes before Gate 2. On any ambiguity, execute the frozen rollback and leave the gate incomplete.
  - Parallelization: Can parallel N | Wave 3 | Blocks 11 | Blocked by 9.
  - References: `commands.py` deploy/render paths; `scripts/repo_agent_health.sh`; `scripts/repo_agent_status.sh`; `scripts/repo_agent_smoke.sh`; launchd template; `docs/auto-worker.md:45-66`.
  - Acceptance criteria: active deployment exactly equals candidate manifest; one known-domain Fala job is loaded; no legacy mutator is active; launchd argv contains exactly `--live` and no `--dry-run`; the correlated Fala DB run records `mode=live`; a natural timestamp/log/Fala run correlation exists without kickstart; health/parity report no failed/waiting/unresolved process. Rollback outcome is recorded if any check fails.
  - QA scenarios: happy—observe one full natural interval and reconcile launchd/log/journal, evidence `.omo/evidence/task-10-live-activation.json`; failure—if parity/topology/runtime check fails, restore and prove previous state, evidence `.omo/evidence/task-10-live-rollback.json`.
  - Commit: No | Live deployment mutation and local evidence only.

- [ ] 11. Execute an isolated Gate 2 canary by natural ticks
  - What to do: create one uniquely titled disposable issue in the repo-agent repository with explicit one-file acceptance criteria, `ai:ready`, correct assignee, and no pre-existing branch/PR/task. Freeze issue URL/body hash and dedicate a Gate 2 receipt/evidence namespace. Do not invoke a tick manually. Observe natural launchd cycles through discovery, direction, claim, Kanban intake, OMP implementation, branch verification/push, PR creation/labels, checks/test evidence, guarded automatic merge, issue closure, and cleanup. Require one subsequent natural reconciliation tick. The supervising agent may only observe and diagnose; it may not implement or merge the canary.
  - Parallelization: Can parallel N | Wave 4 | Blocks 12 | Blocked by 10.
  - References: `docs/auto-worker.md:45-66`; auto-worker conductors in `fala-package.toml`; `src/repo_agent/tick_all.py`; intake/dispatch/triage/cleanup flows; prior incomplete record `.omo/canary-outcome-2026-07-19.md`.
  - Acceptance criteria: distinct issue/PR/task/branch/run/process/receipt/merge IDs reconcile; change meets canary criteria on merged main; checks/evidence gates were genuinely green; issue closes; terminal cleanup receipt proves worktree/branch/claim/lease outcome; next natural tick reconciles idle/no residual work; no manual trigger appears in evidence.
  - QA scenarios: happy—capture the full lifecycle and following reconciliation cycle in `.omo/evidence/task-11-gate2.json`; failure—any timeout/ambiguity records the exact stopped process/mutation state, prevents false completion, and triggers safe rollback policy where applicable in `.omo/evidence/task-11-gate2-failure.json`.
  - Commit: No for runtime evidence; the canary's agent-generated product commit/PR must merge naturally.

- [ ] 12. Reconcile and close Gate 2 before real work
  - What to do: independently read GitHub issue/PR/commit/checks, Kanban task, Fala journal/process rows, launch log, immutable receipts, local clone/worktree/branch/claim/lease, deployment manifest, and origin main. Produce `CLEANUP_CONFIRMED` or `NO_TARGET_RECONCILED` only when exact state proves it; a merge receipt alone is insufficient. Freeze Gate 2 IDs and assert they cannot appear in the Gate 3 namespace. Compare foreign path/ref/worktree hashes with the pre-gate baseline.
  - Parallelization: Can parallel N | Wave 4 | Blocks 13 | Blocked by 11.
  - References: receipt writers/readback in `src/repo_agent/steps/issue_to_pr.py` and `src/repo_agent/steps/triage.py`; cleanup guards in `src/repo_agent/steps/cleanup.py:132-349`; `.omo/canary-outcome-2026-07-19.md:21-30` as the prior failure mode.
  - Acceptance criteria: all authoritative sources agree; cleanup has a durable terminal receipt linked to the merge and Fala run; no stale `PR_OPEN` task/claim/lease or owned worktree remains; foreign state is unchanged; otherwise disposition is `INCOMPLETE - NOT PROMOTED` and Gate 3 does not start.
  - QA scenarios: happy—independent reconciliation yields exact terminal state, evidence `.omo/evidence/task-12-gate2-reconciliation.json`; failure—missing/mismatched receipt or ambiguous API read remains incomplete and blocks Gate 3, evidence `.omo/evidence/task-12-gate2-reconciliation-failure.json`.
  - Commit: No | Evidence only.

- [ ] 13. Reserve Temida issue 3590 with an immutable preflight
  - What to do: read authoritative `mikolaj92/Temida#3590` body, state, labels, assignee, creation time, comments, linked/open PRs, local/remote branch refs, board tasks, and claims. Record URL and body/acceptance hash proving it predates this exercise and is not a canary/smoke task. Translate every issue acceptance criterion into an exact post-merge command/assertion matrix. Abort on changed body/assignee, existing PR/branch collision, duplicate claim/task, incompatible label, dirty clone, or unavailable required checks. Only after the snapshot passes, add `ai:ready` and verify authoritative readback; do not manually create a task/branch/PR.
  - Parallelization: Can parallel N | Wave 5 | Blocks 14 | Blocked by 8, 12.
  - References: `issue://mikolaj92/Temida/3590`; Temida workflow `docs/agents/temida-github-workflow.md`; poll/direction/claim steps; repository context and worktree contracts from Todos 4-5.
  - Acceptance criteria: preflight bundle contains issue hash, exact criterion matrix, no-PR/no-conflict proof, clean origin-main SHA, dedicated Gate 3 namespace, and verified `ai:ready` readback; no Gate 2 ID is present.
  - QA scenarios: happy—authoritative snapshot then one label mutation/readback, evidence `.omo/evidence/task-13-temida-reservation.json`; failure—any drift/collision leaves issue unclaimed or safely quarantined and blocks Gate 3, evidence `.omo/evidence/task-13-temida-reservation-failure.json`.
  - Commit: No | Controlled GitHub label mutation and evidence only.

- [ ] 14. Let natural cycles solve and merge Temida 3590
  - What to do: wait for naturally elapsed launchd intervals and observe the agent discover #3590, create the correct Kanban task/claim, run OMP in an isolated Temida worktree, implement the requested retry behavior, push `ai/fix/*`, open the PR, supply genuine test evidence, pass repository checks, and merge through the guarded triage path with `automerge=true` and `require_human_approval=false`. Do not kickstart, edit Temida, repair the branch manually, waive checks, add fake evidence, or merge manually. If the worker or checks fail, allow only the runtime's bounded, receipt-backed repair/retry policy; otherwise stop fail closed.
  - Parallelization: Can parallel N | Wave 5 | Blocks 15 | Blocked by 13.
  - References: `issue://mikolaj92/Temida/3590`; `src/repo_agent/steps/issue_to_pr.py:504-593` worker execution; triage decision/mutation steps; `fala-package.toml` auto-worker ordering; `docs/auto-worker.md` Gate 3 contract.
  - Acceptance criteria: natural launch evidence correlates to exact Gate 3 run/process IDs; agent-authored commit changes only issue-relevant Temida files; PR owner/head/base/checks/evidence/mergeability gates pass; triage performs the merge; GitHub reports the exact merge SHA and closed issue; no supervising-agent implementation/merge command exists.
  - QA scenarios: happy—capture all natural cycles and authoritative state transitions in `.omo/evidence/task-14-temida-lifecycle.json`; failure—capture exact failed process/check/repair attempt and prove no unsafe merge or unrelated mutation in `.omo/evidence/task-14-temida-lifecycle-failure.json`.
  - Commit: No in this repository; the Temida worker's focused commit must merge through its own PR naturally.

- [ ] 15. Prove Temida acceptance on merged main and finalize provenance
  - What to do: fetch Temida after merge, verify a clean checkout at the exact recorded merge/origin-main SHA, and execute the issue-derived criterion matrix: one transient connection/chunked-response failure retries once with the same mandatory request; a repeated transport failure raises `LLMError`; HTTP and schema/content failures do not retry; request/response contents remain absent from errors/logs; the package test suite required by the issue passes. Independently reconcile GitHub/Kanban/Fala/log/dispatch/merge/cleanup/deployment state and observe one following natural idle reconciliation cycle. Write the final outcome with exact candidate/config/plist hashes, issue/PR/task/run/process/receipt IDs, head/merge/main SHAs, commands/results, cleanup state, and foreign-state comparison. Commit and push only safe plan/outcome evidence and any source documentation required by repo convention; never commit runtime secrets/logs/DBs.
  - Parallelization: Can parallel N | Wave 5 | Blocks F1-F4 | Blocked by 14.
  - References: acceptance body at `issue://mikolaj92/Temida/3590`; `docs/auto-worker.md:56-64`; cleanup/receipt contracts from Todos 3 and 12; source-of-truth mapping `docs/github-kanban-mapping.md`.
  - Acceptance criteria: every #3590 criterion has command, output, exit status, and exact main SHA; issue is closed and PR merged at that SHA; all three receipt types and journal/log rows reconcile; terminal cleanup and following natural idle cycle are proven; local `main` and `origin/main` for this repo agree after safe evidence commit; final disposition is success only if every field agrees.
  - QA scenarios: happy—run criterion matrix on clean merged Temida main and full reconciliation, evidence `.omo/evidence/task-15-temida-acceptance.json`; failure—deliberately compare against a wrong SHA/receipt ID in an offline verifier and prove it rejects the bundle, evidence `.omo/evidence/task-15-temida-acceptance-failure.json`.
  - Commit: Yes | `docs(evidence): record guarded real-issue outcome` | Safe outcome/plan documentation only, authored by `mikolaj92`; push `main` without force.

## Final verification wave (after ALL todos)
> Runs in parallel. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.

- [ ] F1. Plan compliance audit
  - Verify every todo and guardrail against repository commits, candidate manifest, live evidence, Gate 2 bundle, Gate 3 bundle, and final outcome. Reject missing criteria, reused gate identities, manual triggers/mutations, or success inferred from summaries.
  - Evidence: `.omo/evidence/final-real-issue-plan-compliance.txt`.

- [ ] F2. Code quality and safety review
  - Review the final code diff for fail-open merge paths, stale/malformed GitHub data, receipt durability/correlation, multi-repo cross-wiring, branch/worktree ownership, cleanup partial mutations, race/idempotency behavior, secret leakage, and obsolete compatibility paths. Run focused diagnostics/tests for every finding.
  - Evidence: `.omo/evidence/final-real-issue-code-review.txt`.

- [ ] F3. Real lifecycle QA
  - Independently replay receipt/journal/log reconciliation and Temida acceptance commands from clean checkouts at recorded SHAs. Confirm natural scheduler timing/topology and authoritative GitHub/Kanban state rather than trusting the producing worker's report.
  - Evidence: `.omo/evidence/final-real-issue-qa.json`.

- [ ] F4. Scope fidelity and provenance audit
  - Compare pre/post dirty-path hashes, refs, worktrees, boards, repos, deployment artifacts, launchd jobs, candidate/config/Fala/plist identities, and `main`/`origin/main`. Confirm no unrelated work or secret/runtime artifact entered commits and all commits are authored only by `mikolaj92` without AI trailers.
  - Evidence: `.omo/evidence/final-real-issue-provenance.txt`.

## Commit strategy
- Work directly on the current `main`, as requested; never create or switch to an agent/model-named branch.
- Preserve the existing dirty tree, classify it before modification, and publish intended current work before building a live candidate.
- Keep commits atomic by contract: runtime migration baseline; triage safety; receipt correlation; repository context; worktree isolation; deployment provenance; config; tests; final safe outcome record.
- Commit author is only `mikolaj92`; no AI/co-author/generated trailers or metadata.
- Push `main` normally after each verified code/config integration checkpoint and after the safe final outcome record; never force push.
- Build/deploy only from exact clean commits already on `origin/main`. Any post-build code fix requires a new commit, push, candidate ID, manifest, and validation cycle.
- Do not commit deployment instances, databases, logs, tokens, machine-private config, raw receipt stores, or evidence containing secrets.

## Success criteria
- Current intended repository work is reviewed, committed, and pushed on `main`; candidate source equals clean `origin/main`.
- Triage enforces configured approval and merges only authoritative clean/mergeable PRs while preserving every other guard.
- Selected-repository identity is correct through all lifecycle steps; Temida never inherits repo-agent board/clone context.
- Dispatch, merge, and cleanup receipts are immutable, durable, non-clobbering, and correlated to exact Fala processes, launch logs, candidate/config hashes, and GitHub/Kanban entities.
- Worktree/branch preparation and cleanup preserve foreign state and fail closed on collision, dirt, stale provenance, lock, API ambiguity, or partial failure.
- Active deployment is immutable, parity-clean, manifest-backed, rollback-capable, policy-enabled (`executor.enabled=true`, `automerge=true`, `require_human_approval=false`, checks/evidence still true), and runs one known Fala launchd scheduler naturally every 600 seconds.
- Gate 2 independently completes a disposable canary from discovery through terminal cleanup plus a following reconciliation tick.
- Gate 3 independently causes the agent—not the supervisor—to implement, test, open, and automatically merge the fix for `mikolaj92/Temida#3590`, close the issue, and clean its execution state.
- Every #3590 acceptance criterion passes on a clean checkout of the recorded merged `main` SHA.
- GitHub, Kanban, Fala journal/processes, logs, receipts, cleanup, deployment manifest, clone refs, and final outcome all agree; foreign state remains unchanged.
- All four final audits approve. Otherwise the only valid final disposition is `INCOMPLETE - NOT PROMOTED` with exact blockers and rollback state.