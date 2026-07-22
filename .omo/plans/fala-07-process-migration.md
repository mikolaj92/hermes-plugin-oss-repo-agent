# Fala 0.7 Process Migration

## TL;DR
> Summary: Clean-cut the repo-agent from the removed Fala 0.2.1 Python scheduler API to one Fala 0.7.6 package-v2 subprocess graph. Fala owns lifecycle, ordering, retries, dependency cancellation, and auto-worker sequencing; repo-agent retains only guarded domain operations, adapter boundary, diagnostics, and CLI presentation.
> Deliverables: Fala v2 TOML package; generic subprocess effector; 0.7 host/journal facade; migrated domain handlers and tick CLIs; deleted scheduler/bridge glue; exact failed-process diagnostics; updated dependency/deployment provenance; focused real-host and regression evidence.
> Effort: XL
> Risk: High - broad clean cutover across runtime, safety-sensitive effectors, generated deployment candidates, and a currently dirty worktree.

## Scope
### Must have
- Pin `fala-runtime==0.7.6` and Fala commit `9f10d58462b4e134d5b1cffe8ff9172909df70ea` everywhere runtime or deployment provenance is validated.
- Declare package-v2 TOML paths for intake, issue-to-PR, triage branches, cleanup, and a single `auto_worker` path.
- Invoke domain functions through one subprocess entrypoint using `fala.sdk`.
- Convert semantic domain failure (`ok=false` or `status=failed`) into a nonzero subprocess exit with sanitized evidence; otherwise Fala 0.7 records a valid result object as success.
- Use Fala conduction as the only ordering/data channel and Fala process lifecycle as the only scheduler.
- Read exact failed-process evidence from the durable Fala SQLite journal by returned `run_id`.
- Preserve dry-run/live precedence, mutation gates, receipt durability, worktree confinement, claim behavior, and automerge-off policy.
- Preserve unrelated dirty-worktree changes; do not build/promote a candidate until intended changes are committed and plugin/Fala sources are clean.

### Must NOT have
- No compatibility layer for 0.2.x, `fala.models`, `fala.runtime_backend`, `RuntimeBackendService`, `CorrelationPathSpec`, `EffectorRunRequest`, `EffectorRunResult`, or `python_function` adapters.
- No Python scheduler, retry/backoff loop, claim/lease lifecycle, dead-dependent cancellation, homeostat reconciliation, per-effector bridge, tick-all sequencing, or triage follow-up router.
- No upstream Fala serializer change; query the local journal instead.
- No live GitHub/Kanban/git mutation during migration verification; real-host smoke uses inert test effectors and temporary paths.
- No release candidate, launchd cutover, or push from a dirty checkout.

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: tests-after with existing `unittest`/`pytest`; first real-host semantic-failure smoke immediately after the package/entrypoint/runtime slice, before migrating all domain flows.
- QA policy: every todo includes a success and failure scenario; real-host behavior must be observed through `fala.host_run_package`, not mocks alone.
- Evidence: `.omo/evidence/task-<N>-<slug>.txt` or `.json`.

## Execution strategy
### Parallel execution waves
> Parallel work begins only after the core package/entrypoint/runtime contract is proven because every remaining slice depends on it.
- Wave 1: Todos 1-3 sequentially establish, prove, and normalize the Fala 0.7 boundary.
- Wave 2: Todos 4-7 in parallel migrate independent domain/path slices against the proven contract.
- Wave 3: Todos 8-10 in parallel cut over CLIs, delete legacy surfaces, and update deployment provenance/documentation.
- Wave 4: Todo 11 integrates focused regressions and real auto-worker smoke.
- Final wave: F1-F4 in parallel.
- Critical path: 1 → 2 → 3 → 4/5/6/7 → 8 → 11 → F1-F4.

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
|---|---|---|---|
| 1 | none | 2, 3 | none |
| 2 | 1 | 3 | none |
| 3 | 2 | 4-8 | none |
| 4 | 3 | 8, 11 | 5, 6, 7 |
| 5 | 3 | 8, 11 | 4, 6, 7 |
| 6 | 3 | 8, 11 | 4, 5, 7 |
| 7 | 3 | 8, 11 | 4, 5, 6 |
| 8 | 4-7 | 11 | 9, 10 |
| 9 | 3 | 11 | 8, 10 |
| 10 | 3 | 11 | 8, 9 |
| 11 | 4-10 | F1-F4 | none |

## Todos

- [ ] 1. Pin Fala 0.7.6 and add package-v2 graph
  - What to do: update `pyproject.toml` and `uv.lock`; add the repository-owned `fala-package.toml`. Define effectors with stable IDs matching existing domain operations, subprocess adapters targeting one module entrypoint, static max-attempt/timeout regulation, and conduction for `issue_intake`, `issue_to_pr`, triage decision plus merge/comment/repair gates, `cleanup`, and `auto_worker`. The auto-worker graph must cover intake → dispatch → triage branches → cleanup in one correlation path. Use no existing YAML assumption: this checkout has no Fala package asset.
  - Must NOT do: embed Python import refs as `python_function`; express conditional behavior as Python routing; give mutating effectors retries unless the existing operation is proven idempotent/retry-safe.
  - Parallelization: No | Wave 1 | Blocks 2,3
  - References: `pyproject.toml:8-10,33-36`; `uv.lock:32-67`; `src/repo_agent/flows/intake.py:17-43`; `src/repo_agent/flows/issue_to_pr.py:17-76`; `src/repo_agent/flows/triage.py:18-159`; `src/repo_agent/flows/cleanup.py:17-70`; `/Users/mini-m4-main/Developer/Fala/examples/correlation-paths/basic/fala-package.toml:1-81`.
  - Acceptance criteria: Fala 0.7.6 parses the TOML; every conduction dependency names a declared effector; `auto_worker` contains every required stable process ID; dependency metadata resolves to 0.7.6.
  - QA scenarios: run a package-load/instantiate probe for each path and save output to `.omo/evidence/task-1-package-load.txt`; corrupt one conduction ID in a temporary copy and prove parsing/instantiation fails in `.omo/evidence/task-1-invalid-conduction.txt`.
  - Commit: Yes | `feat(fala): declare 0.7 process package` | `fala-package.toml`, `pyproject.toml`, `uv.lock`

- [ ] 2. Implement one strict subprocess effector boundary
  - What to do: migrate `src/repo_agent/envelope.py` to plain domain dictionaries and add a single module entrypoint (prefer `src/repo_agent/effector.py`) that loads the SDK manifest, resolves an allowlisted handler ref, constructs the handler request from `sdk.declared_inputs`, `sdk.config`, and `sdk.conduction`, and writes success/no-op via `sdk.output(values=payload)`. Treat malformed payloads or semantic failure as failure: write sanitized deterministic evidence where possible, print a concise secret-redacted diagnostic, and exit nonzero. Preserve input-over-config dry-run precedence and existing conduction lookup behavior without bridge functions.
  - Must NOT do: dynamic arbitrary imports from untrusted manifest values; consider a valid `result.json` with `ok=false` successful; recreate the removed Fala request/result classes.
  - Parallelization: No | Wave 1 | Blocked by 1 | Blocks 3
  - References: `src/repo_agent/envelope.py:1-134`; `/Users/mini-m4-main/Developer/Fala/python/fala/sdk.py:30-155`; `/Users/mini-m4-main/Developer/Fala/mojo/fala/adapters.mojo:605-620`.
  - Acceptance criteria: success/no-op produce an SDK output envelope and exit 0; `ok=false`, `status=failed`, exception, malformed output, and unknown handler exit nonzero with no secret leakage; dry-run input overrides config.
  - QA scenarios: invoke the entrypoint with temporary SDK manifests for success/no-op/failure and save `.omo/evidence/task-2-effector-contract.txt`; inject a sentinel secret and assert it is absent from stderr/result evidence in `.omo/evidence/task-2-redaction.txt`.
  - Commit: Yes | `feat(fala): add strict subprocess effector` | `src/repo_agent/envelope.py`, `src/repo_agent/effector.py`, focused tests

- [ ] 3. Replace scheduler with host and journal facade, then smoke failure early
  - What to do: replace `src/repo_agent/flows/runtime.py` with a thin synchronous/asynchronous facade around `fala.host_run_package`; normalize returned run status/process IDs; query SQLite using stdlib `sqlite3` with `SELECT id,status,attempt,max_attempts,output_json,error_json FROM processes WHERE run_id=? ORDER BY id`. Validate expected columns and row types, require `output_json`/`error_json` to decode as JSON objects, redact diagnostics, and fail closed on schema/JSON mismatch. Return exact failed process ID/status/attempt/error to CLI models. Immediately run a real temporary package containing one success process, one semantic-failure process, and a dependent sentinel.
  - Must NOT do: retain `FailurePolicy`, run loops, retries, claims, leases, homeostats, cancellation, or process transitions; interpolate `run_id` into SQL; infer success only from host return shape.
  - Parallelization: No | Wave 1 | Blocked by 2 | Blocks 4-10
  - References: `src/repo_agent/flows/runtime.py:1-666`; `/Users/mini-m4-main/Developer/Fala/python/fala/host.py:178-249`; `/Users/mini-m4-main/Developer/Fala/mojo/fala/schema.mojo:167-191`; `/Users/mini-m4-main/Developer/Fala/mojo/fala/journal.mojo:463-475`.
  - Acceptance criteria: real Fala run persists semantic-failure process as `failed`; its dependent does not execute; normalized result exactly reports journal `id/status/attempt/max_attempts/error_json`; malformed persisted JSON produces an explicit facade failure.
  - QA scenarios: run the real three-process package and query its temporary DB into `.omo/evidence/task-3-real-failure.json`; tamper `error_json` and verify fail-closed normalization in `.omo/evidence/task-3-malformed-journal.txt`.
  - Commit: Yes | `refactor(fala): use host package runtime` | `src/repo_agent/flows/runtime.py`, focused tests/fixtures

- [ ] 4. Migrate issue intake handlers and path facade
  - What to do: adapt poll, issue-direction, claim, and Kanban intake handlers to the plain manifest request/envelope contract; make non-applicable work explicit no-op; preserve reject-comment, claim reuse/readback, dry-run, assignee, and idempotent Kanban guarantees. Replace the 0.2 path construction in `flows/intake.py` with a call to the common package host facade and normalized intake summary.
  - Must NOT do: schedule or retry in Python; mutate on empty/ineligible queues; weaken claim validation.
  - Parallelization: Yes | Wave 2 | Blocked by 3 | Blocks 8,11 | With 5,6,7
  - References: `src/repo_agent/steps/poll.py`; `src/repo_agent/steps/issue_direction.py`; `src/repo_agent/steps/claim.py`; `src/repo_agent/steps/kanban_intake.py`; `src/repo_agent/flows/intake.py:17-217`; `tests/test_issue_direction_intake.py`; `tests/test_fala_intake_flow.py`.
  - Acceptance criteria: eligible and rejected dry-run inputs retain current summaries; empty queue is idle/worked=false/mutated=false; live mutation remains guarded; no legacy Fala type imports.
  - QA scenarios: run focused intake tests and a real-host dry-run intake fixture into `.omo/evidence/task-4-intake.txt`; verify an ineligible issue reaches no mutating handler in `.omo/evidence/task-4-ineligible.txt`.
  - Commit: Yes | `refactor(intake): run as Fala subprocess processes` | intake steps/flow/tests

- [ ] 5. Migrate issue-to-PR handlers and path facade
  - What to do: adapt Kanban load, issue parsing, worktree, OMP, branch verification/push, and PR creation handlers to the common manifest contract. Preserve worktree confinement, branch/provenance validation, mutation uncertainty, receipt handling, and no-ready-task idle behavior. Replace Python path construction with one host facade call.
  - Must NOT do: sequence operations in the facade; continue after a failed upstream; add destructive recovery.
  - Parallelization: Yes | Wave 2 | Blocked by 3 | Blocks 8,11 | With 4,6,7
  - References: `src/repo_agent/steps/issue_to_pr.py`; `src/repo_agent/flows/issue_to_pr.py:17-278`; `tests/test_issue_to_pr.py`; `tests/test_facade_fail_closed.py`; `tests/test_receipt_durability.py`.
  - Acceptance criteria: no-ready-task normalizes idle; dry-run produces no external mutation; a middle failure is a failed Fala process and downstream push/PR processes do not run; legacy Fala imports are absent.
  - QA scenarios: focused issue-to-PR tests plus real-host inert success chain in `.omo/evidence/task-5-issue-to-pr.txt`; deliberate middle failure with sentinel downstream in `.omo/evidence/task-5-stop-on-failure.json`.
  - Commit: Yes | `refactor(dispatch): run as Fala subprocess processes` | issue-to-PR step/flow/tests

- [ ] 6. Migrate triage and encode branch gates in Fala
  - What to do: adapt triage/repair handlers and replace Python follow-up routing with package processes. Each merge/comment/repair branch effector reads the durable decision via conduction, returns no-op when unselected, and only the selected branch may mutate. Preserve checks/evidence gates, automerge disabled default, claim rules, receipt durability, and close-linked-issue behavior.
  - Must NOT do: call `run_follow_up_path` or `run_triage_with_router`; allow more than one branch to mutate; treat all-noop as completed work.
  - Parallelization: Yes | Wave 2 | Blocked by 3 | Blocks 8,11 | With 4,5,7
  - References: `src/repo_agent/steps/triage.py`; `src/repo_agent/steps/repair.py`; `src/repo_agent/flows/triage.py:18-625`; `tests/test_fala_triage_router.py`; `tests/test_decide_matrix.py`; `tests/test_triage.py`.
  - Acceptance criteria: merge/comment/repair/skip decisions each select exactly one or zero mutating branches; failed decision cancels/blocks all branch dependents; no Python router remains.
  - QA scenarios: decision matrix through real package dry-run in `.omo/evidence/task-6-triage-matrix.json`; instrument branch handlers and prove at-most-one mutation in `.omo/evidence/task-6-exclusive-branch.txt`.
  - Commit: Yes | `refactor(triage): move routing into Fala graph` | triage/repair steps/flow/tests

- [ ] 7. Migrate cleanup handlers and path facade
  - What to do: adapt cleanup parsing/check/removal/release handlers to the common contract; preserve exact mutation/no-op receipts and ordering. Replace path construction with one host facade call.
  - Must NOT do: delete branch/worktree before safety checks; report partial mutation as clean success; retry destructive cleanup without proven idempotence.
  - Parallelization: Yes | Wave 2 | Blocked by 3 | Blocks 8,11 | With 4,5,6
  - References: `src/repo_agent/steps/cleanup.py`; `src/repo_agent/flows/cleanup.py:17-198`; `tests/test_cleanup_safety.py`; `tests/test_receipt_durability.py`.
  - Acceptance criteria: safe cleanup order is expressed only by conduction; no-target is idle/no-op; partial failure reports exact failed process and preserved mutation evidence; no legacy Fala imports.
  - QA scenarios: focused temp-worktree cleanup cases in `.omo/evidence/task-7-cleanup.txt`; force middle deletion failure and verify later release does not execute in `.omo/evidence/task-7-partial-failure.json`.
  - Commit: Yes | `refactor(cleanup): run as Fala subprocess processes` | cleanup step/flow/tests

- [ ] 8. Cut all tick CLIs over to package paths
  - What to do: update all tick entrypoints and flow exports to call the common host facade and print normalized process/journal evidence. `repo-agent-tick-all` must issue exactly one `host_run_package` call for `auto_worker`; remove its four sequential awaits. Preserve CLI flags, config loading, dry-run/live conflict behavior, JSON shape where still applicable, and nonzero exit on any failed/cancelled/timed-out process or journal normalization error.
  - Must NOT do: retain old flow aliases/router exports or hide an exact failed process behind aggregate `any_failed` only.
  - Parallelization: Yes | Wave 3 | Blocked by 4-7 | Blocks 11 | With 9,10
  - References: `src/repo_agent/tick_intake.py`; `tick_dispatch.py`; `tick_triage.py`; `tick_cleanup.py`; `tick_all.py:21-78`; `tick_common.py:44-93`; `src/repo_agent/flows/__init__.py:1-50`.
  - Acceptance criteria: each single tick maps to one TOML path; tick-all makes one host call; stderr/JSON names exact failed process and persisted error; empty auto-worker is idle/worked=false.
  - QA scenarios: spy on host call count/path IDs in `.omo/evidence/task-8-cli-calls.txt`; execute failing real-host tick fixture and capture exit/status JSON in `.omo/evidence/task-8-cli-failure.json`.
  - Commit: Yes | `refactor(cli): drive Fala package paths` | tick modules, flow exports, CLI tests

- [ ] 9. Delete legacy scheduler and bridge surfaces
  - What to do: delete `src/repo_agent/flows/bridges.py`; remove obsolete scheduler helpers/classes and stale path composition exports/tests; migrate any remaining request typing in every `steps/*.py`, `catalog.py`, adapters, and tests to the plain contract. Remove obsolete compatibility comments/docs.
  - Must NOT do: leave aliases/re-exports/shims; delete unrelated dirty user work.
  - Parallelization: Yes | Wave 3 | Blocked by 3 | Blocks 11 | With 8,10
  - References: `src/repo_agent/flows/bridges.py`; `src/repo_agent/flows/common.py`; `src/repo_agent/catalog.py`; `src/repo_agent/adapters_cli.py`; `src/repo_agent/adapters_omp.py`; `tests/test_flow_bridges.py`; `tests/test_path_composition.py`; `tests/test_fala_runtime_contract.py`.
  - Acceptance criteria: structural/text scan finds no removed Fala API or `python_function`; deleted modules have no imports; package graph tests replace Python object-composition tests.
  - QA scenarios: run import/compile and focused package graph tests into `.omo/evidence/task-9-legacy-removal.txt`; intentionally scan all product/tests for forbidden symbols and save zero-result evidence.
  - Commit: Yes | `refactor(fala): remove legacy scheduler glue` | flows/common/bridges, adapters/catalog, coupled tests

- [ ] 10. Update candidate, parity, scripts, and operator documentation
  - What to do: update `commands.py` and `tools/deployment_parity.py` pinned commit/tag, candidate copy/manifest checks, package asset inclusion, and Fala 0.7 metadata validation. Update health/status/push-smoke scripts and deployment fixtures. Replace operational 0.2.1 instructions with 0.7.6 package-host behavior in README/config examples/docs; remove docs that only describe the deleted intake slice or rewrite them to the current package contract. Candidate generation must continue to reject dirty plugin or Fala source.
  - Must NOT do: perform live deployment/launchd mutation; weaken immutable candidate/provenance checks; conflate plugin version with Fala runtime version.
  - Parallelization: Yes | Wave 3 | Blocked by 3 | Blocks 11 | With 8,9
  - References: `commands.py:31,713-843`; `tools/deployment_parity.py:17,411,482-709`; `scripts/repo_agent_health.sh:76-114`; `scripts/repo_agent_status.sh:134-156`; `scripts/push_and_smoke_fala_ticks.sh:39-41`; `tests/test_deployment_candidate.py`; `tests/test_deployment_parity.py`; `tests/test_health_status_scripts.py`; `README.md`; `docs/fala-intake-slice.md`; `docs/auto-worker.md`; `docs/effector-catalog.md`.
  - Acceptance criteria: focused candidate fixture records Fala tag 0.7.6 and exact commit, contains `fala-package.toml` plus effector module, rejects altered/dirty provenance, and passes parity; docs/scripts contain no active 0.2.1 runtime instruction.
  - QA scenarios: build candidate from clean temporary git fixtures and inspect manifest/archive into `.omo/evidence/task-10-candidate.json`; run dirty-Fala and wrong-commit negative fixtures into `.omo/evidence/task-10-provenance-reject.txt`.
  - Commit: Yes | `chore(fala): update deployment provenance` | commands, parity, scripts, deployment tests, docs/config examples

- [ ] 11. Integrate focused regressions and real auto-worker smoke
  - What to do: reconcile tests around observable contracts after the clean cutover. Run syntax/import checks, focused Fala package/runtime/flow/CLI/deployment suites, then a real temporary SQLite auto-worker package run using inert handlers. Assert process identity/order/statuses from the journal, exact failure evidence, branch exclusivity, idle semantics, and one host invocation. Capture pre/post dirty path inventory and prove unrelated changes are unchanged. Commit intended migration changes before candidate-only checks; use clean temporary/source fixtures rather than altering unrelated work.
  - Must NOT do: add source-text-only tests where runtime behavior is available; run live mutation; claim full-suite success from narrowed tests.
  - Parallelization: No | Wave 4 | Blocked by 4-10 | Blocks F1-F4
  - References: `tests/test_fala_runtime_contract.py`; `tests/test_fala_intake_flow.py`; `tests/test_fala_triage_router.py`; `tests/test_path_composition.py`; deployment tests listed in Todo 10; `scripts/repo_agent_smoke.sh`.
  - Acceptance criteria: focused suite passes; real `auto_worker` run contains every expected process and no Python sequencing; deliberate middle semantic failure is persisted with exact error and blocks dependents; no forbidden legacy symbols/provenance remain; unrelated worktree content is byte-identical.
  - QA scenarios: save focused command/output to `.omo/evidence/task-11-focused-tests.txt`; save success/failure journal rows to `.omo/evidence/task-11-auto-worker.json`; save dirty-path checksums before/after to `.omo/evidence/task-11-dirty-preservation.txt`.
  - Commit: Yes | `test(fala): verify 0.7 process migration` | focused tests/fixtures and any final contract corrections

## Final verification wave (after ALL todos)
> Run in parallel. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.

- [ ] F1. Plan compliance audit
  - Verify every must-have/must-not-have and each todo acceptance criterion against code and saved evidence. Reject any Python-owned scheduling/router or missing failed-process evidence.
  - Evidence: `.omo/evidence/final-plan-compliance.txt`.

- [ ] F2. Code quality and safety review
  - Review the final diff for secret leakage, SQL parameterization, fail-closed parsing, mutation gates, retry/idempotency policy, concurrency hazards, and dead compatibility code.
  - Evidence: `.omo/evidence/final-code-review.txt`.

- [ ] F3. Real manual QA
  - Independently run the real Fala package on temporary SQLite for: two-step success, middle semantic failure, empty auto-worker, and each triage decision. Inspect durable process rows rather than trusting wrapper summaries.
  - Evidence: `.omo/evidence/final-real-host-qa.json`.

- [ ] F4. Scope fidelity and provenance audit
  - Verify no unrelated dirty content changed, no live external mutation occurred, dependency/manifest/candidate provenance agrees on Fala 0.7.6 plus exact commit, and the clean candidate contains runnable package assets.
  - Evidence: `.omo/evidence/final-scope-provenance.txt`.

## Commit strategy
- Work on `main`, as requested.
- Preserve the existing dirty tree; inspect and incorporate intended current changes rather than overwriting them.
- Use the atomic commits named above with author `mikolaj92` only and no AI/co-author trailers.
- Candidate/provenance verification occurs only after intended changes are committed and both source checkouts used for the candidate are clean.
- Push `main` only after focused verification and all final audits approve; no force push.

## Success criteria
- Fala 0.7.6 package host is the sole scheduler/runtime path.
- One `auto_worker` Fala run owns full tick ordering and triage branch gating.
- Every semantic domain failure becomes a durable failed Fala process; exact process ID/status/attempt/persisted error reaches tick JSON/stderr; dependents do not execute.
- Empty/all-noop runs are idle and never reported as useful work.
- No legacy 0.2 API, Python scheduler/router/bridge, or stale 0.2.1 deployment provenance remains.
- Existing safety contracts remain fail closed.
- Focused tests, real-host smoke, candidate/parity checks, and four final audits pass with durable evidence.
- Unrelated worktree content remains unchanged; final verified commits are pushed to `main` without force.
