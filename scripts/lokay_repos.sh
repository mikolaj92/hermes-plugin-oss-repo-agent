#!/usr/bin/env bash

# Shared repo mapping for the mini-m4-0 lokay runtime.
# Format: repo|board|clone_path|priority

lokay_default_repos() {
  cat <<'REPOS'
mikolaj92/Fala|mikolaj92-fala|/Users/mini-m4-main/Developer/hermes-repos/Fala-live|100
mikolaj92/datasource-kit|mikolaj92-datasource-kit|/Users/mini-m4-main/Developer/hermes-repos/datasource-kit-live|90
mikolaj92/reviewkit|mikolaj92-reviewkit|/Users/mini-m4-main/Developer/hermes-repos/reviewkit-live|80
mikolaj92/msds-portal|mikolaj92-msds-portal|/Users/mini-m4-main/Developer/hermes-repos/msds-portal-live|50
mikolaj92/app-factory|mikolaj92-app-factory|/Users/mini-m4-main/Developer/hermes-repos/app-factory-live|45
mikolaj92/basecoat-factory|mikolaj92-basecoat-factory|/Users/mini-m4-main/Developer/hermes-repos/basecoat-factory-live|40
mikolaj92/my-auth|mikolaj92-my-auth|/Users/mini-m4-main/Developer/hermes-repos/my-auth-live|30
mikolaj92/my-usermanager|mikolaj92-my-usermanager|/Users/mini-m4-main/Developer/hermes-repos/my-usermanager-live|30
mikolaj92/Posejdon|mikolaj92-posejdon|/Users/mini-m4-main/Developer/hermes-repos/Posejdon-live|25
mikolaj92/lokay|mikolaj92-lokay|/Users/mini-m4-main/Developer/hermes-repos/lokay-live|20
mikolaj92/influenzer|mikolaj92-influenzer|/Users/mini-m4-main/Developer/hermes-repos/influenzer-live|20
mikolaj92/wolnyrolnik|mikolaj92-wolnyrolnik|/Users/mini-m4-main/Developer/hermes-repos/wolnyrolnik-live|18
mikolaj92/Temida|mikolaj92-temida|/Users/mini-m4-main/Developer/hermes-repos/Temida-repo-agent-live|15
mikolaj92/emitype|mikolaj92-emitype|/Users/mini-m4-main/Developer/hermes-repos/emitype-live|15
mikolaj92/rnkstr|mikolaj92-rnkstr|/Users/mini-m4-main/Developer/hermes-repos/rnkstr-live|15
REPOS
}

lokay_repos() {
  local source="${HERMES_LOKAY_REPOS_FILE:-}"
  local content
  if [[ -n "$source" ]]; then
    [[ -f "$source" && -r "$source" ]] || { printf 'registry-error path=%s\n' "$source" >&2; return 1; }
    content="$(grep -Ev '^[[:space:]]*(#|$)' "$source")" || { printf 'registry-error path=%s\n' "$source" >&2; return 1; }
  else
    content="$(lokay_default_repos)" || return 1
  fi
  local line repo board clone priority extra
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    IFS='|' read -r repo board clone priority extra <<<"$line"
    [[ -n "$repo" && -n "$board" && -n "$clone" && "$priority" =~ ^[0-9]+$ && -z "${extra:-}" ]] || {
      printf 'registry-error malformed-entry=%s\n' "$line" >&2; return 1;
    }
  done <<<"$content"
  printf '%s\n' "$content"
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
