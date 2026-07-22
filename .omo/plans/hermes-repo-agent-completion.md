# Hermes repo-agent completion and promotion

## TL;DR
> Summary: Complete the authorized issue-8 canary lifecycle and harden/promote the repo-agent only through immutable, receipt-backed, fail-closed gates. Integrate the staged safety implementation into a clean merged baseline, rebuild the candidate, reconcile exact cleanup, repair active deployment/launchd drift, and verify provenance after controlled activation.
> Deliverables: reviewed code commit; strict candidate manifest with explicit policy provenance; durable issue-8 cleanup receipt; immutable deployment version/current pointer; regular managed artifacts and canonical plists; safe Fala-only launchd state; post-transition health/parity receipts; final origin/deployment provenance record.
> Effort: XL
> Risk: High - real GitHub cleanup, deployment filesystem, launchd, and runtime policy mutations.

## Scope
### Must have
- Preserve the already completed canary evidence: issue #8, PR #9, merge `49d21868ebd0f1ea3913efdcba6582a14e39f900`, issue closure, and merge receipt.
- Use `/Users/mini-m4-main/Developer/hermes-repos/hermes-plugin-oss-repo-agent-live` at merge `49d21868ebd0f1ea3913efdcba6582a14e39f900` as the integration baseline.
- Integrate only explicitly reviewed safety/parity changes from `/private/tmp/hermes-canary-stage`; preserve the dirty remediation checkout and unrelated user work.
- Keep Fala at tag `0.2.1`, commit `b5f8085f418010a9290613b86671d435551411a9`.
- Make candidate manifests contain and validate effective automation/executor policy, including `automerge=false`, `require_human_approval=true`, `require_checks=true`, `require_test_evidence=true`, and `executor.enabled=false`.
- Allow only `deployment/current` to be a symlink, and only when it resolves directly to a 64-hex child under `deployment/versions` with a valid manifest. Managed scripts, configs, plists, candidate artifacts, and active roots must be private regular files; hardlinks and unexpected backup/runtime artifacts fail parity.
- Complete exact issue-8 cleanup only with durable receipt, closed issue, no open PR, exact branch/worktree identity, clean status, no locks/leases, current `origin/main` provenance, and claim release evidence.
- Keep health observational: no stale-lock deletion and no launchd mutators from health/status. Probe both `user/UID` and `gui/UID`; ambiguity or unknown launchctl errors fail closed.
- Never run legacy shell mutators concurrently with Fala. Boot out legacy services before bootstrapping Fala, and retain verified rollback information.
- Commit intended remediation changes and verify the deployed candidate, Git origin, active plist bytes, manifest, policy, database, parity, health, and launchd state.

### Must NOT have (guardrails, anti-slop, scope boundaries)
- No reset, clean, force push, destructive recovery, wholesale copying, or overwrite of the dirty authoritative checkout.
- No historical issue #5/PR #7 evidence, unrelated repositories, unrelated LaunchAgents, or user-owned files in cleanup/parity scope.
- No production mutation before every prior static and read-only gate passes.
- No launchd `load`, `bootstrap`, `enable`, `disable`, `kickstart`, `kill`, or `unload` except the controlled transition implementation after approval gates; never run the active unsafe health plist.
- No remote branch deletion without the exact issue-8 identity and a durable cleanup receipt protocol.
- No full-suite test, formatter, broad lint, speculative abstraction, or unrelated refactor.
- No claim that candidate smoke, branch 404, or a dry-run no-op proves cleanup or promotion.
- No final success claim if any gate is failed, ambiguous, stale, or unverifiable.

## Verification strategy
> Zero human intervention inside execution steps; every mutation is preceded by an agent-executed gate. Operator approval is required before the first real cleanup/deployment/launchd mutation and is recorded as an explicit gate artifact.
- Test decision: tests-after, using `unittest`/`pytest`, deterministic temporary fixtures, shell syntax checks, candidate smoke, and read-only runtime probes.
- QA policy: every todo includes happy and failure-path assertions; no self-reported subagent result is accepted without direct command output or artifact inspection.
- Evidence: write per-task records under `.omo/evidence/task-<N>-hermes-completion.*`; final evidence must include hashes, exact paths, command output, and mutation receipts.

## Execution strategy
### Parallel execution waves
> Target 5-8 todos per wave. Mutation order is intentionally serialized where state safety requires it.
Wave 1 (no live mutation): baseline snapshot, surgical integration, policy/CLI changes, focused regression.
Wave 2 (candidate only): candidate rebuild, manifest/policy validation, immutable candidate smoke.
Wave 3 (pre-mutation reconciliation): exact cleanup receipt preparation, active deployment inventory, launchd/plist staging, rollback snapshot.
Wave 4 (controlled mutation): exact cleanup, immutable version activation, legacy bootout/Fala bootstrap, post-transition verification.
Wave 5 (final): commit/push provenance, final audit, outcome record.
Critical path: integration → focused tests → candidate → policy/parity/cleanup gates → rollback snapshot → cleanup → deployment/launchd transition → post-transition verification → commit/provenance.

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
|---|---|---|---|
| 1. Freeze baseline and integration workspace | none | 2, 3, 4 | none |
| 2. Integrate strict safety/parity implementation | 1 | 5 | 3, 4 |
| 3. Fix candidate CLI and policy provenance | 1 | 5 | 2, 4 |
| 4. Add exact cleanup receipt contract | 1 | 7 | 2, 3 |
| 5. Run focused integration regression | 2, 3, 4 | 6 | none |
| 6. Build and validate immutable candidate | 5 | 7, 8 | none |
| 7. Reconcile exact issue-8 cleanup | 6 and cleanup gates | 9 | 8 (read-only only) |
| 8. Stage active deployment and rollback snapshot | 6 and parity gates | 9 | 7 read-only reconciliation |
| 9. Perform controlled launchd/deployment transition | 7, 8, operator gate | 10 | none |
| 10. Verify runtime, commit, push, and record outcome | 9 | final audit | none |

## Todos

- [ ] 1. Freeze baseline and integration workspace
  What to do: Record authoritative checkout status without mutation; verify the clean merged clone, `origin/main`, merge receipt, issue/PR state, pinned Fala checkout, and staged six-file safety diff. Create a temporary integration worktree or isolated copy from the clean merged clone without touching the dirty checkout. Record hashes and allowlists before edits.
  Must NOT do: Do not reset/clean either existing checkout, remove historical worktrees, fetch unknown branches, or treat `/tmp` as authoritative source.
  Parallelization: Can parallel: N | Wave 1 | Blocks 2, 3, 4
  References: `/Users/mini-m4-main/.hermes/state/repo-agent-merge-live/mikolaj92_hermes-plugin-oss-repo-agent-9.json`; `/Users/mini-m4-main/Developer/hermes-repos/hermes-plugin-oss-repo-agent-live`; `/private/tmp/hermes-canary-stage/commands.py`; `/private/tmp/hermes-canary-stage/tools/deployment_parity.py`; `/private/tmp/hermes-canary-stage/scripts/repo_agent_health.sh`; `/private/tmp/hermes-canary-stage/scripts/repo_agent_status.sh`; `/private/tmp/hermes-canary-stage/tests/`
  Acceptance criteria: `git -C <integration> rev-parse HEAD` equals `49d21868ebd0f1ea3913efdcba6582a14e39f900`; integration status is clean before edits; dirty authoritative status remains unchanged; Fala is clean at `b5f8085f418010a9290613b86671d435551411a9`; baseline manifest report is written.
  QA scenarios: happy: direct status/hash/receipt inspection; failure: abort if baseline SHA, Fala SHA, or dirty-checkout counts differ unexpectedly. Evidence `.omo/evidence/task-1-hermes-completion.json`.
  Commit: N | Files: evidence only.

- [ ] 2. Integrate strict safety/parity implementation
  What to do: Surgically port the reviewed staged behavior into the integration source. Merge `commands.py` candidate/deployment functions as one coherent unit; preserve unrelated baseline code. Port observational health, cross-domain status, strict parity, canonical `templates/launchd` handling, staged smoke behavior, and staged tests including `test_deployment_candidate.py`. Keep only explicit managed plist inventory and the trusted `deployment/current` pointer policy. Add/update imports and callers required by the staged APIs.
  Must NOT do: Do not wholesale-copy the dirty checkout or the six staged files over unrelated changes; do not reintroduce the legacy launchd template root; do not weaken symlink/hardlink, manifest, Fala, or mutator checks.
  Parallelization: Can parallel: Y | Wave 1 | Blocks 5 | Blocked by 1
  References: `/private/tmp/hermes-canary-stage/commands.py:30-136,619-1242`; `/private/tmp/hermes-canary-stage/tools/deployment_parity.py:20-664`; `/private/tmp/hermes-canary-stage/scripts/repo_agent_health.sh:1-650`; `/private/tmp/hermes-canary-stage/scripts/repo_agent_status.sh:1-350`; `/private/tmp/hermes-canary-stage/scripts/repo_agent_smoke.sh`; `/private/tmp/hermes-canary-stage/tests/test_deployment_candidate.py`; `/private/tmp/hermes-canary-stage/tests/test_deployment_parity.py`; `/private/tmp/hermes-canary-stage/tests/test_health_status_scripts.py`
  Acceptance criteria: integration contains the staged candidate/deployment APIs, observational health, strict status, strict parity, and all required tests; only `deployment/current` is permitted as a symlink; `bash -n` and Python compilation pass for changed files; no unrelated baseline paths are changed.
  QA scenarios: happy: run staged focused tests in the integration environment; failure: tampered symlink, hardlink, backup, duplicate plist, repair-enabled plist, and ambiguous launchd fixture each fail closed. Evidence `.omo/evidence/task-2-hermes-completion.txt`.
  Commit: Y | `feat(repo-agent): harden deployment and runtime safety` | Files: commands.py, tools/deployment_parity.py, scripts/repo_agent_health.sh, scripts/repo_agent_status.sh, scripts/repo_agent_smoke.sh, tests and explicitly required supporting files.

- [ ] 3. Fix candidate CLI and policy provenance
  What to do: Make `validate-fala-candidate` self-contained so it does not load an unrelated default config before validating a candidate. Keep `deploy-fala` config-dependent where required. Extend candidate manifest identity/schema and `validate_fala_candidate()` to include the effective automation and executor policy from `source/config.toml`; reject stale, missing, or mismatched policy in candidate reuse, health, status, and promotion. Update focused candidate tests for safe and unsafe policy.
  Must NOT do: Do not infer policy from environment variables alone, accept a missing manifest field, or weaken `require_human_approval`, checks, test evidence, or executor-disabled defaults.
  Parallelization: Can parallel: Y | Wave 1 | Blocks 5 | Blocked by 1
  References: `/private/tmp/hermes-canary-stage/commands.py:88-136,824-899`; `/private/tmp/hermes-canary-stage/tools/deployment_parity.py:448-664`; `/private/tmp/hermes-canary-stage/scripts/repo_agent_health.sh:32-212`; `/private/tmp/hermes-canary-stage/scripts/repo_agent_status.sh:99-185`; `/private/tmp/hermes-canary-stage/tests/test_deployment_candidate.py`; `/Users/mini-m4-main/.hermes/oss-repo-agent/config.toml`
  Acceptance criteria: `validate-fala-candidate --candidate <valid>` succeeds without a default config; a candidate with any unsafe policy field fails validation; manifest policy fields hash/compare exactly to embedded config; candidate reuse rejects a stale policy.
  QA scenarios: happy: validate the existing candidate with no config argument; failure: remove or alter policy fields and assert nonzero validation plus no promotion. Evidence `.omo/evidence/task-3-hermes-completion.txt`.
  Commit: Y | `fix(repo-agent): bind candidate manifests to policy` | Files: commands.py, tools/deployment_parity.py, tests/test_deployment_candidate.py and policy-validation callers.

- [ ] 4. Add exact cleanup receipt contract
  What to do: Extend the cleanup path so the exact issue-8 receipt identity (`repo`, issue, branch, task, worktree, base SHA, merge SHA, current origin SHA) is reconciled and a durable atomic cleanup receipt is written only after worktree removal, local branch state, lock/lease absence, claim release, closed issue, no open PR, and current-main ancestry are all verified. Preserve quarantine for malformed/uncertain receipts. Ensure cleanup uses the correct production worktree root and does not delete remote branches unless an explicitly separate, verified operation is requested.
  Must NOT do: Do not treat branch HTTP 404 or zero processed dry-run as a receipt; do not process historical or unrelated receipts; do not delete dirty/unverifiable worktrees or user branches.
  Parallelization: Can parallel: Y | Wave 1 | Blocks 5 and 7 | Blocked by 1
  References: `/Users/mini-m4-main/Developer/hermes-plugin-oss-repo-agent/scripts/repo_agent_cleanup.sh:135-369`; `/Users/mini-m4-main/.hermes/state/repo-agent-receipts/t-ef58d201-24ce97d83ed270f0.json`; `/Users/mini-m4-main/.hermes/state/repo-agent-merge-live/mikolaj92_hermes-plugin-oss-repo-agent-9.json`; `tests/test_cleanup_receipts.py`; `tests/test_shell_receipt_durability.py`
  Acceptance criteria: exact identity mismatch quarantines; dirty/locked/ambiguous target remains untouched; successful cleanup produces an atomically published, read-back-verified receipt with `CLEANUP_CONFIRMED`; issue/PR/origin/branch/worktree/claim fields match the canary; no unrelated receipt is changed.
  QA scenarios: happy: disposable fixture with closed issue and clean worktree yields receipt; failure: alter branch, SHA, lock, issue state, or worktree and assert no deletion and no success receipt. Evidence `.omo/evidence/task-4-hermes-completion.txt`.
  Commit: Y | `fix(repo-agent): make cleanup receipt-backed` | Files: cleanup script, receipt helpers, focused cleanup tests.

- [ ] 5. Run focused integration regression
  What to do: Run only the narrowed safety matrix after todos 2-4. Include shell syntax, Python compile, staged candidate/parity/health-status tests, cleanup receipt tests, runtime/bridge/facade contract tests already covering changed APIs, and `git diff --check`. Inspect failures against the contract; fix source-level failures before proceeding.
  Must NOT do: Do not run full-suite discovery, formatter, broad lint, or ignore failures due to test narrowing.
  Parallelization: Can parallel: N | Wave 1 | Blocks 6 | Blocked by 2, 3, 4
  References: integration files from todos 2-4; `/private/tmp/hermes-canary-stage/scripts/repo_agent_smoke.sh`; existing focused test modules.
  Acceptance criteria: every selected command exits zero with explicit non-truncated output; shell/Python syntax and diff checks pass; integration worktree is clean except intended changes.
  QA scenarios: happy: full focused matrix; failure: inject one manifest/plist/launchd/cleanup fault and confirm the relevant test fails before restoring fixture. Evidence `.omo/evidence/task-5-hermes-completion.txt`.
  Commit: N | Files: evidence only unless fixes are required.

- [ ] 6. Build and validate immutable candidate
  What to do: Build a new content-addressed candidate from the clean integration commit and clean pinned Fala source. Vendor Fala with the correct uppercase `Fala` project path, write both lock locations, externalize writable DB/log/environment paths, render all managed launchd artifacts from canonical templates, record explicit policy in the manifest, validate artifact hashes/provenance/runtime identity, lint plists, and run external-state smoke. Verify candidate immutability by hashing before/after validation.
  Must NOT do: Do not use the dirty checkout, `/tmp` as an implicit source, mutable source paths, candidate-local writable state, unpinned Fala, or `push_and_smoke_fala_ticks.sh`.
  Parallelization: Can parallel: N | Wave 2 | Blocks 7, 8 | Blocked by 5
  References: `/private/tmp/hermes-canary-stage/commands.py:688-899`; `/private/tmp/hermes-canary-stage/tools/deployment_parity.py:448-664`; `/tmp/Fala`; candidate `12de360b...` as validation reference; `/tmp/hermes-canary-state-final.sqlite` as smoke pattern.
  Acceptance criteria: new candidate ID is derived from stable identity; validator returns `ok=true`; all manifest artifact bytes/hashes match; Fala commit/tag and policy match; `plutil -lint` passes; smoke reports `any_failed=false` and no candidate mutation; candidate remains immutable.
  QA scenarios: happy: clean candidate build and external smoke; failure: dirty Fala, stale config, altered artifact, mutable path, unsafe policy, or second mode flag fails closed and creates no promotable version. Evidence `.omo/evidence/task-6-hermes-completion.json`.
  Commit: N | Files: candidate/deployment state outside source repo.

- [ ] 7. Reconcile exact issue-8 cleanup
  What to do: Re-read GitHub issue/PR/branch state and all local receipt/worktree/lock/lease state for issue 8. Run the new cleanup contract first in dry-run against the production worktree root; if and only if every exact identity and safety gate passes, run the controlled live cleanup and read back the durable cleanup receipt. If any expected target is absent, record `NO_TARGET_RECONCILED` only with all identity and claim-release evidence; never fabricate removal.
  Must NOT do: Do not clean historical issue #5/#7 state, unrelated repositories, or remote branches; do not run active launchd cleanup.
  Parallelization: Can parallel: N | Wave 3/4 | Blocks 9 | Blocked by 6 and 4
  References: exact issue-8 receipt paths; `/Users/mini-m4-main/.hermes/worktrees/repo-fixer`; production config worktree root; cleanup script and GitHub CLI policy.
  Acceptance criteria: either a `CLEANUP_CONFIRMED` receipt proves exact removal and claim release, or a durable `NO_TARGET_RECONCILED` receipt proves the target was already absent and no claim/lease/worktree remains; ambiguous state exits nonzero without mutation.
  QA scenarios: happy: exact disposable cleanup target yields receipt; failure: missing receipt, unexpected branch, lock, dirty worktree, open PR, or API uncertainty blocks cleanup. Evidence `.omo/evidence/task-7-hermes-completion.json`.
  Commit: N | Files: cleanup receipt/state only.

- [ ] 8. Stage active deployment and rollback snapshot
  What to do: Before any pointer or launchd mutation, inventory active versions, current pointer, scripts/config/runtime/plists, managed labels, user/gui launchd state, Fala DB, and installed health arguments. Create a rollback snapshot containing prior pointer target, exact regular-file bytes, plist bytes, loaded domains, and database/log paths. Materialize the new candidate as an immutable `versions/<64hex>` directory; create only the allowed `deployment/current` pointer; copy active scripts/config/plists as private regular files; remove candidate-source backup/runtime artifacts by rebuilding the managed inventory, not broad cleanup.
  Must NOT do: Do not replace active files or pointer until snapshot hashes and rollback restore checks pass; do not touch unrelated LaunchAgents.
  Parallelization: Can parallel: Y | Wave 3 | Blocks 9 | Blocked by 6
  References: `/Users/mini-m4-main/.hermes/oss-repo-agent/deployment`; `/Users/mini-m4-main/.hermes/scripts`; `/Users/mini-m4-main/Library/LaunchAgents`; `/private/tmp/hermes-canary-stage/commands.py:903-1098`; `/private/tmp/hermes-canary-stage/tools/deployment_parity.py:257-447`.
  Acceptance criteria: staged version is immutable and manifest-backed; current pointer resolves directly to the version; managed roots/files are regular and canonical; rollback snapshot is complete and independently readable; staged active parity returns `ok=true` before transition.
  QA scenarios: happy: validate staged version/current/layout; failure: symlinked artifact, backup file, missing manifest, wrong plist args, old policy, or rollback hash mismatch prevents mutation. Evidence `.omo/evidence/task-8-hermes-completion.json`.
  Commit: N | Files: deployment candidate/state outside source repo.

- [ ] 9. Perform controlled launchd/deployment transition
  What to do: Obtain and record explicit operator approval at the mutation gate. Re-probe both launchd domains immediately before mutation; fail on ambiguity/unknown errors. Boot out all legacy mutator labels and unsafe health label, verify they are absent, then bootstrap exactly the canonical Fala plist in its permitted domain, preserving detected domain and recording every command/result. If any step is ambiguous or fails, restore the prior pointer/files and launchd state from the rollback snapshot; never bootstrap Fala while legacy mutators remain loaded.
  Must NOT do: Do not run the active health script, use legacy `--repair`, load both Fala and legacy mutators, or mutate unrelated services.
  Parallelization: Can parallel: N | Wave 4 | Blocks 10 | Blocked by 7, 8, operator approval
  References: `/private/tmp/hermes-canary-stage/commands.py:925-1242`; staged health/status launchctl helpers; installed managed plist paths; rollback snapshot from todo 8.
  Acceptance criteria: legacy labels absent in both domains; Fala is loaded in exactly one domain with canonical plist; no unknown launchctl result; active current/manifest/policy/parity gates pass; rollback path is tested in a disposable fixture and available for live failure.
  QA scenarios: happy: controlled transition then direct `launchctl print` and plist/manifest checks; failure: simulated bootout/bootstrap failure or dual-domain load triggers rollback and leaves no ambiguous active state. Evidence `.omo/evidence/task-9-hermes-completion.json`.
  Commit: N | Files: deployment/launchd state outside source repo.

- [ ] 10. Verify runtime, commit, push, and record outcome
  What to do: Run observational health and status, Fala DB integrity/freshness/process checks, source-to-active parity, launchd-domain probes, and one bounded Fala tick smoke with external writable state. Verify `origin/main` contains the intended integration commit, deployed manifest plugin/Fala hashes match source, no unrelated LaunchAgent/file changed, cleanup receipt is exact, and rollback metadata exists. Commit and push only the intended integration changes; update the durable canary outcome from incomplete only if every acceptance gate passes.
  Must NOT do: Do not call the deployment successful on candidate-only evidence, do not suppress failed health/status, and do not leave generated state in the source repository.
  Parallelization: Can parallel: N | Wave 5 | Blocks final audit | Blocked by 9
  References: staged health/status/parity tools; deployment manifest; cleanup receipt; GitHub merge receipt; `/Users/mini-m4-main/Developer/hermes-plugin-oss-repo-agent/.omo/canary-outcome-2026-07-19.md`.
  Acceptance criteria: health/status/parity all exit zero; Fala tick is live only under safe policy and produces expected durable DB evidence; `origin/main` and deployed plugin commit match; all receipts read back; final outcome says complete only with no blockers, otherwise remains incomplete with exact evidence.
  QA scenarios: happy: direct post-transition smoke and hash comparison; failure: stale DB, non-live mode, nonzero Fala exit, unresolved run, drift, unknown launchd state, or missing receipt prevents completion claim. Evidence `.omo/evidence/task-10-hermes-completion.json`.
  Commit: Y | `chore(repo-agent): complete fail-closed canary deployment` | Files: intended integration source and tests only; runtime/deployment receipts remain outside Git.

## Final verification wave (after ALL todos)
> Runs in parallel. ALL must APPROVE. No success claim is made until every report is present.
- [ ] F1. Plan compliance audit — compare every changed path and mutation against this plan and dirty-worktree preservation record.
- [ ] F2. Code quality review — inspect changed symbols, manifest schema, launchd domain handling, rollback, and receipt durability; reject unhandled ambiguity.
- [ ] F3. Real manual QA — direct GitHub/receipt/deployment/launchd/health/parity smoke with exact output and no broad suite.
- [ ] F4. Scope fidelity — verify no historical/unrelated issue, branch, LaunchAgent, or user file was touched.

## Commit strategy
- Work from the clean merged production clone on its existing branch; do not create an agent/model-named branch.
- Keep code commits atomic by safety concern where practical, then use one final integration commit only if the repository convention requires it.
- Never commit candidate databases, logs, deployment versions, receipts, or launchd runtime state.
- Push only after focused tests, candidate validation, cleanup evidence, active parity, health, launchd, and rollback gates pass.
- If any gate fails, leave code committed only if it is safe and tested, but do not push/promote or claim completion; record the exact blocker.

## Success criteria
- The issue-8 canary remains fully evidenced and separate from remediation evidence.
- Safety/parity code is committed from the clean merged baseline with all focused tests passing.
- Candidate manifest explicitly proves code, Fala, runtime, artifact, and policy provenance.
- Cleanup has an exact durable receipt or a fully evidenced no-target reconciliation; no ambiguous cleanup is reported as success.
- Active deployment is immutable, parity-clean, manifest-backed, and policy-safe.
- Legacy mutators and repair health are absent; Fala is loaded in exactly one known launchd domain.
- Observational health/status and bounded runtime smoke pass.
- Origin, deployed code, manifests, receipts, and final outcome record agree.
- Otherwise final disposition remains `INCOMPLETE — NOT PROMOTED` with exact blockers.
