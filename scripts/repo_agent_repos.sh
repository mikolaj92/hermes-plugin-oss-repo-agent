#!/usr/bin/env bash

# Shared repo mapping for the mini-m4-0 repo-agent runtime.
# Format: repo|board|clone_path|priority

repo_agent_default_repos() {
  cat <<'REPOS'
mikolaj92/Fala|mikolaj92-fala|/Users/mini-m4-main/Developer/hermes-repos/Fala|100
mikolaj92/datasource-kit|mikolaj92-datasource-kit|/Users/mini-m4-main/Developer/hermes-repos/datasource-kit|90
mikolaj92/reviewkit|mikolaj92-reviewkit|/Users/mini-m4-main/Developer/hermes-repos/reviewkit|80
mikolaj92/anonimizator3000|mikolaj92-anonimizator3000|/Users/mini-m4-main/Developer/hermes-repos/anonimizator3000|70
mikolaj92/splot|mikolaj92-splot|/Users/mini-m4-main/Developer/hermes-repos/splot|60
mikolaj92/msds-portal|mikolaj92-msds-portal|/Users/mini-m4-main/Developer/hermes-repos/msds-portal|50
mikolaj92/OpenAPITransportKit|mikolaj92-openapi-transport-kit|/Users/mini-m4-main/Developer/hermes-repos/OpenAPITransportKit|45
mikolaj92/swift-openapi-dynamic|mikolaj92-swift-openapi-dynamic|/Users/mini-m4-main/Developer/hermes-repos/swift-openapi-dynamic|40
mikolaj92/my-auth|mikolaj92-my-auth|/Users/mini-m4-main/Developer/hermes-repos/my-auth|30
mikolaj92/my-usermanager|mikolaj92-my-usermanager|/Users/mini-m4-main/Developer/hermes-repos/my-usermanager|30
mikolaj92/Posejdon|mikolaj92-posejdon|/Users/mini-m4-main/Developer/hermes-repos/Posejdon|25
mikolaj92/jaskiniowiec|mikolaj92-jaskiniowiec|/Users/mini-m4-main/Developer/hermes-repos/jaskiniowiec|25
mikolaj92/hermes-plugin-oss-repo-agent|mikolaj92-hermes-plugin-oss-repo-agent|/Users/mini-m4-main/Developer/hermes-repos/hermes-plugin-oss-repo-agent|20
mikolaj92/hermes-plugin-build-in-public|mikolaj92-hermes-plugin-build-in-public|/Users/mini-m4-main/Developer/hermes-repos/hermes-plugin-build-in-public|20
mikolaj92/VibeFront|mikolaj92-vibe-front|/Users/mini-m4-main/Developer/hermes-repos/VibeFront|20
mikolaj92/hermetic-alchemy|mikolaj92-hermetic-alchemy|/Users/mini-m4-main/Developer/hermes-repos/hermetic-alchemy|20
mikolaj92/Temida|mikolaj92-temida|/Users/mini-m4-main/Developer/hermes-repos/Temida|15
mikolaj92/rnkstr|mikolaj92-rnkstr|/Users/mini-m4-main/Developer/hermes-repos/rnkstr|15
mikolaj92/emitype|mikolaj92-emitype|/Users/mini-m4-main/Developer/hermes-repos/emitype|15
mikolaj92/MikoDukcja|mikolaj92-miko-dukcja|/Users/mini-m4-main/Developer/hermes-repos/MikoDukcja|15
mikolaj92/dotfiles|mikolaj92-dotfiles|/Users/mini-m4-main/Developer/hermes-repos/dotfiles|5
REPOS
}

repo_agent_repos() {
  local source="${HERMES_REPO_AGENT_REPOS_FILE:-}"
  local content
  if [[ -n "$source" ]]; then
    [[ -f "$source" && -r "$source" ]] || { printf 'registry-error path=%s\n' "$source" >&2; return 1; }
    content="$(grep -Ev '^[[:space:]]*(#|$)' "$source")" || { printf 'registry-error path=%s\n' "$source" >&2; return 1; }
  else
    content="$(repo_agent_default_repos)" || return 1
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

repo_agent_board_for_repo() {
  local wanted="$1" repo board clone priority entries
  entries="$(repo_agent_repos)" || return 1
  while IFS='|' read -r repo board clone priority; do
    [[ "$repo" == "$wanted" ]] || continue
    printf '%s\n' "$board"
    return 0
  done <<<"$entries"
  return 1
}

repo_agent_clone_for_repo() {
  local wanted="$1" repo board clone priority entries
  entries="$(repo_agent_repos)" || return 1
  while IFS='|' read -r repo board clone priority; do
    [[ "$repo" == "$wanted" ]] || continue
    printf '%s\n' "$clone"
    return 0
  done <<<"$entries"
  return 1
}

repo_agent_priority_for_repo() {
  local wanted="$1" repo board clone priority entries
  entries="$(repo_agent_repos)" || return 1
  while IFS='|' read -r repo board clone priority; do
    [[ "$repo" == "$wanted" ]] || continue
    printf '%s\n' "$priority"
    return 0
  done <<<"$entries"
  return 1
}

repo_agent_kanban_priority_for_text() {
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
