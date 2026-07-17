#!/usr/bin/env bash
# Parent/operator helper: commit+push main, sync mini, dry-run Fala ticks.
# Safe defaults: dry-run only; no force-push; no git config changes.
set -euo pipefail

ROOT="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
REMOTE_HOST="${REMOTE_HOST:-mini-m4-0@192.168.1.52}"
REMOTE_PASS="${REMOTE_PASS:?set REMOTE_PASS in the environment}"
REMOTE_PATH="${REMOTE_PATH:-/Users/mini-m4-main/Developer/hermes-plugin-oss-repo-agent}"
FALA_PATH_REMOTE="${FALA_PATH_REMOTE:-/Users/mini-m4-main/Developer/Fala}"

cd "$ROOT"

echo "== git status =="
git status
git log --oneline -5

echo "== stage & commit (if dirty) =="
if [[ -n "$(git status --porcelain)" ]]; then
  git add -A src/repo_agent tests docs pyproject.toml scripts
  git commit -m "$(cat <<'EOF'
feat(fala): full auto-worker correlation paths and tick CLIs

Wire issue_to_pr, pr_triage (+ merge/comment/repair router), and cleanup
as Fala 0.2.x CorrelationPathSpecs. Add repo-agent-tick-dispatch/triage/cleanup
entrypoints (dry-run default). Effectors pull fields from conduction for
composition. Unit tests for path graphs and conduction helpers.
EOF
)"
else
  echo "working tree clean"
fi

echo "== pull --rebase && push origin main =="
git pull --rebase origin main
git push origin main
echo "HEAD=$(git rev-parse HEAD)"

echo "== ensure Fala 0.2.1 on mini =="
sshpass -p "$REMOTE_PASS" ssh -o StrictHostKeyChecking=no "$REMOTE_HOST" \
  "cd '$FALA_PATH_REMOTE' && git fetch --tags && git checkout 0.2.1 && git status -sb && cat pyproject.toml | head -5"

echo "== rsync local → mini =="
sshpass -p "$REMOTE_PASS" rsync -az --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude '.git' --exclude '.fala' \
  "$ROOT/" "$REMOTE_HOST:$REMOTE_PATH/"

echo "== uv sync + tests + dry-run ticks on mini =="
sshpass -p "$REMOTE_PASS" ssh -o StrictHostKeyChecking=no "$REMOTE_HOST" bash -s <<REMOTE
set -euo pipefail
cd "$REMOTE_PATH"
uv sync
uv run python -m unittest tests.test_path_composition tests.test_atomic_effectors tests.test_fala_intake_flow -v
uv run repo-agent-tick-intake --dry-run --json | head -c 2000 || true
echo
uv run repo-agent-tick-dispatch --dry-run --json | head -c 2000 || true
echo
uv run repo-agent-tick-triage --dry-run --decide-only --json | head -c 2000 || true
echo
uv run repo-agent-tick-cleanup --branch 'ai/fix/0-smoke' --dry-run --json | head -c 2000 || true
echo
echo OK_SMOKE
REMOTE

echo "done"
