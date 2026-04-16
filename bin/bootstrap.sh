#!/usr/bin/env bash
set -euo pipefail

: "${LIFERADAR_DB_HOST:=liferadar-db}"
: "${LIFERADAR_DB_PORT:=5432}"
: "${LIFERADAR_DB_NAME:=life_radar}"
: "${LIFERADAR_DB_USER:=life_radar}"
: "${LIFERADAR_DB_PASSWORD:=change-me-in-env}"
: "${LIFERADAR_MATRIX_ENABLED:=true}"

export PGPASSWORD="$LIFERADAR_DB_PASSWORD"
SCHEMA_PATH="/opt/liferadar/schema.sql"
IDENTITY_DIR="/app/identity"
WORKSPACE_DIR="/app/workspace"
LEGACY_OPENCLAW_ROOT="/home/node/.openclaw"

# The worker persists state on /app volumes, but parts of the Matrix toolchain
# still reference the historical OpenClaw paths. Recreate that layout so both
# legacy env values and the new canonical /app paths resolve to the same files.
mkdir -p "$IDENTITY_DIR" "$WORKSPACE_DIR/workspace_data" "$WORKSPACE_DIR/liferadar/reports"
mkdir -p "$LEGACY_OPENCLAW_ROOT"
ln -sfn "$IDENTITY_DIR" "$LEGACY_OPENCLAW_ROOT/identity"
ln -sfn "$WORKSPACE_DIR" "$LEGACY_OPENCLAW_ROOT/workspace"

# Workaround: if LIFERADAR_DB_HOST is the short name "liferadar-db",
# try to resolve the full container name. Docker's embedded DNS sometimes
# doesn't resolve short container aliases.
if [ "$LIFERADAR_DB_HOST" = "liferadar-db" ]; then
  for suffix in "-pr-5" "-pr-4" "-pr-3" "-pr-2" "-pr-1" ""; do
    candidate="liferadar-db${suffix}"
    if pg_isready -h "$candidate" -p "$LIFERADAR_DB_PORT" -U "$LIFERADAR_DB_USER" -d "$LIFERADAR_DB_NAME" >/dev/null 2>&1; then
      echo "Resolved DB host: $candidate"
      export LIFERADAR_DB_HOST="$candidate"
      break
    fi
  done
fi

until pg_isready -h "$LIFERADAR_DB_HOST" -p "$LIFERADAR_DB_PORT" -U "$LIFERADAR_DB_USER" -d "$LIFERADAR_DB_NAME" >/dev/null 2>&1; do
  echo "waiting for liferadar-db at ${LIFERADAR_DB_HOST}:${LIFERADAR_DB_PORT}"
  sleep 2
done

psql \
  --host "$LIFERADAR_DB_HOST" \
  --port "$LIFERADAR_DB_PORT" \
  --username "$LIFERADAR_DB_USER" \
  --dbname "$LIFERADAR_DB_NAME" \
  --set ON_ERROR_STOP=1 \
  --file "$SCHEMA_PATH"

if [[ "${LIFERADAR_MATRIX_ENABLED,,}" != "false" ]]; then
  /opt/liferadar/bin/backfill-matrix-history.sh || true
  /opt/liferadar/bin/prune-matrix-noise-events.sh || true
fi
/opt/liferadar/bin/graph-sync-mail.mjs || true
/opt/liferadar/bin/google-calendar-ingest.mjs || true
/opt/liferadar/bin/derive-needs-state.sh || true
/opt/liferadar/bin/extract-memory.mjs || true
/opt/liferadar/bin/google-calendar-reconcile.mjs || true
