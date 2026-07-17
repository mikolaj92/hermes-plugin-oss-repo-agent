"""Fala 0.2.x correlation path definitions for repo-agent."""

from repo_agent.flows.cleanup import CLEANUP_PATH, run_cleanup_flow
from repo_agent.flows.common import PathRunResult, path_conduction_graph, path_ids
from repo_agent.flows.intake import INTAKE_FLOW, INTAKE_PATH, run_intake_flow
from repo_agent.flows.issue_to_pr import ISSUE_TO_PR_PATH, run_issue_to_pr_flow
from repo_agent.flows.triage import (
    PR_COMMENT_PATH,
    PR_MERGE_PATH,
    PR_REPAIR_PATH,
    PR_TRIAGE_PATH,
    run_follow_up_path,
    run_pr_triage_decide,
    run_triage_with_router,
)

ALL_PATHS = (
    INTAKE_PATH,
    ISSUE_TO_PR_PATH,
    PR_TRIAGE_PATH,
    PR_MERGE_PATH,
    PR_COMMENT_PATH,
    PR_REPAIR_PATH,
    CLEANUP_PATH,
)

__all__ = [
    "ALL_PATHS",
    "CLEANUP_PATH",
    "INTAKE_FLOW",
    "INTAKE_PATH",
    "ISSUE_TO_PR_PATH",
    "PR_COMMENT_PATH",
    "PR_MERGE_PATH",
    "PR_REPAIR_PATH",
    "PR_TRIAGE_PATH",
    "PathRunResult",
    "path_conduction_graph",
    "path_ids",
    "run_cleanup_flow",
    "run_follow_up_path",
    "run_intake_flow",
    "run_issue_to_pr_flow",
    "run_pr_triage_decide",
    "run_triage_with_router",
]
