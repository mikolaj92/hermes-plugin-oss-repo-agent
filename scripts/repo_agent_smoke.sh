#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTIVE_SCRIPTS="${HERMES_REPO_AGENT_ACTIVE_SCRIPTS:-${HOME:-/Users/mini-m4-main}/.hermes/scripts}"
parity_args=(
  --source-root "${HERMES_REPO_AGENT_PARITY_SOURCE_ROOT:-$ROOT/scripts}"
  --active-root "$ACTIVE_SCRIPTS"
  --template-root "${HERMES_REPO_AGENT_PARITY_TEMPLATE_ROOT:-$ROOT/templates/launchd}"
)
active_plist_root="${HERMES_REPO_AGENT_ACTIVE_PLIST_ROOT:-${HERMES_REPO_AGENT_PARITY_PLIST_ROOT:-}}"
config_root="${HERMES_REPO_AGENT_ACTIVE_CONFIG_ROOT:-${HERMES_REPO_AGENT_PARITY_CONFIG_ROOT:-}}"
render_root="${HERMES_REPO_AGENT_RENDER_ROOT:-${HERMES_REPO_AGENT_PARITY_RENDER_ROOT:-}}"
[[ -n "$active_plist_root" ]] && parity_args+=(--active-plist-root "$active_plist_root")
[[ -n "$render_root" ]] && parity_args+=(--render-root "$render_root")
[[ -n "$config_root" ]] && parity_args+=(--active-config-root "$config_root")
[[ -n "${HERMES_REPO_AGENT_DEPLOYMENT_MANIFEST:-}" ]] && parity_args+=(--manifest "$HERMES_REPO_AGENT_DEPLOYMENT_MANIFEST")
python3 "$ROOT/tools/deployment_parity.py" "${parity_args[@]}" >/dev/null
bash -n "$ROOT/scripts/repo_agent_health.sh"
bash -n "$ROOT/scripts/repo_agent_status.sh"
bash -n "$ROOT/scripts/repo_agent_hermes_update.sh"
bash -n "$ROOT/scripts/repo_agent_repos.sh"
bash -n "$ROOT/scripts/repo_agent_smoke.sh"

python3 -m py_compile \
  "$ROOT/src/repo_agent/tick_intake.py" \
  "$ROOT/src/repo_agent/tick_dispatch.py" \
  "$ROOT/src/repo_agent/tick_triage.py" \
  "$ROOT/src/repo_agent/tick_cleanup.py" \
  "$ROOT/src/repo_agent/tick_all.py"

grep -Fq 'repo-agent-tick-all' "$ROOT/pyproject.toml"
grep -Fq 'Fala gate' "$ROOT/scripts/repo_agent_health.sh"
grep -Fq 'Fala gate' "$ROOT/scripts/repo_agent_status.sh"
grep -Fq 'Recent Decisions' "$ROOT/scripts/repo_agent_status.sh"
grep -Fq 'hermes update --backup --yes' "$ROOT/scripts/repo_agent_hermes_update.sh"
grep -Fq 'repo-agent-tick-all' "$ROOT/templates/launchd/oss-repo-agent-fala-tick-all.plist.template"


if [[ "${HERMES_REPO_AGENT_SMOKE_MODEL:-0}" == 1 ]]; then
  provider="${HERMES_REPO_AGENT_SMOKE_PROVIDER:-custom}"
  model="${HERMES_REPO_AGENT_SMOKE_MODEL_NAME:-auto/claude-sonnet}"
  response="$(
    cd /tmp
    HERMES_ACCEPT_HOOKS=1 hermes --provider "$provider" -m "$model" --ignore-rules -z 'Respond exactly OK'
  )"
  [[ "$response" == OK ]] || {
    printf 'repo-agent model smoke failed provider=%s model=%s response=%s\n' "$provider" "$model" "$response" >&2
    exit 1
  }
fi

if [[ "${HERMES_REPO_AGENT_SMOKE_HEALTH:-0}" == 1 ]]; then
  "$ROOT/scripts/repo_agent_health.sh"
fi

printf '%s\n' 'repo-agent smoke ok'
