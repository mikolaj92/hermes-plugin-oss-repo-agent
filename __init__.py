from __future__ import annotations

from pathlib import Path

from . import commands


SKILLS = (
    ("repo-gh-cli-policy", "GitHub CLI safety policy for repository automation"),
    ("repo-audit-finding-format", "Structured audit finding format"),
    ("repo-fix-issue-pr", "Guarded issue fixing workflow"),
    ("repo-review-agent-pr", "Agent PR review workflow"),
)


def register(ctx):
    ctx.register_cli_command(
        "oss-repo-agent",
        "Manage guarded GitHub issue and PR automation",
        commands.setup_parser,
        commands.handle_cli,
        description="Generic OSS repository agent workflow",
    )
    base = Path(__file__).parent / "skills"
    for name, description in SKILLS:
        ctx.register_skill(name, base / name / "SKILL.md", description=description)
