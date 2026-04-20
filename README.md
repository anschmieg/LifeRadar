# LifeRadar

Personal intelligence and communications triage system.

Ingests messages from Telegram, WhatsApp, Matrix/Beeper legacy data, Microsoft Graph (Outlook), and Google Calendar into
PostgreSQL/pgvector, then exposes everything via an MCP API for Hermes Agent.

**Status:** Phase 1 complete. See SPEC.md for full roadmap.

## Local Matrix Harness

```bash
cp .env.local.example .env.local
# Set LIFERADAR_MATRIX_HOMESERVER_URL in .env.local
bin/localdev/matrix-up.sh
bin/localdev/matrix-auth.sh
bin/localdev/matrix-smoke.sh
```

If you set `LIFERADAR_API_KEY`, include it as `Authorization: Bearer ...` or `X-API-Key`
when calling write endpoints such as `POST /messages/send` or when proxying through `/mcp`.
In production, MCP is exposed at `https://liferadar.nothing.pink/mcp`; do not publish a
separate MCP subdomain.

The local Matrix harness is self-contained and uses [docker-compose.local.yaml](/Users/adrian/Projects/LifeRadar/docker-compose.local.yaml:1). It keeps local state under `./data/` and intentionally disables Outlook, Google Calendar, and the legacy SQLite Matrix backfill path by default so Matrix testing stays isolated.

## General Dev Compose

```bash
cp .env.example .env
# Fill in credentials (see .env.example)
docker compose up -d
docker compose logs -f worker
```

## Architecture

- **life-radar-worker** — probe pipeline running every 5 minutes
- **life-radar-db** — pgvector:pg17 with connector state tables
- **life-radar-api** (Phase 2) — FastAPI HTTP API
- **life-radar-chat-gateway** — direct Telegram/WhatsApp auth, sync, and send runtime
- **life-radar-matrix-bridge** — internal Matrix send bridge
- **MCP server** (Phase 3) — OpenAPI-generated MCP tools for Hermes

## Connector Notes

- Telegram uses a direct personal-account connector with browser-assisted login.
- WhatsApp uses a persistent consumer multi-device session with QR login.
- Matrix runs through the first-class `liferadar-matrix` client path and is controlled by `LIFERADAR_MATRIX_ENABLED`.
- Matrix sync now persists a global `matrix_sync_checkpoint` plus per-conversation
  `matrix_room_checkpoint` metadata to avoid re-walking full history each cycle.
- The raw HTTP Matrix path is retained as an explicit recovery mode, not the normal ingest path.
- `POST /messages/send` performs direct sends for `source='telegram'` and `source='whatsapp'`.
- Matrix send remains available when `LIFERADAR_MATRIX_ENABLED=true` and a valid Matrix session is present.

## Phases

1. Phase 1 (done) - Standalone Docker Compose, all probe scripts
2. Phase 2 - FastAPI HTTP API server
3. Phase 3 - MCP server (OpenAPI to MCP generation)
4. Phase 4 - On-demand agent for deep-dive tasks
5. Phase 5 - Full task management with Linear/project tool integration

## Docs

- SPEC.md - Full specification and roadmap
- schema/ - PostgreSQL schema with pgvector

## Project Utilities

Utility scripts live in `bin/`. For the Nextcloud legacy migration compare status, run:

```bash
./bin/nextcloud-status
```

For local Matrix work, use:

```bash
bin/localdev/matrix-up.sh
bin/localdev/matrix-auth.sh
bin/localdev/matrix-smoke.sh --decryption
bin/localdev/matrix-reset.sh
```
