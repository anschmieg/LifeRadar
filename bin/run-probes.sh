#!/usr/bin/env bash
set -euo pipefail

: "${LIFERADAR_PROBE_INTERVAL_SEC:=300}"
: "${LIFERADAR_MATRIX_RUST_RECOVER_HTTP_ON_FAILURE:=1}"
: "${LIFERADAR_MATRIX_ENABLED:=true}"

/opt/liferadar/bin/bootstrap.sh

run_step() {
  local label="$1"
  shift
  if "$@"; then
    echo "[liferadar] ${label}: ok"
    return 0
  fi

  local code=$?
  echo "[liferadar] ${label}: failed (exit ${code})" >&2
  return $code
}

while true; do
  matrix_ingest_failed=0

  if [[ "${LIFERADAR_MATRIX_ENABLED,,}" != "false" ]]; then
    run_step "matrix-probe" /usr/local/bin/liferadar-matrix || true
    if ! run_step "matrix-ingest" env LIFERADAR_MATRIX_RUST_MODE=ingest_live_history /usr/local/bin/liferadar-matrix; then
      matrix_ingest_failed=1
      if [[ "$LIFERADAR_MATRIX_RUST_RECOVER_HTTP_ON_FAILURE" == "1" ]]; then
        run_step "matrix-recover-http" env LIFERADAR_MATRIX_RUST_MODE=recover_http /usr/local/bin/liferadar-matrix || true
      fi
    fi
  fi

  run_step "msgraph-sync" /opt/liferadar/bin/graph-sync-mail.mjs || true
  run_step "google-calendar-ingest" /opt/liferadar/bin/google-calendar-ingest.mjs || true
  run_step "derive-needs-state" /opt/liferadar/bin/derive-needs-state.sh || true
  run_step "extract-memory" /opt/liferadar/bin/extract-memory.mjs || true
  run_step "google-calendar-reconcile" /opt/liferadar/bin/google-calendar-reconcile.mjs || true

  if [[ "$matrix_ingest_failed" -eq 1 ]]; then
    echo "[liferadar] matrix ingest failed; connector health should now reflect the failure" >&2
  fi

  sleep "$LIFERADAR_PROBE_INTERVAL_SEC"
done
