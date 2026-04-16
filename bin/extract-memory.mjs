#!/usr/bin/env node
import { runPsql, setRuntimeMetadata } from '../lib/runtime.mjs';

const EXTRACTOR_VERSION = 'sql-v2';

const sql = `
with scanned as (
  select
    e.id,
    e.conversation_id,
    e.source,
    e.external_id,
    e.sender_id,
    coalesce(nullif(e.sender_label, ''), e.sender_id, 'unknown') as sender_label,
    e.occurred_at,
    btrim(coalesce(e.content_text, '')) as content_text,
    lower(btrim(coalesce(e.content_text, ''))) as content_lc,
    e.is_inbound,
    c.title as conversation_title,
    c.external_id as conversation_external_id,
    c.social_weight,
    c.participants
  from life_radar.message_events e
  join life_radar.conversations c on c.id = e.conversation_id
  where coalesce(e.content_text, '') <> ''
),
self_notes as (
  select *
  from scanned
  where source = 'direct_note'
),
relationship_latest as (
  select distinct on (s.conversation_id)
    s.*
  from scanned s
  where s.is_inbound = true
    and s.source in ('matrix', 'outlook')
    and s.content_text not like '[undecrypted]%'
    and coalesce(s.conversation_title, '') <> ''
    and s.conversation_title !~* '(bridge bot|telegram bridge bot|signal bridge bot|whatsapp bridge bot|discord bridge bot|bot$)'
    and (
      coalesce(s.social_weight, 0) >= 0.40
      or jsonb_array_length(coalesce(s.participants, '[]'::jsonb)) <= 2
    )
  order by s.conversation_id, s.occurred_at desc
),
preference_candidates as (
  select
    s.id as source_event_id,
    'preference'::text as kind,
    'user'::text as subject_type,
    'self'::text as subject_key,
    left(s.content_text, 120) as title,
    left(s.content_text, 240) as summary,
    s.content_text as detail,
    'normal'::text as sensitivity,
    0.90::numeric(8,4) as confidence,
    jsonb_build_object(
      'extractor', '${EXTRACTOR_VERSION}',
      'rule', 'preference_phrase',
      'source', s.source,
      'external_id', s.external_id
    ) as provenance
  from self_notes s
  where s.content_lc ~ '(^|[^[:alnum:]_])i (prefer|like|love|hate|dislike)[[:>:]]'
),
skill_candidates as (
  select
    s.id as source_event_id,
    'skill'::text as kind,
    'user'::text as subject_type,
    'self'::text as subject_key,
    case
      when s.content_lc like '%concise%' then 'Be concise'
      when s.content_lc like '%short%' then 'Keep replies short'
      when s.content_lc like '%direct%' then 'Be direct'
      when s.content_lc like '%warm%' then 'Be warm'
      when s.content_lc like '%professional tone%' then 'Use a professional tone'
      when s.content_lc like '%in my tone%' then 'Match my tone'
      else left(s.content_text, 120)
    end as title,
    left(s.content_text, 240) as summary,
    s.content_text as detail,
    'normal'::text as sensitivity,
    0.88::numeric(8,4) as confidence,
    jsonb_build_object(
      'extractor', '${EXTRACTOR_VERSION}',
      'rule', 'communication_style',
      'source', s.source,
      'external_id', s.external_id
    ) as provenance
  from self_notes s
  where s.content_lc ~ '(be concise|keep it short|be direct|be warm|in my tone|professional tone)'
),
relationship_candidates as (
  select
    s.id as source_event_id,
    'relationship'::text as kind,
    'person'::text as subject_type,
    coalesce(nullif(s.conversation_external_id, ''), nullif(s.sender_id, ''), nullif(s.sender_label, '')) as subject_key,
    s.conversation_title as title,
    left('Recent interaction: ' || s.content_text, 240) as summary,
    s.content_text as detail,
    case when coalesce(s.social_weight, 0) >= 0.85 then 'high' else 'normal' end as sensitivity,
    least(0.95::numeric(8,4), greatest(0.60::numeric(8,4), coalesce(s.social_weight, 0.60)::numeric(8,4))) as confidence,
    jsonb_build_object(
      'extractor', '${EXTRACTOR_VERSION}',
      'rule', 'relationship_recent_human_message',
      'source', s.source,
      'external_id', s.external_id,
      'conversation_title', s.conversation_title
    ) as provenance
  from relationship_latest s
),
fact_candidates as (
  select
    s.id as source_event_id,
    'fact'::text as kind,
    'user'::text as subject_type,
    'self'::text as subject_key,
    left(s.content_text, 120) as title,
    left(s.content_text, 240) as summary,
    s.content_text as detail,
    'normal'::text as sensitivity,
    0.82::numeric(8,4) as confidence,
    jsonb_build_object(
      'extractor', '${EXTRACTOR_VERSION}',
      'rule', 'self_fact_phrase',
      'source', s.source,
      'external_id', s.external_id
    ) as provenance
  from self_notes s
  where s.content_lc ~ '(my name is|i live in|i work at|i study|i am from)'
),
all_candidates as (
  select * from preference_candidates
  union all
  select * from skill_candidates
  union all
  select * from relationship_candidates
  union all
  select * from fact_candidates
),
purged as (
  delete from life_radar.memory_records
  where provenance->>'extractor' = 'sql-v1'
  returning kind
),
ins as (
  insert into life_radar.memory_records (
    kind, subject_type, subject_key, title, summary, detail,
    sensitivity, confidence, source_event_id, provenance
  )
  select
    c.kind, c.subject_type, c.subject_key, c.title, c.summary, c.detail,
    c.sensitivity, c.confidence, c.source_event_id, c.provenance
  from all_candidates c
  where not exists (
    select 1
    from life_radar.memory_records mr
    where mr.kind = c.kind
      and mr.source_event_id = c.source_event_id
      and mr.active = true
  )
  returning kind
),
counts as (
  select
    (select count(*) from scanned) as scanned_messages,
    (select count(*) from all_candidates) as candidate_count,
    (select count(*) from ins) as inserted_count,
    (select count(*) from ins where kind = 'fact') as facts_inserted,
    (select count(*) from ins where kind = 'preference') as preferences_inserted,
    (select count(*) from ins where kind = 'relationship') as relationships_inserted,
    (select count(*) from ins where kind = 'skill') as skills_inserted
)
select row_to_json(counts) from counts;
`;

const result = JSON.parse(runPsql(sql, { tuplesOnly: true }) || '{}');
setRuntimeMetadata('memory_extraction', {
  extractor: EXTRACTOR_VERSION,
  scanned_messages: Number(result.scanned_messages || 0),
  candidate_count: Number(result.candidate_count || 0),
  inserted_count: Number(result.inserted_count || 0),
  facts_inserted: Number(result.facts_inserted || 0),
  preferences_inserted: Number(result.preferences_inserted || 0),
  relationships_inserted: Number(result.relationships_inserted || 0),
  skills_inserted: Number(result.skills_inserted || 0),
  extracted_at: new Date().toISOString(),
});

console.log(`liferadar memory extraction complete: scanned=${result.scanned_messages || 0} candidates=${result.candidate_count || 0} inserted=${result.inserted_count || 0}`);
