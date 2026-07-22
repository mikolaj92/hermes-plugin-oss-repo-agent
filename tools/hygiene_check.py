#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path


BLOCKED_PATTERNS = [
    ("local-user-path", re.compile("/" + "Users/")),
    ("linux-real-home", re.compile(r"/home/[A-Za-z0-9._-]+/")),
    ("windows-user-path", re.compile(r"[A-Za-z]:\\\\Users\\\\")),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+")),
    ("bearer-token", re.compile(r"Authorization:\s*Bearer\s+\S+", re.I)),
    ("slack-token", re.compile(r"xoxb-[A-Za-z0-9-]+")),
    ("openai-key", re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{12,}")),
    ("dotenv-api-key", re.compile(r"(?:OPENAI_API_KEY|ANTHROPIC_API_KEY)\s*=")),
    ("hermes-auth-file", re.compile(r"\.hermes/auth\.json")),
    ("automerge-env", re.compile(r"HERMES_PR_AUTOMERGE\s*=\s*1")),
    ("forbidden-merge-command", re.compile(r"gh\s+pr\s+merge")),
    ("force-push", re.compile(r"git\s+push\s+(?:--force|-f)")),
    ("social-credential", re.compile(r"(?:x_api_key|twitter_token|mastodon_token|reddit_client_secret)\s*[:=]", re.I)),
]

SKIP_PARTS = {".git", ".omo", ".venv", "__pycache__", ".pytest_cache"}


def is_allowed_runtime_line(rel: Path, name: str, line: str) -> bool:
    rel_text = rel.as_posix()
    if rel_text == "tools/hygiene_check.py" and name in {"automerge-env", "forbidden-merge-command"}:
        return True
    if name == "local-user-path" and (
        rel_text.startswith("scripts/") or rel_text.startswith("templates/launchd/")
    ):
        return True
    if name == "automerge-env" and rel_text == "scripts/cron_repo_pr_triage.sh":
        return True
    if name == "automerge-env" and rel_text == "scripts/repo_pr_triage.sh" and "HERMES_PR_AUTOMERGE=1" in line:
        return True
    if name == "forbidden-merge-command" and rel_text == "scripts/repo_pr_triage.sh":
        return "gh pr merge" in line and "decision\" == \"merge\"" not in line
    if name == "openai-key" and rel_text == "scripts/repo_agent_cleanup.sh" and "claim-or-task-" + "identity-mismatch" in line:
        return True
    return False


def iter_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        yield path


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    root = Path(argv[0] if argv else ".").resolve()
    failures: list[str] = []
    for path in iter_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(root)
        for idx, line in enumerate(text.splitlines(), start=1):
            for name, pattern in BLOCKED_PATTERNS:
                if pattern.search(line) and not is_allowed_runtime_line(rel, name, line):
                    failures.append(f"{rel}:{idx}: {name}: {line.strip()[:160]}")
    if failures:
        print("Hygiene check failed:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print("Hygiene check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
