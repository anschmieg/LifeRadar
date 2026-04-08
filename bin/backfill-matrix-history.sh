#!/usr/bin/env bash
set -euo pipefail

: "${LIFE_RADAR_DB_HOST:=life-radar-db}"
: "${LIFE_RADAR_DB_PORT:=5432}"
: "${LIFE_RADAR_DB_NAME:=life_radar}"
: "${LIFE_RADAR_DB_USER:=life_radar}"
: "${LIFE_RADAR_DB_PASSWORD:=change-me-in-env}"
: "${MATRIX_MESSAGES_DB:=/app/workspace/workspace_data/messages.db}"
: "${MATRIX_SESSION_PATH:=/app/identity/matrix-session.json}"
: "${LIFE_RADAR_BACKFILL_SOURCE:=matrix_history_backfill}"
: "${LIFE_RADAR_BACKFILL_FORCE:=0}"

if [[ ! -f "$MATRIX_MESSAGES_DB" ]]; then
  echo "matrix history backfill skipped: source DB missing at $MATRIX_MESSAGES_DB"
  exit 0
fi

export PGPASSWORD="$LIFE_RADAR_DB_PASSWORD"
SELF_USER_ID=""
if [[ -f "$MATRIX_SESSION_PATH" ]]; then
  SELF_USER_ID="$(jq -r '.user_id // empty' "$MATRIX_SESSION_PATH")"
fi

source_count="$(sqlite3 "$MATRIX_MESSAGES_DB" "select count(*) from messages where room_id is not null and room_id <> '';" )"
source_max_ts="$(sqlite3 "$MATRIX_MESSAGES_DB" "select coalesce(max(timestamp),0) from messages where room_id is not null and room_id <> '';" )"

if [[ "$source_count" == "0" ]]; then
  echo "matrix history backfill skipped: no importable rows"
  exit 0
fi

state_json="$(psql \
  --host "$LIFE_RADAR_DB_HOST" \
  --port "$LIFE_RADAR_DB_PORT" \
  --username "$LIFE_RADAR_DB_USER" \
  --dbname "$LIFE_RADAR_DB_NAME" \
  --tuples-only --no-align \
  --command "select value::text from life_radar.runtime_metadata where key = '${LIFE_RADAR_BACKFILL_SOURCE}';" | tr -d '\n')"

if [[ "$LIFE_RADAR_BACKFILL_FORCE" != "1" && -n "$state_json" ]]; then
  previous_count="$(jq -r '.source_count // 0' <<<"$state_json")"
  previous_max_ts="$(jq -r '.source_max_ts // 0' <<<"$state_json")"
  if [[ "$previous_count" == "$source_count" && "$previous_max_ts" == "$source_max_ts" ]]; then
    echo "matrix history backfill skipped: source unchanged (${source_count} rows)"
    exit 0
  fi
fi

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

conversations_csv="$tmp_dir/conversations.csv"
message_events_csv="$tmp_dir/message_events.csv"

sqlite3 "$MATRIX_MESSAGES_DB" <<SQL
.mode csv
.once '$conversations_csv'
select
  room_id,
  min(nullif(channel, '')),
  max(timestamp),
  count(*),
  json_object(
    'legacy_channel', min(nullif(channel, '')),
    'legacy_import', 1,
    'legacy_message_count', count(*),
    'legacy_latest_timestamp', max(timestamp)
  )
from messages
where room_id is not null and room_id <> ''
group by room_id;
.once '$message_events_csv'
select
  id,
  room_id,
  sender,
  timestamp,
  body,
  sender,
  urgency,
  coalesce(replied, 0),
  coalesce(reply_age_days, 0),
  coalesce(embedding_id, ''),
  case when '$SELF_USER_ID' <> '' and sender = '$SELF_USER_ID' then 0 else 1 end,
  json_object(
    'legacy_channel', channel,
    'legacy_room_id', room_id,
    'legacy_urgency', urgency,
    'legacy_replied', coalesce(replied, 0),
    'legacy_reply_age_days', coalesce(reply_age_days, 0),
    'legacy_embedding_id', nullif(embedding_id, '')
  ),
  json_object(
    'import_source', 'legacy_messages_db',
    'legacy_channel', channel,
    'legacy_room_id', room_id
  )
from messages
where room_id is not null and room_id <> '';
SQL

psql \
  --host "$LIFE_RADAR_DB_HOST" \
  --port "$LIFE_RADAR_DB_PORT" \
  --username "$LIFE_RADAR_DB_USER" \
  --dbname "$LIFE_RADAR_DB_NAME" \
  --set ON_ERROR_STOP=1 <<SQL
create temporary table stage_matrix_conversations (
  external_id text,
  account_id text,
  last_event_epoch bigint,
  message_count bigint,
  metadata jsonb
);

create temporary table stage_matrix_message_events (
  external_id text,
  conversation_external_id text,
  sender_id text,
  occurred_at_epoch bigint,
  content_text text,
  sender_label text,
  legacy_urgency text,
  legacy_replied integer,
  legacy_reply_age_days integer,
  legacy_embedding_id text,
  is_inbound integer,
  content_json jsonb,
  provenance jsonb
);

\copy stage_matrix_conversations from '$conversations_csv' with (format csv)
\copy stage_matrix_message_events from '$message_events_csv' with (format csv)

insert into life_radar.conversations (
  source,
  external_id,
  account_id,
  last_event_at,
  metadata
)
select
  'matrix',
  s.external_id,
  nullif(s.account_id, ''),
  to_timestamp(s.last_event_epoch),
  coalesce(s.metadata, '{}'::jsonb)
from stage_matrix_conversations s
on conflict (source, external_id) do update
set
  account_id = coalesce(excluded.account_id, life_radar.conversations.account_id),
  last_event_at = greatest(
    coalesce(life_radar.conversations.last_event_at, to_timestamp(0)),
    coalesce(excluded.last_event_at, to_timestamp(0))
  ),
  metadata = life_radar.conversations.metadata || excluded.metadata,
  updated_at = now();

insert into life_radar.message_events (
  conversation_id,
  source,
  external_id,
  sender_id,
  sender_label,
  occurred_at,
  content_text,
  content_json,
  is_inbound,
  provenance
)
select
  c.id,
  'matrix',
  e.external_id,
  nullif(e.sender_id, ''),
  nullif(e.sender_label, ''),
  to_timestamp(e.occurred_at_epoch),
  nullif(e.content_text, ''),
  coalesce(e.content_json, '{}'::jsonb),
  case when e.is_inbound = 0 then false else true end,
  coalesce(e.provenance, '{}'::jsonb)
from stage_matrix_message_events e
join life_radar.conversations c
  on c.source = 'matrix'
 and c.external_id = e.conversation_external_id
on conflict (source, external_id) do update
set
  conversation_id = excluded.conversation_id,
  sender_id = excluded.sender_id,
  sender_label = excluded.sender_label,
  occurred_at = excluded.occurred_at,
  content_text = excluded.content_text,
  content_json = excluded.content_json,
  is_inbound = excluded.is_inbound,
  provenance = life_radar.message_events.provenance || excluded.provenance,
  updated_at = now();

with agg as (
  select
    me.conversation_id,
    max(me.occurred_at) as last_event_at,
    jsonb_agg(distinct jsonb_build_object(
      'sender_id', me.sender_id,
      'sender_label', coalesce(me.sender_label, me.sender_id)
    )) filter (where me.sender_id is not null and me.sender_id <> '') as participants,
    count(*) filter (where me.is_inbound) as inbound_count,
    count(*) as total_count
  from life_radar.message_events me
  join life_radar.conversations c on c.id = me.conversation_id
  where c.source = 'matrix'
  group by me.conversation_id
)
update life_radar.conversations c
set
  participants = coalesce(agg.participants, '[]'::jsonb),
  last_event_at = agg.last_event_at,
  metadata = c.metadata || jsonb_build_object(
    'message_count', agg.total_count,
    'inbound_message_count', agg.inbound_count,
    'historical_backfill', true
  ),
  updated_at = now()
from agg
where c.id = agg.conversation_id;

insert into life_radar.runtime_metadata (key, value)
values (
  '${LIFE_RADAR_BACKFILL_SOURCE}',
  jsonb_build_object(
    'source_db', '${MATRIX_MESSAGES_DB}',
    'source_count', ${source_count},
    'source_max_ts', ${source_max_ts},
    'imported_at', now(),
    'self_user_id', nullif('${SELF_USER_ID}', ''),
    'force', ${LIFE_RADAR_BACKFILL_FORCE}
  )
)
on conflict (key) do update
set
  value = excluded.value,
  updated_at = now();
SQL

conversation_count="$(psql \
  --host "$LIFE_RADAR_DB_HOST" \
  --port "$LIFE_RADAR_DB_PORT" \
  --username "$LIFE_RADAR_DB_USER" \
  --dbname "$LIFE_RADAR_DB_NAME" \
  --tuples-only --no-align \
  --command "select count(*) from life_radar.conversations where source = 'matrix';" | tr -d '\n')"
message_count="$(psql \
  --host "$LIFE_RADAR_DB_HOST" \
  --port "$LIFE_RADAR_DB_PORT" \
  --username "$LIFE_RADAR_DB_USER" \
  --dbname "$LIFE_RADAR_DB_NAME" \
  --tuples-only --no-align \
  --command "select count(*) from life_radar.message_events where source = 'matrix';" | tr -d '\n')"

echo "matrix history backfill complete: conversations=${conversation_count} messages=${message_count} imported_rows=${source_count}"
