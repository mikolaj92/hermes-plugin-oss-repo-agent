"""Allowlisted atomic Fala effectors used by Lokay correlation paths."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable


@dataclass(frozen=True)
class EffectorEntry:
    id: str
    domain: str  # intake | issue_to_pr | triage | repair | cleanup
    ref: str
    intent: str
    mutates: bool


# IDs are process-local names shared by standalone and auto_worker paths. The
# two add_issue_label implementations intentionally use domain-prefixed IDs.
EFFECTORS: tuple[EffectorEntry, ...] = (
    # intake
    EffectorEntry("read_open_issues", "intake", "lokay.steps.poll.read_open_issues", "Read open GitHub issues for one repository.", False),
    EffectorEntry("normalize_issue_rows", "intake", "lokay.steps.poll.normalize_issue_rows", "Normalize one open-issue response into rows.", False),
    EffectorEntry("filter_issue_eligibility", "intake", "lokay.steps.poll.filter_issue_eligibility", "Apply issue readiness and assignee eligibility policy.", False),
    EffectorEntry("select_issue_candidate", "intake", "lokay.steps.poll.select_issue_candidate", "Select one deterministic eligible issue candidate.", False),
    EffectorEntry("decide_issue_action", "intake", "lokay.steps.issue_direction.decide_issue_action", "Decide accept, rejection comment, or skip for an issue.", False),
    EffectorEntry("read_issue_comments", "intake", "lokay.steps.issue_direction.read_issue_comments", "Read comments for one issue.", False),
    EffectorEntry("decide_issue_comment", "intake", "lokay.steps.issue_direction.decide_issue_comment", "Decide whether the issue comment marker is present.", False),
    EffectorEntry("post_issue_comment", "intake", "lokay.steps.issue_direction.post_issue_comment", "Post one issue comment.", True),
    EffectorEntry("verify_issue_comment", "intake", "lokay.steps.issue_direction.verify_issue_comment", "Verify the issue comment marker by read-back.", False),
    EffectorEntry("reserve_claim_file", "intake", "lokay.steps.claim.reserve_claim_file", "Reserve one local issue claim file.", True),
    EffectorEntry("read_issue_claim_state", "intake", "lokay.steps.claim.read_issue_claim_state", "Read authoritative issue assignee and labels.", False),
    EffectorEntry("assign_issue", "intake", "lokay.steps.claim.assign_issue", "Assign one GitHub issue.", True),
    EffectorEntry("intake_add_issue_label", "intake", "lokay.steps.claim.add_issue_label", "Add one claim label to a GitHub issue.", True),
    EffectorEntry("verify_issue_claim", "intake", "lokay.steps.claim.verify_issue_claim", "Verify issue assignment and required labels.", False),
    EffectorEntry("build_issue_claim_result", "intake", "lokay.steps.claim.build_issue_claim_result", "Aggregate issue claim evidence and mutation results.", False),
    EffectorEntry("read_intake_tasks", "intake", "lokay.steps.kanban_intake.read_intake_tasks", "Read intake tasks from Kanban.", False),
    EffectorEntry("find_intake_marker", "intake", "lokay.steps.kanban_intake.find_intake_marker", "Find an exact intake task marker.", False),
    EffectorEntry("create_intake_task", "intake", "lokay.steps.kanban_intake.create_intake_task", "Create one Kanban intake task.", True),
    EffectorEntry("reconcile_intake_task", "intake", "lokay.steps.kanban_intake.reconcile_intake_task", "Reconcile an intake task after creation.", False),
    # issue_to_pr
    EffectorEntry("parse_issue_ref_from_task", "issue_to_pr", "lokay.steps.issue_to_pr.parse_issue_ref_from_task", "Parse repository, issue, and branch from a task.", False),
    EffectorEntry("issue_to_pr_add_issue_label", "issue_to_pr", "lokay.steps.issue_to_pr.add_issue_label", "Add one label to a GitHub issue.", True),
    EffectorEntry("aggregate_issue_label_results", "issue_to_pr", "lokay.steps.issue_to_pr.aggregate_issue_label_results", "Aggregate individual issue label results.", False),
    EffectorEntry("read_dispatch_tasks", "issue_to_pr", "lokay.steps.issue_to_pr.read_dispatch_tasks", "Read dispatch tasks from Kanban.", False),
    EffectorEntry("select_dispatch_task", "issue_to_pr", "lokay.steps.issue_to_pr.select_dispatch_task", "Select one ready dispatch task.", False),
    EffectorEntry("read_fix_tasks", "issue_to_pr", "lokay.steps.issue_to_pr.read_fix_tasks", "Read fix tasks from Kanban.", False),
    EffectorEntry("find_fix_task_marker", "issue_to_pr", "lokay.steps.issue_to_pr.find_fix_task_marker", "Find an exact fix-task marker.", False),
    EffectorEntry("create_fix_task", "issue_to_pr", "lokay.steps.issue_to_pr.create_fix_task", "Create one Kanban fix task.", True),
    EffectorEntry("reconcile_fix_task", "issue_to_pr", "lokay.steps.issue_to_pr.reconcile_fix_task", "Reconcile a fix task after creation.", False),
    EffectorEntry("read_task_for_completion", "issue_to_pr", "lokay.steps.issue_to_pr.read_task_for_completion", "Read one task before completion.", False),
    EffectorEntry("decide_task_completion", "issue_to_pr", "lokay.steps.issue_to_pr.decide_task_completion", "Decide whether a task should be completed.", False),
    EffectorEntry("complete_task", "issue_to_pr", "lokay.steps.issue_to_pr.complete_task", "Complete one Kanban task.", True),
    EffectorEntry("verify_task_completed", "issue_to_pr", "lokay.steps.issue_to_pr.verify_task_completed", "Verify a Kanban task is completed.", False),
    EffectorEntry("read_clone_preconditions", "issue_to_pr", "lokay.steps.issue_to_pr.read_clone_preconditions", "Read clone cleanliness and origin preconditions.", False),
    EffectorEntry("fetch_clone_origin", "issue_to_pr", "lokay.steps.issue_to_pr.fetch_clone_origin", "Fetch one clone origin.", True),
    EffectorEntry("read_base_ref", "issue_to_pr", "lokay.steps.issue_to_pr.read_base_ref", "Read the authoritative remote base ref.", False),
    EffectorEntry("read_worktree_inventory", "issue_to_pr", "lokay.steps.issue_to_pr.read_worktree_inventory", "Read controlled worktree inventory.", False),
    EffectorEntry("read_branch_provenance", "issue_to_pr", "lokay.steps.issue_to_pr.read_branch_provenance", "Read one branch ownership provenance record.", False),
    EffectorEntry("create_local_branch", "issue_to_pr", "lokay.steps.issue_to_pr.create_local_branch", "Create one local branch at the base ref.", True),
    EffectorEntry("write_branch_provenance", "issue_to_pr", "lokay.steps.issue_to_pr.write_branch_provenance", "Write one branch provenance record.", True),
    EffectorEntry("add_worktree", "issue_to_pr", "lokay.steps.issue_to_pr.add_worktree", "Add one confined git worktree.", True),
    EffectorEntry("verify_worktree_head", "issue_to_pr", "lokay.steps.issue_to_pr.verify_worktree_head", "Verify the new worktree head.", False),
    EffectorEntry("read_omp_preconditions", "issue_to_pr", "lokay.steps.issue_to_pr.read_omp_preconditions", "Read OMP worktree confinement preconditions.", False),
    EffectorEntry("invoke_omp", "issue_to_pr", "lokay.steps.issue_to_pr.invoke_omp", "Invoke one OMP worker in a worktree.", True),
    EffectorEntry("verify_omp_postconditions", "issue_to_pr", "lokay.steps.issue_to_pr.verify_omp_postconditions", "Verify OMP head and changed-path postconditions.", False),
    EffectorEntry("read_worktree_head", "issue_to_pr", "lokay.steps.issue_to_pr.read_worktree_head", "Read the worktree HEAD.", False),
    EffectorEntry("read_base_head", "issue_to_pr", "lokay.steps.issue_to_pr.read_base_head", "Read the base HEAD.", False),
    EffectorEntry("decide_branch_has_commits", "issue_to_pr", "lokay.steps.issue_to_pr.decide_branch_has_commits", "Decide whether a branch contains commits beyond base.", False),
    EffectorEntry("read_push_head", "issue_to_pr", "lokay.steps.issue_to_pr.read_push_head", "Read the exact local push head.", False),
    EffectorEntry("push_branch", "issue_to_pr", "lokay.steps.issue_to_pr.push_branch", "Push one branch to origin.", True),
    EffectorEntry("read_pushed_ref", "issue_to_pr", "lokay.steps.issue_to_pr.read_pushed_ref", "Read the pushed remote ref.", False),
    EffectorEntry("verify_push_oid", "issue_to_pr", "lokay.steps.issue_to_pr.verify_push_oid", "Verify local and remote push OIDs match.", False),
    EffectorEntry("read_open_pr_for_branch", "issue_to_pr", "lokay.steps.issue_to_pr.read_open_pr_for_branch", "Read open PRs for one branch.", False),
    EffectorEntry("decide_existing_pr", "issue_to_pr", "lokay.steps.issue_to_pr.decide_existing_pr", "Decide whether an existing PR can be reused.", False),
    EffectorEntry("create_pull_request", "issue_to_pr", "lokay.steps.issue_to_pr.create_pull_request", "Create one pull request.", True),
    EffectorEntry("reconcile_pull_request", "issue_to_pr", "lokay.steps.issue_to_pr.reconcile_pull_request", "Reconcile a pull request after uncertain creation.", False),
    EffectorEntry("normalize_pr_labels", "issue_to_pr", "lokay.steps.issue_to_pr.normalize_pr_labels", "Normalize the requested PR labels.", False),
    EffectorEntry("add_pr_label", "issue_to_pr", "lokay.steps.issue_to_pr.add_pr_label", "Add one label to a pull request.", True),
    EffectorEntry("aggregate_pr_label_results", "issue_to_pr", "lokay.steps.issue_to_pr.aggregate_pr_label_results", "Aggregate individual PR label results.", False),
    EffectorEntry("build_dispatch_receipt", "issue_to_pr", "lokay.steps.issue_to_pr.build_dispatch_receipt", "Build the canonical dispatch receipt payload.", False),
    EffectorEntry("publish_dispatch_receipt", "issue_to_pr", "lokay.steps.issue_to_pr.publish_dispatch_receipt", "Publish one dispatch receipt.", True),
    EffectorEntry("verify_dispatch_receipt", "issue_to_pr", "lokay.steps.issue_to_pr.verify_dispatch_receipt", "Verify the published dispatch receipt.", False),
    EffectorEntry("check_worktree_dirty", "issue_to_pr", "lokay.steps.issue_to_pr.check_worktree_dirty", "Read whether a worktree is dirty.", False),
    EffectorEntry("list_controlled_worktrees", "issue_to_pr", "lokay.steps.issue_to_pr.list_controlled_worktrees", "Read controlled worktrees.", False),
    # triage
    EffectorEntry("read_open_prs", "triage", "lokay.steps.triage.read_open_prs", "Read open PR rows for one repository.", False),
    EffectorEntry("filter_fix_prs", "triage", "lokay.steps.triage.filter_fix_prs", "Filter PR rows to AI fix branches.", False),
    EffectorEntry("select_fix_pr", "triage", "lokay.steps.triage.select_fix_pr", "Select one deterministic fix PR.", False),
    EffectorEntry("load_pr_fields", "triage", "lokay.steps.triage.load_pr_fields", "Read full PR fields for triage.", False),
    EffectorEntry("evaluate_checks", "triage", "lokay.steps.triage.evaluate_checks", "Interpret PR check evidence.", False),
    EffectorEntry("evaluate_test_evidence", "triage", "lokay.steps.triage.evaluate_test_evidence", "Interpret PR test evidence.", False),
    EffectorEntry("decide_triage_action", "triage", "lokay.steps.triage.decide_triage_action", "Decide merge, comment, repair, or skip.", False),
    EffectorEntry("read_pr_assignees", "triage", "lokay.steps.triage.read_pr_assignees", "Read PR assignees.", False),
    EffectorEntry("decide_pr_assignee", "triage", "lokay.steps.triage.decide_pr_assignee", "Decide whether PR assignment is needed.", False),
    EffectorEntry("assign_pr", "triage", "lokay.steps.triage.assign_pr", "Assign one pull request.", True),
    EffectorEntry("verify_pr_assignee", "triage", "lokay.steps.triage.verify_pr_assignee", "Verify PR assignment by read-back.", False),
    EffectorEntry("read_pr_comments", "triage", "lokay.steps.triage.read_pr_comments", "Read comments for one PR.", False),
    EffectorEntry("decide_pr_comment", "triage", "lokay.steps.triage.decide_pr_comment", "Decide whether a PR comment is needed.", False),
    EffectorEntry("post_pr_comment", "triage", "lokay.steps.triage.post_pr_comment", "Post one PR comment.", True),
    EffectorEntry("verify_pr_comment", "triage", "lokay.steps.triage.verify_pr_comment", "Verify the PR comment marker.", False),
    EffectorEntry("read_merge_preconditions", "triage", "lokay.steps.triage.read_merge_preconditions", "Read merge preconditions.", False),
    EffectorEntry("merge_pr", "triage", "lokay.steps.triage.merge_pr", "Merge one pull request.", True),
    EffectorEntry("read_merge_postcondition", "triage", "lokay.steps.triage.read_merge_postcondition", "Read authoritative merge postcondition.", False),
    EffectorEntry("verify_merge_provenance", "triage", "lokay.steps.triage.verify_merge_provenance", "Verify merge provenance.", False),
    EffectorEntry("verify_linked_merge_provenance", "triage", "lokay.steps.triage.verify_linked_merge_provenance", "Verify provenance before closing a linked issue.", False),
    EffectorEntry("read_linked_issue_state", "triage", "lokay.steps.triage.read_linked_issue_state", "Read linked issue state.", False),
    EffectorEntry("close_linked_issue", "triage", "lokay.steps.triage.close_linked_issue", "Close the linked GitHub issue.", True),
    EffectorEntry("verify_linked_issue_closed", "triage", "lokay.steps.triage.verify_linked_issue_closed", "Verify the linked issue is closed.", False),
    EffectorEntry("build_merge_receipt", "triage", "lokay.steps.triage.build_merge_receipt", "Build the canonical merge receipt payload.", False),
    EffectorEntry("read_receipt_merge_provenance", "triage", "lokay.steps.triage.read_receipt_merge_provenance", "Read fresh merge provenance for the receipt.", False),
    EffectorEntry("publish_merge_receipt", "triage", "lokay.steps.triage.publish_merge_receipt", "Publish one merge receipt.", True),
    EffectorEntry("verify_merge_receipt", "triage", "lokay.steps.triage.verify_merge_receipt", "Verify the published merge receipt.", False),
    # repair
    EffectorEntry("build_repair_prompt", "repair", "lokay.steps.repair.build_repair_prompt", "Build an OMP repair prompt.", False),
    EffectorEntry("read_review_tasks", "repair", "lokay.steps.repair.read_review_tasks", "Read review-fix tasks from Kanban.", False),
    EffectorEntry("find_review_marker", "repair", "lokay.steps.repair.find_review_marker", "Find an exact review-task marker.", False),
    EffectorEntry("create_review_task", "repair", "lokay.steps.repair.create_review_task", "Create one review-fix task.", True),
    EffectorEntry("reconcile_review_task", "repair", "lokay.steps.repair.reconcile_review_task", "Reconcile a review-fix task after creation.", False),
    EffectorEntry("read_task_for_block", "repair", "lokay.steps.repair.read_task_for_block", "Read one task before blocking.", False),
    EffectorEntry("decide_task_block", "repair", "lokay.steps.repair.decide_task_block", "Decide whether a task should be blocked.", False),
    EffectorEntry("block_task", "repair", "lokay.steps.repair.block_task", "Block one Kanban task.", True),
    EffectorEntry("verify_task_blocked", "repair", "lokay.steps.repair.verify_task_blocked", "Verify a Kanban task is blocked.", False),
    # cleanup
    EffectorEntry("check_issue_closed", "cleanup", "lokay.steps.cleanup.check_issue_closed", "Read whether a linked issue is closed.", False),
    EffectorEntry("check_no_open_pr_for_branch", "cleanup", "lokay.steps.cleanup.check_no_open_pr_for_branch", "Read whether a branch has an open PR.", False),
    EffectorEntry("resolve_cleanup_branch_source", "cleanup", "lokay.steps.cleanup.resolve_cleanup_branch_source", "Resolve the authoritative cleanup branch source.", False),
    EffectorEntry("parse_cleanup_issue_number", "cleanup", "lokay.steps.cleanup.parse_cleanup_issue_number", "Parse the cleanup issue number.", False),
    EffectorEntry("read_branch_ownership", "cleanup", "lokay.steps.cleanup.read_branch_ownership", "Read one branch ownership key.", False),
    EffectorEntry("derive_cleanup_paths", "cleanup", "lokay.steps.cleanup.derive_cleanup_paths", "Derive confined cleanup paths.", False),
    EffectorEntry("validate_cleanup_identity", "cleanup", "lokay.steps.cleanup.validate_cleanup_identity", "Validate cleanup ownership identity.", False),
    EffectorEntry("verify_cleanup_guards", "cleanup", "lokay.steps.cleanup.verify_cleanup_guards", "Verify cleanup close and open-PR guards.", False),
    EffectorEntry("read_worktree_ownership", "cleanup", "lokay.steps.cleanup.read_worktree_ownership", "Read worktree ownership before removal.", False),
    EffectorEntry("read_worktree_cleanliness", "cleanup", "lokay.steps.cleanup.read_worktree_cleanliness", "Read worktree cleanliness.", False),
    EffectorEntry("remove_worktree", "cleanup", "lokay.steps.cleanup.remove_worktree", "Remove one controlled worktree.", True),
    EffectorEntry("verify_worktree_absent", "cleanup", "lokay.steps.cleanup.verify_worktree_absent", "Verify a worktree is absent.", False),
    EffectorEntry("verify_branch_delete_guards", "cleanup", "lokay.steps.cleanup.verify_branch_delete_guards", "Verify local branch deletion guards.", False),
    EffectorEntry("read_local_branch_ownership", "cleanup", "lokay.steps.cleanup.read_local_branch_ownership", "Read local branch ownership.", False),
    EffectorEntry("delete_local_branch", "cleanup", "lokay.steps.cleanup.delete_local_branch", "Delete one owned local branch.", True),
    EffectorEntry("verify_local_branch_absent", "cleanup", "lokay.steps.cleanup.verify_local_branch_absent", "Verify a local branch is absent.", False),
    EffectorEntry("verify_claim_release_evidence", "cleanup", "lokay.steps.cleanup.verify_claim_release_evidence", "Verify evidence permits claim release.", False),
    EffectorEntry("read_claim_identity", "cleanup", "lokay.steps.cleanup.read_claim_identity", "Read one claim identity.", False),
    EffectorEntry("release_claim_file", "cleanup", "lokay.steps.cleanup.release_claim_file", "Release one claim file.", True),
    EffectorEntry("verify_claim_absent", "cleanup", "lokay.steps.cleanup.verify_claim_absent", "Verify a claim file is absent.", False),
    EffectorEntry("collect_cleanup_receipt_evidence", "cleanup", "lokay.steps.cleanup.collect_cleanup_receipt_evidence", "Collect canonical cleanup receipt evidence.", False),
    EffectorEntry("decide_cleanup_outcome", "cleanup", "lokay.steps.cleanup.decide_cleanup_outcome", "Decide the cleanup receipt outcome.", False),
    EffectorEntry("build_cleanup_receipt", "cleanup", "lokay.steps.cleanup.build_cleanup_receipt", "Build the canonical cleanup receipt.", False),
    EffectorEntry("publish_cleanup_receipt", "cleanup", "lokay.steps.cleanup.publish_cleanup_receipt", "Publish one cleanup receipt.", True),
    EffectorEntry("verify_cleanup_receipt", "cleanup", "lokay.steps.cleanup.verify_cleanup_receipt", "Verify the published cleanup receipt.", False),
    EffectorEntry("read_maintenance_tasks", "cleanup", "lokay.steps.cleanup.read_maintenance_tasks", "Read maintenance tasks from Kanban.", False),
    EffectorEntry("find_maintenance_marker", "cleanup", "lokay.steps.cleanup.find_maintenance_marker", "Find an exact maintenance marker.", False),
    EffectorEntry("create_maintenance_task", "cleanup", "lokay.steps.cleanup.create_maintenance_task", "Create one maintenance task.", True),
    EffectorEntry("reconcile_maintenance_task", "cleanup", "lokay.steps.cleanup.reconcile_maintenance_task", "Reconcile a maintenance task after creation.", False),
    # no-target cleanup reconciliation
    EffectorEntry("validate_reconcile_identity", "cleanup", "lokay.steps.cleanup_reconcile.validate_reconcile_identity", "Validate no-target reconciliation identity.", False),
    EffectorEntry("read_local_receipts", "cleanup", "lokay.steps.cleanup_reconcile.read_local_receipts", "Read local cleanup receipts.", False),
    EffectorEntry("read_claim_process_evidence", "cleanup", "lokay.steps.cleanup_reconcile.read_claim_process_evidence", "Read claim and process evidence.", False),
    EffectorEntry("read_github_terminal_state", "cleanup", "lokay.steps.cleanup_reconcile.read_github_terminal_state", "Read GitHub terminal state.", False),
    EffectorEntry("read_remote_provenance", "cleanup", "lokay.steps.cleanup_reconcile.read_remote_provenance", "Read remote branch provenance.", False),
    EffectorEntry("read_reconcile_worktree_state", "cleanup", "lokay.steps.cleanup_reconcile.read_reconcile_worktree_state", "Read reconciliation worktree state.", False),
    EffectorEntry("decide_no_target_reconciliation", "cleanup", "lokay.steps.cleanup_reconcile.decide_no_target_reconciliation", "Decide whether no-target reconciliation is safe.", False),
    EffectorEntry("update_task_receipt", "cleanup", "lokay.steps.cleanup_reconcile.update_task_receipt", "Update the terminal task receipt.", True),
    EffectorEntry("publish_reconcile_receipt", "cleanup", "lokay.steps.cleanup_reconcile.publish_reconcile_receipt", "Publish a no-target reconciliation receipt.", True),
    EffectorEntry("verify_no_target_reconciliation", "cleanup", "lokay.steps.cleanup_reconcile.verify_no_target_reconciliation", "Verify no-target reconciliation postconditions.", False),
    # approved protocol-boundary helpers
    EffectorEntry("adapters_run_cmd", "issue_to_pr", "lokay.adapters_cli.run_cmd", "Run one external command through the CLI adapter.", True),
    EffectorEntry("adapters_gh_json", "intake", "lokay.adapters_cli.gh_json", "Read one GitHub JSON response through the CLI adapter.", False),
    EffectorEntry("adapters_hermes_kanban_json", "intake", "lokay.adapters_cli.hermes_kanban_json", "Read one Kanban JSON response through the CLI adapter.", False),
)


def list_effectors() -> list[dict]:
    return [asdict(e) for e in EFFECTORS]


def domains() -> set[str]:
    return {e.domain for e in EFFECTORS}


def by_domain(domain: str) -> list[EffectorEntry]:
    return [e for e in EFFECTORS if e.domain == domain]


def resolve(ref: str) -> Callable:
    """Import a shipped effector by catalog ref."""
    from importlib import import_module

    mod_name, _, attr = ref.rpartition(".")
    return getattr(import_module(mod_name), attr)


def load_all() -> dict[str, Callable]:
    return {e.id: resolve(e.ref) for e in EFFECTORS}
