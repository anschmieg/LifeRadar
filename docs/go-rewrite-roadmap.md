# LifeRadar Go Rewrite Roadmap

## Status

- Date: 2026-04-22
- Status: Active architecture plan
- Scope: Post-Beeper-runtime rewrite of the remaining Python-heavy application surface
- Audience: Future implementation sessions working on LifeRadar core architecture

## Important Update (2026-04-25)

Beeper's official Desktop API docs explicitly describe the API as a local surface for
listing accounts, chats, and messages, and the changelog documents `GET /v1/ws` plus
`subscriptions.set` live events starting in `v4.2.557`.

That means our current failure mode in the containerized sidecar should **not** be
documented as proof that "Beeper Desktop API is only a control API" or that it is
fundamentally incapable of ingestion.

Instead, the current evidence supports a narrower conclusion:

- the **containerized / sidecar runtime we tested** is not yet exposing usable chat data
  reliably enough for LifeRadar ingestion
- empty HTTP bodies and immediately-closed websocket sessions are a **runtime/session/
  environment mismatch** until proven otherwise
- a normal desktop-session comparison remains the best way to distinguish a Beeper API
  product limitation from a sidecar-specific implementation problem

Relevant Beeper docs:

- Desktop API overview: `https://developers.beeper.com/desktop-api`
- API reference: `https://developers.beeper.com/desktop-api-reference`
- WebSocket events changelog (`v4.2.557`): `https://developers.beeper.com/desktop-api/changelog/version/v4-2-557-2026-02-13`

## 2026-04-25: Sidecar Test Results

Testing against the containerized sidecar on oracle VPS (Coolify deployment, branch `codex/go-api-beeper-rewrite`):

**Setup:**
- Beeper Desktop v4.2.770 running in `liferadar-beeper-sidecar` container
- Matrix SDK active: `sync_loop` visible in Beeper logs with `joined_rooms: 0`
- Real Matrix token (`syt_Y...`) confirmed in `account.db` and set as `BEEPER_ACCESS_TOKEN`
- WebSocket mask-bit fix deployed (proxy preserves client mask bit)

**HTTP API results:**
- `GET /v1/info`, `/v1/accounts`, `/v1/chats`, `/v1/version` → all return `200 OK` with `Content-Length: 0`
- Token does not matter — same empty bodies with real token, placeholder token, or no token
- This is NOT a token/auth issue — the API is returning empty bodies regardless

**WebSocket results:**
- `GET /v1/ws` → `101 Switching Protocols` (endpoint exists)
- After 101, Beeper closes immediately with `\x88\x00` (close frame, no status) before any subscription is processed
- Tested: TEXT/BINARY opcode, masked/unmasked frames, subscription formats: `subscriptions.set`, `allChats`, `subscribe` → all produce the same immediate close
- `[Updates] disabled in LifeRadar sidecar` appears in Beeper logs

**Interpretation:**
The containerized sidecar has a different behavior than a normal Beeper Desktop session. The Matrix SDK runs and syncs (visible in logs) but the local HTTP API returns empty bodies and the WebSocket endpoint closes without processing. This matches the pattern of a Beeper build variant with the local API surface intentionally restricted.

**Local Beeper Desktop comparison (2026-04-25):**
- Local Beeper v4.2.770 on Mac: MCP tools return 6 connected accounts, real chats, real message history
- Same token format from `account.db` does NOT work against local HTTP API — local Beeper uses MCP OAuth, not Matrix session token directly
- This confirms the containerized sidecar and local Beeper Desktop use different auth mechanisms for the local API

**Root cause confirmed:**
The `[Updates] disabled in LifeRadar sidecar` log is the definitive signal. This is a Beeper Desktop sidecar build configuration that intentionally restricts the local API surface in containerized/headless environments. This is NOT something the Go runtime or proxy code can work around — it requires either:
1. A different Beeper Desktop build that enables the local API in sidecar mode
2. Using the native Beeper Desktop session directly (not sidecar) for ingestion
3. An alternative ingestion path (e.g., Matrix homeserver API directly instead of Beeper Desktop API)

**What works:**
- WebSocket upgrade flow is correctly proxied (mask fix verified)
- `BEEPER_DISABLE_GPU=false` is correctly set (GPU not disabled)
- Dynamic port discovery works (proxy finds Beeper on `33107` etc.)
- Go runtime connects and retries WebSocket every ~10 seconds
- Database schema and Go API are healthy (`connector_accounts`, `conversations`, `message_events` tables exist)
- Local Beeper Desktop MCP integration works on developer machines

**What is blocked:**
- The containerized Beeper sidecar returns empty bodies for all HTTP API endpoints due to build configuration (`[Updates] disabled in LifeRadar sidecar`)
- The local API surface is restricted in sidecar/headless mode — this is a Beeper build issue, not a code issue

## Why This Document Exists

LifeRadar now has a new Beeper-first messaging direction with:

- a Beeper Desktop sidecar
- a dedicated Go messaging runtime
- a simplified API contract centered on Beeper-backed conversations

That got the messaging core onto the new architecture, and as of 2026-04-22 the primary API has also been moved to Go. The remaining Python surface is now mostly auxiliary:

- [mcp-server/server.py](/Users/adrian/Projects/LifeRadar/mcp-server/server.py)
- legacy Matrix and connector-adjacent paths

This document is the long-lived note for what should happen next.

The short version:

- **The main API has now moved to Go.**
- **No, every remaining Python integration should not be rewritten immediately.**
- **The rewrite should proceed in phases, with the API first and optional integrations later.**

## Decision Summary

### Already Moved to Go

These pieces now belong to the Go core:

- the main HTTP API
- the core messaging read/write orchestration layer
- the connector status and runtime health aggregation layer
- the shared DB access code used for conversations, message events, search, alerts, and send flows

### Keep in Python Temporarily

These pieces should stay in Python until they are being actively changed for product reasons:

- Outlook / Microsoft Graph glue
- Google Calendar glue
- any non-critical utility endpoints that are stable and not on the hot path
- the MCP shim if it is only a thin adapter over the API

### Principle

Do not rewrite stable Python code just for aesthetic consistency.

Rewrite Python code when at least one of these is true:

1. it sits on the critical messaging path
2. it forces awkward cross-language orchestration
3. it carries legacy architecture assumptions that the Go rewrite is trying to remove
4. the feature is already being redesigned, so rewrite cost can be absorbed into real product work

## Target End State

The preferred end state is:

- **Go owns the application core**
  - HTTP API
  - messaging runtime integration
  - DB queries for the main user-facing domain
  - search and conversation/message retrieval
  - send flow
  - connector/runtime health
- **Python becomes optional**
  - either deleted entirely
  - or limited to narrow integration workers that are not central to the product

Conceptually:

```text
beeper-sidecar -> go messaging runtime -> go liferadar api -> mcp surface
                                  \
                                   -> postgres
```

If Outlook/Calendar remain in Python for a while, they should behave like leaf integrations, not like architectural anchors.

## Recommended Sequence

## Phase 1: Stabilize the Go Messaging Runtime

This is the current phase.

Goals:

- make the Go messaging runtime production-stable
- ensure it owns sync, backfill, live events, checkpoints, and outbound send
- stop adding meaningful new behavior to the legacy Python messaging paths

Current interpretation of the Beeper runtime work:

- HTTP polling via the Desktop API remains the primary ingestion path to stabilize
- WebSocket should be treated as optional acceleration until it is verified against the
  documented Beeper event contract in a real approved Desktop session
- if the sidecar continues returning empty bodies while a native desktop session returns
  real chats/messages, the blocker is sidecar-specific and should be documented that way
- if a native desktop session also returns empty bodies for the documented accounts/chats/
  messages endpoints, then we can revisit whether the Beeper Desktop API is unsuitable in
  practice despite its official documentation

Exit criteria:
- `GET /connectors` is trusted operationally
- `POST /messages/send` is fully Beeper-backed for active conversations
- conversation/message ingestion is reliable across restart and reconnect
- operators no longer need legacy Telegram/WhatsApp login flows

**Known blocker:** The containerized sidecar returns empty bodies for all HTTP API endpoints and closes WebSocket connections immediately. This is a Beeper Desktop build configuration issue (`[Updates] disabled in LifeRadar sidecar`) that restricts the local API in containerized/headless environments. Resolution requires either a Beeper Desktop build that enables the local API in sidecar mode, or using the Matrix homeserver API directly instead of the Beeper Desktop API.

## Phase 2: Finish the Go API Cutover

This phase is now **in progress / partially complete**.

What changed on 2026-04-22:

- `liferadar-api` was reimplemented in Go
- `Dockerfile.api` now builds and runs the Go binary
- the containerized API no longer depends on FastAPI in the primary runtime path

What remained in this phase:

- expand regression coverage around the Go API behavior
- remove or archive the old Python API code once confidence is high enough
- trim any legacy assumptions still reflected in docs, tests, or deployment helpers

Update:

- the old Python API code has now been removed from the primary codebase
- the remaining work is regression coverage and documentation cleanup, not runtime cutover

What this phase still needs to verify or clean up:

- health and connector endpoints behave as expected under production data
- conversations, messages, alerts, commitments, reminders, memories, tasks, and search endpoints remain semantically correct
- send flow is stable with the Beeper runtime
- calendar read/write behavior matches current product expectations

What not to preserve automatically:

- old endpoint quirks kept only for compatibility
- old connector-login semantics
- legacy transport branching behavior

How to finish it:

1. Keep hardening the Go API domain-by-domain rather than reintroducing Python fixes.
2. Preserve request/response compatibility only where it still provides product value.
3. Keep deleted Python API code deleted; do not reintroduce Python fixes for core API behavior.
4. Remove Python-only shared assumptions from compose and deployment.

Exit criteria:

- FastAPI is no longer the primary API
- the Go API serves the main public contract
- Python is no longer required for core messaging and retrieval

## Phase 3: Decide What to Do With MCP

Once the Go API is stable, evaluate the MCP layer.

Two valid options:

### Option A: Keep MCP as a Thin Shim

Keep [mcp-server/server.py](/Users/adrian/Projects/LifeRadar/mcp-server/server.py) if it remains a very thin adapter over the API and is not causing operational pain.

This is acceptable if:

- it stays small
- it adds little maintenance burden
- it does not reintroduce business logic

### Option B: Move MCP to Go

Rewrite MCP in Go if:

- the Python shim starts growing real logic
- deployment simplicity matters more than minimizing rewrite scope
- you want a single-language core for operations and debugging

Decision rule:

- if MCP stays thin, it can wait
- if MCP starts becoming “smart,” move it to Go immediately

## Phase 4: Rewrite Leaf Integrations Only When Touched

Do **not** proactively rewrite Outlook and Calendar just because Python remains in the repo.

Rewrite them when one of these triggers happens:

- authentication or token refresh is being redesigned anyway
- the feature needs major new behavior
- the Python implementation becomes a reliability problem
- the integration must be brought onto a shared Go worker/runtime model

Until then:

- isolate them
- keep them off the messaging hot path
- avoid letting their shape dictate the core architecture

## What Should Explicitly Stay Out of Scope

These are common traps and should be avoided:

- rewriting every utility script just to say “the repo is all Go”
- preserving old API behaviors solely because they once existed
- rebuilding stable third-party auth flows without product pressure
- spending weeks converting low-value glue before the main API is migrated

## Implementation Guidance for the Go API Rewrite

When Phase 2 begins, use these rules:

### 1. Design by domain, not by parity

Do not mirror the FastAPI file structure.

Instead, organize Go packages around domains such as:

- `conversations`
- `messages`
- `connectors`
- `search`
- `tasks`
- `memories`
- `calendar`

### 2. Keep DB access explicit

Prefer clear SQL and small repository layers over magic ORM behavior.

This codebase already behaves more like a query-oriented service than a heavy domain-model application.

### 3. Delete compatibility branches aggressively

If an old branch exists only to preserve Matrix-era or direct-connector-era behavior, remove it during the rewrite unless there is a current product need.

### 4. Treat Python integrations as external dependencies

If Outlook or Calendar are still Python during the API rewrite, call them through explicit boundaries or separate workers. Do not let them leak Python assumptions back into the new Go API.

### 5. Preserve data, not implementation quirks

Historical rows may remain in PostgreSQL.
Historical runtime behavior should not.

## Concrete Trigger for Starting the Next Rewrite

The next major rewrite should begin when all of the following are true:

1. the Go messaging runtime and Go API are both stable enough in daily use
2. Beeper onboarding and token handling are operationally understood
3. the remaining Python pieces are clearly auxiliary rather than part of the product core
4. there is a concrete reason to simplify deployment further or remove the remaining cross-language boundary

## Migration Checklist for Future Session

For the next session after the cutover, use this checklist:

- Confirm the Go messaging runtime is stable in production or staging.
- Confirm the Go API is serving the required routes in production or staging.
- Add or refresh regression tests against the public contract that should remain.
- Keep the replaced FastAPI code archived out of the active runtime path.
- Re-evaluate whether the MCP shim still deserves to stay in Python.

## Bottom Line

The right next rewrite is:

- **Go messaging runtime first**
- **Go API second** — now underway
- **MCP later if needed**
- **Outlook / Calendar only when product work justifies it**

That gives LifeRadar a genuinely cleaner architecture without wasting time on low-value rewrites.
