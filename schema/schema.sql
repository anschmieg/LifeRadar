CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS life_radar;

CREATE TABLE IF NOT EXISTS life_radar.runtime_metadata (
  key TEXT PRIMARY KEY,
  value JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS life_radar.connector_accounts (
  provider TEXT NOT NULL,
  account_id TEXT NOT NULL,
  display_label TEXT,
  auth_state TEXT NOT NULL DEFAULT 'logged_out',
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  last_synced_at TIMESTAMPTZ,
  last_error_at TIMESTAMPTZ,
  last_error TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (provider, account_id)
);

CREATE TABLE IF NOT EXISTS life_radar.connector_sync_checkpoints (
  provider TEXT NOT NULL,
  account_id TEXT NOT NULL,
  checkpoint_key TEXT NOT NULL,
  checkpoint_value JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (provider, account_id, checkpoint_key)
);

CREATE TABLE IF NOT EXISTS life_radar.conversations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source TEXT NOT NULL,
  external_id TEXT NOT NULL,
  account_id TEXT,
  title TEXT,
  participants JSONB NOT NULL DEFAULT '[]'::jsonb,
  state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'archived', 'muted')),
  needs_read BOOLEAN NOT NULL DEFAULT FALSE,
  needs_reply BOOLEAN NOT NULL DEFAULT FALSE,
  important_now BOOLEAN NOT NULL DEFAULT FALSE,
  waiting_on_other BOOLEAN NOT NULL DEFAULT FALSE,
  follow_up_later BOOLEAN NOT NULL DEFAULT FALSE,
  ready_to_act BOOLEAN NOT NULL DEFAULT FALSE,
  blocked_needs_context BOOLEAN NOT NULL DEFAULT FALSE,
  last_event_at TIMESTAMPTZ,
  last_triaged_at TIMESTAMPTZ,
  priority_score NUMERIC(8,4),
  urgency_score NUMERIC(8,4),
  social_weight NUMERIC(8,4),
  reward_value NUMERIC(8,4),
  energy_fit NUMERIC(8,4),
  effort_estimate_minutes INTEGER,
  due_at TIMESTAMPTZ,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS life_radar.message_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID REFERENCES life_radar.conversations(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  external_id TEXT NOT NULL,
  sender_id TEXT,
  sender_label TEXT,
  occurred_at TIMESTAMPTZ NOT NULL,
  content_text TEXT,
  content_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  is_inbound BOOLEAN NOT NULL DEFAULT TRUE,
  reply_needed BOOLEAN,
  needs_read BOOLEAN,
  needs_reply BOOLEAN,
  importance_score NUMERIC(8,4),
  triage_summary TEXT,
  provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS life_radar.commitments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID REFERENCES life_radar.conversations(id) ON DELETE SET NULL,
  source_event_id UUID REFERENCES life_radar.message_events(id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  summary TEXT,
  owner_role TEXT NOT NULL CHECK (owner_role IN ('user', 'other', 'shared', 'assistant')),
  status TEXT NOT NULL CHECK (status IN ('open', 'in_progress', 'blocked', 'done', 'cancelled')),
  due_at TIMESTAMPTZ,
  importance_score NUMERIC(8,4),
  urgency_score NUMERIC(8,4),
  social_weight NUMERIC(8,4),
  confidence NUMERIC(8,4),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS life_radar.reminders (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_entity_type TEXT NOT NULL,
  source_entity_id UUID,
  title TEXT NOT NULL,
  summary TEXT,
  status TEXT NOT NULL CHECK (status IN ('scheduled', 'queued', 'sent', 'snoozed', 'cancelled', 'completed')),
  remind_at TIMESTAMPTZ NOT NULL,
  remind_channel TEXT,
  timing_reason TEXT,
  cadence_profile TEXT,
  effort_estimate_minutes INTEGER,
  confidence NUMERIC(8,4),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS life_radar.planned_actions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_entity_type TEXT NOT NULL,
  source_entity_id UUID,
  title TEXT NOT NULL,
  summary TEXT,
  status TEXT NOT NULL CHECK (status IN ('proposed', 'scheduled', 'ready', 'done', 'cancelled')),
  scheduled_start TIMESTAMPTZ,
  scheduled_end TIMESTAMPTZ,
  calendar_provider TEXT,
  calendar_external_id TEXT,
  effort_estimate_minutes INTEGER,
  reward_value NUMERIC(8,4),
  energy_fit NUMERIC(8,4),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS life_radar.memory_records (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind TEXT NOT NULL CHECK (kind IN ('fact', 'preference', 'relationship', 'skill')),
  subject_type TEXT NOT NULL,
  subject_key TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT,
  detail TEXT,
  sensitivity TEXT NOT NULL DEFAULT 'normal' CHECK (sensitivity IN ('low', 'normal', 'high', 'restricted')),
  confidence NUMERIC(8,4),
  active BOOLEAN NOT NULL DEFAULT TRUE,
  source_event_id UUID REFERENCES life_radar.message_events(id) ON DELETE SET NULL,
  provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS life_radar.decision_contexts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_entity_type TEXT NOT NULL,
  source_entity_id UUID,
  title TEXT NOT NULL,
  summary TEXT,
  context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  prepared_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS life_radar.draft_candidates (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID REFERENCES life_radar.conversations(id) ON DELETE CASCADE,
  source_event_id UUID REFERENCES life_radar.message_events(id) ON DELETE SET NULL,
  channel TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ready', 'needs_context', 'approved', 'sent', 'discarded')),
  draft_text TEXT,
  tone_notes TEXT,
  grounded BOOLEAN NOT NULL DEFAULT FALSE,
  provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS life_radar.feedback_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  target_type TEXT NOT NULL,
  target_id UUID,
  feedback_type TEXT NOT NULL CHECK (feedback_type IN ('explicit', 'implicit')),
  signal TEXT NOT NULL,
  value JSONB NOT NULL DEFAULT '{}'::jsonb,
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  provenance JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS life_radar.external_projections (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_entity_type TEXT NOT NULL,
  source_entity_id UUID NOT NULL,
  target_system TEXT NOT NULL,
  target_object_type TEXT NOT NULL,
  target_object_id TEXT NOT NULL,
  sync_state TEXT NOT NULL CHECK (sync_state IN ('active', 'pending', 'conflict', 'archived', 'deleted')),
  last_synced_at TIMESTAMPTZ,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (target_system, target_object_type, target_object_id)
);

CREATE TABLE IF NOT EXISTS life_radar.graph_edges (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  from_type TEXT NOT NULL,
  from_id UUID NOT NULL,
  edge_type TEXT NOT NULL,
  to_type TEXT NOT NULL,
  to_id UUID NOT NULL,
  weight NUMERIC(8,4),
  confidence NUMERIC(8,4),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (from_type, from_id, edge_type, to_type, to_id)
);

CREATE TABLE IF NOT EXISTS life_radar.embeddings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_type TEXT NOT NULL,
  entity_id UUID NOT NULL,
  embedding_model TEXT NOT NULL,
  embedding VECTOR(1536) NOT NULL,
  content_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (entity_type, entity_id, embedding_model)
);

CREATE TABLE IF NOT EXISTS life_radar.runtime_probes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  candidate_id TEXT NOT NULL,
  candidate_type TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ok', 'warn', 'fail')),
  observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  latency_ms INTEGER,
  freshness_seconds INTEGER,
  total_events BIGINT,
  decrypt_failures BIGINT,
  encrypted_non_text BIGINT,
  running_processes INTEGER,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS life_radar.messaging_candidates (
  candidate_id TEXT PRIMARY KEY,
  candidate_type TEXT NOT NULL,
  last_status TEXT NOT NULL CHECK (last_status IN ('ok', 'warn', 'fail')),
  last_probe_at TIMESTAMPTZ NOT NULL,
  latest_freshness_seconds INTEGER,
  latest_total_events BIGINT,
  latest_decrypt_failures BIGINT,
  latest_encrypted_non_text BIGINT,
  latest_running_processes INTEGER,
  latest_notes TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conversations_last_event_at ON life_radar.conversations (last_event_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversations_reply_flags ON life_radar.conversations (needs_reply, needs_read, important_now);
CREATE INDEX IF NOT EXISTS idx_message_events_conversation_id ON life_radar.message_events (conversation_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_commitments_status_due_at ON life_radar.commitments (status, due_at);
CREATE INDEX IF NOT EXISTS idx_reminders_status_remind_at ON life_radar.reminders (status, remind_at);
CREATE INDEX IF NOT EXISTS idx_planned_actions_status_start ON life_radar.planned_actions (status, scheduled_start);
CREATE INDEX IF NOT EXISTS idx_memory_records_subject ON life_radar.memory_records (kind, subject_type, subject_key);
CREATE INDEX IF NOT EXISTS idx_external_projections_source ON life_radar.external_projections (source_entity_type, source_entity_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_from ON life_radar.graph_edges (from_type, from_id, edge_type);
CREATE INDEX IF NOT EXISTS idx_graph_edges_to ON life_radar.graph_edges (to_type, to_id, edge_type);
CREATE INDEX IF NOT EXISTS idx_runtime_probes_candidate_observed ON life_radar.runtime_probes (candidate_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_embeddings_lookup ON life_radar.embeddings (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_vector_cosine ON life_radar.embeddings USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_connector_accounts_provider_state ON life_radar.connector_accounts (provider, auth_state, enabled);
