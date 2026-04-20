#!/usr/bin/env bash
set -euo pipefail

: "${LIFERADAR_DB_HOST:=liferadar-db}"
: "${LIFERADAR_DB_PORT:=5432}"
: "${LIFERADAR_DB_NAME:=life_radar}"
: "${LIFERADAR_DB_USER:=life_radar}"
: "${LIFERADAR_DB_PASSWORD:=change-me-in-env}"
: "${LIFERADAR_MATRIX_CANDIDATE_ID:=matrix-nio-current}"
: "${LIFERADAR_MATRIX_CANDIDATE_TYPE:=matrix-native}"
: "${MATRIX_MESSAGES_DB:=/app/workspace/workspace_data/messages.db}"
: "${MATRIX_SESSION_PATH:=/app/identity/matrix-session.json}"
: "${MATRIX_NIO_STORE:=/app/identity/nio-store}"
: "${LIFERADAR_REPORT_DIR:=/app/workspace/liferadar/reports}"

export PGPASSWORD="$LIFERADAR_DB_PASSWORD"
mkdir -p "$LIFERADAR_REPORT_DIR"

observed_at="$(date -u +%FT%TZ)"
status="fail"
notes=""
notes_sql="NULL"
latency_ms=0
freshness_seconds=0
running_processes=0
total_events=0
decrypt_failures=0
encrypted_non_text=0
max_timestamp=0
min_timestamp=0

if [ -f "$MATRIX_MESSAGES_DB" ]; then
  metrics="$(sqlite3 "$MATRIX_MESSAGES_DB" "select count(*), coalesce(sum(case when instr(body,'[decryption failed:')=1 then 1 else 0 end),0), coalesce(sum(case when instr(body,'[encrypted non-text:')=1 then 1 else 0 end),0), coalesce(max(timestamp),0), coalesce(min(timestamp),0) from messages;")"
  IFS='|' read -r total_events decrypt_failures encrypted_non_text max_timestamp min_timestamp <<<"$metrics"
fi

if [ "$max_timestamp" -gt 0 ]; then
  now_epoch="$(date -u +%s)"
  freshness_seconds=$((now_epoch - max_timestamp))
fi

running_processes="$( (ps -eo args= | grep -E 'matrix_(e2e_daemon|triage_worker|sync)' | grep -v grep | wc -l | tr -d ' ') || true )"

if [ ! -f "$MATRIX_MESSAGES_DB" ]; then
  notes="messages.db missing"
elif [ ! -f "$MATRIX_SESSION_PATH" ]; then
  notes="matrix session missing"
elif [ ! -d "$MATRIX_NIO_STORE" ]; then
  notes="nio store missing"
elif [ "$total_events" -eq 0 ]; then
  notes="no events ingested"
elif [ "$running_processes" -eq 0 ]; then
  status="warn"
  notes="message corpus exists but no matrix runtime process is running"
elif [ "$freshness_seconds" -gt 900 ]; then
  status="warn"
  notes="runtime is stale beyond 15 minutes"
else
  status="ok"
  notes="runtime is live enough for continued evaluation"
fi

if [ -n "$notes" ]; then
  notes_sql="\$\$${notes//$/\\$}\$\$"
fi

psql \
  --host "$LIFERADAR_DB_HOST" \
  --port "$LIFERADAR_DB_PORT" \
  --username "$LIFERADAR_DB_USER" \
  --dbname "$LIFERADAR_DB_NAME" \
  --set ON_ERROR_STOP=1 <<SQL
INSERT INTO life_radar.runtime_probes (
  candidate_id,
  candidate_type,
  status,
  observed_at,
  latency_ms,
  freshness_seconds,
  total_events,
  decrypt_failures,
  encrypted_non_text,
  running_processes,
  metadata,
  notes
) VALUES (
  '${LIFERADAR_MATRIX_CANDIDATE_ID}',
  '${LIFERADAR_MATRIX_CANDIDATE_TYPE}',
  '${status}',
  '${observed_at}',
  ${latency_ms},
  ${freshness_seconds},
  ${total_events},
  ${decrypt_failures},
  ${encrypted_non_text},
  ${running_processes},
  jsonb_build_object(
    'messages_db', '${MATRIX_MESSAGES_DB}',
    'session_path', '${MATRIX_SESSION_PATH}',
    'nio_store', '${MATRIX_NIO_STORE}',
    'max_timestamp', ${max_timestamp},
    'min_timestamp', ${min_timestamp}
  ),
  ${notes_sql}
);

INSERT INTO life_radar.messaging_candidates (
  candidate_id,
  candidate_type,
  last_status,
  last_probe_at,
  latest_freshness_seconds,
  latest_total_events,
  latest_decrypt_failures,
  latest_encrypted_non_text,
  latest_running_processes,
  latest_notes,
  metadata,
  updated_at
) VALUES (
  '${LIFERADAR_MATRIX_CANDIDATE_ID}',
  '${LIFERADAR_MATRIX_CANDIDATE_TYPE}',
  '${status}',
  '${observed_at}',
  ${freshness_seconds},
  ${total_events},
  ${decrypt_failures},
  ${encrypted_non_text},
  ${running_processes},
  ${notes_sql},
  jsonb_build_object(
    'messages_db', '${MATRIX_MESSAGES_DB}',
    'session_path', '${MATRIX_SESSION_PATH}',
    'nio_store', '${MATRIX_NIO_STORE}'
  ),
  NOW()
)
ON CONFLICT (candidate_id) DO UPDATE SET
  last_status = EXCLUDED.last_status,
  last_probe_at = EXCLUDED.last_probe_at,
  latest_freshness_seconds = EXCLUDED.latest_freshness_seconds,
  latest_total_events = EXCLUDED.latest_total_events,
  latest_decrypt_failures = EXCLUDED.latest_decrypt_failures,
  latest_encrypted_non_text = EXCLUDED.latest_encrypted_non_text,
  latest_running_processes = EXCLUDED.latest_running_processes,
  latest_notes = EXCLUDED.latest_notes,
  metadata = EXCLUDED.metadata,
  updated_at = NOW();
SQL

report_path="$LIFERADAR_REPORT_DIR/matrix-bakeoff-latest.md"
mkdir -p "$LIFERADAR_REPORT_DIR"
tmp_report="$(mktemp "$LIFERADAR_REPORT_DIR/.matrix-bakeoff-latest.XXXXXX")"
cat > "$tmp_report" <<REPORT
# Matrix Candidate Report

- observed_at: ${observed_at}
- candidate_id: ${LIFERADAR_MATRIX_CANDIDATE_ID}
- candidate_type: ${LIFERADAR_MATRIX_CANDIDATE_TYPE}
- status: ${status}
- notes: ${notes:-none}
- total_events: ${total_events}
- decrypt_failures: ${decrypt_failures}
- encrypted_non_text: ${encrypted_non_text}
- freshness_seconds: ${freshness_seconds}
- running_processes: ${running_processes}
- messages_db: ${MATRIX_MESSAGES_DB}
- session_path: ${MATRIX_SESSION_PATH}
- nio_store: ${MATRIX_NIO_STORE}
REPORT
mv "$tmp_report" "$report_path"
