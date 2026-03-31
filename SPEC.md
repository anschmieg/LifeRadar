# LifeRadar — Specification

## Overview

LifeRadar is a personal intelligence and communications triage system. It continuously
ingests messages from Matrix/Beeper (E2EE), Microsoft Graph (Outlook mail), and Google
Calendar, stores them in PostgreSQL/pgvector, and exposes everything via an API to
power an AI agent (Hermes).

**Migrated from:** `openclaw/overlay/life-radar/`

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Coolify                                                     │
│  ┌─────────────────┐  ┌──────────────────────────────────┐  │
│  │  life-radar-db  │  │  life-radar-worker (container)    │  │
│  │  (pgvector:pg17)│  │  ├─ Rust probe (Matrix E2EE)      │  │
│  │                 │  │  ├─ graph-sync-mail.mjs (Outlook) │  │
│  │                 │  │  ├─ google-calendar-reconcile     │  │
│  │                 │  │  ├─ derive-needs-state.sh (triage) │  │
│  │                 │  │  └─ extract-memory.mjs            │  │
│  └─────────────────┘  └──────────────┬───────────────────┘  │
│                                      │                       │
│  ┌───────────────────────────────────▼───────────────────┐  │
│  │  life-radar-api  (Phase 2)                           │  │
│  │  FastAPI · GET/POST · OpenAPI 3.1                     │  │
│  └────────────────────────┬───────────────────────────────┘  │
└───────────────────────────┼─────────────────────────────────┘
                            │ HTTP
                            ▼
┌───────────────────────────────────────────────────────────────┐
│  Hermes LXC (10.99.244.100)                                  │
│  ├─ Hermes Agent (systemd gateway)                           │
│  └─ Hermes MCP client → life-radar MCP server (Phase 3)    │
└───────────────────────────────────────────────────────────────┘
```

---

## Database Schema

Schema: `life_radar` (PostgreSQL + pgvector)

| Table | Purpose |
|---|---|
| `conversations` | Unified inbox: Matrix + Outlook + direct notes |
| `message_events` | Individual messages with triage flags |
| `commitments` | Explicit or inferred commitments from messages |
| `reminders` | Time-bound follow-ups |
| `planned_actions` | Tasks with scheduling, effort, energy fit |
| `memory_records` | Extracted facts, preferences, relationships, skills |
| `decision_contexts` | Context for pending decisions |
| `draft_candidates` | AI-composed replies awaiting approval |
| `feedback_events` | Implicit/explicit feedback signals |
| `external_projections` | Sync state with Google Calendar |
| `graph_edges` | Relationship graph between entities |
| `embeddings` | pgvector embeddings for semantic search |
| `runtime_probes` | Probe health metrics over time |
| `messaging_candidates` | Per-source messaging health |
| `runtime_metadata` | Sync state, auth tokens, probe timestamps |

---

## Phase 1 — Extraction & Standalone Docker

**Status: DONE** (2026-03-31)

- [x] Extracted all files from `openclaw/overlay/life-radar/`
- [x] Removed hardcoded credentials from `docker-compose.yml`
- [x] Created `.env.example` with all required variables
- [x] `schema.sql` — full pgvector schema, 15 tables
- [x] `bin/run-probes.sh` — probe orchestrator (5 min interval)
- [x] `bin/bootstrap.sh` — DB init + one-time migrations
- [x] `bin/graph-sync-mail.mjs` — MSGraph delta sync (Outlook)
- [x] `bin/google-calendar-reconcile.mjs` — bidirectional Google Calendar
- [x] `bin/derive-needs-state.sh` — v3 triage scoring
- [x] `bin/extract-memory.mjs` — fact/preference/relationship extraction
- [x] `bin/capture-direct-note.mjs` — capture notes from CLI
- [x] `bin/backfill-matrix-history.sh` — SQLite → PostgreSQL migration
- [x] `bin/prune-matrix-noise-events.sh` — clean non-message Matrix events
- [x] `bin/list-memory.mjs` — query memory records
- [x] `bin/probe-matrix-candidate.sh` — legacy nio probe for bakeoff
- [x] `lib/runtime.mjs` — shared DB/OAuth/HTTP utilities
- [x] `matrix-rust-probe/` — Rust binary using matrix-sdk for E2EE ingestion
- [x] `Dockerfile.worker` — 2-stage build: Rust compilation + Node.js runtime
- [x] `fixtures/msgraph/` — test fixtures for MSGraph sync
- [x] Repo committed and pushed to GitHub

### Volume Migration (required before first deploy)

The OpenClaw deployment has Matrix identity data on host paths. Before standing up
the standalone LifeRadar:

1. Stop the OpenClaw `life-radar-worker` container (Matrix probe will stop ingesting)
2. The following paths must be migrated to Docker named volumes:
   - `/data/openclaw/config/.openclaw/identity/matrix-session.json` → `matrix-identity` volume
   - `/data/openclaw/config/.openclaw/identity/matrix-rust-sdk-store/` → `matrix-identity` volume
   - `/data/openclaw/config/.openclaw/identity/beeper-e2e-keys.txt` → `matrix-identity` volume
   - `/data/openclaw/config/.openclaw/identity/.e2ee-export-passphrase` → `matrix-identity` volume
3. Update `.env` with the existing `life-radar-db` password (to reuse existing data)
4. Set `LIFE_RADAR_DB_PASSWORD` to the password currently used by the running
   `life-radar-db` container on Oracle
5. Point `MATRIX_SESSION_PATH` etc. at the new volume mounts, not the old host paths

The `matrix-rust-probe` binary at `/usr/local/bin/life-radar-matrix-rust-probe` in the
OpenClaw container is the compiled form of `matrix-rust-probe/` in this repo. The
`Dockerfile.worker` compiles it fresh during the Docker build.

---

## Phase 2 — API Server

**Status: TODO** | **Owner:** Hermes agent

**Goal:** Expose the DB via a FastAPI HTTP server consumed by Hermes.

### Technology

- **FastAPI** + **uvicorn** — native async, OpenAPI 3.1 auto-generation, Pydantic validation
- **asyncpg** — async PostgreSQL driver with connection pooling
- **pydantic** — request/response models

### Endpoints

```
GET   /health               liveness check
GET   /alerts               urgent triage items (needs_reply=True, priority≥0.8)
GET   /conversations        search/filter inbox (source, priority, needs_reply)
GET   /conversations/{id}   full conversation with messages
GET   /memories             query memory records (tag, entity, keyword)
GET   /tasks                list planned_actions
POST  /tasks                create task
GET   /calendar/events      read Google Calendar (date range)
POST  /calendar/events      upsert calendar event
POST  /messages/send        send Matrix/Outlook message (user-approved only)
GET   /search               semantic search over memories + conversations
GET   /probe-status         probe health + last-run timestamps
```

### TODO

- [ ] Create `api/` directory with FastAPI application
- [ ] Define Pydantic models for all request/response types
- [ ] Implement each endpoint with `asyncpg`
- [ ] Add OpenAPI route at `/openapi.json`
- [ ] Write `Dockerfile.api` (or extend `Dockerfile.worker` with multi-target)
- [ ] Add API key auth (HMAC or static key for Hermes → API calls)
- [ ] Add Docker Compose service entry for `life-radar-api`
- [ ] Update `.env.example` with API server port variable

### OpenAPI → MCP Strategy

FastAPI auto-generates OpenAPI 3.1 at `/openapi.json`. Use `openapi-to-mcp` to
auto-generate the MCP tool schema from the same spec — define the API once,
expose it both as HTTP and as MCP tools.

---

## Phase 3 — MCP Server

**Status: TODO** | **Owner:** Hermes agent

**Goal:** Hermes connects to LifeRadar as an MCP server.

### Approach

1. FastAPI generates OpenAPI 3.1 at `/openapi.json`
2. `openapi-to-mcp` CLI generates MCP tool definitions from the spec
3. MCP server runs as a stdio sidecar (same container as API, or separate)
4. Hermes `mcp_config.yaml` points to the MCP server endpoint

**Alternative:** `fastapi-mcp` decorator — simpler but less flexible.

### MCP Tools (auto-generated)

```
alerts           GET  /alerts
conversations    GET  /conversations
conversation     GET  /conversations/{id}
memories         GET  /memories
tasks            GET/POST /tasks
calendar-events  GET/POST /calendar/events
send-message     POST /messages/send
search           GET  /search
probe-status     GET  /probe-status
```

### TODO

- [ ] Verify `openapi-to-mcp` compatibility with FastAPI 3.1 output
- [ ] Create `mcp/` directory with MCP stdio server entrypoint
- [ ] Bridge FastAPI HTTP responses → MCP JSON-RPC tool results
- [ ] Add MCP config snippet to `~/.hermes/mcp_config.yaml`
- [ ] Test end-to-end: Hermes → MCP → FastAPI → PostgreSQL

---

## Phase 4 — On-Demand Agent Sessions (Future)

Spawnable agent sessions for deep-dive tasks triggered by triage or user request:

- **Summarize** — "summarize the last 10 messages from @person"
- **Draft reply** — "write a reply to the most urgent message"
- **Research** — "what does the Zattler conversation say about the Canale property?"
- **Task breakdown** — "turn this vague note into actionable subtasks"

Implementation: `POST /agent/sessions` spins up a lightweight agent subprocess with
relevant conversation context injected via system prompt.

---

## Phase 5 — Linear as Source of Truth

**Status: TODO** | **Owner:** Hermes agent

**Goal:** Make Linear the single source of truth for tasks. Deprecate `planned_actions` table in favor of Linear via MCP.

### Rationale

- LifeRadar's `planned_actions` table has always been a local prototype
- Linear already has workspace, projects, labels, issues — mature UX
- Single source eliminates sync complexity
- Hermes can create/update/list via Linear MCP

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Linear (source of truth)                                    │
│  - Workspaces, Projects, Issues, Labels                      │
└────────────────────────┬────────────────────────────────────┘
                          │ MCP
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  Hermes LXC                                               │
│  ├── Hermes Agent                                          │
│  └── Linear MCP client                                    │
└─────────────────────────────────────────────────────────────┘
```

**Removed from LifeRadar:**
- `planned_actions` table → deprecated (read-only projection, optional)
- Phase 5 "task management extensions" → not needed

### Integration Contract

Hermes uses Linear MCP for all task operations:

| Operation | Linear API |
|-----------|-----------|
| List tasks | `issues` query (filter by project, labels, assignee) |
| Create task | `issueCreate` mutation |
| Update task | `issueUpdate` mutation |
| Close task | `issueUpdate` (set completed state) |
| Query by priority | Filter by `priority:high` label |
| Query by project | Filter by project identifier |

### Projects (from existing Linear setup)

| Project | Purpose |
|---------|---------|
| `Configuration` | Model providers, subagent config |
| `Communication Hub` | LifeRadar sync, email, calendar |
| `Study Programs` | Research tasks |

### Labels (canonical)

| Label | Usage |
|-------|-------|
| `priority:high` | Needs immediate attention |
| `priority:medium` | Scheduled for this week |
| `priority:low` | Backlog |
| `type:config` | Configuration changes |
| `type:integration` | Third-party integrations |
| `blocked` | Waiting on something |

### Hermes Tool Interface

Once Linear MCP is configured, Hermes can:
- "What are my high priority items?"
- "Create an issue in Communication Hub to sync LifeRadar reminders"
- "Mark the Google Calendar integration issue as done"
- "What's blocked?"

### Deprecation Path

1. **Phase 5 start:** Add Linear MCP to `~/.hermes/mcp_config.yaml`
2. **Immediate:** New tasks created via Linear MCP, not `planned_actions`
3. **Optional:** Migrate existing `planned_actions` records to Linear issues (one-time script)
4. **Future:** Drop `planned_actions` table or keep as read-only audit log

### Open Questions

- Does OpenClaw also use Linear, or keep its own task tracking?
- Should LifeRadar reminders create Linear issues automatically?
- Do you want to keep any data in `planned_actions` as historical?

---

### Required

| Variable | Description |
|---|---|
| `LIFE_RADAR_DB_PASSWORD` | PostgreSQL password (reuse existing to keep data) |
| `MATRIX_SESSION_PATH` | Matrix session JSON file |
| `MATRIX_E2EE_EXPORT_PATH` | Beeper E2EE key export |
| `MATRIX_E2EE_EXPORT_PASSPHRASE_PATH` | Beeper E2EE passphrase |

### Optional

| Variable | Description |
|---|---|
| `MSGRAPH_CLIENT_ID` | Azure AD app client ID |
| `MSGRAPH_CLIENT_SECRET` | Azure AD app client secret |
| `MSGRAPH_TENANT_ID` | Azure AD tenant ID |
| `MSGRAPH_REFRESH_TOKEN` | OAuth refresh token |
| `GOOGLE_CALENDAR_CLIENT_ID` | GCP OAuth client ID |
| `GOOGLE_CALENDAR_CLIENT_SECRET` | GCP OAuth client secret |
| `GOOGLE_CALENDAR_REFRESH_TOKEN` | OAuth refresh token |
| `GOOGLE_CALENDAR_ID` | Calendar ID (default: primary) |
| `LIFE_RADAR_PROBE_INTERVAL_SEC` | Probe interval (default: 300) |

---

## Deployment Checklist

```
1. Volume migration — copy Matrix identity files from OpenClaw host paths to the
   new Docker named volumes (matrix-identity). See Phase 1 Migration Notes.
2. Set LIFE_RADAR_DB_PASSWORD to the existing database password (preserve data)
3. Fill in remaining .env variables (MSGraph, Google Calendar if used)
4. docker compose build life-radar-worker
5. docker compose up -d life-radar-db life-radar-worker
6. Verify probes run: docker compose logs life-radar-worker
7. Phase 2: docker compose build life-radar-api && docker compose up -d life-radar-api
8. Phase 3: Configure ~/.hermes/mcp_config.yaml to point at life-radar-api
```

---

## Test Plan

This defines what "good enough" means for each subsystem. These criteria must be met before declaring a phase stable.

### Messaging Runtime (Bakeoff)

| Criterion | Threshold | Probe |
|-----------|----------|------|
| Continuous live sync | No gaps >15 min between runs | `runtime_probes.freshness_seconds` |
| Decrypt success | ≥95% of events readable | `messaging_candidates.latest_decrypt_failures` |
| Restart-safe persistence | Survives container restart without manual intervention | Manual restart test |
| Bounded lag | ≤5 min event-to-DB latency | `runtime_probes.latest_freshness_seconds` |
| Health metrics | Exposed in DB and logs | Query `runtime_probes` table |
| Resource usage | ≤500MB RAM, ≤1 CPU | `docker stats` |

### Memory System

| Criterion | Threshold |
|-----------|-----------|
| One UUID linking | Raw event, canonical entity, embedding row, graph edges, external projections all share stable UUID |
| Embedding sync | New messages create embeddings within 1 probe cycle |
| Graph integrity | Edge queries return consistent results |

### Product Behavior

| Criterion | Threshold |
|-----------|-----------|
| `needs_read` vs `needs_reply` distinction | False positive rate <10% on sample of 50 conversations |
| Obligation extraction | Explicit commitments detected from clear language patterns |
| Reminder creation | Direct commands ("remind me...") create `reminders` records |
| Calendar integration | User instructions create Google Calendar events |
| Draft grounding | Generated drafts cite source message IDs |

### Inspectability

| Criterion | Threshold |
|-----------|-----------|
| Prioritization explainability | `conversations.metadata` shows triage scores and reasoning |
| Memory influence | Query shows which memories affected a decision |
| Reminder timing reasoning | `reminders.metadata` shows trigger conditions |
| Projection readiness | `external_projections` shows sync status |

### Known Areas Needing Refinement

- Bridged group-chat behavior
- Bot and bridge-message suppression
- Undecrypted-event handling
- Reply-worthiness thresholds by conversation size and participant type
- Escalation into `important_now` and `follow_up_later`
