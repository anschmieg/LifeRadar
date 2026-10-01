# Signal connector (read-only linked device)

LifeRadar ingests Signal as a **linked device**. It pairs by QR, receives
inbound messages, and has **no send path** — the gateway is read-only.

Default state: **OFF**. `LIFERADAR_SIGNAL_ENABLED=false` and the sidecar only
starts under the `signal` compose profile. Nothing in this document enables it
in production.

## Architecture

```
Signal network
      │  (linked-device protocol)
      ▼
liferadar-signal-api            bbernhard/signal-cli-rest-api:0.101
  MODE=json-rpc                 (private backend + egress, NO published ports)
  config + sole state mount: /app/connectors/signal
      ▲  HTTP + WebSocket (private `liferadar-signal-backend`)
      │
chat-gateway SignalConnector
  existing session mount: /app/connectors
  state init: uid 1000, mode 0700 (one-shot `liferadar-signal-state-init`)
  └─ src/signal-proxy.mjs       deny-by-default loopback allowlist proxy
       binds 127.0.0.1:8099     (only caller of the sidecar)
  └─ src/signal-client.mjs      typed GET + json-rpc receive websocket
  └─ src/providers/signal.mjs   pairing, session, live ingest
      │
      ▼
Postgres (conversations, messages, connector accounts, checkpoints)
```

Three layers, each deliberately narrow:

1. **Sidecar** — no `ports:` or Traefik labels. It has no interface on the
   shared Coolify network. The private `liferadar-signal-backend` carries only
   gateway-to-sidecar traffic; a separate egress network, attached only to the
   sidecar, allows its linked-device connection to Signal.
2. **Allowlist proxy** (`src/signal-proxy.mjs`) — the only gateway process that
   talks to the sidecar. GET-only, exact-route allowlist, query params rebuilt
   from scratch, binds loopback only.
3. **Connector** — never constructs an outbound request. Pairing uses the
   in-process `createSignalApi`/`SignalSidecarClient`; the live client is built
   from the receive stream.

## Receipt policy

| Receipt | Behaviour |
| --- | --- |
| Delivery (signal-cli default on receive) | Accepted, logged at debug, **never persisted, never answered** |
| Read / viewed | Disabled. The proxy rejects `send_read_receipts=true` (403), rejects the explicit upstream receipt POST endpoint, and never exposes a receipt method. |

In the pinned upstream surface, `send_read_receipts` is a normal-mode receive
query option (default `false`). `MODE=json-rpc` instead starts `signal-cli`
daemon without `--send-read-receipts`; this is the relevant setting for the
websocket receive stream. The proxy still forces `send_read_receipts=false` as
an upgrade-compatibility guard and denies attempts to enable it. Delivery
receipts remain signal-cli's documented default and are accepted. The
connector records `{ delivery: 'accepted', read: 'disabled' }` in account
metadata.

## Configuration

| Env | Default | Meaning |
| --- | --- | --- |
| `LIFERADAR_SIGNAL_ENABLED` | `false` | Register the connector + proxy |
| `LIFERADAR_SIGNAL_SIDECAR_URL` | `http://liferadar-signal-api:8080` | Sidecar as seen from the gateway; used only by the proxy |
| `LIFERADAR_SIGNAL_PROXY_PORT` | `8099` | Loopback port of the allowlist proxy |
| `LIFERADAR_SIGNAL_DEVICE_NAME` | `liferadar` | Linked-device name (`[A-Za-z0-9._-]{1,64}`) |
| `LIFERADAR_SIGNAL_LOG_LEVEL` | `info` | Sidecar log level |

## Setup (opt-in)

1. Keep `LIFERADAR_SIGNAL_ENABLED=false` until you explicitly decide to pair; this task does not enable it.
2. For an authorized local test only, run `COMPOSE_PROFILES=signal docker compose up -d liferadar-signal-api`. The initializer creates only `<DATA_ROOT>/connectors/signal` as uid 1000, mode `0700`; that one directory is mounted in the sidecar exactly at `/app/connectors/signal` and is its `SIGNAL_CLI_CONFIG_DIR`.
3. Pair: `POST /internal/connectors/signal/login`, then poll
   `GET /internal/connectors/signal/login/{attempt_id}` and scan the returned
   `qr_svg` from the Signal app (Settings → Linked devices → Link a device).
   The QR/`device_link_uri` lives in memory only; submit clears `qr_text`/`qr_svg`.
4. The connector writes `account` + `paired_at` to `<sessionDir>/signal/signal.session`
   (mode `0600`) and attaches the receive websocket.

## Security invariants

- **No session secrets in Postgres or logs.** Session keys live in the sidecar's
  sole persistent mount at `<DATA_ROOT>/connectors/signal`. Postgres only stores
  the paired account id and pairing metadata. Proxy and connector logs carry no
  query strings or bodies, so `device_link_uri` cannot leak.
- **Intake, never send.** `sendMessage()` throws read-only; there is no
  send/reaction/typing/presence/receipt method. The proxy denies every non-GET
  method, including `POST /v1/receipts/{number}`.
- **Durable.** Session and receive state ride an existing durable bind mount
  (`<DATA_ROOT>/connectors`), surviving restarts and redeploys.
- **Single-flight reconnect** with bounded jittered backoff; a failed write
  never advances the per-dialog checkpoint.

## Tests

```
cd chat-gateway && node --test test/*.test.mjs
```

Covers the proxy allow/deny matrix, the client's loopback-only + typed surface,
pairing lifecycle, receipt/typing handling, live ingest + cursor semantics, and
the receive-only guarantee.
