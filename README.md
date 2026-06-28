# oss-repo-agent

Hermes plugin for a generic, guarded GitHub issue and PR workflow.

## Install

```bash
hermes plugins install mikolaj92/hermes-plugin-oss-repo-agent --enable
```

This repository is a standalone Hermes plugin: `plugin.yaml` and `__init__.py`
live at the repository root.

The plugin registers:

- CLI namespace: `hermes oss-repo-agent ...`
- Skills:
  - `oss-repo-agent:repo-gh-cli-policy`
  - `oss-repo-agent:repo-audit-finding-format`
  - `oss-repo-agent:repo-fix-issue-pr`
  - `oss-repo-agent:repo-review-agent-pr`

## Commands

```bash
hermes oss-repo-agent validate --config <config.json-or-yaml>
hermes oss-repo-agent bootstrap --config <config> --apply
hermes oss-repo-agent intake --config <config> --live
hermes oss-repo-agent dispatch --config <config> --live --run-executor
hermes oss-repo-agent pr-triage --config <config> --live --comment
hermes oss-repo-agent render-launchd --config <config> --output <dir>
```

Live mutation requires both `mode: live` in config and an explicit live/apply CLI
flag. Executor runs require live mode, `--run-executor`, and
`executor.enabled: true`.

## Configuration

Default path: `~/.hermes/oss-repo-agent/config.yaml`

Override with `HERMES_OSS_REPO_AGENT_CONFIG` or `--config`.

Start from [`examples/config.example.yaml`](examples/config.example.yaml).

## v0 limitations

- No PR merge behavior.
- No force-push or branch deletion behavior.
- Launchd output is template-only and macOS-specific.
- GitHub access goes through the `gh` CLI wrappers only.
- Local git commands are rendered with `GIT_MASTER=1` and executed with that
  environment variable set.
