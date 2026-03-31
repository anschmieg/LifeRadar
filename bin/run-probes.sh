#!/usr/bin/env bash
set -euo pipefail

: "${LIFE_RADAR_PROBE_INTERVAL_SEC:=300}"

/opt/life-radar/bin/bootstrap.sh

while true; do
  # Removed nio-current probe - using matrix-rust-sdk only
  /usr/local/bin/life-radar-matrix-rust-probe || true
  env LIFE_RADAR_MATRIX_RUST_MODE=ingest_live_history /usr/local/bin/life-radar-matrix-rust-probe || true
  /opt/life-radar/bin/graph-sync-mail.mjs || true
  /opt/life-radar/bin/derive-needs-state.sh || true
  /opt/life-radar/bin/extract-memory.mjs || true
  /opt/life-radar/bin/google-calendar-reconcile.mjs || true
  sleep "$LIFE_RADAR_PROBE_INTERVAL_SEC"
done
