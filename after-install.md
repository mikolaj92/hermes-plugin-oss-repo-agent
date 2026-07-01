# Start here

Run a safe dry-run first. It does not merge, force push, delete branches, or run an executor.

```bash
hermes oss-repo-agent --config config.yaml init
hermes oss-repo-agent --config config.yaml validate
hermes oss-repo-agent --config config.yaml intake --limit 3
hermes oss-repo-agent --config config.yaml dispatch --max 2
```

The first command writes a starter `config.yaml` with `mode: dry-run`, `automerge: false`, and `executor.enabled: false`.

To use real repositories, edit `github.assignee` and `repos:` in the generated
config and keep running dry-run commands until the planned work looks correct.
The assignee is the public GitHub claim account; Kanban idempotency is still the
hard duplicate guard.

Live mutation requires both `mode: live` in config and an explicit CLI live flag. Executor runs also require `--run-executor` and `executor.enabled: true`.

CI-style checks:

```bash
python3 -m unittest discover -s tests
python3 tools/hygiene_check.py .
scripts/repo_agent_smoke.sh
```

The live mini runtime scripts are in `scripts/`. Keep changes there, commit and
push this repository, then deploy those scripts into `~/.hermes/scripts/`.
