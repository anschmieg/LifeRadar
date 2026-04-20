#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_command node
load_local_env
ensure_local_dirs

if [[ -z "${LIFERADAR_MATRIX_HOMESERVER_URL:-}" ]]; then
  echo "Set LIFERADAR_MATRIX_HOMESERVER_URL in ${ENV_FILE} before starting auth." >&2
  exit 1
fi

node "${REPO_ROOT}/bin/oauth-device-flow.mjs" \
  --homeserver "${LIFERADAR_MATRIX_HOMESERVER_URL}" \
  --output "${HOST_SESSION_PATH}" \
  "$@"
