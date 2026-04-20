#!/usr/bin/env bash
set -euo pipefail

LOCALDEV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${LOCALDEV_DIR}/../.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/docker-compose.local.yaml"
ENV_FILE="${LIFERADAR_LOCAL_ENV_FILE:-${REPO_ROOT}/.env.local}"
LOCAL_MATRIX_SERVICES=(
  liferadar-db
  liferadar-api
  liferadar-worker
  liferadar-matrix-bridge
)
HOST_DATA_DIR="${REPO_ROOT}/data"
HOST_IDENTITY_DIR="${HOST_DATA_DIR}/identity"
HOST_WORKSPACE_DIR="${HOST_DATA_DIR}/workspace"
HOST_CONNECTORS_DIR="${HOST_DATA_DIR}/connectors"
HOST_OUTLOOK_CACHE_DIR="${HOST_DATA_DIR}/outlook-token-cache"
HOST_POSTGRES_DIR="${HOST_DATA_DIR}/postgres"
HOST_SESSION_PATH="${HOST_IDENTITY_DIR}/matrix-session.json"

require_command() {
  local name="$1"
  if ! command -v "$name" >/dev/null 2>&1; then
    echo "Missing required command: $name" >&2
    exit 1
  fi
}

ensure_env_file() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    echo "Missing env file: ${ENV_FILE}" >&2
    echo "Copy ${REPO_ROOT}/.env.local.example to ${ENV_FILE} and fill in the Matrix homeserver." >&2
    exit 1
  fi
}

load_local_env() {
  ensure_env_file
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
}

compose_local() {
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
}

compose_local_matrix() {
  compose_local "$@" "${LOCAL_MATRIX_SERVICES[@]}"
}

ensure_local_dirs() {
  mkdir -p \
    "${HOST_IDENTITY_DIR}" \
    "${HOST_WORKSPACE_DIR}/workspace_data" \
    "${HOST_WORKSPACE_DIR}/liferadar/reports" \
    "${HOST_CONNECTORS_DIR}" \
    "${HOST_OUTLOOK_CACHE_DIR}" \
    "${HOST_POSTGRES_DIR}"
}

api_url() {
  local port="${LIFERADAR_API_PORT:-18000}"
  printf 'http://127.0.0.1:%s' "$port"
}

bridge_url() {
  local port="${LIFERADAR_MATRIX_BRIDGE_PORT:-18010}"
  printf 'http://127.0.0.1:%s' "$port"
}

db_query() {
  local sql="$1"
  compose_local exec -T liferadar-db psql \
    -U "${LIFERADAR_DB_USER:-life_radar}" \
    -d "${LIFERADAR_DB_NAME:-life_radar}" \
    -tA \
    -c "$sql"
}

python_json() {
  local expression="$1"
  python3 -c "import json,sys; data=json.load(sys.stdin); value=${expression}; print(value if not isinstance(value, (dict, list)) else json.dumps(value))"
}

wait_for_http_json() {
  local url="$1"
  local attempts="${2:-40}"
  local sleep_seconds="${3:-2}"
  local i
  for ((i=0; i<attempts; i+=1)); do
    if curl --silent --show-error --fail "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$sleep_seconds"
  done
  echo "Timed out waiting for ${url}" >&2
  return 1
}
