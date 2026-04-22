# LifeRadar

Personal intelligence and communications triage system.

Ingests Beeper Desktop conversations into PostgreSQL/pgvector, then exposes them through the LifeRadar API and MCP surface for Hermes Agent. Legacy Matrix data can remain in the database for history and recovery, but it is no longer the active messaging runtime.

**Status:** Messaging runtime and primary API have been rewritten around Beeper Desktop + Go. See [SPEC.md](/Users/adrian/Projects/LifeRadar/SPEC.md) for the broader roadmap.

## Local Beeper Runtime

```bash
cp .env.example .env
# Set BEEPER_APPIMAGE_URL if you do not want the default latest AppImage URL.
# Start the rewritten stack.
docker compose -f docker-compose.local.yaml up -d liferadar-db liferadar-beeper-sidecar liferadar-messaging-runtime liferadar-api
```

If you set `LIFERADAR_API_KEY`, include it as `Authorization: Bearer ...` or `X-API-Key`
when calling write endpoints such as `POST /messages/send` or when proxying through `/mcp`.
In production, MCP is exposed at `https://liferadar.nothing.pink/mcp`; do not publish a
separate MCP subdomain.

The local Beeper runtime is defined in [docker-compose.local.yaml](/Users/adrian/Projects/LifeRadar/docker-compose.local.yaml:1). It keeps persistent Beeper state under `./data/beeper-home`, runs Beeper Desktop under `Xvfb`, and exposes noVNC only when you enable it in the environment.

## General Dev Compose

```bash
cp .env.example .env
# Fill in credentials, especially BEEPER_ACCESS_TOKEN after onboarding in Beeper Desktop.
docker compose up -d
docker compose logs -f liferadar-messaging-runtime
```

## Architecture

- **liferadar-beeper-sidecar** — Beeper Desktop under `Xvfb`, with optional VNC/noVNC for onboarding
- **liferadar-messaging-runtime** — Go service for account discovery, chat sync, live events, and outbound send
- **life-radar-db** — pgvector:pg17 with connector state tables
- **liferadar-api** — Go HTTP API over the rewritten messaging runtime
- **liferadar-mcp** — MCP server that keeps the external tool surface stable
- **liferadar-worker** — background probes for Outlook/calendar/memory extraction
- **docker-compose.legacy.yaml** — optional Matrix-era services for archived recovery/import scenarios

## Onboarding

1. Start `liferadar-beeper-sidecar` with `BEEPER_VNC_ENABLED=true` and `BEEPER_NOVNC_ENABLED=true`.
2. Open the noVNC endpoint and sign into Beeper Desktop manually.
3. Enable the Beeper Desktop API in Beeper settings.
4. Create an access token and copy it into `BEEPER_ACCESS_TOKEN`.
5. Restart `liferadar-messaging-runtime`.
6. Disable VNC/noVNC for steady-state operation once the token and profile volume are working.

`GET /connectors` now reports Beeper-centric runtime health, token validity, connected accounts, last sync, and last live-event freshness. `POST /messages/send` only sends for conversations whose metadata marks them as `transport=beeper_desktop`.

## Rewrite Notes

- Beeper Desktop is the single active messaging transport in v1.
- Telegram, WhatsApp, and Signal are modeled through Beeper account metadata, not through separate LifeRadar connectors.
- Legacy Matrix or direct connector records can stay in the database for history, but LifeRadar no longer preserves their runtime quirks just for compatibility.
- The old connector login pages are intentionally retired. Operator onboarding now happens inside Beeper Desktop.

## Phases

1. Phase 1 (done) - Standalone Docker Compose, all probe scripts
2. Phase 2 (done) - Application API, now implemented in Go
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

For legacy Matrix recovery work, the old helper scripts remain in `bin/localdev/`, but they are no longer part of the primary architecture.

If you need the archived Matrix stack, layer [docker-compose.legacy.yaml](/Users/adrian/Projects/LifeRadar/docker-compose.legacy.yaml:1) on top of the main compose file instead of using it in the default deployment path.
