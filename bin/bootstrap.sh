#!/usr/bin/env bash
set -euo pipefail

: "${LIFE_RADAR_DB_HOST:=life-radar-db}"
: "${LIFE_RADAR_DB_PORT:=5432}"
: "${LIFE_RADAR_DB_NAME:=life_radar}"
: "${LIFE_RADAR_DB_USER:=life_radar}"
: "${LIFE_RADAR_DB_PASSWORD:=change-me-in-env}"

export PGPASSWORD="$LIFE_RADAR_DB_PASSWORD"
SCHEMA_PATH="/opt/life-radar/schema.sql"
IDENTITY_DIR="/app/identity"
WORKSPACE_DIR="/app/workspace"
LEGACY_OPENCLAW_ROOT="/home/node/.openclaw"

# The worker persists state on /app volumes, but parts of the Matrix toolchain
# still reference the historical OpenClaw paths. Recreate that layout so both
# legacy env values and the new canonical /app paths resolve to the same files.
mkdir -p "$IDENTITY_DIR" "$WORKSPACE_DIR/workspace_data" "$WORKSPACE_DIR/life-radar/reports"
mkdir -p "$LEGACY_OPENCLAW_ROOT"
ln -sfn "$IDENTITY_DIR" "$LEGACY_OPENCLAW_ROOT/identity"
ln -sfn "$WORKSPACE_DIR" "$LEGACY_OPENCLAW_ROOT/workspace"

# Workaround: if LIFE_RADAR_DB_HOST is the short name "life-radar-db",
# try to resolve the full container name. Docker's embedded DNS sometimes
# doesn't resolve short container aliases.
if [ "$LIFE_RADAR_DB_HOST" = "life-radar-db" ]; then
  for suffix in "-pr-5" "-pr-4" "-pr-3" "-pr-2" "-pr-1" ""; do
    candidate="life-radar-db${suffix}"
    if pg_isready -h "$candidate" -p "$LIFE_RADAR_DB_PORT" -U "$LIFE_RADAR_DB_USER" -d "$LIFE_RADAR_DB_NAME" >/dev/null 2>&1; then
      echo "Resolved DB host: $candidate"
      export LIFE_RADAR_DB_HOST="$candidate"
      break
    fi
  done
fi

until pg_isready -h "$LIFE_RADAR_DB_HOST" -p "$LIFE_RADAR_DB_PORT" -U "$LIFE_RADAR_DB_USER" -d "$LIFE_RADAR_DB_NAME" >/dev/null 2>&1; do
  echo "waiting for life-radar-db at ${LIFE_RADAR_DB_HOST}:${LIFE_RADAR_DB_PORT}"
  sleep 2
done

psql \
  --host "$LIFE_RADAR_DB_HOST" \
  --port "$LIFE_RADAR_DB_PORT" \
  --username "$LIFE_RADAR_DB_USER" \
  --dbname "$LIFE_RADAR_DB_NAME" \
  --set ON_ERROR_STOP=1 \
  --file "$SCHEMA_PATH"

/opt/life-radar/bin/backfill-matrix-history.sh || true
/opt/life-radar/bin/prune-matrix-noise-events.sh || true
/opt/life-radar/bin/graph-sync-mail.mjs || true
/opt/life-radar/bin/google-calendar-ingest.mjs || true
/opt/life-radar/bin/derive-needs-state.sh || true
/opt/life-radar/bin/extract-memory.mjs || true
/opt/life-radar/bin/google-calendar-reconcile.mjs || true
