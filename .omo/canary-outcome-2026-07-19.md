# Hermes OSS repo-agent live canary disposition

**Disposition: INCOMPLETE — NOT PROMOTED**

This record separates the authorized live GitHub canary from deployment and cleanup gates. Authorization permitted the live canary, but did not waive fail-closed safety requirements. No production cutover is claimed.

## Completed canary phases

- Disposable issue `#8` was created in `mikolaj92/hermes-plugin-oss-repo-agent` with the `ai:ready` label and assigned to `mikolaj92`.
- Live intake claimed issue `#8`; live issue-to-PR dispatch created fixer task `t_ef58d201`.
- The fixer changed only `src/repo_agent/production_canary.py` and `tests/test_production_canary.py`, adding the deterministic marker `hermes-production-canary`.
- Focused worker evidence passed: `tests/test_production_canary.py` reported `1 passed`, and the module import emitted `hermes-production-canary`.
- PR `#9` merged into `main`:
  - head: `065b26c8fb610f9c60b4663856b16d5b4ac749c0`
  - merge: `49d21868ebd0f1ea3913efdcba6582a14e39f900`
  - GitHub checks completed successfully.
- Issue `#8` was closed at `2026-07-19T20:29:28Z`.
- Durable merge receipt `/Users/mini-m4-main/.hermes/state/repo-agent-merge-live/mikolaj92_hermes-plugin-oss-repo-agent-9.json` reached `ISSUE_CLOSED_CONFIRMED` and records `originMainSha=49d21868ebd0f1ea3913efdcba6582a14e39f900`.
- The clean production clone `/Users/mini-m4-main/Developer/hermes-repos/hermes-plugin-oss-repo-agent-live` was observed at that merge SHA with a clean worktree and `origin/main` aligned.

## Cleanup disposition

Cleanup remains **unproven and was not performed as a live mutation**.

- No terminal cleanup receipt exists under `/Users/mini-m4-main/.hermes/state/repo-agent-cleanup-live` or `/Users/mini-m4-main/.hermes/state/repo-agent-merge-live/cleanup`.
- The exact task receipt `/Users/mini-m4-main/.hermes/state/repo-agent-receipts/t-ef58d201-24ce97d83ed270f0.json` still reports `phase=PR_OPEN` and `outcome=pr-open`; its lock file remains present and worker liveness is unresolved.
- The merge receipt proves issue `#8`/PR `#9` merge and issue-closure provenance (`ISSUE_CLOSED_CONFIRMED`), but it is not a terminal cleanup receipt. It records the exact branch, task, merge SHA, and `originMainSha`; those identities were not converted into cleanup evidence.
- No local issue-`#8` branch or worktree was found. The expected remote branch was observed at head `065b26c8fb610f9c60b4663856b16d5b4ac749c0`, and no open PR was observed in the local audit. GitHub API reads returned HTTP `403`, so authoritative remote state remains unavailable.
- An isolated cleanup dry-run used temporary state and quarantined the merge receipt as `identity-mismatch`; it reported `processed=4 removed=0 skipped=4 failures=0`. It did not modify production cleanup state.
- No remote branch deletion, claim release, lease release, worktree removal, or live cleanup worker was issued.

## Candidate validation

Candidate `12de360b9b3fc2179283b884912e14185fd5348c3c84ae03342a26d256db1732` was validated without promotion:

- plugin commit: `8bc17611e22a11ff62535d693be6695d4c0ce9ed`
- Fala tag: `0.2.1`
- pinned Fala commit: `b5f8085f418010a9290613b86671d435551411a9`
- candidate validation: `ok=true`
- `plutil -lint`: passed
- final external-state smoke: `any_failed=false`, `mutated=false`
- candidate immutability, artifact hashes, source provenance, and project layout checks passed.

The candidate's embedded policy is fail-safe (`automerge=false`, human approval/checks/test evidence required, executor disabled). Candidate validation and smoke prove buildability and dry-run behavior only; they do not prove active deployment parity or production promotion.

## Staged safety review

The staged safety/parity tree `/private/tmp/hermes-canary-stage` remains uncommitted and separate from the authoritative dirty checkout. Its focused regression matrix passed:

- shell syntax and Python compilation passed;
- health/status tests: `16` passed;
- deployment-candidate tests: `12` passed;
- deployment-parity tests: `7` passed;
- `git diff --check` passed.

The read-only audits found that these staged safety changes are substantially absent from the authoritative checkout. They were not copied, merged, committed, deployed, or installed implicitly. The staged CLI's candidate-validation/config-loading behavior remains review-only and is not treated as a production fix.

## Promotion blockers

The proper active-scripts-root parity scan against `/Users/mini-m4-main/.hermes/scripts` failed with nine errors:

1. active scripts root is a symlink;
2. source backup artifact `repo_issue_to_pr_dispatch.sh.bak-20260719T094138` is unexpected;
3. the same backup artifact is present in the active version;
4. Fala launchd template `ProgramArguments` do not match the canonical contract;
5. active Fala plist `oss-repo-agent-fala-tick-all.plist` is missing;
6. installed health plist `ProgramArguments` mismatch the source contract;
7. installed intake plist `ProgramArguments` mismatch the source contract;
8. active config is a symlink rather than a private regular file;
9. active `runtime.sh` is an unexpected config-root artifact.

Additional independently observed blockers:

- installed health plist invokes `repo_agent_health.sh --repair`, uses `StartInterval=300`, and has `RunAtLoad=true`; the source template is observational with no repair argument, interval `600`, and `RunAtLoad=false`;
- no Fala launchd service is registered in the observed user domain;
- inspected active deployment versions have no `manifest.json` and no Fala plist;
- active configuration enables live execution with `automerge=true`, no human approval, no required checks/test evidence, and an enabled `omp` executor;
- the authoritative remediation checkout remains dirty (`57` modified paths and `14` untracked paths), so it is not an implicit deployment source;
- the trusted immutable `deployment/current` policy is unresolved: the installed layout is an old checkout-style tree, while strict staged parity requires a direct 64-hex version candidate and rejects other symlinked roots/artifacts;
- final operator approval for promotion is absent.

## Mutations explicitly not performed

No deployment promotion, current-pointer cutover, launchd bootstrap/load/unload/reload, Fala plist installation or registration, remote branch deletion, live cleanup, or production health execution was performed after the blockers were identified.

## Required disposition

Keep the canary outcome incomplete. Before any promotion is reconsidered, reconcile the immutable deployment/current architecture, remove backup artifacts from the managed inventory, align all managed templates and installed plists, establish candidate manifests and policy provenance, disable the active unsafe policy, prove cleanup with an exact durable receipt, and obtain explicit operator approval. Re-run active parity, observational health, launchd-domain checks, candidate validation, and post-promotion smoke only after those gates pass.
## Current fail-closed reconciliation

This addendum supersedes stale cleanup observations above where they conflict with the later read-only reconciliation. The canary remains separate from remediation and deployment state.

- No exact per-wave mutation-gate artifact or approval ID exists. The generic issue-to-main plan remains `status: awaiting-approval`; neither that plan nor the earlier canary authorization authorizes cleanup, deployment promotion, launchd mutation, or a push.
- The reviewed source changes are committed locally as `6dcb981056fefc1f535301d518c9417523b60a16` in the clean live clone. `origin/main` remains `49d21868ebd0f1ea3913efdcba6582a14e39f900`; no push occurred.
- Candidate `bb95b1351121201da778725f4c9c50eea1e372df4fc8eefd32509996c91c295a` validates successfully, is immutable, binds the safe automation/executor policy, and uses plugin commit `6dcb981056fefc1f535301d518c9417523b60a16` with pinned Fala `0.2.1` / `b5f8085f418010a9290613b86671d435551411a9`. Candidate validation is not deployment promotion.
- Active deployment parity fails: the current tree is an old symlinked version without a manifest and Fala plist; managed scripts/config roots are symlinked or drifted; backup/runtime artifacts remain; installed health still invokes `--repair`; and active policy remains live, automerge-enabled, approval/check/test-evidence-disabled, with the executor enabled.
- Launchd is not safe to transition: legacy labels are loaded in `user/501`, no Fala service is registered, and `gui/501` returns `rc=125` (`Domain does not support specified action`). This domain ambiguity fails closed.

**Disposition remains: INCOMPLETE — NOT PROMOTED.** No cleanup, deployment cutover, launchd mutation, production health execution, remote branch deletion, claim/lease mutation, or push is authorized by the evidence currently available.
## Final bounded reconciliation

- Immutable candidate `bb95b1351121201da778725f4c9c50eea1e372df4fc8eefd32509996c91c295a` completed an external-state full dry-run with exit code `0`; JSON reported `dry_run=true` and `any_failed=false`. Intake completed five ticks with zero eligible issues and all process outputs `mutated=false`; dispatch and triage found no repositories; cleanup was `noop` with six ticks, `no_branch`, and all outputs `mutated=false`. UV environment and SQLite state were external to the immutable candidate.
- Cleanup identity reconciliation failed closed: no terminal cleanup receipt, no positively matched issue-8 worktree/claim/lease postcondition, the task receipt remains `PR_OPEN`/`pr-open` with an empty lock artifact, and configured runtime paths diverge from the receipt paths. No cleanup mutation or terminal receipt was emitted.
- Launchd inventory is observationally complete for supported `user/501`: six legacy services remain loaded and enabled, including health with `--repair`; canonical Fala is absent. `gui/501` does not support the required print/probe action. No launchd mutator or health repair was run.
- Deployment parity, active policy, and rollback preconditions remain failed. The generic authorization is still `APPROVED_PENDING_PREFLIGHT_GATES`; no exact per-wave mutation gate or approval ID exists. The local source commit remains unpushed at `6dcb981056fefc1f535301d518c9417523b60a16`, with `origin/main` at `49d21868ebd0f1ea3913efdcba6582a14e39f900`.

**Final disposition: INCOMPLETE — NOT PROMOTED.** Cleanup, deployment promotion, launchd transition, production verification, and push remain unperformed because the fail-closed gates are not satisfied.

## Follow-up recheck 2026-07-20

- GitHub was rechecked with authenticated `gh`: issue `#8` remains `CLOSED`, PR `#9` remains `MERGED`, and the issue branch is currently observable at head `065b26c8fb610f9c60b4663856b16d5b4ac749c0`. This resolves the earlier transient `404` observation but does not establish cleanup.
- The task receipt still reports `phase=PR_OPEN` / `outcome=pr-open`; its `.lock` artifact remains, no worker process is running, no issue-8 controlled worktree is present, and no terminal cleanup receipt exists. Cleanup therefore remains failed closed and unmutated.
- Re-run focused safety tests passed: `Ran 42 tests ... OK`.
- Active parity still fails, active policy remains unsafe, six legacy launchd services remain loaded in `user/501`, Fala remains absent, and `gui/501` still returns unsupported-domain error `125`. No production mutation was performed.

**Updated disposition: INCOMPLETE — NOT PROMOTED.**


## Administrative checklist disposition 2026-07-20

- The authorization artifact does contain declared wave identifiers: `wave-cleanup-2026-07-19`, `wave-deployment-2026-07-19`, `wave-launchd-2026-07-19`, `wave-verification-2026-07-19`, and `wave-push-2026-07-19`. These identifiers are not evidence that the corresponding preflight gates passed; the artifact status remains `APPROVED_FOR_EXECUTION_PENDING_GATE_CHECKS`.
- The cleanup, deployment, launchd, and production-smoke actions were not completed. Their checklist items are administratively blocked, not successful.
- Parity and receipt verification was completed observationally and failed closed. The operational outcome is recorded here; no live health repair, launchd mutation, deployment cutover, cleanup mutation, branch deletion, or push occurred.

**Final disposition remains: INCOMPLETE — NOT PROMOTED.**


## Final verification 2026-07-22

**Final disposition: INCOMPLETE — NOT PROMOTED.**

This section supersedes every earlier disposition and blocker statement in this historical record.

- Issue `#8` and PR `#9` reconciliation completed; cleanup evidence remains preserved.
- The immutable deployment version `a6c859e69d46355d02ae7ee8eb5919d5f34199dc2d11c3f0742bc5e6e2df1e7e` was activated and resolved candidate validation passed with no errors.
- The installed Fala launch agent is loaded in `user/501` from the immutable version plist and its latest observed run exited `0`.
- Health passes with valid database integrity, a latest completed live run, no failed or waiting processes, and 82 unresolved historical runs reported observationally.
- Status correctly fails closed on those 82 unresolved historical `created` or failed runs. Because the acceptance contract requires health and status both to exit zero, promotion cannot be claimed until those historical runs are reconciled through an auditable supported transition.
- Repository verification after final repairs: 66 focused lifecycle/deployment/health tests passed; the two repaired shell regressions passed; shell and Python syntax checks passed; hygiene passed. The full suite was not clean because the local Fala checkout lacks source modules required by ten test imports.

## Final source hardening verification 2026-07-22

**Disposition: INCOMPLETE — NOT PROMOTED.**

- The final source hardening preserves existing journal metadata on new runs, rejects conflicting metadata on replay, and requires the latest run to be `completed` in both health and status gates.
- Focused runtime and health/status verification passed: `23` tests. The isolated adapter cwd-forwarding regression also passed.
- The full repository suite passed in the required Fala Mojo environment: `207` tests with `FALA_HOME=../Fala` under the sibling Fala Pixi environment. The same real-host intake test fails outside that environment because Mojo/Fala source discovery is unavailable; this is an environment prerequisite, not a source regression.
- Python and shell syntax checks, `git diff --check`, and repository hygiene passed.
- The immutable candidate `cfa07aa61e50b602add8dd1d1cc6c4b444fc12ce690e50749538d820f7486419` remains loaded in `user/501`; launchd reports four runs and latest exit code `0`. The latest journal run is `completed`, `live`, and has no failed or waiting processes.
- Promotion acceptance still fails closed: status reports `99` unresolved historical runs; health exits nonzero because available home-volume space is below the configured `5 GiB` minimum; and the repository smoke detects active-script/layout parity drift because the active scripts root resolves to the immutable version root without the managed script inventory.
- These operational blockers are not suppressed or repaired by the source hardening. No additional deployment cutover, launchd mutation, historical-run rewrite, or cleanup mutation is claimed.

The source changes may be committed and pushed to `main` under the operator's explicit instruction, but that push does not change the deployment disposition. Promotion remains incomplete until health, status, and active parity all exit zero and provenance is reverified.

## Promotion closure 2026-07-23

**Final disposition: PROMOTED.**

This section supersedes every earlier disposition and blocker statement in this historical record.

- Deployed plugin revision `c919c72749aa67ea6d07472ba063aee435029f06` equals the clean, pushed `main` and `origin/main`; sibling Fala is clean at pinned commit `69bc2ec9d4cdf61773114847c0c582fb2652296d` (`0.7.9`).
- Cleanup receipt `~/.hermes/state/repo-agent-merge-live/cleanup-outcomes/mikolaj92_hermes-plugin-oss-repo-agent-8-ai_fix_8-mikolaj92-hermes-plugin-oss-repo-agent-8-issue-mikolaj92-hermes-plugin-oss-repo-.json` records `NO_TARGET_RECONCILED` for issue `#8`, PR `#9`, task `t_ef58d201`, merge/origin-main SHA `49d21868ebd0f1ea3913efdcba6582a14e39f900`. That outcome string means no deletion target remained: GitHub confirms the reconciled end-state—issue `#8` closed, PR `#9` merged, and the exact remote branch absent—while the receipt preserves `remote_branch_deleted=false` because this cleanup run performed no deletion.
- Immutable candidate `d4d45d4df7a9afec57de381fa0ce54962ba2ed375599f4d90f151adcb966b40d` validates with no errors and binds the deployed plugin commit, pinned Fala, lock hash `490aa2c72faa1c9ae2cec7b6dfc1383cba96f705fee41736d8647fecbd398c7e`, and live policy provenance.
- `deployment/current` resolves to that immutable version. The drifted managed health script was installed from the same committed source; five-script/config/template parity then reported `ok=true` with no errors, and `bash scripts/repo_agent_smoke.sh` printed `repo-agent smoke ok`.
- Launchd topology contains exactly one repo-agent mutator: `com.mikolaj92.hermes.repo-agent-fala-tick-all` in `user/501`; all legacy mutator labels are absent and the latest launchd exit is `0`.
- Two incomplete manual invocations failed before exercising the worker: first `uv` tried to create `.venv` inside the immutable candidate, then Fala source discovery failed without `FALA_HOME`. A faithful invocation using all launchd environment paths (`UV_PROJECT_ENVIRONMENT`, `UV_CACHE_DIR`, `FALA_EFFECTOR_ROOT`, and `FALA_HOME`) plus its exact `WorkingDirectory` completed live run `auto-worker-20260723T131252Z-bfe01c66` with `37` successful ticks and no failed or waiting steps. These were invocation-environment errors, not a CPython 3.14.3 or deployed-worker regression.
- After the managed-script repair and worker smoke, final observational health and status were rerun. Both exited `0`: health reported `summary failures=0 warnings=1 repair=0`; status reported `Gate summary failures=0`, database integrity `ok`, schema `6`, zero unresolved runs, and zero failed or waiting processes.
- Focused deployment, parity, health/status, and receipt verification previously passed `58` tests; the full Fala-environment verification previously passed `207` tests.

One health warning remains non-blocking: the scheduled Hermes updater log is stale. Its launchd job is present with latest exit `0`; no acceptance failure remains.

## Post-promotion Definition of Done audit 2026-07-23

**Final disposition: INCOMPLETE — DoD NOT MET.**

This section supersedes the `PROMOTED` disposition above. The deployment passed its operational health, status, parity, and worker-smoke gates, but the required final F1–F4 verification wave did not approve it:

- **F1 — REJECT:** final deployed Fala/plugin provenance and the accumulated change set are not reconciled with the literal completion plan, and no path-by-path baseline proves preservation of the original dirty worktree.
- **F2 — REJECT:** `write_cleanup_receipt` in `src/repo_agent/steps/cleanup.py` publishes receipts with direct `Path.write_text()` rather than the repository's atomic, fsync-backed, no-clobber pattern. A crash, short write, or concurrent cleanup can lose, truncate, or overwrite terminal mutation evidence. Cleanup receipts also omit the Fala `process_id` supplied at the request boundary.
- **F3 — REJECT:** the final record summarizes manual QA, but does not retain exact command output and exit codes for every required GitHub, receipt, deployment, launchd, health, parity, and smoke check.
- **F4 — REJECT:** no contemporaneous pre/post inventory proves that the final live cutover did not touch unrelated branches, LaunchAgents, historical issue/PR state, or user files.

The known-weak deployed revision `c919c72749aa67ea6d07472ba063aee435029f06` remains `deployment/current` pending an explicit operator decision to keep it running temporarily or roll it back. Its continued presence is not acceptance of the receipt-durability risk and must not be represented as DoD completion.
