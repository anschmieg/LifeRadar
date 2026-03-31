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

## Architecture

- **life-radar-worker** — probe pipeline running every 5 minutes
- **life-radar-db** — pgvector:pg17 with 15 tables
- **life-radar-api** (Phase 2) — FastAPI HTTP API
- **MCP server** (Phase 3) — OpenAPI-generated MCP tools for Hermes

## Phases

1. Phase 1 (done) - Standalone Docker Compose, all probe scripts
2. Phase 2 - FastAPI HTTP API server
3. Phase 3 - MCP server (OpenAPI to MCP generation)
4. Phase 4 - On-demand agent for deep-dive tasks
5. Phase 5 - Full task management with Linear/project tool integration

## Docs

- SPEC.md - Full specification and roadmap
- schema/ - PostgreSQL schema with pgvector