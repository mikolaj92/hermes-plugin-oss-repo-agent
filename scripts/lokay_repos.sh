#!/usr/bin/env bash

# Shared registry access for the mini-m4-0 lokay runtime.
# Format: repo|board|clone_path|priority
# Source of truth: root-canonical TOML via candidate-local
#   python -m lokay.registry --config <path> --format shell

lokay_reject_retired_env() {
  if [[ -n "${HERMES_LOKAY_REPOS_FILE+x}" ]]; then
    printf 'registry-error retired-env=HERMES_LOKAY_REPOS_FILE\n' >&2
    return 1
  fi
  if [[ -n "${HERMES_LOKAY_WORKTREE_ROOT+x}" ]]; then
    printf 'registry-error retired-env=HERMES_LOKAY_WORKTREE_ROOT\n' >&2
    return 1
  fi
  return 0
}

lokay_config_path() {
  local configured="${HERMES_LOKAY_CONFIG:-}"
  if [[ -n "$configured" ]]; then
    printf '%s\n' "$configured"
    return 0
  fi
  printf '%s\n' "${HOME:-/Users/mini-m4-main}/.hermes/lokay/config.toml"
}

lokay_candidate_root() {
  local deployment_root candidate
  deployment_root="${HERMES_LOKAY_DEPLOYMENT_ROOT:-${HOME:-/Users/mini-m4-main}/.hermes/lokay/deployment}"
  if [[ ! -L "$deployment_root/current" ]]; then
    printf 'registry-error missing-current path=%s/current\n' "$deployment_root" >&2
    return 1
  fi
  candidate="$(realpath "$deployment_root/current" 2>/dev/null || true)"
  if [[ -z "$candidate" || ! -d "$candidate" || -L "$candidate" ]]; then
    printf 'registry-error invalid-current path=%s/current\n' "$deployment_root" >&2
    return 1
  fi
  printf '%s\n' "$candidate"
}

lokay_candidate_pythonpath() {
  local candidate
  candidate="$(lokay_candidate_root)" || return 1
  if [[ ! -d "$candidate/source/project/src" || -L "$candidate/source/project/src" ]]; then
    printf 'registry-error source-unavailable path=%s/source/project/src\n' "$candidate" >&2
    return 1
  fi
  printf '%s\n' "$candidate/source/project/src"
}


lokay_managed_python() {
  local candidate managed_python
  candidate="$(lokay_candidate_root)" || return 1
  managed_python="$candidate/source/project/.venv/bin/python"
  if [[ "$managed_python" != /* || ! -x "$managed_python" || -L "$managed_python" ]] \
    || ! "$managed_python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; then
    printf 'registry-error interpreter-unavailable path=%s\n' "$managed_python" >&2
    return 1
  fi
  printf '%s\n' "$managed_python"
}

lokay_repos() {
  lokay_reject_retired_env || return 1
  local config python content status pythonpath
  config="$(lokay_config_path)" || return 1
  if [[ ! -f "$config" || ! -r "$config" || -L "$config" ]]; then
    printf 'registry-error path=%s\n' "$config" >&2
    return 1
  fi
  python="$(lokay_managed_python)" || return 1
  pythonpath="$(lokay_candidate_pythonpath)" || return 1
  status=0
  content="$(PYTHONPATH="$pythonpath" "$python" -m lokay.registry --config "$config" --format shell 2>&1)" || status=$?
  if [[ "$status" -ne 0 ]]; then
    printf 'registry-error config=%s details=%s\n' "$config" "$(printf '%s' "$content" | tr '\n' ' ' | sed 's/  */ /g')" >&2
    return 1
  fi
  [[ -n "$content" ]] || { printf 'registry-error empty config=%s\n' "$config" >&2; return 1; }
  local line repo board clone priority extra
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    IFS='|' read -r repo board clone priority extra <<<"$line"
    [[ -n "$repo" && -n "$board" && -n "$clone" && "$priority" =~ ^[0-9]+$ && -z "${extra:-}" ]] || {
      printf 'registry-error malformed-entry=%s\n' "$line" >&2
      return 1
    }
  done <<<"$content"
  printf '%s\n' "$content"
}

lokay_worktree_root() {
  lokay_reject_retired_env || return 1
  local config python root status pythonpath
  config="$(lokay_config_path)" || return 1
  if [[ ! -f "$config" || ! -r "$config" || -L "$config" ]]; then
    printf 'registry-error path=%s\n' "$config" >&2
    return 1
  fi
  python="$(lokay_managed_python)" || return 1
  pythonpath="$(lokay_candidate_pythonpath)" || return 1
  status=0
  root="$(PYTHONPATH="$pythonpath" "$python" - "$config" <<'PY' 2>&1
from pathlib import Path
import sys

from lokay.registry import ConfigError, load_registry

try:
    document = load_registry(sys.argv[1])
except ConfigError as exc:
    print(f"registry-error: {exc}", file=sys.stderr)
    raise SystemExit(2)
print(str(Path(str(document.data["paths"]["worktree_root"])).expanduser()))
PY
  )" || status=$?
  if [[ "$status" -ne 0 ]]; then
    printf 'registry-error worktree-root config=%s details=%s\n' "$config" "$(printf '%s' "$root" | tr '\n' ' ' | sed 's/  */ /g')" >&2
    return 1
  fi
  [[ -n "$root" && "$root" == /* ]] || {
    printf 'registry-error worktree-root-invalid value=%s\n' "$root" >&2
    return 1
  }
  printf '%s\n' "$root"
}

lokay_board_for_repo() {
  local wanted="$1" repo board clone priority entries
  entries="$(lokay_repos)" || return 1
  while IFS='|' read -r repo board clone priority; do
    [[ "$repo" == "$wanted" ]] || continue
    printf '%s\n' "$board"
    return 0
  done <<<"$entries"
  return 1
}

lokay_clone_for_repo() {
  local wanted="$1" repo board clone priority entries
  entries="$(lokay_repos)" || return 1
  while IFS='|' read -r repo board clone priority; do
    [[ "$repo" == "$wanted" ]] || continue
    printf '%s\n' "$clone"
    return 0
  done <<<"$entries"
  return 1
}

lokay_priority_for_repo() {
  local wanted="$1" repo board clone priority entries
  entries="$(lokay_repos)" || return 1
  while IFS='|' read -r repo board clone priority; do
    [[ "$repo" == "$wanted" ]] || continue
    printf '%s\n' "$priority"
    return 0
  done <<<"$entries"
  return 1
}

lokay_kanban_priority_for_text() {
  local text
  text="$(printf '%s' "$*" | tr '[:upper:]' '[:lower:]')"
  case "$text" in
    *priority:p0*|*critical*|*urgent*|*p0*) printf '0\n' ;;
    *security*|*vulnerability*) printf '0\n' ;;
    *priority:p1*|*high*) printf '1\n' ;;
    *bug*|*regression*|*crash*|*failing*) printf '1\n' ;;
    *priority:p2*|*medium*) printf '2\n' ;;
    *priority:p3*|*low*) printf '4\n' ;;
    *docs*|*documentation*|*readme*) printf '3\n' ;;
    *) printf '1\n' ;;
  esac
}
