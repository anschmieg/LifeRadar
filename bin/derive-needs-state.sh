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
with latest_message as (
  select distinct on (me.conversation_id)
    me.conversation_id,
    me.id as message_id,
    me.occurred_at,
    me.sender_id,
    me.content_text,
    me.is_inbound,
    me.content_json,
    me.provenance
  from life_radar.message_events me
  join life_radar.conversations c on c.id = me.conversation_id
  where c.source in ('matrix', 'outlook')
    and coalesce((me.content_json->>'removed')::boolean, false) = false
  order by me.conversation_id, me.occurred_at desc, me.id desc
),
latest_outbound as (
  select me.conversation_id, max(me.occurred_at) as latest_outbound_at
  from life_radar.message_events me
  join life_radar.conversations c on c.id = me.conversation_id
  where c.source in ('matrix', 'outlook')
    and me.is_inbound = false
    and coalesce((me.content_json->>'removed')::boolean, false) = false
  group by me.conversation_id
),
latest_inbound as (
  select me.conversation_id, max(me.occurred_at) as latest_inbound_at
  from life_radar.message_events me
  join life_radar.conversations c on c.id = me.conversation_id
  where c.source in ('matrix', 'outlook')
    and me.is_inbound = true
    and coalesce((me.content_json->>'removed')::boolean, false) = false
  group by me.conversation_id
),
base as (
  select
    c.id as conversation_id,
    c.source,
    lm.message_id,
    lm.occurred_at as latest_occurred_at,
    coalesce(lm.is_inbound, false) as latest_is_inbound,
    coalesce(lm.content_text, '') as latest_content_text,
    coalesce(lm.sender_id, '') as latest_sender_id,
    coalesce(jsonb_array_length(c.participants), 0) as participant_count,
    coalesce((lm.content_json->>'is_read')::boolean, false) as latest_is_read,
    coalesce((lm.provenance->>'mail_is_human')::boolean, true) as latest_mail_is_human,
    lo.latest_outbound_at,
    li.latest_inbound_at
  from life_radar.conversations c
  left join latest_message lm on lm.conversation_id = c.id
  left join latest_outbound lo on lo.conversation_id = c.id
  left join latest_inbound li on li.conversation_id = c.id
  where c.source in ('matrix', 'outlook')
),
derived as (
  select
    b.*,
    (
      case
        when b.source = 'matrix' and b.participant_count between 2 and 4 then 0.85
        when b.source = 'matrix' and b.participant_count between 5 and 8 then 0.55
        when b.source = 'outlook' and b.participant_count between 2 and 6 then 0.8
        when b.source = 'outlook' and b.participant_count between 7 and 12 then 0.5
        else 0.25
      end
    )::numeric(8,4) as social_weight,
    (
      case
        when b.latest_content_text ~* '\m(today|tonight|tomorrow|asap|urgent|deadline|before|by [0-9]{1,2}(:[0-9]{2})?)\M' then 0.95
        when b.latest_content_text like '%?%' then 0.75
        when b.latest_content_text ~* '\m(call|meeting|meet|schedule|buy|remind|reply|send|review|pay|book)\M' then 0.7
        when b.latest_occurred_at >= now() - interval '24 hours' then 0.55
        else 0.2
      end
    )::numeric(8,4) as urgency_score,
    (
      b.message_id is not null
      and b.latest_is_inbound = true
      and (
        b.source = 'matrix'
        or (b.source = 'outlook' and b.latest_is_read = false)
      )
    ) as needs_read,
    (
      b.message_id is not null
      and b.latest_is_inbound = true
      and b.latest_content_text not in ('[undecrypted]', '[non-text message]', '[non-text event]', '')
      and case
        when b.source = 'matrix' then (
          b.participant_count <= 6
          and b.latest_sender_id not like '%bot:%'
        )
        when b.source = 'outlook' then (
          b.participant_count <= 12
          and b.latest_mail_is_human
        )
        else false
      end
      and (b.latest_outbound_at is null or b.latest_outbound_at < b.latest_occurred_at)
    ) as needs_reply,
    (
      b.latest_outbound_at is not null
      and (b.latest_inbound_at is null or b.latest_outbound_at > b.latest_inbound_at)
    ) as waiting_on_other,
    (
      b.source = 'matrix'
      and b.message_id is not null
      and b.latest_content_text = '[undecrypted]'
    ) as blocked_needs_context,
    (
      b.message_id is not null
      and b.latest_is_inbound = true
      and b.latest_content_text not in ('[non-text message]', '[non-text event]', '')
      and (
        b.latest_occurred_at >= now() - interval '48 hours'
        or b.latest_content_text ~* '\m(today|tonight|tomorrow|asap|urgent|deadline)\M'
      )
      and (
        case
          when b.source = 'matrix' then (b.participant_count <= 8 or b.latest_content_text = '[undecrypted]')
          when b.source = 'outlook' then (b.latest_mail_is_human and b.latest_is_read = false)
          else false
        end
      )
    ) as important_now,
    (
      b.message_id is not null
      and b.latest_is_inbound = true
      and b.latest_content_text not in ('[undecrypted]', '[non-text message]', '[non-text event]', '')
      and b.latest_occurred_at < now() - interval '18 hours'
      and b.latest_occurred_at >= now() - interval '14 days'
      and case
        when b.source = 'matrix' then (
          b.participant_count <= 12
          and (b.latest_outbound_at is null or b.latest_outbound_at < b.latest_occurred_at)
        )
        when b.source = 'outlook' then (
          b.latest_mail_is_human
          and b.latest_is_read = false
        )
        else false
      end
    ) as follow_up_later
  from base b
),
scored as (
  select
    d.*,
    (
      least(
        1.0,
        greatest(
          case when d.needs_reply then 0.95 when d.needs_read then 0.6 else 0.0 end,
          case when d.important_now then 0.9 when d.follow_up_later then 0.55 else 0.0 end
        )
        + (d.social_weight * 0.2)
        + (d.urgency_score * 0.35)
        + case when d.blocked_needs_context then 0.15 else 0.0 end
      )
    )::numeric(8,4) as priority_score
  from derived d
)
update life_radar.conversations c
set
  needs_read = d.needs_read,
  needs_reply = d.needs_reply,
  waiting_on_other = d.waiting_on_other,
  blocked_needs_context = d.blocked_needs_context,
  ready_to_act = (d.needs_read or d.needs_reply or d.important_now or d.follow_up_later),
  important_now = d.important_now,
  follow_up_later = d.follow_up_later,
  priority_score = d.priority_score,
  urgency_score = d.urgency_score,
  social_weight = d.social_weight,
  last_triaged_at = now(),
  metadata = c.metadata || jsonb_build_object(
    'triage_version', 'v3',
    'triaged_at', now(),
    'triage_participant_count', d.participant_count,
    'latest_sender_id', d.latest_sender_id,
    'latest_content_text', left(d.latest_content_text, 240),
    'important_now', d.important_now,
    'follow_up_later', d.follow_up_later,
    'latest_occurred_at', d.latest_occurred_at,
    'latest_is_read', d.latest_is_read,
    'priority_score', d.priority_score,
    'urgency_score', d.urgency_score,
    'social_weight', d.social_weight
  ),
  updated_at = now()
from scored d
where c.id = d.conversation_id;

update life_radar.message_events
set needs_read = null,
    needs_reply = null,
    updated_at = now()
where source in ('matrix', 'outlook')
  and (needs_read is not null or needs_reply is not null);

with latest_flagged as (
  select distinct on (me.conversation_id)
    me.id,
    c.needs_read,
    c.needs_reply
  from life_radar.message_events me
  join life_radar.conversations c on c.id = me.conversation_id
  where c.source in ('matrix', 'outlook')
    and coalesce((me.content_json->>'removed')::boolean, false) = false
  order by me.conversation_id, me.occurred_at desc, me.id desc
)
update life_radar.message_events me
set needs_read = lf.needs_read,
    needs_reply = lf.needs_reply,
    updated_at = now()
from latest_flagged lf
where me.id = lf.id;

insert into life_radar.runtime_metadata (key, value)
values (
  'conversation_needs_state',
  jsonb_build_object(
    'version', 'v3',
    'updated_at', now(),
    'matrix', jsonb_build_object(
      'needs_read_count', (select count(*) from life_radar.conversations where source = 'matrix' and needs_read),
      'needs_reply_count', (select count(*) from life_radar.conversations where source = 'matrix' and needs_reply),
      'important_now_count', (select count(*) from life_radar.conversations where source = 'matrix' and important_now),
      'follow_up_later_count', (select count(*) from life_radar.conversations where source = 'matrix' and follow_up_later),
      'waiting_on_other_count', (select count(*) from life_radar.conversations where source = 'matrix' and waiting_on_other),
      'blocked_count', (select count(*) from life_radar.conversations where source = 'matrix' and blocked_needs_context)
    ),
    'outlook', jsonb_build_object(
      'needs_read_count', (select count(*) from life_radar.conversations where source = 'outlook' and needs_read),
      'needs_reply_count', (select count(*) from life_radar.conversations where source = 'outlook' and needs_reply),
      'important_now_count', (select count(*) from life_radar.conversations where source = 'outlook' and important_now),
      'follow_up_later_count', (select count(*) from life_radar.conversations where source = 'outlook' and follow_up_later),
      'waiting_on_other_count', (select count(*) from life_radar.conversations where source = 'outlook' and waiting_on_other),
      'blocked_count', (select count(*) from life_radar.conversations where source = 'outlook' and blocked_needs_context)
    )
  )
)
on conflict (key) do update
set value = excluded.value,
    updated_at = now();
SQL

echo "liferadar needs-state derivation complete"
