#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_command docker
load_local_env

compose_local down -v --remove-orphans || true
rm -rf \
  "${HOST_IDENTITY_DIR}" \
  "${HOST_WORKSPACE_DIR}" \
  "${HOST_CONNECTORS_DIR}" \
  "${HOST_OUTLOOK_CACHE_DIR}" \
  "${HOST_POSTGRES_DIR}"

echo "Cleared local Matrix harness state under ${HOST_DATA_DIR}."
