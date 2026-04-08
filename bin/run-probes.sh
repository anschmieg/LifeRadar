#!/usr/bin/env bash
set -euo pipefail

: "${LIFE_RADAR_PROBE_INTERVAL_SEC:=300}"
: "${LIFE_RADAR_MATRIX_RUST_RECOVER_HTTP_ON_FAILURE:=0}"

/opt/life-radar/bin/bootstrap.sh

run_step() {
  local label="$1"
  shift
  if "$@"; then
    echo "[life-radar] ${label}: ok"
    return 0
  fi

  local code=$?
  echo "[life-radar] ${label}: failed (exit ${code})" >&2
  return $code
}

while true; do
  matrix_ingest_failed=0

  run_step "matrix-probe" /usr/local/bin/life-radar-matrix-rust-probe || true
  if ! run_step "matrix-ingest" env LIFE_RADAR_MATRIX_RUST_MODE=ingest_live_history /usr/local/bin/life-radar-matrix-rust-probe; then
    matrix_ingest_failed=1
    if [[ "$LIFE_RADAR_MATRIX_RUST_RECOVER_HTTP_ON_FAILURE" == "1" ]]; then
      run_step "matrix-recover-http" env LIFE_RADAR_MATRIX_RUST_MODE=recover_http /usr/local/bin/life-radar-matrix-rust-probe || true
    fi
  fi

  run_step "msgraph-sync" /opt/life-radar/bin/graph-sync-mail.mjs || true
  run_step "google-calendar-ingest" /opt/life-radar/bin/google-calendar-ingest.mjs || true
  run_step "derive-needs-state" /opt/life-radar/bin/derive-needs-state.sh || true
  run_step "extract-memory" /opt/life-radar/bin/extract-memory.mjs || true
  run_step "google-calendar-reconcile" /opt/life-radar/bin/google-calendar-reconcile.mjs || true

  if [[ "$matrix_ingest_failed" -eq 1 ]]; then
    echo "[life-radar] matrix ingest failed; connector health should now reflect the failure" >&2
  fi

  sleep "$LIFE_RADAR_PROBE_INTERVAL_SEC"
done
