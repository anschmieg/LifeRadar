#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_command docker
load_local_env
ensure_local_dirs

compose_local config >/dev/null
compose_local_matrix up -d --build

wait_for_http_json "$(api_url)/health"
wait_for_http_json "$(bridge_url)/health"

if [[ ! -f "${HOST_SESSION_PATH}" ]]; then
  echo "Local stack is up. No Matrix session found at ${HOST_SESSION_PATH} yet."
  echo "Run bin/localdev/matrix-auth.sh next."
else
  echo "Local stack is up and session file is present at ${HOST_SESSION_PATH}."
fi

compose_local ps
