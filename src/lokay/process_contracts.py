"""Immutable per-process adapter contracts for the twelve canonical paths.

Each process owns exactly one Fala correlation path with path_id == process_id.
Sibling mutation ownership is disjoint; aggregate aliases are rejected.
"""

from __future__ import annotations

from dataclasses import dataclass

from lokay.registry import PROCESS_GRAPH_CONTRACT, PROCESS_IDS

# Aggregate / composite identities that must never appear as process path IDs.
FORBIDDEN_PATH_ALIASES: frozenset[str] = frozenset(
    {
        "tick_all",
        "auto_worker",
        "issue_intake",
        "lifecycle_ok",
    }
)


@dataclass(frozen=True, slots=True)
class ProcessContract:
    """Exact ownership and invocation bounds for one catalog process."""

    process_id: str
    path_id: str
    allowed_effectors: tuple[str, ...]
    forbidden_sibling_effectors: tuple[str, ...]
    required_inputs: tuple[str, ...]
    predecessor_groups: tuple[tuple[str, ...], ...]
    output_receipts: tuple[str, ...]
    mutation_scopes: tuple[str, ...]
    lock_scope: str
    max_ticks: int

    def allows_effector(self, effector_id: str) -> bool:
        return effector_id in self.allowed_effectors

    def rejects_sibling_effector(self, effector_id: str) -> bool:
        return effector_id in self.forbidden_sibling_effectors


REPO_ISSUE_POLL_CONTRACT = ProcessContract(
    process_id='repo_issue_poll',
    path_id='repo_issue_poll',
    allowed_effectors=(
        'read_open_issues',
        'normalize_issue_rows',
    ),
    forbidden_sibling_effectors=(),
    required_inputs=(
        'repos',
        'limit',
    ),
    predecessor_groups=(),
    output_receipts=(
        'repo_poll',
        'issue_snapshot',
    ),
    mutation_scopes=('none',),
    lock_scope='poll/repo',
    max_ticks=8,
)

ISSUE_TRIAGE_CONTRACT = ProcessContract(
    process_id='issue_triage',
    path_id='issue_triage',
    allowed_effectors=(
        'read_triage_receipt_index',
        'select_triage_candidate',
        'reserve_triage_run_budget',
        'read_triage_issue_state',
        'read_triage_comments',
        'read_triage_repository_state',
        'build_triage_context',
        'classify_triage_issue',
        'verify_triage_repository_unchanged',
        'publish_triage_decision_receipt',
        'read_triage_canonical_issue',
        'read_triage_labels',
        'decide_triage_mutation',
    ),
    forbidden_sibling_effectors=(),
    required_inputs=(
        'repos',
        'issue_snapshot_or_child_handoff',
    ),
    predecessor_groups=(
        ('issue_snapshot',),
        ('child_handoff',),
    ),
    output_receipts=('issue_decision',),
    mutation_scopes=('decision',),
    lock_scope='issue/repo/number',
    max_ticks=24,
)

ISSUE_FEEDBACK_CONTRACT = ProcessContract(
    process_id='issue_feedback',
    path_id='issue_feedback',
    allowed_effectors=(
        'ensure_triage_label',
        'publish_triage_mutation_authorization',
        'mutate_triage_issue_labels',
        'post_triage_feedback',
        'verify_triage_feedback',
        'observe_triage_feedback',
        'publish_triage_feedback_receipt',
        'publish_triage_mutation_verification',
    ),
    forbidden_sibling_effectors=(
        'split_mixed_triage_issue',
        'publish_triage_close_authorization',
        'close_triage_issue',
        'verify_triage_issue_closed',
        'publish_triage_close_verification',
        'verify_triage_receipt',
        'build_triage_terminal',
        'filter_issue_eligibility',
        'select_issue_candidate',
        'decide_issue_priority',
        'decide_issue_action',
        'read_issue_comments',
        'decide_issue_comment',
        'post_issue_comment',
        'verify_issue_comment',
        'reserve_claim_file',
        'read_issue_claim_state',
        'assign_issue',
        'intake_add_issue_label',
        'verify_issue_claim',
        'build_issue_claim_result',
        'read_intake_tasks',
        'find_intake_marker',
        'create_intake_task',
        'reconcile_intake_task',
    ),
    required_inputs=('issue_decision',),
    predecessor_groups=(('issue_decision',),),
    output_receipts=(
        'feedback',
        'feedback_verified',
    ),
    mutation_scopes=(
        'issue_labels',
        'issue_comments',
    ),
    lock_scope='issue/repo/number',
    max_ticks=16,
)

ISSUE_SPLIT_CONTRACT = ProcessContract(
    process_id='issue_split',
    path_id='issue_split',
    allowed_effectors=('split_mixed_triage_issue',),
    forbidden_sibling_effectors=(
        'ensure_triage_label',
        'publish_triage_mutation_authorization',
        'mutate_triage_issue_labels',
        'post_triage_feedback',
        'verify_triage_feedback',
        'observe_triage_feedback',
        'publish_triage_feedback_receipt',
        'publish_triage_mutation_verification',
        'publish_triage_close_authorization',
        'close_triage_issue',
        'verify_triage_issue_closed',
        'publish_triage_close_verification',
        'verify_triage_receipt',
        'build_triage_terminal',
        'filter_issue_eligibility',
        'select_issue_candidate',
        'decide_issue_priority',
        'decide_issue_action',
        'read_issue_comments',
        'decide_issue_comment',
        'post_issue_comment',
        'verify_issue_comment',
        'reserve_claim_file',
        'read_issue_claim_state',
        'assign_issue',
        'intake_add_issue_label',
        'verify_issue_claim',
        'build_issue_claim_result',
        'read_intake_tasks',
        'find_intake_marker',
        'create_intake_task',
        'reconcile_intake_task',
    ),
    required_inputs=('issue_decision',),
    predecessor_groups=(('issue_decision',),),
    output_receipts=(
        'split',
        'child_handoff',
        'split_verified',
    ),
    mutation_scopes=('issue_create',),
    lock_scope='split/repo/number',
    max_ticks=8,
)

ISSUE_CLOSE_CONTRACT = ProcessContract(
    process_id='issue_close',
    path_id='issue_close',
    allowed_effectors=(
        'publish_triage_close_authorization',
        'close_triage_issue',
        'verify_triage_issue_closed',
        'publish_triage_close_verification',
        'verify_triage_receipt',
    ),
    forbidden_sibling_effectors=(
        'ensure_triage_label',
        'publish_triage_mutation_authorization',
        'mutate_triage_issue_labels',
        'post_triage_feedback',
        'verify_triage_feedback',
        'observe_triage_feedback',
        'publish_triage_feedback_receipt',
        'publish_triage_mutation_verification',
        'split_mixed_triage_issue',
        'build_triage_terminal',
        'filter_issue_eligibility',
        'select_issue_candidate',
        'decide_issue_priority',
        'decide_issue_action',
        'read_issue_comments',
        'decide_issue_comment',
        'post_issue_comment',
        'verify_issue_comment',
        'reserve_claim_file',
        'read_issue_claim_state',
        'assign_issue',
        'intake_add_issue_label',
        'verify_issue_claim',
        'build_issue_claim_result',
        'read_intake_tasks',
        'find_intake_marker',
        'create_intake_task',
        'reconcile_intake_task',
    ),
    required_inputs=('issue_decision_or_split_verified',),
    predecessor_groups=(
        ('issue_decision',),
        ('split_verified',),
    ),
    output_receipts=(
        'close_authorization',
        'close_verified',
    ),
    mutation_scopes=('issue_close',),
    lock_scope='issue/repo/number',
    max_ticks=12,
)

ISSUE_READY_CONTRACT = ProcessContract(
    process_id='issue_ready',
    path_id='issue_ready',
    allowed_effectors=(
        'build_triage_terminal',
        'filter_issue_eligibility',
        'select_issue_candidate',
        'decide_issue_priority',
        'decide_issue_action',
        'read_issue_comments',
        'decide_issue_comment',
        'post_issue_comment',
        'verify_issue_comment',
        'reserve_claim_file',
        'read_issue_claim_state',
        'assign_issue',
        'intake_add_issue_label',
        'verify_issue_claim',
        'build_issue_claim_result',
        'read_intake_tasks',
        'find_intake_marker',
        'create_intake_task',
        'reconcile_intake_task',
    ),
    forbidden_sibling_effectors=(
        'ensure_triage_label',
        'publish_triage_mutation_authorization',
        'mutate_triage_issue_labels',
        'post_triage_feedback',
        'verify_triage_feedback',
        'observe_triage_feedback',
        'publish_triage_feedback_receipt',
        'publish_triage_mutation_verification',
        'split_mixed_triage_issue',
        'publish_triage_close_authorization',
        'close_triage_issue',
        'verify_triage_issue_closed',
        'publish_triage_close_verification',
        'verify_triage_receipt',
    ),
    required_inputs=('issue_decision',),
    predecessor_groups=(('issue_decision',),),
    output_receipts=(
        'claim',
        'task_handoff',
    ),
    mutation_scopes=(
        'issue_claim',
        'issue_assign',
        'kanban_task',
    ),
    lock_scope='issue/repo/number',
    max_ticks=32,
)

ISSUE_TO_PR_CONTRACT = ProcessContract(
    process_id='issue_to_pr',
    path_id='issue_to_pr',
    allowed_effectors=(
        'read_dispatch_tasks',
        'select_dispatch_task',
        'parse_issue_ref_from_task',
        'read_merged_closing_prs',
        'decide_held_issue_already_merged',
        'read_fix_tasks',
        'find_fix_task_marker',
        'create_fix_task',
        'reconcile_fix_task',
        'read_clone_preconditions',
        'fetch_clone_origin',
        'read_base_ref',
        'read_worktree_inventory',
        'read_branch_provenance',
        'create_local_branch',
        'write_branch_provenance',
        'add_worktree',
        'verify_worktree_head',
        'read_omp_preconditions',
        'invoke_omp',
        'verify_omp_postconditions',
        'read_worktree_head',
        'read_base_head',
        'decide_branch_has_commits',
        'read_push_head',
        'push_branch',
        'read_pushed_ref',
        'verify_push_oid',
        'update_branch_local_oid',
        'verify_updated_branch_local_oid',
        'read_open_pr_for_branch',
        'decide_existing_pr',
        'create_pull_request',
        'reconcile_pull_request',
        'normalize_pr_labels',
        'add_pr_label',
        'aggregate_pr_label_results',
        'issue_to_pr_add_issue_label',
        'aggregate_issue_label_results',
        'build_dispatch_receipt',
        'publish_dispatch_receipt',
        'verify_dispatch_receipt',
        'read_task_for_completion',
        'decide_task_completion',
        'complete_task',
        'verify_task_completed',
    ),
    forbidden_sibling_effectors=(),
    required_inputs=(
        'claim',
        'task_handoff',
    ),
    predecessor_groups=((
            'claim',
            'task_handoff',
        ),),
    output_receipts=(
        'implementation',
        'pr_opened',
    ),
    mutation_scopes=(
        'git_worktree',
        'git_push',
        'pull_request',
        'kanban_task',
    ),
    lock_scope='task/board/id',
    max_ticks=69,
)

PR_TRIAGE_CONTRACT = ProcessContract(
    process_id='pr_triage',
    path_id='pr_triage',
    allowed_effectors=(
        'read_open_prs',
        'filter_fix_prs',
        'select_fix_pr',
        'load_pr_fields',
        'evaluate_checks',
        'evaluate_test_evidence',
        'decide_triage_action',
    ),
    forbidden_sibling_effectors=(
        'read_review_tasks',
        'find_review_marker',
        'create_review_task',
        'reconcile_review_task',
        'build_repair_prompt',
        'read_repair_context',
        'read_repair_remote_head',
        'fetch_repair_remote_head',
        'verify_fetched_repair_remote_head',
        'read_repair_worktree_inventory',
        'read_repair_branch_provenance',
        'read_repair_worktree_cleanliness',
        'read_repair_remote_ancestry',
        'decide_repair_worktree_fast_forward',
        'read_repair_worktree_branch_before_fast_forward',
        'read_repair_worktree_head_before_fast_forward',
        'read_repair_worktree_cleanliness_before_fast_forward',
        'decide_repair_worktree_fast_forward_execution',
        'read_repair_creation_evidence',
        'read_repair_attempt_state',
        'read_repair_base_head',
        'decide_legacy_repair_head_refresh',
        'update_legacy_repair_pr_branch',
        'verify_legacy_repair_pr_head',
        'fast_forward_repair_worktree',
        'decide_repair_worktree_ownership',
        'create_repair_branch',
        'write_repair_branch_provenance',
        'add_repair_worktree',
        'verify_repair_worktree',
        'read_repair_attempt_baseline',
        'read_repair_completed_receipt',
        'read_repair_attempt_reconciliation',
        'read_repair_attempt_recovery_evidence',
        'claim_repair_attempt_recovery',
        'verify_repair_attempt_recovery',
        'read_repair_recovery_continuation_evidence',
        'claim_repair_recovery_continuation',
        'verify_repair_recovery_continuation',
        'decide_repair_attempt',
        'reserve_repair_attempt',
        'verify_repair_attempt_reservation',
        'read_repair_omp_preconditions',
        'invoke_repair_omp',
        'verify_repair_omp_postconditions',
        'read_repair_worktree_head',
        'decide_repair_push',
        'push_repair_branch',
        'read_repair_pushed_ref',
        'verify_repair_push_oid',
        'read_existing_repair_pr',
        'verify_existing_repair_pr',
        'build_repair_receipt',
        'publish_repair_receipt',
        'verify_repair_receipt',
        'update_repair_branch_provenance',
        'verify_updated_repair_branch_provenance',
        'read_pr_assignees',
        'decide_pr_assignee',
        'assign_pr',
        'verify_pr_assignee',
        'read_pr_comments',
        'decide_pr_comment',
        'post_pr_comment',
        'verify_pr_comment',
        'read_merge_preconditions',
        'merge_pr',
        'read_merge_postcondition',
        'verify_merge_provenance',
        'verify_linked_merge_provenance',
        'read_linked_issue_state',
        'close_linked_issue',
        'verify_linked_issue_closed',
        'build_merge_receipt',
        'read_receipt_merge_provenance',
        'publish_merge_receipt',
        'verify_merge_receipt',
    ),
    required_inputs=(
        'repos',
        'pr_opened_or_repair_verified',
    ),
    predecessor_groups=(
        ('pr_opened',),
        ('repair_verified',),
    ),
    output_receipts=('pr_decision',),
    mutation_scopes=('decision',),
    lock_scope='pr/repo/number',
    max_ticks=16,
)

PR_REPAIR_CONTRACT = ProcessContract(
    process_id='pr_repair',
    path_id='pr_repair',
    allowed_effectors=(
        'read_review_tasks',
        'find_review_marker',
        'create_review_task',
        'reconcile_review_task',
        'build_repair_prompt',
        'read_repair_context',
        'read_repair_remote_head',
        'fetch_repair_remote_head',
        'verify_fetched_repair_remote_head',
        'read_repair_worktree_inventory',
        'read_repair_branch_provenance',
        'read_repair_worktree_cleanliness',
        'read_repair_remote_ancestry',
        'decide_repair_worktree_fast_forward',
        'read_repair_worktree_branch_before_fast_forward',
        'read_repair_worktree_head_before_fast_forward',
        'read_repair_worktree_cleanliness_before_fast_forward',
        'decide_repair_worktree_fast_forward_execution',
        'read_repair_creation_evidence',
        'read_repair_attempt_state',
        'read_repair_base_head',
        'decide_legacy_repair_head_refresh',
        'update_legacy_repair_pr_branch',
        'verify_legacy_repair_pr_head',
        'fast_forward_repair_worktree',
        'decide_repair_worktree_ownership',
        'create_repair_branch',
        'write_repair_branch_provenance',
        'add_repair_worktree',
        'verify_repair_worktree',
        'read_repair_attempt_baseline',
        'read_repair_completed_receipt',
        'read_repair_attempt_reconciliation',
        'read_repair_attempt_recovery_evidence',
        'claim_repair_attempt_recovery',
        'verify_repair_attempt_recovery',
        'read_repair_recovery_continuation_evidence',
        'claim_repair_recovery_continuation',
        'verify_repair_recovery_continuation',
        'decide_repair_attempt',
        'reserve_repair_attempt',
        'verify_repair_attempt_reservation',
        'read_repair_omp_preconditions',
        'invoke_repair_omp',
        'verify_repair_omp_postconditions',
        'read_repair_worktree_head',
        'decide_repair_push',
        'push_repair_branch',
        'read_repair_pushed_ref',
        'verify_repair_push_oid',
        'read_existing_repair_pr',
        'verify_existing_repair_pr',
        'build_repair_receipt',
        'publish_repair_receipt',
        'verify_repair_receipt',
        'update_repair_branch_provenance',
        'verify_updated_repair_branch_provenance',
    ),
    forbidden_sibling_effectors=(
        'read_open_prs',
        'filter_fix_prs',
        'select_fix_pr',
        'load_pr_fields',
        'evaluate_checks',
        'evaluate_test_evidence',
        'decide_triage_action',
        'read_pr_assignees',
        'decide_pr_assignee',
        'assign_pr',
        'verify_pr_assignee',
        'read_pr_comments',
        'decide_pr_comment',
        'post_pr_comment',
        'verify_pr_comment',
        'read_merge_preconditions',
        'merge_pr',
        'read_merge_postcondition',
        'verify_merge_provenance',
        'verify_linked_merge_provenance',
        'read_linked_issue_state',
        'close_linked_issue',
        'verify_linked_issue_closed',
        'build_merge_receipt',
        'read_receipt_merge_provenance',
        'publish_merge_receipt',
        'verify_merge_receipt',
    ),
    required_inputs=('pr_decision',),
    predecessor_groups=(('pr_decision',),),
    output_receipts=(
        'repair_reservation',
        'repair_verified',
    ),
    mutation_scopes=(
        'git_worktree',
        'git_push',
        'pull_request',
        'omp_repair',
        'kanban_task',
    ),
    lock_scope='repair/repo/number/head',
    max_ticks=85,
)

PR_MERGE_CONTRACT = ProcessContract(
    process_id='pr_merge',
    path_id='pr_merge',
    allowed_effectors=(
        'read_pr_assignees',
        'decide_pr_assignee',
        'assign_pr',
        'verify_pr_assignee',
        'read_pr_comments',
        'decide_pr_comment',
        'post_pr_comment',
        'verify_pr_comment',
        'read_merge_preconditions',
        'merge_pr',
        'read_merge_postcondition',
        'verify_merge_provenance',
        'verify_linked_merge_provenance',
        'read_linked_issue_state',
        'close_linked_issue',
        'verify_linked_issue_closed',
        'build_merge_receipt',
        'read_receipt_merge_provenance',
        'publish_merge_receipt',
        'verify_merge_receipt',
    ),
    forbidden_sibling_effectors=(
        'read_open_prs',
        'filter_fix_prs',
        'select_fix_pr',
        'load_pr_fields',
        'evaluate_checks',
        'evaluate_test_evidence',
        'decide_triage_action',
        'read_review_tasks',
        'find_review_marker',
        'create_review_task',
        'reconcile_review_task',
        'build_repair_prompt',
        'read_repair_context',
        'read_repair_remote_head',
        'fetch_repair_remote_head',
        'verify_fetched_repair_remote_head',
        'read_repair_worktree_inventory',
        'read_repair_branch_provenance',
        'read_repair_worktree_cleanliness',
        'read_repair_remote_ancestry',
        'decide_repair_worktree_fast_forward',
        'read_repair_worktree_branch_before_fast_forward',
        'read_repair_worktree_head_before_fast_forward',
        'read_repair_worktree_cleanliness_before_fast_forward',
        'decide_repair_worktree_fast_forward_execution',
        'read_repair_creation_evidence',
        'read_repair_attempt_state',
        'read_repair_base_head',
        'decide_legacy_repair_head_refresh',
        'update_legacy_repair_pr_branch',
        'verify_legacy_repair_pr_head',
        'fast_forward_repair_worktree',
        'decide_repair_worktree_ownership',
        'create_repair_branch',
        'write_repair_branch_provenance',
        'add_repair_worktree',
        'verify_repair_worktree',
        'read_repair_attempt_baseline',
        'read_repair_completed_receipt',
        'read_repair_attempt_reconciliation',
        'read_repair_attempt_recovery_evidence',
        'claim_repair_attempt_recovery',
        'verify_repair_attempt_recovery',
        'read_repair_recovery_continuation_evidence',
        'claim_repair_recovery_continuation',
        'verify_repair_recovery_continuation',
        'decide_repair_attempt',
        'reserve_repair_attempt',
        'verify_repair_attempt_reservation',
        'read_repair_omp_preconditions',
        'invoke_repair_omp',
        'verify_repair_omp_postconditions',
        'read_repair_worktree_head',
        'decide_repair_push',
        'push_repair_branch',
        'read_repair_pushed_ref',
        'verify_repair_push_oid',
        'read_existing_repair_pr',
        'verify_existing_repair_pr',
        'build_repair_receipt',
        'publish_repair_receipt',
        'verify_repair_receipt',
        'update_repair_branch_provenance',
        'verify_updated_repair_branch_provenance',
    ),
    required_inputs=('pr_decision',),
    predecessor_groups=(('pr_decision',),),
    output_receipts=(
        'merge_verified',
        'finalization',
    ),
    mutation_scopes=(
        'pr_assign',
        'pr_comment',
        'pr_merge',
        'issue_close',
    ),
    lock_scope='merge/repo/number/head',
    max_ticks=32,
)

CLEANUP_CONTRACT = ProcessContract(
    process_id='cleanup',
    path_id='cleanup',
    allowed_effectors=(
        'resolve_cleanup_branch_source',
        'parse_cleanup_issue_number',
        'read_branch_ownership',
        'derive_cleanup_paths',
        'validate_cleanup_identity',
        'check_issue_closed',
        'check_no_open_pr_for_branch',
        'verify_cleanup_guards',
        'read_worktree_ownership',
        'read_worktree_cleanliness',
        'remove_worktree',
        'verify_worktree_absent',
        'verify_branch_delete_guards',
        'read_local_branch_ownership',
        'delete_local_branch',
        'verify_local_branch_absent',
        'verify_claim_release_evidence',
        'read_claim_identity',
        'release_claim_file',
        'verify_claim_absent',
        'collect_cleanup_receipt_evidence',
        'decide_cleanup_outcome',
        'build_cleanup_receipt',
        'publish_cleanup_receipt',
        'verify_cleanup_receipt',
        'read_maintenance_tasks',
        'find_maintenance_marker',
        'create_maintenance_task',
        'reconcile_maintenance_task',
    ),
    forbidden_sibling_effectors=(),
    required_inputs=('finalization',),
    predecessor_groups=(('finalization',),),
    output_receipts=('cleanup_verified',),
    mutation_scopes=(
        'worktree_remove',
        'branch_delete',
        'claim_release',
        'kanban_task',
    ),
    lock_scope='cleanup/repo/number/head',
    max_ticks=43,
)

CLEANUP_RECONCILE_CONTRACT = ProcessContract(
    process_id='cleanup_reconcile',
    path_id='cleanup_reconcile',
    allowed_effectors=(
        'validate_reconcile_identity',
        'read_local_receipts',
        'read_claim_process_evidence',
        'read_github_terminal_state',
        'read_remote_provenance',
        'read_reconcile_worktree_state',
        'decide_no_target_reconciliation',
        'update_task_receipt',
        'publish_reconcile_receipt',
        'verify_no_target_reconciliation',
    ),
    forbidden_sibling_effectors=(),
    required_inputs=('unresolved_cleanup_evidence',),
    predecessor_groups=(('unresolved cleanup evidence',),),
    output_receipts=('cleanup_reconciliation',),
    mutation_scopes=(
        'task_receipt',
        'reconcile',
    ),
    lock_scope='cleanup_reconcile/subject',
    max_ticks=15,
)

# Explicit twelve-contract registry in canonical PROCESS_IDS order.
PROCESS_CONTRACTS: dict[str, ProcessContract] = {
    'repo_issue_poll': REPO_ISSUE_POLL_CONTRACT,
    'issue_triage': ISSUE_TRIAGE_CONTRACT,
    'issue_feedback': ISSUE_FEEDBACK_CONTRACT,
    'issue_split': ISSUE_SPLIT_CONTRACT,
    'issue_close': ISSUE_CLOSE_CONTRACT,
    'issue_ready': ISSUE_READY_CONTRACT,
    'issue_to_pr': ISSUE_TO_PR_CONTRACT,
    'pr_triage': PR_TRIAGE_CONTRACT,
    'pr_repair': PR_REPAIR_CONTRACT,
    'pr_merge': PR_MERGE_CONTRACT,
    'cleanup': CLEANUP_CONTRACT,
    'cleanup_reconcile': CLEANUP_RECONCILE_CONTRACT,
}

PROCESS_CONTRACT_LIST: tuple[ProcessContract, ...] = tuple(
    PROCESS_CONTRACTS[process_id] for process_id in PROCESS_IDS
)


def contract_for(process_id: str) -> ProcessContract:
    """Return the immutable contract for a canonical process id."""
    try:
        return PROCESS_CONTRACTS[process_id]
    except KeyError as exc:
        raise KeyError(f"unknown process contract: {process_id}") from exc


def validate_contract_registry() -> None:
    """Fail closed when the contract map drifts from the catalog process set."""
    if tuple(PROCESS_CONTRACTS) != PROCESS_IDS:
        raise RuntimeError(
            "PROCESS_CONTRACTS keys must match PROCESS_IDS order exactly: "
            f"got {tuple(PROCESS_CONTRACTS)!r}, expected {PROCESS_IDS!r}"
        )
    if len(PROCESS_CONTRACTS) != 12:
        raise RuntimeError("exactly twelve process contracts are required")
    for process_id, contract in PROCESS_CONTRACTS.items():
        if contract.process_id != process_id:
            raise RuntimeError(f"contract process_id mismatch for {process_id}")
        if contract.path_id != process_id:
            raise RuntimeError(
                f"path_id must equal process_id for {process_id}: {contract.path_id!r}"
            )
        if contract.path_id in FORBIDDEN_PATH_ALIASES:
            raise RuntimeError(f"forbidden path alias registered: {contract.path_id}")
        if not contract.allowed_effectors:
            raise RuntimeError(f"contract {process_id} has empty allowed_effectors")
        if contract.max_ticks < len(contract.allowed_effectors):
            raise RuntimeError(
                f"contract {process_id} max_ticks {contract.max_ticks} "
                f"cannot cover {len(contract.allowed_effectors)} effectors"
            )
        graph_row = PROCESS_GRAPH_CONTRACT[process_id]
        if contract.output_receipts != tuple(graph_row["output_receipts"]):
            raise RuntimeError(f"output_receipts drift for {process_id}")
        if contract.predecessor_groups != tuple(
            tuple(group) for group in graph_row["predecessor_groups"]
        ):
            raise RuntimeError(f"predecessor_groups drift for {process_id}")
        overlap = set(contract.allowed_effectors) & set(
            contract.forbidden_sibling_effectors
        )
        if overlap:
            raise RuntimeError(
                f"contract {process_id} allows forbidden sibling effectors: "
                f"{sorted(overlap)}"
            )
    for group in (
        ("issue_feedback", "issue_split", "issue_close", "issue_ready"),
        ("pr_triage", "pr_repair", "pr_merge"),
    ):
        for index, left in enumerate(group):
            left_set = set(PROCESS_CONTRACTS[left].allowed_effectors)
            for right in group[index + 1 :]:
                right_set = set(PROCESS_CONTRACTS[right].allowed_effectors)
                shared = left_set & right_set
                if shared:
                    raise RuntimeError(
                        f"sibling effector ownership overlap {left}/{right}: "
                        f"{sorted(shared)}"
                    )


validate_contract_registry()
