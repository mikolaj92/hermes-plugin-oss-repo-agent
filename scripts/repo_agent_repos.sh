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
REPOS
}

repo_agent_repos() {
  if [[ -n "${HERMES_REPO_AGENT_REPOS_FILE:-}" && -f "$HERMES_REPO_AGENT_REPOS_FILE" ]]; then
    grep -Ev '^[[:space:]]*(#|$)' "$HERMES_REPO_AGENT_REPOS_FILE"
  else
    repo_agent_default_repos
  fi
}

repo_agent_board_for_repo() {
  local wanted="$1" repo board clone priority
  while IFS='|' read -r repo board clone priority; do
    [[ "$repo" == "$wanted" ]] || continue
    printf '%s\n' "$board"
    return 0
  done < <(repo_agent_repos)
  return 1
}

repo_agent_clone_for_repo() {
  local wanted="$1" repo board clone priority
  while IFS='|' read -r repo board clone priority; do
    [[ "$repo" == "$wanted" ]] || continue
    printf '%s\n' "$clone"
    return 0
  done < <(repo_agent_repos)
  return 1
}

repo_agent_priority_for_repo() {
  local wanted="$1" repo board clone priority
  while IFS='|' read -r repo board clone priority; do
    [[ "$repo" == "$wanted" ]] || continue
    printf '%s\n' "${priority:-0}"
    return 0
  done < <(repo_agent_repos)
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
