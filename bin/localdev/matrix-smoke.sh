#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_command curl
require_command docker
require_command python3
load_local_env
ensure_local_dirs

EXPECTED_CANDIDATE_ID="${LIFERADAR_MATRIX_RUST_CANDIDATE_ID:-matrix-rust-sdk}"

RUN_DECRYPTION=0
RUN_LEGACY_IMPORT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --decryption)
      RUN_DECRYPTION=1
      ;;
    --legacy-import)
      RUN_LEGACY_IMPORT=1
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
  shift
done

compose_local_matrix up -d
wait_for_http_json "$(api_url)/health"
wait_for_http_json "$(bridge_url)/health"

assert_json_value() {
  local url="$1"
  local expression="$2"
  local expected="$3"
  local actual
  actual="$(curl --silent --show-error --fail "$url" | python_json "$expression")"
  if [[ "${actual}" != "${expected}" ]]; then
    echo "Assertion failed for ${url}: expected ${expected}, got ${actual}" >&2
    exit 1
  fi
}

run_worker_mode() {
  local mode="$1"
  shift
  compose_local exec -T liferadar-worker env LIFERADAR_MATRIX_RUST_MODE="${mode}" "$@" /usr/local/bin/liferadar-matrix
}

missing_session_output="$(
  set +e
  compose_local run --rm --no-deps -T \
    -e MATRIX_RUST_SESSION_PATH=/tmp/does-not-exist-session.json \
    --entrypoint /usr/local/bin/liferadar-matrix \
    liferadar-worker 2>&1
  status=$?
  printf '\n__EXIT__=%s\n' "$status"
)"
missing_session_status="$(printf '%s' "${missing_session_output}" | awk -F= '/__EXIT__=/{print $2}' | tail -n1)"
missing_session_text="$(printf '%s' "${missing_session_output}" | sed '/__EXIT__=/d')"
if [[ "${missing_session_status}" != "0" ]]; then
  if [[ "${missing_session_text}" != *"failed to read"* ]]; then
    echo "Missing-session check did not emit the expected read failure." >&2
    printf '%s\n' "${missing_session_text}" >&2
    exit 1
  fi
elif [[ "${missing_session_text}" != *$"${EXPECTED_CANDIDATE_ID}"$'\tfail'* ]]; then
  echo "Missing-session check did not fail clearly." >&2
  printf '%s\n' "${missing_session_text}" >&2
  exit 1
fi

if [[ ! -f "${HOST_SESSION_PATH}" ]]; then
  echo "Matrix session file is missing at ${HOST_SESSION_PATH}. Run bin/localdev/matrix-auth.sh first." >&2
  exit 1
fi

assert_json_value "$(api_url)/health" "data['status']" "ok"
assert_json_value "$(bridge_url)/health" "data['status']" "ok"

probe_output="$(run_worker_mode probe)"
if [[ "${probe_output}" != *$"${EXPECTED_CANDIDATE_ID}"$'\t'* ]]; then
  echo "Probe output was unexpected:" >&2
  printf '%s\n' "${probe_output}" >&2
  exit 1
fi

inspect_recent_output="$(run_worker_mode inspect_recent)"
printf '%s' "${inspect_recent_output}" | python3 -c "import json,sys; data=json.load(sys.stdin); assert 'rooms' in data"
recent_room_id="$(printf '%s' "${inspect_recent_output}" | python_json "data['rooms'][0]['room_id'] if data.get('rooms') else ''")"

ingest_output="$(run_worker_mode ingest_live_history)"
if [[ "${ingest_output}" != matrix\ SDK\ ingest\ complete:* ]]; then
  echo "Ingest output was unexpected:" >&2
  printf '%s\n' "${ingest_output}" >&2
  exit 1
fi

checkpoint_value="$(db_query "select value->>'next_batch' from life_radar.runtime_metadata where key = 'matrix_sync_checkpoint';")"
if [[ -z "${checkpoint_value}" ]]; then
  echo "matrix_sync_checkpoint was not written." >&2
  exit 1
fi

conversation_count="$(db_query "select count(*) from life_radar.conversations where source = 'matrix';")"
if [[ "${conversation_count}" -lt 1 ]]; then
  echo "No Matrix conversations were ingested." >&2
  exit 1
fi

event_count_before="$(db_query "select count(*) from life_radar.message_events where source = 'matrix';")"
run_worker_mode ingest_live_history >/dev/null
event_count_after="$(db_query "select count(*) from life_radar.message_events where source = 'matrix';")"
duplicate_count="$(db_query "select count(*) from (select external_id from life_radar.message_events where source = 'matrix' group by external_id having count(*) > 1) dup;")"
if [[ "${event_count_after}" -lt "${event_count_before}" ]]; then
  echo "Matrix event count decreased after immediate re-ingest." >&2
  exit 1
fi
if [[ "${duplicate_count}" != "0" ]]; then
  echo "Duplicate Matrix events detected after re-ingest." >&2
  exit 1
fi

conversation_id="${LIFERADAR_MATRIX_SMOKE_CONVERSATION_ID:-}"
if [[ -z "${conversation_id}" ]]; then
  if [[ -n "${recent_room_id}" ]]; then
    conversation_id="$(db_query "select id from life_radar.conversations where source = 'matrix' and external_id = '${recent_room_id}' order by updated_at desc limit 1;")"
  fi
fi
if [[ -z "${conversation_id}" ]]; then
  conversation_id="$(
    curl --silent --show-error --fail \
      -H "x-api-key: ${LIFERADAR_API_KEY}" \
      "$(api_url)/conversations?source=matrix&limit=1" \
      | python_json "data[0]['id'] if data else ''"
  )"
fi
if [[ -z "${conversation_id}" ]]; then
  echo "Could not determine a Matrix conversation to use for send validation." >&2
  exit 1
fi

send_payload="$(
  python3 - <<'PY'
import json, uuid
print(json.dumps({
    "conversation_id": "",
    "content_text": f"[LifeRadar local smoke {uuid.uuid4()}]",
}))
PY
)"
send_payload="$(
  printf '%s' "${send_payload}" | python3 -c "import json,sys; data=json.load(sys.stdin); data['conversation_id'] = '${conversation_id}'; print(json.dumps(data))"
)"
send_response="$(
  curl --silent --show-error --fail \
    -H "Content-Type: application/json" \
    -H "x-api-key: ${LIFERADAR_API_KEY}" \
    -d "${send_payload}" \
    "$(api_url)/messages/send"
)"
send_status="$(printf '%s' "${send_response}" | python_json "data['status']")"
send_message_id="$(printf '%s' "${send_response}" | python_json "data['message_id']")"
if [[ "${send_status}" != "sent" ]] || [[ -z "${send_message_id}" ]]; then
  echo "Matrix send validation failed." >&2
  printf '%s\n' "${send_response}" >&2
  exit 1
fi

session_snapshot_before="$(
  python3 - <<PY
import json, pathlib
path = pathlib.Path("${HOST_SESSION_PATH}")
data = json.loads(path.read_text())
print(json.dumps({
    "access_token": data.get("access_token", ""),
    "refresh_token": data.get("refresh_token", ""),
    "next_batch": data.get("next_batch", ""),
    "user_id": data.get("user_id", ""),
    "device_id": data.get("device_id", ""),
}))
PY
)"

compose_local restart liferadar-worker liferadar-matrix-bridge liferadar-api >/dev/null
wait_for_http_json "$(api_url)/health"
wait_for_http_json "$(bridge_url)/health"
run_worker_mode probe >/dev/null

session_snapshot_after="$(
  python3 - <<PY
import json, pathlib
path = pathlib.Path("${HOST_SESSION_PATH}")
data = json.loads(path.read_text())
print(json.dumps({
    "access_token": data.get("access_token", ""),
    "refresh_token": data.get("refresh_token", ""),
    "next_batch": data.get("next_batch", ""),
    "user_id": data.get("user_id", ""),
    "device_id": data.get("device_id", ""),
}))
PY
)"

if [[ "$(printf '%s' "${session_snapshot_after}" | python_json "data['next_batch']")" == "" ]]; then
  echo "Session next_batch was lost after restart." >&2
  exit 1
fi
if [[ "$(printf '%s' "${session_snapshot_before}" | python_json "data['user_id']")" != "$(printf '%s' "${session_snapshot_after}" | python_json "data['user_id']")" ]]; then
  echo "Session user_id changed unexpectedly after restart." >&2
  exit 1
fi
if [[ "$(printf '%s' "${session_snapshot_before}" | python_json "data['device_id']")" != "$(printf '%s' "${session_snapshot_after}" | python_json "data['device_id']")" ]]; then
  echo "Session device_id changed unexpectedly after restart." >&2
  exit 1
fi

run_worker_mode recover_http >/dev/null
session_snapshot_recover="$(
  python3 - <<PY
import json, pathlib
path = pathlib.Path("${HOST_SESSION_PATH}")
data = json.loads(path.read_text())
print(json.dumps({
    "access_token": data.get("access_token", ""),
    "refresh_token": data.get("refresh_token", ""),
    "next_batch": data.get("next_batch", ""),
    "user_id": data.get("user_id", ""),
    "device_id": data.get("device_id", ""),
}))
PY
)"
if [[ "$(printf '%s' "${session_snapshot_recover}" | python_json "data['user_id']")" != "$(printf '%s' "${session_snapshot_after}" | python_json "data['user_id']")" ]]; then
  echo "Recover HTTP changed the persisted Matrix identity unexpectedly." >&2
  exit 1
fi
if [[ "$(printf '%s' "${session_snapshot_recover}" | python_json "data['device_id']")" != "$(printf '%s' "${session_snapshot_after}" | python_json "data['device_id']")" ]]; then
  echo "Recover HTTP changed the persisted Matrix device unexpectedly." >&2
  exit 1
fi
if [[ "$(printf '%s' "${session_snapshot_recover}" | python_json "data['next_batch']")" == "" ]]; then
  echo "Recover HTTP left the persisted Matrix checkpoint empty." >&2
  exit 1
fi

if [[ "${RUN_DECRYPTION}" == "1" ]]; then
  missing_key_output="$(compose_local exec -T liferadar-worker env \
    LIFERADAR_MATRIX_RUST_MODE=key_import \
    LIFERADAR_MATRIX_KEY_IMPORT_ENABLED=true \
    MATRIX_ROOM_KEYS_PATH=/tmp/missing-matrix-room-keys.txt \
    MATRIX_ROOM_KEYS_PASSPHRASE_PATH=/tmp/missing-matrix-room-keys-passphrase.txt \
    /usr/local/bin/liferadar-matrix)"
  if [[ "${missing_key_output}" != *"key_import_status=missing"* ]]; then
    echo "Decryption lane missing-key check failed." >&2
    printf '%s\n' "${missing_key_output}" >&2
    exit 1
  fi

  if [[ ! -f "${HOST_IDENTITY_DIR}/matrix-e2e-keys.txt" ]] || [[ ! -f "${HOST_IDENTITY_DIR}/.e2e-keys-passphrase" ]]; then
    echo "Decryption lane requires key export files under ${HOST_IDENTITY_DIR}." >&2
    exit 1
  fi

  key_import_output="$(compose_local exec -T liferadar-worker env \
    LIFERADAR_MATRIX_RUST_MODE=key_import \
    LIFERADAR_MATRIX_KEY_IMPORT_ENABLED=true \
    /usr/local/bin/liferadar-matrix)"
  if [[ "${key_import_output}" != *"key_import_status=imported"* && "${key_import_output}" != *"key_import_status=cached"* ]]; then
    echo "Key import did not report imported/cached." >&2
    printf '%s\n' "${key_import_output}" >&2
    exit 1
  fi

  key_import_cached_output="$(compose_local exec -T liferadar-worker env \
    LIFERADAR_MATRIX_RUST_MODE=key_import \
    LIFERADAR_MATRIX_KEY_IMPORT_ENABLED=true \
    /usr/local/bin/liferadar-matrix)"
  if [[ "${key_import_cached_output}" != *"key_import_status=cached"* ]]; then
    echo "Second key import did not report cached." >&2
    printf '%s\n' "${key_import_cached_output}" >&2
    exit 1
  fi

  if [[ -z "${LIFERADAR_MATRIX_SMOKE_ROOM_ID:-}" ]]; then
    echo "Set LIFERADAR_MATRIX_SMOKE_ROOM_ID in ${ENV_FILE} to run the decryption inspect_room check." >&2
    exit 1
  fi
  inspect_room_output="$(
    compose_local exec -T liferadar-worker env \
      LIFERADAR_MATRIX_RUST_MODE=inspect_room \
      LIFERADAR_MATRIX_KEY_IMPORT_ENABLED=true \
      LIFERADAR_MATRIX_RUST_ROOM_ID="${LIFERADAR_MATRIX_SMOKE_ROOM_ID}" \
      /usr/local/bin/liferadar-matrix
  )"
  printf '%s' "${inspect_room_output}" | python3 - <<'PY'
import json, sys
data = json.load(sys.stdin)
assert data["room_found"] is True
messages = data["room"]["messages"]
assert any(msg.get("body") and msg["body"] != "[undecrypted]" for msg in messages), "no decrypted messages surfaced"
PY
fi

if [[ "${RUN_LEGACY_IMPORT}" == "1" ]]; then
  if [[ ! -f "${HOST_WORKSPACE_DIR}/workspace_data/messages.db" ]]; then
    echo "Legacy-import lane requires ${HOST_WORKSPACE_DIR}/workspace_data/messages.db." >&2
    exit 1
  fi
  compose_local exec -T liferadar-worker /opt/liferadar/bin/backfill-matrix-history.sh >/dev/null
  legacy_state="$(db_query "select value->>'source_count' from life_radar.runtime_metadata where key = 'matrix_history_backfill';")"
  if [[ -z "${legacy_state}" ]]; then
    echo "Legacy Matrix import did not write runtime metadata." >&2
    exit 1
  fi
fi

echo "Local Matrix smoke checks passed."
