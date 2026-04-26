# Beeper Sidecar — Integration Audit & Decision Memo

**Date:** 2026-04-25
**Branch:** `codex/go-api-beeper-rewrite`
**Status:** UNDER REVISION — prior conclusion was premature

---

## What Changed

The prior memo concluded the sidecar was blocked by a Beeper build restriction. That conclusion was **premature** — it was based on incomplete evidence and did not audit our code against the official Beeper Desktop API documentation.

This memo corrects the record.

---

## Doc Audit: What We Were Likely Doing Wrong

### Finding 1: WebSocket subscription is missing `requestID`

**Severity:** High — concrete bug

The Beeper WebSocket API docs (from `developers.beeper.com/desktop-api/websocket-experimental`) show this subscription format:

```json
{
  "type": "subscriptions.set",
  "requestID": "r2",
  "chatIDs": ["!tdvY9_XjNZ6F5P8DzBxDiqBwla0:ba_aBcD1234EfGh.local-ai.localhost", "..."]
}
```

Our Go runtime (`messaging-runtime/cmd/liferadar-messaging-runtime/main.go:439-442`) sends:

```go
if err := conn.WriteJSON(map[string]any{
    "type":    "subscriptions.set",
    "chatIDs": []string{"*"},
}); err != nil {
```

**Missing: `requestID` field.** The `requestID` is required for the server to correlate subscription responses. Without it, the server may accept the connection but never send events, or close immediately.

This is a real bug in our code, not a Beeper product limitation.

---

### Finding 2: Auth token type is unconfirmed

**Severity:** High — root cause unknown

The docs specify:
```
Authorization: Bearer <your_token>
```

Our Go runtime uses `BEEPER_ACCESS_TOKEN` which is a Matrix session token extracted from `account.db`.

**Critical unknown:** Does the Beeper Desktop API accept Matrix tokens as Bearer tokens, or does it require a separate Desktop API token?

The native desktop MCP tools work, but they may use OAuth/MCP authentication — a different token type than what we're passing. We have not captured what token the native desktop session actually uses for its local API calls.

**The empty HTTP bodies problem** (sidecar returns `Content-Length: 0` even with a real token) could be:
1. Wrong token type being rejected silently (API returns 200 with empty body)
2. Containerized environment suppressing the local API (our prior conclusion)
3. Something else

**We cannot rule out token type without a live capture of the native desktop API headers.**

---

### Finding 3: The `[Updates] disabled` log is from our own patch

**Correction:** The log message `Updates disabled in LifeRadar sidecar` is **added by our Dockerfile** (line 86), not a signal from Beeper's official build. It was added to prevent crash loops from Beeper's auto-updater in the containerized environment.

This means the "definitive signal" in the prior memo was wrong. The log confirms our patch runs, not that Beeper is blocking the API.

---

## What Remains Uncertain

1. **Whether Matrix tokens work as Bearer tokens** for the Desktop API — this has never been tested with a proper comparison capture
2. **Whether the containerized AppImage build** has the same Desktop API code path as the native desktop build
3. **Whether WebSocket `subscriptions.set` with `requestID`** fixes the immediate-close issue (it may — missing request ID is a known protocol error)
4. **What the native desktop actually sends** in its WebSocket subscription frames

---

## What We Fixed Correctly

| Fix | Status |
|-----|--------|
| WebSocket mask bit preservation in proxy | **Correct** — RFC 6455 compliant |
| Dynamic port discovery | **Correct** — works |
| Xvfb/display setup | **Correct** — Beeper runs |
| `BEEPER_DISABLE_GPU=false` | **Correct** — GPU not artificially disabled |
| HTTP body forwarding | **Correct** — complete requests sent |

---

## Recommended Next Steps (in order)

### Step 1: Fix `requestID` in WebSocket subscription

Add `requestID` to the subscription message:

```go
if err := conn.WriteJSON(map[string]any{
    "type":     "subscriptions.set",
    "requestID": "r1",  // add this
    "chatIDs":  []string{"*"},
}); err != nil {
```

Test if this alone fixes the WebSocket close issue. This is a zero-risk change.

### Step 2: Capture native desktop WebSocket frames

Before assuming token mismatch:
1. Run a proxy or tcpdump on the local machine between Beeper and the API
2. Capture the exact WebSocket subscription frame the native Beeper sends
3. Compare: does native include additional fields? Different `requestID` format? Different auth header?

If native sends `requestID` with a specific format, replicate it exactly.

### Step 3: Verify token type

If Step 1 doesn't fix the close issue, investigate token type:
- What token does native Beeper use for the Desktop API? (OAuth token, not Matrix token?)
- Is there a separate Desktop API token endpoint?
- Does the `/v1/info` endpoint (which returns 811 bytes on native) reveal auth requirements?

### Step 4: Then decide

After steps 1-3, if sidecar still fails:
- Decide if the containerized path is viable
- If not, pivot to Matrix API (which bypasses Beeper Desktop API entirely)

---

## Updated Decision

**DO NOT PAUSE YET.** First try:
1. Fix `requestID` bug (5-minute change)
2. Capture native WebSocket frames (30-minute test)
3. Compare token types (if needed)

If after those tests the sidecar is still blocked, then pivot. The prior memo's conclusion was based on incomplete audit and should be disregarded.

---

## Files Audited

- `bin/beeper-sidecar-proxy.py` — WebSocket relay, HTTP body forwarding
- `bin/beeper-sidecar-entrypoint.sh` — Xvfb, display, Beeper startup
- `messaging-runtime/cmd/liferadar-messaging-runtime/main.go` — WebSocket subscription, HTTP API calls, token handling
- `liferadar-api/main.go` — proxies to messaging runtime, handles send flow
- `docker-compose.preview.yaml` — environment configuration
- `Dockerfile.beeper-sidecar` — AppImage extraction, updates patch
- `mcp-server/server.py` — MCP tool layer (talks to Go API, not directly to Beeper)

---

## Sources

- Beeper Desktop API overview: `https://developers.beeper.com/desktop-api`
- WebSocket experimental docs: `https://developers.beeper.com/desktop-api/websocket-experimental`
- WebSocket changelog (`v4.2.557`): `https://developers.beeper.com/desktop-api/changelog/version/v4-2-557-2026-02-13`
- MCP search results (subscription format confirmed via `search_docs`)
