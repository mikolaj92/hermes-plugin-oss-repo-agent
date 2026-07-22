# Hermes repo-agent remediation

## TL;DR
> Summary: Harden the shell/Fala issue-to-PR, triage, claims, receipts, cleanup, parity, and deployment safety contracts without live mutations.
> Deliverables: canonical bridges; fail-closed provenance/claim/receipt/cleanup behavior; focused regression coverage; durability/parity/health verification.
> Effort: XL
> Risk: High - broad existing checkout changes and mutation-sensitive lifecycle state.

## Scope
### Must have
- Preserve current safety decisions: shell-only unattended execution, automerge disabled by default, checks/test evidence required, no destructive recovery.
- Repair active bridge structure and canonical conduction IDs, input precedence, aggregate failure handling, and provenance propagation.
- Harden active claim reuse/readback and add focused edge coverage.
- Add receipt durability and deployment/parity/health fault coverage while preserving metadata-only render/bootstrap behavior.
- Run only focused mocked/temp-fixture validation.

### Must NOT have
- No live GitHub mutation, launchd reload, deployment cutover, force push, production canary, full suite, formatter, or broad lint.
- No reset/clean/destructive recovery or wholesale recovery-copy replacement.

## Verification strategy
- Test decision: tests-after using unittest/pytest and deterministic mocks/temp fixtures.
- QA policy: every change has focused happy and failure-path assertions.
- Evidence: command output and focused test artifacts in the session.

## Execution strategy
### Waves
1. Reconcile and repair bridge structure and hardened triage fixtures.
2. Add provenance/claim regression coverage and repair claim state handling.
3. Add receipt, deployment durability, parity, and health fault coverage.
4. Run the complete focused validation matrix and final safety audit.

### Dependency matrix
| Work | Depends on | Blocks | Parallel |
|---|---|---|---|
| Bridges/triage | current source reads | focused bridge/triage tests | claims/deployment audit |
| Claims | current claim source | focused claim tests | receipt/deployment tests |
| Receipts/deployment | current command/script reads | final matrix | claims/bridges |
| Final matrix | all preceding repairs | completion | no |

## Todos
- [x] Repair bridge source structure, canonical IDs, and input-first precedence; assert aggregate failure semantics.
- [x] Update triage fixtures and add forged/mismatched provenance/readback regression cases.
- [x] Harden claim identity, authoritative reuse verification, uncertain mutation classification, and active-claim edge coverage.
- [x] Add Python and shell receipt durability/lifecycle fault coverage without weakening fail-closed behavior.
- [x] Add init/deployment fsync, metadata-only command, parity, and health cutover fault coverage.
- [x] Run focused syntax and behavioral validation; reconcile all failures against the hardened contracts.
- [x] Perform final scope/safety audit and preserve live-mutation blocks.

## Success criteria
- Focused tests pass for bridges, triage, cleanup, claims, deployment/parity/runtime/candidate behavior.
- Syntax checks pass for changed Python and shell files.
- No live mutation or cutover is performed; failures remain explicit and fail closed.
