# LifeRadar

Personal intelligence and communications triage system.

Ingests messages from Matrix/Beeper, Microsoft Graph (Outlook), and Google Calendar into
PostgreSQL/pgvector, then exposes everything via an MCP API for Hermes Agent.

**Status:** Phase 1 complete. See SPEC.md for full roadmap.

## Quick Start

```bash
cp .env.example .env
# Fill in credentials (see .env.example)
docker compose up -d
docker compose logs -f worker
```

If you set `LIFE_RADAR_API_KEY`, include it as `Authorization: Bearer ...` or `X-API-Key`
when calling write endpoints such as `POST /messages/send` or when proxying through `/mcp`.
In production, MCP is exposed at `https://liferadar.nothing.pink/mcp`; do not publish a
separate MCP subdomain.

## Architecture

- **life-radar-worker** — probe pipeline running every 5 minutes
- **life-radar-db** — pgvector:pg17 with 15 tables
- **life-radar-api** (Phase 2) — FastAPI HTTP API
- **life-radar-matrix-bridge** — internal Matrix send bridge
- **MCP server** (Phase 3) — OpenAPI-generated MCP tools for Hermes

## Connector Notes

- Matrix/Beeper uses `matrix-rust-sdk` as the primary E2EE transport.
- Matrix sync now persists a global `matrix_sync_checkpoint` plus per-conversation
  `matrix_room_checkpoint` metadata to avoid re-walking full history each cycle.
- The raw HTTP Matrix path is retained as an explicit recovery mode, not the normal ingest path.
- `POST /messages/send` performs a real Matrix send for `source='matrix'` by calling the internal
  Matrix bridge service; other sources currently return `501 Not Implemented`.

## Phases

1. Phase 1 (done) - Standalone Docker Compose, all probe scripts
2. Phase 2 - FastAPI HTTP API server
3. Phase 3 - MCP server (OpenAPI to MCP generation)
4. Phase 4 - On-demand agent for deep-dive tasks
5. Phase 5 - Full task management with Linear/project tool integration

## Docs

- SPEC.md - Full specification and roadmap
- schema/ - PostgreSQL schema with pgvector
