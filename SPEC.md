# LifeRadar — Specification

## Overview

LifeRadar is a personal intelligence and communications triage system. It continuously
ingests messages from Matrix/Beeper (E2EE), Microsoft Graph (Outlook mail), and Google
Calendar, stores them in PostgreSQL/pgvector, and exposes everything via an API to
power an AI agent (Hermes).

**Migrated from:** `openclaw/overlay/life-radar/` ( NousResearch/Hermes-Agent#life-radar )

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Coolify                                                     │
│  ┌─────────────────┐  ┌──────────────────────────────────┐  │
│  │  life-radar-db  │  │  life-radar-worker (container)   │  │
│  │  (pgvector:pg17)│  │  ├─ Rust probe (Matrix E2EE)      │  │
│  │                 │  │  ├─ graph-sync-mail.mjs (Outlook)│  │
│  │                 │  │  ├─ google-calendar-reconcile    │  │
│  │                 │  │  ├─ derive-needs-state.sh (triage)│  │
│  │                 │  │  └─ extract-memory.mjs            │  │
│  └─────────────────┘  └──────────────┬───────────────────┘  │
│                                      │                       │
│  ┌───────────────────────────────────▼───────────────────┐  │
│  │  life-radar-api  (Phase 2 — FastAPI)                  │  │
│  │  Exposes: alerts, conversations, memories, calendar,    │  │
│  │  tasks, send-message, semantic-search                  │  │
│  └────────────────────────┬───────────────────────────────┘  │
└───────────────────────────┼─────────────────────────────────┘
                            │ HTTP/MCP
                            ▼
┌───────────────────────────────────────────────────────────────┐
│  Hermes LXC (10.99.244.100)                                  │
│  ├─ Hermes Agent (systemd gateway)                           │
│  └─ Hermes MCP client → life-radar MCP server               │
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

## Phase 1 — Extraction & Standalone (DONE)

- [x] Extracted all files from `openclaw/overlay/life-radar/`
- [x] Removed hardcoded credentials from `docker-compose.yml`
- [x] Created cleaned `.env.example` with all required variables
- [x] `schema.sql` — full pgvector schema, 15 tables
- [x] `bin/run-probes.sh` — probe orchestrator (5min interval)
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
- [x] `fixtures/msgraph/` — test fixtures for MSGraph sync

---

## Phase 2 — API Server (NEXT)

**Goal:** Expose the DB via a FastAPI HTTP server consumed by Hermes.

### Technology Decision

- **FastAPI** — native async, OpenAPI schema auto-generation, Pydantic validation
- **flask** was considered but lacks built-in OpenAPI generation
- After FastAPI exposes OpenAPI → use **FastAPI-to-MCP bridge** or
  **openapi-to-mcp** to auto-generate MCP server from the same spec

**Key libraries:**
- `fastapi` + `uvicorn` — API server
- `asyncpg` — async PostgreSQL driver (better for connection pooling than psql subprocess)
- `pydantic` — request/response models
- `openapi-to-mcp` or custom MCP transport over stdio

### Endpoints

```
GET  /health                          — liveness check
GET  /alerts                          — urgent triage items
GET  /conversations                   — search/filter inbox
GET  /conversations/{id}              — conversation detail + messages
GET  /memories                        — query memory records
GET  /tasks                           — planned_actions list/filter
POST /tasks                           — create task
GET  /calendar/events                 — read Google Calendar
POST /calendar/events                 — upsert calendar event
POST /messages/send                   — send Matrix/Outlook message (user-approved)
GET  /search                          — semantic search over memories/conversations
GET  /probe-status                    — probe health metrics
```

### TODO

- [ ] Create `api/` directory with FastAPI app
- [ ] Define Pydantic models for all response types
- [ ] Implement each endpoint with asyncpg
- [ ] Add OpenAPI route at `/openapi.json`
- [ ] Write `Dockerfile.api` (or merge into `Dockerfile.worker`)
- [ ] Add auth (API key or HMAC signature for Hermes→API calls)

---

## Phase 3 — MCP Server

**Goal:** Hermes speaks MCP natively. MCP server = wrapper around FastAPI.

### Approach: OpenAPI → MCP

1. FastAPI generates OpenAPI 3.1 spec at `/openapi.json`
2. `openapi-to-mcp` CLI or library consumes the spec and generates MCP tools
3. MCP server runs as a sidecar or embedded stdio server
4. Hermes connects via `mcp` tool config in `~/.hermes/mcp_config.yaml`

**Alternatives considered:**
- `fastapi-mcp` — FastAPI decorator to expose as MCP, less flexible
- Manual MCP server — more work but full control over tool definitions

**Key insight:** Define the tool schema once in FastAPI Pydantic models → auto-generate MCP tool list from the same spec.

### Tools (MCP)

```
alerts          — GET /alerts
conversations   — GET /conversations
conversation    — GET /conversations/{id}
memories        — GET /memories
tasks           — GET/POST /tasks
calendar-events — GET/POST /calendar/events
send-message    — POST /messages/send (user-approved)
search          — GET /search
probe-status    — GET /probe-status
```

### TODO

- [ ] Create `mcp/` directory with MCP server
- [ ] Implement stdio MCP transport
- [ ] Bridge FastAPI responses → MCP tool results
- [ ] Add MCP config snippet to `~/.hermes/mcp_config.yaml`
- [ ] Test end-to-end: Hermes → MCP → API → DB

---

## Phase 4 — On-Demand Agent (Future)

A spawnable agent container (or subprocess) that can be launched by the triage
worker or by the user for deep-dive tasks:

- **Summarization** — "summarize the last 10 messages from @person"
- **Draft reply** — "write a reply to the most urgent message"
- **Research** — "what does the conversation with Dr. Zattler say about X?"
- **Task breakdown** — "turn this vague note into actionable subtasks"

Implementation: separate FastAPI endpoint `POST /agent/sessions` that spins up
a lightweight agent subprocess with conversation context injected.

---

## Phase 5 — Task Management (Future)

The `planned_actions` table is the foundation. Currently:

- Actions are created from direct notes (`capture-direct-note.mjs`)
- Actions are synced bidirectionally with Google Calendar

Future extensions:

- Natural language task creation via agent
- Priority/energy/effort scoring (extend v3 triage to tasks)
- Recurring tasks (cadence profiles from reminders)
- Dependencies between tasks
- Integration with Linear or other project management tools

---

## Environment Variables

### Required

| Variable | Description |
|---|---|
| `LIFE_RADAR_DB_PASSWORD` | PostgreSQL password |
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

## Deployment

Deployed on Coolify as a standalone Docker Compose application.

```
cd ~/Projects/LifeRadar
cp .env.example .env
# Fill in credentials
docker compose up -d
```

The worker container runs `run-probes.sh` on a 5-minute loop.
The API container (Phase 2+) runs `uvicorn api.main:app`.

---

## Migration Notes

- The original implementation ran inside the OpenClaw Docker stack
- Identity files (Matrix session, E2EE keys) must be migrated from the OpenClaw
  host volumes (`/data/openclaw/config`) to the new `matrix-identity` Docker volume
- The existing `life-radar-db` Docker volume on Oracle can be reused; set
  `LIFE_RADAR_DB_PASSWORD` to the existing password
- The `life_radar_db` container name changes to `life-radar-db` in standalone
