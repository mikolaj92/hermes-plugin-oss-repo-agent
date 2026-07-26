#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTIVE_SCRIPTS="${HERMES_LOKAY_ACTIVE_SCRIPTS:-${HOME:-/Users/mini-m4-main}/.hermes/scripts}"
parity_args=(
  --source-root "${HERMES_LOKAY_PARITY_SOURCE_ROOT:-$ROOT/scripts}"
  --active-root "$ACTIVE_SCRIPTS"
  --template-root "${HERMES_LOKAY_PARITY_TEMPLATE_ROOT:-$ROOT/templates/launchd}"
)
active_plist_root="${HERMES_LOKAY_ACTIVE_PLIST_ROOT:-${HERMES_LOKAY_PARITY_PLIST_ROOT:-}}"
config_root="${HERMES_LOKAY_ACTIVE_CONFIG_ROOT:-${HERMES_LOKAY_PARITY_CONFIG_ROOT:-}}"
render_root="${HERMES_LOKAY_RENDER_ROOT:-${HERMES_LOKAY_PARITY_RENDER_ROOT:-}}"
[[ -n "$active_plist_root" ]] && parity_args+=(--active-plist-root "$active_plist_root")
[[ -n "$render_root" ]] && parity_args+=(--render-root "$render_root")
[[ -n "$config_root" ]] && parity_args+=(--active-config-root "$config_root")
[[ -n "${HERMES_LOKAY_DEPLOYMENT_MANIFEST:-}" ]] && parity_args+=(--manifest "$HERMES_LOKAY_DEPLOYMENT_MANIFEST")
python3 "$ROOT/tools/deployment_parity.py" "${parity_args[@]}" >/dev/null
bash -n "$ROOT/scripts/lokay_health.sh"
bash -n "$ROOT/scripts/lokay_status.sh"
bash -n "$ROOT/scripts/lokay_hermes_update.sh"
bash -n "$ROOT/scripts/lokay_repos.sh"
bash -n "$ROOT/scripts/lokay_smoke.sh"

python3 -m py_compile \
  "$ROOT/src/lokay/tick_intake.py" \
  "$ROOT/src/lokay/tick_dispatch.py" \
  "$ROOT/src/lokay/tick_triage.py" \
  "$ROOT/src/lokay/tick_cleanup.py" \
  "$ROOT/src/lokay/tick_all.py"

grep -Fq 'lokay-tick-all' "$ROOT/pyproject.toml"
grep -Fq 'fala-deployment' "$ROOT/scripts/lokay_health.sh"
grep -Fq 'Fala gate' "$ROOT/scripts/lokay_status.sh"
grep -Fq 'Recent Decisions' "$ROOT/scripts/lokay_status.sh"
grep -Fq 'hermes update --backup --yes' "$ROOT/scripts/lokay_hermes_update.sh"
grep -Fq 'lokay-tick-all' "$ROOT/templates/launchd/lokay-fala-tick-all.plist.template"


if [[ "${HERMES_LOKAY_SMOKE_MODEL:-0}" == 1 ]]; then
  provider="${HERMES_LOKAY_SMOKE_PROVIDER:-custom}"
  model="${HERMES_LOKAY_SMOKE_MODEL_NAME:-auto/claude-sonnet}"
  response="$(
    cd /tmp
    HERMES_ACCEPT_HOOKS=1 hermes --provider "$provider" -m "$model" --ignore-rules -z 'Respond exactly OK'
  )"
  [[ "$response" == OK ]] || {
    printf 'lokay model smoke failed provider=%s model=%s response=%s\n' "$provider" "$model" "$response" >&2
    exit 1
  }
fi

if [[ "${HERMES_LOKAY_SMOKE_HEALTH:-0}" == 1 ]]; then
  "$ROOT/scripts/lokay_health.sh"
fi

printf '%s\n' 'lokay smoke ok'
