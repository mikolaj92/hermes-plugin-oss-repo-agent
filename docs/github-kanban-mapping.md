# GitHub to Hermes Kanban Mapping

This plugin is an adapter. GitHub owns repository facts; Hermes Kanban owns
agent work. The adapter reconciles the two without inventing a second workflow.

## Source of Truth

| Domain | Source of truth | Adapter behavior |
| --- | --- | --- |
| Issue existence/state | GitHub issue state | Create or complete Kanban work to match GitHub. |
| PR existence/state | GitHub PR state | Claim/triage PRs and create repair work when needed. |
| Agent work ownership | Hermes Kanban task assignee/status | Start workers only from Kanban work items. |
| Merge readiness | GitHub PR checks/review/body | Gate or repair via Kanban, never infer readiness from task text alone. |
| Local execution leftovers | Local controlled worktrees | Clean only closed clean worktrees; dirty worktrees become Kanban maintenance work. |

## Issue Mapping

| GitHub condition | Kanban mapping | Notes |
| --- | --- | --- |
| Open actionable issue | `[issue] owner/repo#N` assigned to `repo-agent-intake` | Intake claims the GitHub issue first when configured. |
| Existing non-done `[issue]`, `[fix-pr]`, or `[fix-pr-review]` | No new task | Duplicate prevention uses title/body matching plus idempotency keys. |
| Closed issue with open Kanban intake/fix work | Complete stale task | GitHub closure wins over Kanban ready/blocked state. |
| `frozen` in title/body/labels | Block/no fixer task | Frozen work remains visible but does not spawn PR work. |

## Label and Priority Mapping

| GitHub label/content | Kanban effect |
| --- | --- |
| `priority:p0`, `p0`, `critical`, `urgent` | Highest priority/score boost |
| `priority:p1`, `high` | High priority/score boost |
| `priority:p2`, `medium` | Medium priority/score boost |
| `priority:p3`, `low` | Low priority/score penalty |
| `security` | Security score boost |
| `bug`, `regression`, `crash`, `failing` | Bug score boost |
| `docs`, `documentation`, `readme` | Lower score |
| `ai:blocked` | GitHub issue is not pulled into intake |
| `frozen` | Dispatcher blocks/no-ops PR creation |

## Work Mapping

| Kanban task | GitHub action | Completion condition |
| --- | --- | --- |
| `[issue]` | Create/ensure explicit `[fix-pr]` if actionable | `[issue]` completes after fixer task exists. |
| `[fix-pr]` | Worker creates branch and PR | Completes when an open PR exists for the task branch. |
| `[fix-pr-review]` | Worker updates existing PR branch | Completes when PR closes or branch has an open PR after worker run. |
| `[maintenance] dirty worktree` | No GitHub mutation | Human or agent cleans local dirty worktree; cleanup can remove it later. |

## PR Mapping

| GitHub PR condition | Kanban mapping |
| --- | --- |
| Owner-authored `ai/fix/*` PR | Assign to `mikolaj92` for triage. |
| External-author PR | Skip; adapter must not take over contributor work. |
| Missing `ai:generated` or `ai:pr-opened` | Owner-authored or empty-author `ai/fix/*` PRs attempt label repair first; if repair cannot establish the labels, skip/report failure. External or non-agent PRs remain skipped. |
| Checks failing, merge conflicts, or missing test evidence | Create `[fix-pr-review]` with checks/review context. |
| Review not approved and approval required | Block/comment only; no repair task is useful. |
| Clean, checks passing, approval satisfied, automerge enabled | Merge through GitHub gate. |

## Reconciliation Commands

| Command | Purpose |
| --- | --- |
| `repo_issue_intake.sh --live` | GitHub issues to Kanban intake tasks. |
| `repo_issue_to_pr_dispatch.sh --live` | Kanban issue/fix tasks to PR work decisions. |
| `repo_pr_triage.sh --live --comment` | GitHub PR state to Kanban repair tasks/comments. |
| `repo_agent_cleanup.sh --live` | Closed GitHub issues to local worktree cleanup or maintenance tasks. |
| `repo_agent_backfill.sh --live` | Run reconciliation without workers to repair drift. |
| `repo_agent_webhook.sh --event <event> --live` | Optional webhook entrypoint that triggers the same reconciliation paths. |
