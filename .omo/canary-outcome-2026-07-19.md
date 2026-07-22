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

