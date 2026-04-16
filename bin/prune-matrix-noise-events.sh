#!/usr/bin/env bash
set -euo pipefail

: "${LIFERADAR_DB_HOST:=liferadar-db}"
: "${LIFERADAR_DB_PORT:=5432}"
: "${LIFERADAR_DB_NAME:=life_radar}"
: "${LIFERADAR_DB_USER:=life_radar}"
: "${LIFERADAR_DB_PASSWORD:=change-me-in-env}"

export PGPASSWORD="$LIFERADAR_DB_PASSWORD"

psql \
  --host "$LIFERADAR_DB_HOST" \
  --port "$LIFERADAR_DB_PORT" \
  --username "$LIFERADAR_DB_USER" \
  --dbname "$LIFERADAR_DB_NAME" \
  --set ON_ERROR_STOP=1 <<'SQL'
with deleted as (
  delete from life_radar.message_events me
  where me.source = 'matrix'
    and coalesce(me.content_json->>'event_type', '') not in ('m.room.message', 'm.room.encrypted')
  returning me.conversation_id
),
recomputed as (
  select
    c.id as conversation_id,
    max(me.occurred_at) as last_event_at
  from life_radar.conversations c
  left join life_radar.message_events me on me.conversation_id = c.id
  where c.source = 'matrix'
  group by c.id
)
update life_radar.conversations c
set last_event_at = r.last_event_at,
    updated_at = now()
from recomputed r
where c.id = r.conversation_id
  and c.source = 'matrix';

insert into life_radar.runtime_metadata (key, value)
values (
  'matrix_noise_prune',
  jsonb_build_object(
    'version', 'v1',
    'pruned_at', now(),
    'remaining_non_message_events', (
      select count(*)
      from life_radar.message_events
      where source = 'matrix'
        and coalesce(content_json->>'event_type', '') not in ('m.room.message', 'm.room.encrypted')
    )
  )
)
on conflict (key) do update
set value = excluded.value,
    updated_at = now();
SQL

echo "liferadar matrix noise prune complete"
