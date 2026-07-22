# Mega-atomic Fala effectors (composition inventory)

These are **bricks only**. Correlation paths that wire them via `conduction`
come later. Every effector is an allowlisted subprocess adapter with:

- inputs in the package-host request input
- agent settings in the package-host effector configuration
- upstream outputs under `request.input["conduction"][<effector_id>]`
- output envelope: `{status, ok, mutated, dry_run, reason? , ...}`

Machine-readable: `repo_agent.catalog.EFFECTORS` / `list_effectors()`.

| id | domain | mutates | intent |
|----|--------|---------|--------|
| poll_eligible_issues | intake | no | List eligible open issues |
| claim_github_issue | intake | yes | Assign/label issue |
| ensure_kanban_intake | intake | yes | Ensure Kanban `[issue]` |
| load_kanban_task | issue_to_pr | no | Load Kanban task |
| parse_issue_ref_from_task | issue_to_pr | no | Parse repo#N + branch |
| create_fix_pr_task | issue_to_pr | yes | Create `[fix-pr]` task |
| complete_kanban_task | issue_to_pr | yes | Complete task |
| refresh_clone_base | issue_to_pr | yes | git fetch origin |
| prepare_worktree | issue_to_pr | yes | Create/reuse worktree |
| run_omp_worker | issue_to_pr | yes | Single OMP run |
| verify_branch_has_commits | issue_to_pr | no | HEAD ≠ base |
| open_pull_request | issue_to_pr | yes | gh pr create |
| apply_pr_labels | issue_to_pr | yes | Label PR |
| write_dispatch_receipt | issue_to_pr | yes | Receipt JSON |
| check_worktree_dirty | issue_to_pr | no | Dirty worktree? |
| list_controlled_worktrees | issue_to_pr | no | List controlled worktrees |
| push_branch | issue_to_pr | yes | Push branch (no force) |
| apply_issue_labels | intake | yes | Label GitHub issue |
| list_ai_fix_prs | triage | no | List ai/fix PRs |
| load_pr_fields | triage | no | Load PR JSON |
| evaluate_checks | triage | no | Checks pass? |
| evaluate_test_evidence | triage | no | Evidence in body? |
| decide_triage_action | triage | no | merge/comment/repair/skip |
| claim_pr_assignee | triage | yes | Assign PR |
| comment_pr_once | triage | yes | Comment PR |
| merge_pull_request | triage | yes | Merge PR |
| close_linked_issue | triage | yes | Close issue |
| write_merge_receipt | triage | yes | Merge receipt |
| build_repair_prompt | repair | no | Build OMP repair prompt |
| create_review_fix_task | repair | yes | `[fix-pr-review]` task |
| block_kanban_task | repair | yes | Block task |
| parse_issue_from_branch | cleanup | no | Issue from branch name |
| check_issue_closed | cleanup | no | Issue closed? |
| check_no_open_pr_for_branch | cleanup | no | No open PR? |
| remove_worktree | cleanup | yes | Remove worktree |
| delete_local_fix_branch | cleanup | yes | Delete local branch |
| release_active_issue_claim | cleanup | yes | Drop claim file |
| create_maintenance_task | cleanup | yes | Dirty worktree task |

## Composition (correlation paths)

Defined declaratively in `fala-package.toml`; every edge is explicit
`conduction` between subprocess effectors. `auto_worker` is the sole scheduled
path and is invoked through `repo-agent-tick-all`:

```bash
uv run repo-agent-tick-all --dry-run
uv run repo-agent-tick-all --live
```

Individual ticks are manual diagnostics only:

```bash
uv run repo-agent-tick-intake --dry-run
uv run repo-agent-tick-dispatch --dry-run
uv run repo-agent-tick-triage --dry-run
uv run repo-agent-tick-cleanup --branch 'ai/fix/N-slug' --dry-run
```

- `issue_intake`: poll → direction decide → reject comment → claim → kanban
- `issue_to_pr`: load → parse → worktree → omp → verify → push → open_pr → labels → receipt → complete
- `pr_triage`: list → load → checks → evidence → **decide** → gated (`pr_merge` | `pr_comment_block` | `pr_repair`)
- `pr_merge`: claim_pr → merge → receipt → close_issue
- `pr_repair`: review_task → prompt → worktree → omp → push
- `cleanup`: parse → issue_closed → no_open_pr → remove_worktree → delete_branch → release_claim
