import json
import os
import shlex
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DISPATCHER = ROOT / "scripts" / "repo_issue_to_pr_dispatch.sh"
CLEANUP = ROOT / "scripts" / "repo_agent_cleanup.sh"
TRIAGE = ROOT / "scripts" / "repo_pr_triage.sh"
WEBHOOK = ROOT / "scripts" / "repo_agent_webhook.sh"


class ContextMiningBlockersTest(unittest.TestCase):
    def test_dispatcher_unknown_states_are_transient(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls_file = root / "calls.log"
            log_file = root / "dispatch.log"
            repos_file = root / "repos.txt"
            lock_dir = root / "locks"
            worktree_root = root / "worktrees"
            repo_path = self._make_repo(root / "repo")
            repos_file.write_text(f"owner/repo|board|{repo_path}|10\n", encoding="utf-8")

            env = os.environ | {
                "CALLS_FILE": str(calls_file),
                "HERMES_REPO_AGENT_REPOS_FILE": str(repos_file),
                "HERMES_ISSUE_TO_PR_LOG": str(log_file),
                "HERMES_ISSUE_TO_PR_LOCK_DIR": str(lock_dir),
                "HERMES_WORKTREE_ROOT": str(worktree_root),
                "BASH_FUNC_hermes%%": self._dispatcher_hermes(
                    {
                        "board": [
                            {
                                "id": "task-issue-unknown",
                                "status": "ready",
                                "title": "[issue] owner/repo#123 Broken issue",
                                "body": "Issue task should not stale-complete when gh is unavailable.",
                                "priority": 1,
                            },
                            {
                                "id": "task-review-unknown",
                                "status": "ready",
                                "title": "[fix-pr-review] owner/repo#55 Review task",
                                "body": "PR review task should not stale-complete when gh is unavailable.",
                                "priority": 2,
                            },
                            {
                                "id": "task-fix-unknown",
                                "status": "ready",
                                "title": "[fix-pr] owner/repo#124 Broken fix",
                                "body": "Fix task should not stale-complete when gh is unavailable.",
                                "priority": 3,
                                "branch": "ai/fix/124-broken-fix",
                            },
                        ]
                    }
                ),
                "BASH_FUNC_gh%%": self._gh_unknown_states(),
            }

            result = subprocess.run(
                ["bash", str(DISPATCHER), "--dry-run", "--max", "3"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            log = log_file.read_text(encoding="utf-8")
            calls = calls_file.read_text(encoding="utf-8") if calls_file.exists() else ""
            self.assertIn("ISSUE_STATE_UNKNOWN repo=owner/repo issue=123 action=skip", log)
            self.assertIn("PR_STATE_UNKNOWN repo=owner/repo pr=55 action=skip", log)
            self.assertIn("ISSUE_STATE_UNKNOWN repo=owner/repo issue=124 action=skip", log)
            self.assertNotIn("complete-stale-issue", log)
            self.assertNotIn("complete-stale-review", log)
            self.assertNotIn("complete-stale-fix", log)
            self.assertNotIn("kanban\t--board\tboard\tcomplete", calls)
            self.assertNotIn("kanban\t--board\tboard\tcreate", calls)

    def test_cleanup_keeps_unknown_github_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls_file = root / "calls.log"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            git_wrapper = fake_bin / "git"
            real_git = subprocess.run(
                ["bash", "-lc", "command -v git"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            git_wrapper.write_text(
                textwrap.dedent(
                    f"""
                    #!/usr/bin/env bash
                    printf 'GIT\\t%s\n' "$*" >> "$CALLS_FILE"
                    exec {shlex.quote(real_git)} "$@"
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            git_wrapper.chmod(0o755)

            clone_path = self._make_repo(root / "repo")
            worktree_root = (root / "worktrees").resolve()
            self._run_git(clone_path, "worktree", "add", str(worktree_root / "board" / "repo" / "ai-fix-123"), "-b", "ai/fix/123-test")
            repos_file = root / "repos.txt"
            repos_file.write_text(f"owner/repo|board|{clone_path}|10\n", encoding="utf-8")
            log_file = root / "cleanup.log"

            env = os.environ | {
                "CALLS_FILE": str(calls_file),
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "HERMES_REPO_AGENT_REPOS_FILE": str(repos_file),
                "HERMES_REPO_CLEANUP_LOG": str(log_file),
                "HERMES_REPO_CLEANUP_LOCK_DIR": str(root / "locks"),
                "HERMES_WORKTREE_ROOT": str(worktree_root),
                "BASH_FUNC_gh%%": self._cleanup_gh_unknown_issue(),
            }

            result = subprocess.run(
                ["bash", str(CLEANUP), "--live"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            log = log_file.read_text(encoding="utf-8")
            calls = calls_file.read_text(encoding="utf-8") if calls_file.exists() else ""
            self.assertIn("KEEP repo=owner/repo issue=123 branch=ai/fix/123-test reason=issue-state-unknown", log)
            self.assertNotIn("worktree\tremove", calls)
            self.assertNotIn("branch\t-D", calls)

    def test_dispatcher_existing_pr_finalizes_ready_task_and_repairs_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls_file = root / "calls.log"
            log_file = root / "dispatch.log"
            repos_file = root / "repos.txt"
            repo_path = self._make_repo(root / "repo")
            repos_file.write_text(f"owner/repo|board|{repo_path}|10\n", encoding="utf-8")

            env = os.environ | {
                "CALLS_FILE": str(calls_file),
                "HERMES_REPO_AGENT_REPOS_FILE": str(repos_file),
                "HERMES_ISSUE_TO_PR_LOG": str(log_file),
                "HERMES_ISSUE_TO_PR_LOCK_DIR": str(root / "locks"),
                "HERMES_WORKTREE_ROOT": str(root / "worktrees"),
                "HERMES_ISSUE_TO_PR_RUN_OPENCODE": "1",
                "HERMES_ALLOW_UNSAFE_CLAUDE": "1",
                "BASH_FUNC_hermes%%": self._dispatcher_hermes(
                    {
                        "board": [
                            {
                                "id": "task-existing-pr",
                                "status": "ready",
                                "title": "[fix-pr] owner/repo#123 Existing PR",
                                "body": "Repo-agent retry attempt=1/3 next_retry_after=1970-01-01T00:00:00Z",
                                "priority": 1,
                                "branch": "ai/fix/123-existing-pr",
                            }
                        ]
                    }
                ),
                "BASH_FUNC_gh%%": self._dispatcher_gh_existing_pr(label_edit_fails=False),
            }

            result = subprocess.run(
                ["bash", str(DISPATCHER), "--live", "--run-opencode", "--max", "1"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            log = log_file.read_text(encoding="utf-8")
            calls = calls_file.read_text(encoding="utf-8")
            self.assertIn("LABELS_REPAIRED repo=owner/repo pr=77", log)
            self.assertIn("action=complete-existing-pr", log)
            self.assertIn("pr edit 77 --repo owner/repo --add-label ai:generated --add-label ai:pr-opened", calls)
            self.assertIn("kanban\t--board\tboard\tcomplete\ttask-existing-pr", calls)
            self.assertNotIn("claude", calls.lower())
            self.assertNotIn("action=run-claude", log)

    def test_no_pr_blocks_retry_until_attempts_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            due = self._run_blocked_no_pr_dispatch(root / "due", attempt=1)
            exhausted = self._run_blocked_no_pr_dispatch(root / "exhausted", attempt=3)

            due_log = due["log"].read_text(encoding="utf-8")
            due_calls = due["calls"].read_text(encoding="utf-8")
            self.assertIn("action=recover-blocked-fix-task", due_log)
            self.assertIn("kanban\t--board\tboard\tunblock\ttask-no-pr-1", due_calls)

            exhausted_log = exhausted["log"].read_text(encoding="utf-8")
            exhausted_calls = exhausted["calls"].read_text(encoding="utf-8")
            self.assertIn("NO_PR_RETRIES_EXHAUSTED task=task-no-pr-3", exhausted_log)
            self.assertNotIn("kanban\t--board\tboard\tunblock\ttask-no-pr-3", exhausted_calls)
            self.assertNotIn("action=run-claude", exhausted_log)

    def test_triage_repairs_missing_ai_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls_file = root / "calls.log"
            log_file = root / "triage.log"
            repos_file = root / "repos.txt"
            repo_path = self._make_repo(root / "repo")
            repos_file.write_text(f"owner/repo|board|{repo_path}|10\n", encoding="utf-8")

            env = os.environ | {
                "CALLS_FILE": str(calls_file),
                "HERMES_REPO_AGENT_REPOS_FILE": str(repos_file),
                "HERMES_PR_TRIAGE_LOG": str(log_file),
                "HERMES_PR_TRIAGE_LOCK_DIR": str(root / "locks"),
                "HERMES_PR_REQUIRE_TEST_EVIDENCE": "0",
                "HERMES_PR_ALLOW_NO_CHECKS": "1",
                "BASH_FUNC_gh%%": self._triage_gh_missing_labels(),
                "BASH_FUNC_hermes%%": self._noop_hermes(),
            }

            result = subprocess.run(
                ["bash", str(TRIAGE), "--live"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            log = log_file.read_text(encoding="utf-8")
            calls = calls_file.read_text(encoding="utf-8")
            self.assertIn("LABELS_REPAIRED repo=owner/repo pr=7", log)
            self.assertNotIn("missing-required-ai-labels", log)
            self.assertIn("pr edit 7 --repo owner/repo --add-label ai:generated --add-label ai:pr-opened", calls)
            self.assertIn("DECISION repo=owner/repo pr=7", log)

    def test_webhook_fail_open_and_issue_comment_pr_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            webhook = scripts_dir / "repo_agent_webhook.sh"
            webhook.write_text(WEBHOOK.read_text(encoding="utf-8"), encoding="utf-8")
            webhook.chmod(0o755)
            runs_file = root / "runs.log"
            for name in [
                "repo_issue_intake.sh",
                "repo_issue_to_pr_dispatch.sh",
                "repo_pr_triage.sh",
                "repo_agent_cleanup.sh",
            ]:
                path = scripts_dir / name
                exit_code = 1 if name == "repo_issue_intake.sh" else 0
                path.write_text(
                    "#!/usr/bin/env bash\n"
                    f"printf '%s\\n' {shlex.quote(name)} >> {shlex.quote(str(runs_file))}\n"
                    f"exit {exit_code}\n",
                    encoding="utf-8",
                )
                path.chmod(0o755)

            home = root / "home"
            webhook_log = home / ".hermes" / "logs" / "repo-agent-webhook.log"
            broad_env = os.environ | {
                "HOME": str(home),
                "GITHUB_EVENT_NAME": "issue_comment",
                "HERMES_REPO_AGENT_WEBHOOK_LOG": str(webhook_log),
            }
            broad = subprocess.run(
                ["bash", str(webhook), "--event", "issue_comment"],
                cwd=root,
                env=broad_env,
                text=True,
                capture_output=True,
                check=False,
            )

            broad_log = webhook_log.read_text(encoding="utf-8") if webhook_log.exists() else ""
            runs = runs_file.read_text(encoding="utf-8") if runs_file.exists() else ""
            self.assertNotEqual(broad.returncode, 0)
            self.assertIn("STEP_FAILED name=intake", broad_log)
            self.assertIn("DONE event=issue_comment failures=1", broad_log)
            self.assertIn("repo_issue_intake.sh", runs)
            self.assertIn("repo_issue_to_pr_dispatch.sh", runs)
            self.assertIn("repo_pr_triage.sh", runs)
            self.assertIn("repo_agent_cleanup.sh", runs)

            runs_file.write_text("", encoding="utf-8")
            webhook_log.unlink(missing_ok=True)
            payload = root / "payload.json"
            payload.write_text(json.dumps({"issue": {"pull_request": {"url": "https://api.example/pr/7"}}}), encoding="utf-8")
            pr_env = os.environ | {
                "HOME": str(home),
                "GITHUB_EVENT_NAME": "issue_comment",
                "GITHUB_EVENT_PATH": str(payload),
                "HERMES_REPO_AGENT_WEBHOOK_LOG": str(webhook_log),
            }
            pr_result = subprocess.run(
                ["bash", str(webhook), "--event", "issue_comment", "--payload", str(payload)],
                cwd=root,
                env=pr_env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(pr_result.returncode, 0, pr_result.stderr + pr_result.stdout)
            pr_log = webhook_log.read_text(encoding="utf-8") if webhook_log.exists() else ""
            pr_runs = runs_file.read_text(encoding="utf-8") if runs_file.exists() else ""
            self.assertIn("PAYLOAD_KIND kind=pr-comment", pr_log)
            self.assertIn("repo_pr_triage.sh", pr_runs)
            self.assertNotIn("repo_issue_intake.sh", pr_runs)
            self.assertNotIn("repo_issue_to_pr_dispatch.sh", pr_runs)

    def _make_repo(self, path: Path) -> Path:
        path.mkdir(parents=True)
        self._run_git(path, "init")
        self._run_git(path, "config", "user.email", "test@example.com")
        self._run_git(path, "config", "user.name", "Test User")
        (path / "README.md").write_text("fixture\n", encoding="utf-8")
        self._run_git(path, "add", "README.md")
        self._run_git(path, "commit", "-m", "initial")
        return path

    def _run_git(self, cwd: Path, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=os.environ | {"GIT_MASTER": "1"},
            text=True,
            capture_output=True,
            check=True,
        )

    def _dispatcher_hermes(self, boards: dict[str, list[dict[str, object]]]) -> str:
        encoded = json.dumps(boards)
        return textwrap.dedent(
            f'''
            () {{
              printf 'HERMES' >> "$CALLS_FILE"
              printf '\\t%s' "$@" >> "$CALLS_FILE"
              printf '\\n' >> "$CALLS_FILE"
              python3 - "$@" <<'PY'
import json
import os
import sys

boards = json.loads({encoded!r})
args = sys.argv[1:]
if args and args[0] == "kanban":
    remaining = args[1:]
    board = ""
    if "--board" in remaining:
        index = remaining.index("--board")
        if index + 1 < len(remaining):
            board = remaining[index + 1]
        remaining = remaining[:index] + remaining[index + 2:]
    if remaining and remaining[0] == "list":
        print(json.dumps(boards.get(board, [])))
        raise SystemExit(0)
    if remaining and remaining[0] == "show":
        task_id = remaining[1]
        for tasks in boards.values():
            for task in tasks:
                if task.get("id") == task_id:
                    print(task.get("body", ""))
                    raise SystemExit(0)
        raise SystemExit(1)
    if remaining and remaining[0] in {"assign", "block", "comment", "complete", "create", "reassign", "unblock"}:
        raise SystemExit(0)
if args[:2] == ["kanban", "list"]:
    board = args[2]
    print(json.dumps(boards.get(board, [])))
    raise SystemExit(0)
if args[:2] == ["kanban", "show"]:
    task_id = args[2]
    for tasks in boards.values():
        for task in tasks:
            if task.get("id") == task_id:
                print(task.get("body", ""))
                raise SystemExit(0)
    raise SystemExit(1)
if args[:2] in (["kanban", "complete"], ["kanban", "create"], ["kanban", "unblock"], ["kanban", "assign"], ["kanban", "block"], ["kanban", "comment"], ["kanban", "reassign"]):
    raise SystemExit(0)
raise SystemExit(0)
PY
            }}
            '''
        ).strip()

    def _gh_unknown_states(self) -> str:
        return textwrap.dedent(
            '''
            () {
              printf 'GH\\t%s\n' "$*" >> "$CALLS_FILE"
              if [[ "$1 $2" == "issue view" || "$1 $2" == "pr view" ]]; then
                return 1
              fi
              if [[ "$1 $2" == "pr list" ]]; then
                printf '[]\n'
                return 0
              fi
              return 0
            }
            '''
        ).strip()

    def _cleanup_gh_unknown_issue(self) -> str:
        return textwrap.dedent(
            '''
            () {
              printf 'GH\\t%s\n' "$*" >> "$CALLS_FILE"
              if [[ "$1 $2" == "issue view" ]]; then
                return 1
              fi
              if [[ "$1 $2" == "pr list" ]]; then
                printf '0\n'
                return 0
              fi
              return 0
            }
            '''
        ).strip()

    def _dispatcher_gh_existing_pr(self, *, label_edit_fails: bool) -> str:
        label_exit = 1 if label_edit_fails else 0
        return textwrap.dedent(
            f'''
            () {{
              printf 'GH\\t%s\n' "$*" >> "$CALLS_FILE"
              if [[ "$1 $2" == "issue view" ]]; then
                printf 'OPEN\n'
                return 0
              fi
              if [[ "$1 $2" == "pr list" ]]; then
                printf '[{{"number":77,"url":"https://github.example/owner/repo/pull/77"}}]\n'
                return 0
              fi
              if [[ "$1 $2" == "pr edit" ]]; then
                return {label_exit}
              fi
              return 0
            }}
            '''
        ).strip()

    def _run_blocked_no_pr_dispatch(self, root: Path, *, attempt: int) -> dict[str, Path]:
        root.mkdir()
        calls_file = root / "calls.log"
        log_file = root / "dispatch.log"
        repos_file = root / "repos.txt"
        repo_path = self._make_repo(root / "repo")
        repos_file.write_text(f"owner/repo|board|{repo_path}|10\n", encoding="utf-8")
        task_id = f"task-no-pr-{attempt}"
        body = f"repo-agent worker finished without an open PR for branch ai/fix/123-no-pr; repo-agent retry attempt={attempt}/3 next_retry_after=1970-01-01T00:00:00Z"
        env = os.environ | {
            "CALLS_FILE": str(calls_file),
            "HERMES_REPO_AGENT_REPOS_FILE": str(repos_file),
            "HERMES_ISSUE_TO_PR_LOG": str(log_file),
            "HERMES_ISSUE_TO_PR_LOCK_DIR": str(root / "locks"),
            "HERMES_WORKTREE_ROOT": str(root / "worktrees"),
            "HERMES_ISSUE_TO_PR_RUN_OPENCODE": "1",
            "HERMES_ALLOW_UNSAFE_CLAUDE": "1",
            "BASH_FUNC_hermes%%": self._dispatcher_hermes(
                {
                    "board": [
                        {
                            "id": task_id,
                            "status": "blocked",
                            "title": "[fix-pr] owner/repo#123 No PR retry",
                            "body": body,
                            "priority": 1,
                            "branch": "ai/fix/123-no-pr",
                        }
                    ]
                }
            ),
            "BASH_FUNC_gh%%": self._dispatcher_gh_no_pr(),
        }
        result = subprocess.run(
            ["bash", str(DISPATCHER), "--live", "--run-opencode", "--max", "1"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return {"log": log_file, "calls": calls_file}

    def _dispatcher_gh_no_pr(self) -> str:
        return textwrap.dedent(
            '''
            () {
              printf 'GH\\t%s\n' "$*" >> "$CALLS_FILE"
              if [[ "$1 $2" == "issue view" ]]; then
                printf 'OPEN\n'
                return 0
              fi
              if [[ "$1 $2" == "pr list" ]]; then
                printf '[]\n'
                return 0
              fi
              return 0
            }
            '''
        ).strip()

    def _triage_gh_missing_labels(self) -> str:
        return textwrap.dedent(
            '''
            () {
              printf 'GH\\t%s\n' "$*" >> "$CALLS_FILE"
              if [[ "$1 $2" == "pr list" ]]; then
                printf '%s\n' '[{"number":7,"title":"Fix bug","url":"https://github.example/owner/repo/pull/7","headRefName":"ai/fix/foo","baseRefName":"main","isDraft":false,"mergeStateStatus":"CLEAN","reviewDecision":"APPROVED","labels":[],"author":{"login":"owner"}}]'
                return 0
              fi
              if [[ "$1 $2" == "pr edit" ]]; then
                return 0
              fi
              if [[ "$1 $2" == "pr checks" ]]; then
                return 0
              fi
              return 0
            }
            '''
        ).strip()

    def _noop_hermes(self) -> str:
        return textwrap.dedent(
            '''
            () {
              printf 'HERMES\\t%s\n' "$*" >> "$CALLS_FILE"
              return 0
            }
            '''
        ).strip()


if __name__ == "__main__":
    unittest.main()
