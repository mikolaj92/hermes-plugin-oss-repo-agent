"""Complete inventory of mega-atomic Fala effectors for later path composition."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable


@dataclass(frozen=True)
class EffectorEntry:
    id: str
    domain: str  # intake | issue_to_pr | triage | repair | cleanup
    ref: str
    intent: str
    mutates: bool  # may perform external writes when dry_run=false


# Domain-tagged catalog — composition happens later via correlation_paths.
EFFECTORS: tuple[EffectorEntry, ...] = (
    # --- intake (aligned existing) ---
    EffectorEntry(
        "poll_eligible_issues",
        "intake",
        "repo_agent.steps.poll.poll_eligible_issues",
        "List open GitHub issues eligible for intake (read-only gh).",
        False,
    ),
    EffectorEntry(
        "decide_issue_action",
        "intake",
        "repo_agent.steps.issue_direction.decide_issue_action",
        "Pure sense/direction gate: accept | reject_comment | skip.",
        False,
    ),
    EffectorEntry(
        "comment_issue_once",
        "intake",
        "repo_agent.steps.issue_direction.comment_issue_once",
        "Durable issue comment when direction rejects (never silent drop).",
        True,
    ),
    EffectorEntry(
        "claim_github_issue",
        "intake",
        "repo_agent.steps.claim.claim_github_issue",
        "Assign/label one selected GitHub issue.",
        True,
    ),
    EffectorEntry(
        "ensure_kanban_intake",
        "intake",
        "repo_agent.steps.kanban_intake.ensure_kanban_intake",
        "Ensure idempotent Hermes Kanban [issue] task.",
        True,
    ),
    # --- issue_to_pr ---
    EffectorEntry(
        "load_kanban_task",
        "issue_to_pr",
        "repo_agent.steps.issue_to_pr.load_kanban_task",
        "Load one Kanban task (by id or first ready fix/issue).",
        False,
    ),
    EffectorEntry(
        "parse_issue_ref_from_task",
        "issue_to_pr",
        "repo_agent.steps.issue_to_pr.parse_issue_ref_from_task",
        "Pure parse of repo#issue and branch name from task.",
        False,
    ),
    EffectorEntry(
        "create_fix_pr_task",
        "issue_to_pr",
        "repo_agent.steps.issue_to_pr.create_fix_pr_task",
        "Create Kanban [fix-pr] task for an issue.",
        True,
    ),
    EffectorEntry(
        "complete_kanban_task",
        "issue_to_pr",
        "repo_agent.steps.issue_to_pr.complete_kanban_task",
        "Mark one Kanban task completed.",
        True,
    ),
    EffectorEntry(
        "refresh_clone_base",
        "issue_to_pr",
        "repo_agent.steps.issue_to_pr.refresh_clone_base",
        "git fetch origin for configured clone.",
        True,
    ),
    EffectorEntry(
        "prepare_worktree",
        "issue_to_pr",
        "repo_agent.steps.issue_to_pr.prepare_worktree",
        "Create/reuse controlled git worktree for branch.",
        True,
    ),
    EffectorEntry(
        "run_omp_worker",
        "issue_to_pr",
        "repo_agent.steps.issue_to_pr.run_omp_worker",
        "Single OMP run in a worktree (no PR side effects).",
        True,
    ),
    EffectorEntry(
        "verify_branch_has_commits",
        "issue_to_pr",
        "repo_agent.steps.issue_to_pr.verify_branch_has_commits",
        "Assert worktree HEAD differs from base tip.",
        False,
    ),
    EffectorEntry(
        "open_pull_request",
        "issue_to_pr",
        "repo_agent.steps.issue_to_pr.open_pull_request",
        "gh pr create for branch (or detect existing open PR).",
        True,
    ),
    EffectorEntry(
        "apply_pr_labels",
        "issue_to_pr",
        "repo_agent.steps.issue_to_pr.apply_pr_labels",
        "Add labels to a PR (ai:generated / ai:pr-opened).",
        True,
    ),
    EffectorEntry(
        "write_dispatch_receipt",
        "issue_to_pr",
        "repo_agent.steps.issue_to_pr.write_dispatch_receipt",
        "Atomic JSON receipt write for dispatch provenance.",
        True,
    ),
    EffectorEntry(
        "check_worktree_dirty",
        "issue_to_pr",
        "repo_agent.steps.issue_to_pr.check_worktree_dirty",
        "Read whether worktree has uncommitted changes.",
        False,
    ),
    EffectorEntry(
        "list_controlled_worktrees",
        "issue_to_pr",
        "repo_agent.steps.issue_to_pr.list_controlled_worktrees",
        "List git worktrees (optionally under worktree_root).",
        False,
    ),
    EffectorEntry(
        "push_branch",
        "issue_to_pr",
        "repo_agent.steps.issue_to_pr.push_branch",
        "git push -u origin branch from worktree (never force).",
        True,
    ),
    EffectorEntry(
        "apply_issue_labels",
        "intake",
        "repo_agent.steps.issue_to_pr.apply_issue_labels",
        "Add labels on GitHub issue (ai:in-progress / ai:blocked / finish).",
        True,
    ),
    # --- triage ---
    EffectorEntry(
        "list_ai_fix_prs",
        "triage",
        "repo_agent.steps.triage.list_ai_fix_prs",
        "List open ai/fix/* PRs for a repo.",
        False,
    ),
    EffectorEntry(
        "load_pr_fields",
        "triage",
        "repo_agent.steps.triage.load_pr_fields",
        "Load full PR JSON for decisions.",
        False,
    ),
    EffectorEntry(
        "evaluate_checks",
        "triage",
        "repo_agent.steps.triage.evaluate_checks",
        "Pure: interpret statusCheckRollup pass/fail/pending.",
        False,
    ),
    EffectorEntry(
        "evaluate_test_evidence",
        "triage",
        "repo_agent.steps.triage.evaluate_test_evidence",
        "Pure: PR body has test evidence markers.",
        False,
    ),
    EffectorEntry(
        "decide_triage_action",
        "triage",
        "repo_agent.steps.triage.decide_triage_action",
        "Pure router: merge | comment_block | repair | skip.",
        False,
    ),
    EffectorEntry(
        "claim_pr_assignee",
        "triage",
        "repo_agent.steps.triage.claim_pr_assignee",
        "Assign PR to maintainer account.",
        True,
    ),
    EffectorEntry(
        "comment_pr_once",
        "triage",
        "repo_agent.steps.triage.comment_pr_once",
        "Post one PR comment body.",
        True,
    ),
    EffectorEntry(
        "merge_pull_request",
        "triage",
        "repo_agent.steps.triage.merge_pull_request",
        "Merge open PR with optional head OID match (guarded).",
        True,
    ),
    EffectorEntry(
        "close_linked_issue",
        "triage",
        "repo_agent.steps.triage.close_linked_issue",
        "Close linked GitHub issue after merge.",
        True,
    ),
    EffectorEntry(
        "write_merge_receipt",
        "triage",
        "repo_agent.steps.triage.write_merge_receipt",
        "Atomic merge receipt JSON write.",
        True,
    ),
    # --- repair ---
    EffectorEntry(
        "build_repair_prompt",
        "repair",
        "repo_agent.steps.repair.build_repair_prompt",
        "Pure: build OMP repair prompt from PR context.",
        False,
    ),
    EffectorEntry(
        "create_review_fix_task",
        "repair",
        "repo_agent.steps.repair.create_review_fix_task",
        "Create Kanban [fix-pr-review] for a PR.",
        True,
    ),
    EffectorEntry(
        "block_kanban_task",
        "repair",
        "repo_agent.steps.repair.block_kanban_task",
        "Block a Kanban task with reason.",
        True,
    ),
    # --- cleanup ---
    EffectorEntry(
        "parse_issue_from_branch",
        "cleanup",
        "repo_agent.steps.cleanup.parse_issue_from_branch",
        "Pure: issue number from ai/fix branch name.",
        False,
    ),
    EffectorEntry(
        "check_issue_closed",
        "cleanup",
        "repo_agent.steps.cleanup.check_issue_closed",
        "Read whether GitHub issue is closed.",
        False,
    ),
    EffectorEntry(
        "check_no_open_pr_for_branch",
        "cleanup",
        "repo_agent.steps.cleanup.check_no_open_pr_for_branch",
        "Read whether branch still has open PR.",
        False,
    ),
    EffectorEntry(
        "remove_worktree",
        "cleanup",
        "repo_agent.steps.cleanup.remove_worktree",
        "git worktree remove for one path.",
        True,
    ),
    EffectorEntry(
        "delete_local_fix_branch",
        "cleanup",
        "repo_agent.steps.cleanup.delete_local_fix_branch",
        "Delete local branch only (never remote).",
        True,
    ),
    EffectorEntry(
        "release_active_issue_claim",
        "cleanup",
        "repo_agent.steps.cleanup.release_active_issue_claim",
        "Drop matching active-issue claim file.",
        True,
    ),
    EffectorEntry(
        "create_maintenance_task",
        "cleanup",
        "repo_agent.steps.cleanup.create_maintenance_task",
        "Kanban [maintenance] for dirty worktree follow-up.",
        True,
    ),
)


def list_effectors() -> list[dict]:
    return [asdict(e) for e in EFFECTORS]


def domains() -> set[str]:
    return {e.domain for e in EFFECTORS}


def by_domain(domain: str) -> list[EffectorEntry]:
    return [e for e in EFFECTORS if e.domain == domain]


def resolve(ref: str) -> Callable:
    """Import shipped effector by catalog ref."""
    from importlib import import_module

    mod_name, _, attr = ref.rpartition(".")
    return getattr(import_module(mod_name), attr)


def load_all() -> dict[str, Callable]:
    return {e.id: resolve(e.ref) for e in EFFECTORS}
