# LifeRadar Matrix Rehabilitation — Comprehensive Report

## Document Info

| Field | Value |
|-------|-------|
| Date | 2026-04-16 |
| Status | Draft |
| Scope | Phase A (Decouple Beeper) + Phase B (Self-Generated E2EE) |
| Effort Estimate | ~6–8 hours across 3 sessions |

---

## 1. Executive Summary

LifeRadar currently uses Matrix as its primary messaging integration, but the implementation is tightly coupled to Beeper Desktop. This coupling appears in three forms:

1. **Feature flag gating** — `LIFE_RADAR_BEEPER_ENABLED` gates all Matrix operations
2. **Beeper-specific key management** — E2EE room keys are imported from a Beeper Desktop export file
3. **Beeper-specific OAuth** — Login flow targets `matrix.beeper.com` specifically

The coupling was appropriate when the system was bootstrapped from an OpenClaw deployment that used Beeper Desktop as its Matrix client. However, it now blocks two important capabilities:

- Running Matrix without Beeper Desktop running as a sidecar
- Supporting any other Matrix homeserver (e.g., a self-hosted homeserver, or a different managed provider)

This report defines two phases:

- **Phase A — Decouple Beeper** (the path of least resistance): Generalize the existing Rust SDK integration so it works without Beeper-specific env vars, key exports, or kill-switches. The work is 90% renames, env-var additions, and conditional logic removal — no rewrites.
- **Phase B — Self-Generated E2EE** (the principled end state): Use Matrix's built-in E2E Backup protocol so LifeRadar generates and manages its own room keys, eliminating the Beeper Desktop key export dependency entirely.

---

## 2. Current Architecture

### 2.1 System Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         LIFE RADAR ARCHITECTURE                             │
│                                                                             │
│  ┌────────────────────────────────────────────────────────────────────┐   │
│  │  life-radar-worker                                                  │   │
│  │  ┌────────────────────────────────────────────────────────────────┐ │   │
│  │  │  run-probes.sh (orchestrator, 5-min loop)                      │ │   │
│  │  │                                                              │ │   │
│  │  │  if [ "$LIFE_RADAR_BEEPER_ENABLED" = "true" ]; then          │ │   │
│  │  │      life-radar-matrix-rust-probe --mode=ingest_live_history  │ │   │
│  │  │      life-radar-matrix-rust-probe --mode=send_message         │ │   │
│  │  │  fi                                                           │ │   │
│  │  │                                                              │ │   │
│  │  │  graph-sync-mail.mjs (Outlook)                               │ │   │
│  │  │  google-calendar-ingest.mjs                                   │ │   │
│  │  │  derive-needs-state.sh                                        │ │   │
│  │  │  extract-memory.mjs                                          │ │   │
│  │  └────────────────────────────────────────────────────────────────┘ │   │
│  └────────────────────────────────────────────────────────────────────┘   │
│                                    ▲                                        │
│                                    │ env: BEEPER_ENABLED=true               │
│                                    │ env: MATRIX_E2EE_EXPORT_PATH=...       │
│                                    │ env: MATRIX_E2EE_EXPORT_PASSPHRASE     │
│  ┌─────────────────────────────────│────────────────────────────────────┐   │
│  │  life-radar-matrix-bridge       │                                    │   │
│  │  FastAPI :8010                  │                                    │   │
│  │  POST /send → spawns Rust probe │                                    │   │
│  │  in send_message mode          │                                    │   │
│  └─────────────────────────────────│────────────────────────────────────┘   │
│                                    ▲                                        │
│  ┌─────────────────────────────────│────────────────────────────────────┐   │
│  │  life-radar-api                 │                                    │   │
│  │  FastAPI :8000                  │                                    │   │
│  │  POST /messages/send           │                                    │   │
│  │  if source=='matrix' && BEEPER_ENABLED: call matrix-bridge           │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌────────────────────────────────────────────────────────────────────┐   │
│  │  life-radar-db (PostgreSQL/pgvector)                                │   │
│  │  conversations, message_events, runtime_probes, messaging_candidates│   │
│  └────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌────────────────────────────────────────────────────────────────────┐   │
│  │  life-radar-identity (Docker named volume → /data/life-radar/identity)│ │
│  │  matrix-session.json      ← access_token, refresh_token, user_id     │   │
│  │  matrix-rust-sdk-store/   ← SQLite: crypto, room state, event cache  │   │
│  │  beeper-e2e-keys.txt      ← Beeper Desktop Megolm session export    │   │
│  │  .e2ee-export-passphrase  ← Passphrase to decrypt the above          │   │
│  └────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Key Files

| File | Role | Beeper Coupling |
|------|------|-----------------|
| `bin/run-probes.sh:26` | Orchestrator — gates Matrix ops behind `BEEPER_ENABLED` | **Hard gate** |
| `bin/bootstrap.sh:52` | DB init — runs backfill/prune only when `BEEPER_ENABLED=true` | **Hard gate** |
| `bin/oauth-device-flow.mjs:21` | OAuth flow — default homeserver hardcoded to Beeper | **Hardcoded URL** |
| `matrix-rust-probe/src/main.rs:83` | Key export path defaulting to `beeper-e2e-keys.txt` | **Beeper path** |
| `matrix-rust-probe/src/main.rs:1734` | Session tokens — `refresh_token: None` (no token refresh) | **Broken** |
| `api/main.py:1078` | API send — blocks Matrix sends unless `BEEPER_ENABLED` | **Hard gate** |
| `docker-compose.yaml:89` | Worker env — passes BEEPER_ENABLED to container | **Env var** |
| `.env.example:29` | Default paths all point to `/data/openclaw/config/.openclaw/...` | **OpenClaw paths** |

### 2.3 Data Flow Analysis

**Ingest (read) path:**
1. `run-probes.sh` checks `LIFE_RADAR_BEEPER_ENABLED` — if false, Matrix probe is skipped entirely
2. If true: `life-radar-matrix-rust-probe` runs in `ingest_live_history` mode
3. SDK loads session from `matrix-session.json` (access_token, user_id, device_id, homeserver)
4. SDK optionally imports room keys from `beeper-e2e-keys.txt` via `client.encryption().import_room_keys()`
5. SDK performs `sync_once()` to get new events from the homeserver
6. Events are decrypted (if keys available), parsed, upserted to PostgreSQL
7. `next_batch` token persisted to `runtime_metadata` table

**Send path:**
1. `POST /messages/send` in API
2. If `source=='matrix'` AND `BEEPER_ENABLED` (line 1078), call `run_matrix_send()`
3. `run_matrix_send()` POSTs to `http://life-radar-matrix-bridge:8010/send`
4. Bridge spawns Rust binary with `MODE=send_message`, `SEND_ROOM_ID`, `SEND_TEXT`
5. SDK syncs, resolves room, sends text, returns `event_id`

**Token refresh:** Not implemented. The session file stores a `refresh_token` (from the OAuth device code flow), but the Rust probe does not use it. This means the session eventually expires and requires re-authentication.

### 2.4 Session File Format

The `matrix-session.json` file (written by `oauth-device-flow.mjs`):

```json
{
  "access_token": "syt_...",
  "refresh_token": "xxxx",          // ← written but not used by Rust probe
  "user_id": "@user:matrix.beeper.com",
  "device_id": "BBBBBBBBBBB",
  "homeserver": "https://matrix.beeper.com",
  "expires_at": "2026-04-16T12:00:00Z",
  "expires_in": 3600,
  "saved_at": "2026-04-13T..."
}
```

The Rust probe's `SessionFile` struct (main.rs:31-38) matches this format but **does not use `refresh_token`** in `SessionTokens` (line 1734).

---

## 3. Problem Statement

### 3.1 The Beeper Dependency Tree

```
LIFE_RADAR_BEEPER_ENABLED (env var)
  ├── run-probes.sh:26 gates matrix-probe and matrix-ingest
  ├── bootstrap.sh:52 gates backfill and prune scripts
  ├── docker-compose.yaml:89 passed to worker container
  ├── api/main.py:1078 gates Matrix send in API
  └── .env.example:13 documents the flag

         ↓ (what the flag actually gates)

Matrix operations
  ├── matrix-rust-probe binary (built once, used for many modes)
  ├── matrix-session.json (access token from Beeper's OAuth flow)
  ├── matrix-rust-sdk-store/ (SDK state, SQLite)
  ├── beeper-e2e-keys.txt (E2EE keys exported from Beeper Desktop)
  ├── .e2ee-export-passphrase (passphrase for the above)
  └── matrix-bridge FastAPI service

         ↓ (what this prevents)

1. Running Matrix without Beeper Desktop present
2. Connecting to any Matrix homeserver other than beeper.com
3. Using Matrix as a standalone feature, not gated behind a "Beeper mode"
4. Clean token refresh (sessions eventually expire without refresh)
```

### 3.2 Specific Issues

| Issue | Severity | Location | Description |
|-------|----------|----------|-------------|
| Feature flag kill-switch | High | `run-probes.sh:26` | All Matrix ops require `BEEPER_ENABLED=true`. Can't run Matrix standalone. |
| Hardcoded homeserver | High | `oauth-device-flow.mjs:21` | Default `https://matrix.beeper.com` with no override path |
| No token refresh | High | `main.rs:1734` | `refresh_token: None` — sessions expire and need manual re-auth |
| Beeper key export required | Medium | `main.rs:83` | Default paths point to `beeper-e2e-keys.txt` — no fallback for self-managed keys |
| Beeper-specific env vars | Medium | `docker-compose.yaml:89` | `LIFE_RADAR_BEEPER_ENABLED` passed to worker, no `LIFE_RADAR_MATRIX_ENABLED` equivalent |
| API gated on Beeper flag | High | `api/main.py:1078` | Matrix send blocked unless `BEEPER_ENABLED=true` |
| OpenClaw path references | Low | `.env.example:29` | Default paths use `openclaw/.openclaw/identity/...` from migration era |
| `provider_hints` Beeper references | Low | `adapter.rs:116` | Hardcoded `["beeper-matrix-compatible"]` hint |

### 3.3 Impact of Each Issue

**High severity:**
- Cannot run Matrix ingest without setting `BEEPER_ENABLED=true`
- Sessions expire without automatic refresh, requiring manual re-auth
- Cannot target a different Matrix homeserver (e.g., self-hosted)
- API send blocked even when Matrix is configured and working

**Medium severity:**
- Key management requires Beeper Desktop to be running (to export keys)
- Key rotation requires re-exporting from Beeper Desktop

**Low severity:**
- Cosmetic Beeper references in diagnostics (provider_hints, metadata)
- Migration-era path references in `.env.example`

---

## 4. Phase A — Decouple Beeper (Path of Least Resistance)

### Philosophy

The `matrix-rust-probe` is well-structured. The SDK handles E2EE, sync, and sending. The work is **configuration generalization**, not code rewriting. 90% of changes are:
- Renaming env vars (Beeper-specific → generic)
- Removing conditional gates
- Adding token refresh (one-line, if `refresh_token` exists)
- Making homeserver configurable (already parameterized, just needs env defaults)

### 4.1 Step-by-Step Implementation

---

#### Step A1 — Replace `LIFE_RADAR_BEEPER_ENABLED` with `LIFE_RADAR_MATRIX_ENABLED`

**Files:** `bin/run-probes.sh`, `bin/bootstrap.sh`, `docker-compose.yaml`, `.env.example`

**Change:**

```bash
# Before (run-probes.sh:26)
if [[ "${LIFE_RADAR_BEEPER_ENABLED,,}" == "true" ]]; then
    run_step "matrix-probe" /usr/local/bin/life-radar-matrix-rust-probe || true
    if ! run_step "matrix-ingest" env LIFE_RADAR_MATRIX_RUST_MODE=ingest_live_history /usr/local/bin/life-radar-matrix-rust-probe; then
      matrix_ingest_failed=1
      ...
    fi
fi

# After
if [[ "${LIFE_RADAR_MATRIX_ENABLED,,}" != "false" ]]; then
    run_step "matrix-probe" /usr/local/bin/life-radar-matrix-rust-probe || true
    if ! run_step "matrix-ingest" env LIFE_RADAR_MATRIX_RUST_MODE=ingest_live_history /usr/local/bin/life-radar-matrix-rust-probe; then
      matrix_ingest_failed=1
      ...
    fi
fi
```

**Also update:**
- `bootstrap.sh:52` — same pattern, replace `BEEPER_ENABLED` check with `MATRIX_ENABLED`
- `docker-compose.yaml` — replace `LIFE_RADAR_BEEPER_ENABLED` env var with `LIFE_RADAR_MATRIX_ENABLED`
- `.env.example` — replace `LIFE_RADAR_BEEPER_ENABLED=false` with `LIFE_RADAR_MATRIX_ENABLED=true`
- `api/main.py:81` — replace `BEEPER_ENABLED` check with `MATRIX_ENABLED`

**Backward compatibility:** `LIFE_RADAR_BEEPER_ENABLED=true` implies `LIFE_RADAR_MATRIX_ENABLED=true` for one release cycle, then remove.

---

#### Step A2 — Add `LIFE_RADAR_MATRIX_HOMESERVER_URL` env var

**Files:** `oauth-device-flow.mjs`, `.env.example`, `docker-compose.yaml`

**Change in `oauth-device-flow.mjs`:**

```javascript
// Before
const { values } = parseArgs({
  options: {
    homeserver: { type: 'string', short: 'h', default: 'https://matrix.beeper.com' },
    ...
  },
});

// After — check env var first, fall back to Beeper default for migration compat
const HOMESERVER = process.env.LIFE_RADAR_MATRIX_HOMESERVER_URL
    || process.env.MATRIX_HOMESERVER_URL
    || values.homeserver
    || 'https://matrix.beeper.com';
```

This means the script already supports a non-Beeper homeserver via env var, no code change needed beyond precedence.

**Also add to `docker-compose.yaml` worker service:**
```yaml
LIFE_RADAR_MATRIX_HOMESERVER_URL: "${LIFE_RADAR_MATRIX_HOMESERVER_URL}"
```

---

#### Step A3 — Enable Token Refresh

**File:** `matrix-rust-probe/src/main.rs:1730-1736`

**Change:**

```rust
// Before
let session = MatrixSession {
    meta: SessionMeta { user_id, device_id },
    tokens: SessionTokens {
        access_token: session_file.access_token.clone(),
        refresh_token: None,  // ← hardcoded None
    },
};

// After — use refresh_token from session file if available
let refresh_token = session_file.refresh_token.clone()
    .filter(|t| !t.is_empty());

let session = MatrixSession {
    meta: SessionMeta { user_id, device_id },
    tokens: SessionTokens {
        access_token: session_file.access_token.clone(),
        refresh_token,  // ← use what's in the session file
    },
};
```

**Verification:** Check that `session_file` struct includes `refresh_token` field. Based on `oauth-device-flow.mjs:234`, it does: `refresh_token: tokenData.refresh_token || null`. The Rust `SessionFile` struct (main.rs:31-38) does not currently have `refresh_token` field, so this requires adding the field to `SessionFile` first.

---

#### Step A4 — Rename Beeper-Specific Env Vars (Optional but Recommended)

**Files:** `matrix-rust-probe/src/main.rs`, `docker-compose.yaml`, `.env.example`

| Old Env Var | New Env Var | Default |
|-------------|-------------|---------|
| `MATRIX_E2EE_EXPORT_PATH` | `MATRIX_ROOM_KEYS_PATH` | `/app/identity/matrix-e2e-keys.txt` (no longer Beeper-specific) |
| `MATRIX_E2EE_EXPORT_PASSPHRASE_PATH` | `MATRIX_ROOM_KEYS_PASSPHRASE_PATH` | `/app/identity/.e2e-keys-passphrase` |

**Rationale:** The import function `maybe_import_room_keys` already handles missing files gracefully. Renaming these makes the system portable to any Matrix client that exports room keys in the standard Megolm format. Note: the file format from Beeper Desktop is a standard Megolm export, so any Matrix client export in the same format would work.

**Implementation:**
```rust
// Before (main.rs:83)
key_export_path: PathBuf::from(env_var(
    "MATRIX_E2EE_EXPORT_PATH",
    "/app/identity/beeper-e2e-keys.txt",
)),

// After
key_export_path: PathBuf::from(env_var(
    "MATRIX_ROOM_KEYS_PATH",
    "/app/identity/matrix-e2e-keys.txt",  // new canonical path
)),
```

**Back-compat shim:** If `MATRIX_E2EE_EXPORT_PATH` is set but `MATRIX_ROOM_KEYS_PATH` is not, use the former. Document the transition.

---

#### Step A5 — Add `LIFE_RADAR_MATRIX_ENABLED` to API Send Path

**File:** `api/main.py:1077-1083`

**Change:**

```python
# Before
if conversation["source"] == "matrix":
    if not BEEPER_ENABLED:
        raise HTTPException(
            status_code=501,
            detail="Sending messages for source 'matrix' is disabled while Beeper integration is off",
        )
    message_id = await run_matrix_send(conversation["external_id"], request.content_text)

# After
if conversation["source"] == "matrix":
    matrix_enabled = os.environ.get("LIFE_RADAR_MATRIX_ENABLED", "true").lower() != "false"
    if not matrix_enabled:
        raise HTTPException(
            status_code=501,
            detail="Matrix messaging is disabled (LIFE_RADAR_MATRIX_ENABLED=false)",
        )
    message_id = await run_matrix_send(conversation["external_id"], request.content_text)
```

Note: We flip the default to `true` (enabled) since Matrix is the primary messaging source.

---

#### Step A6 — Remove `LIFE_RADAR_BEEPER_ENABLED` from All Dockerfiles

**Files:** `docker-compose.yaml` (already updated in A1), any Dockerfile env var passes

The `Dockerfile.worker` and `Dockerfile.matrix-bridge` don't directly reference the flag — it's passed through `docker-compose.yaml` env vars.

---

#### Step A7 — Clean Up Beeper-Specific Metadata

**File:** `matrix-rust-probe/src/adapter.rs:116`

**Change:**
```rust
// Before
"provider_hints": ["beeper-matrix-compatible"],

// After
"provider_hints": ["matrix-compatible"],
```

Also update `adapter.rs:186-204` `provider_hints()` function — the "beeper" detection in event fields is still useful for bridge message filtering, just rename the hint.

---

#### Step A8 — Update `oauth-device-flow.mjs` to Accept Homeserver via Env Var

Already done conceptually in Step A2. Verify the precedence order is correct and document that `LIFE_RADAR_MATRIX_HOMESERVER_URL` is the canonical env var for CI/automated use.

---

#### Step A9 — Update `.env.example` with New Env Vars

```env
# === Matrix / Beeper (formerly LIFE_RADAR_BEEPER_ENABLED) ===
LIFE_RADAR_MATRIX_ENABLED=true
LIFE_RADAR_MATRIX_HOMESERVER_URL=https://matrix.beeper.com

# Session and identity (copied from OpenClaw data mount, or fresh from oauth-device-flow.mjs)
MATRIX_SESSION_PATH=/data/life-radar/identity/matrix-session.json
MATRIX_RUST_SESSION_PATH=${MATRIX_SESSION_PATH}
MATRIX_RUST_STORE=/data/life-radar/identity/matrix-rust-sdk-store

# Room key export (Beeper Desktop format, or any Matrix client Megolm export)
# Supports: Beeper Desktop export, Element export, or Matrix E2E Backup restore
MATRIX_ROOM_KEYS_PATH=/data/life-radar/identity/matrix-e2e-keys.txt
MATRIX_ROOM_KEYS_PASSPHRASE_PATH=/data/life-radar/identity/.e2e-keys-passphrase

# Backward compat — remove in next release
# MATRIX_E2EE_EXPORT_PATH (use MATRIX_ROOM_KEYS_PATH instead)
# MATRIX_E2EE_EXPORT_PASSPHRASE_PATH (use MATRIX_ROOM_KEYS_PASSPHRASE_PATH instead)
# LIFE_RADAR_BEEPER_ENABLED (use LIFE_RADAR_MATRIX_ENABLED instead)
```

---

#### Step A10 — Docker Compose Service Update

Update the `life-radar-worker` service env vars in `docker-compose.yaml`:

```yaml
environment:
  # ... existing DB vars ...
  LIFE_RADAR_MATRIX_ENABLED: "${LIFE_RADAR_MATRIX_ENABLED:-true}"
  LIFE_RADAR_MATRIX_HOMESERVER_URL: "${LIFE_RADAR_MATRIX_HOMESERVER_URL:-https://matrix.beeper.com}"
  MATRIX_SESSION_PATH: "${MATRIX_SESSION_PATH}"
  MATRIX_RUST_SESSION_PATH: "${MATRIX_RUST_SESSION_PATH}"
  MATRIX_RUST_STORE: "${MATRIX_RUST_STORE}"
  MATRIX_ROOM_KEYS_PATH: "${MATRIX_ROOM_KEYS_PATH}"
  MATRIX_ROOM_KEYS_PASSPHRASE_PATH: "${MATRIX_ROOM_KEYS_PASSPHRASE_PATH}"
```

---

### 4.2 Phase A Implementation Summary

| Step | Change | Files | Risk | Est. Time |
|------|--------|-------|------|-----------|
| A1 | Replace kill-switch flag | `run-probes.sh`, `bootstrap.sh`, `docker-compose.yaml`, `.env.example` | Low | 30m |
| A2 | Add MATRIX_HOMESERVER_URL env | `oauth-device-flow.mjs`, `docker-compose.yaml` | None | 15m |
| A3 | Enable token refresh | `main.rs` (add refresh_token to SessionFile, use in SessionTokens) | Medium | 30m |
| A4 | Rename Beeper env vars | `main.rs`, `docker-compose.yaml`, `.env.example` | Low | 20m |
| A5 | Update API send gate | `api/main.py` | Low | 10m |
| A6 | Remove BEEPER_ENABLED from Dockerfiles | `docker-compose.yaml` | Low | 5m |
| A7 | Clean Beeper metadata | `adapter.rs` | None | 10m |
| A8 | Verify OAuth homeserver precedence | `oauth-device-flow.mjs` | None | 10m |
| A9 | Update .env.example | `.env.example` | Low | 10m |
| A10 | Update docker-compose env vars | `docker-compose.yaml` | Low | 10m |

**Total Phase A: ~2.5 hours**

---

## 5. Phase B — Self-Generated E2EE (Matrix E2E Backup)

### 5.1 Why This Matters

Currently, E2EE room keys must come from a **Beeper Desktop export**. This creates a hard dependency: Beeper Desktop must have been used at some point to create and export the keys, and the keys must be refreshed if room membership changes (new devices, new joined rooms, etc.).

The Matrix protocol includes **E2E Backup** — a standardized way for clients to store and retrieve room keys from the homeserver. This means:

1. LifeRadar can generate its own keys (via the Rust SDK)
2. Keys can be uploaded to the homeserver via `/_matrix/client/v3/room_keys/backup`
3. Keys can be downloaded on session restore via `/_matrix/client/v3/room_keys/backup`
4. No Beeper Desktop export needed — ever

The evidence this works: **Element logged into your Beeper account successfully**. Element is a standard Matrix client using standard Matrix protocols. If Element can decrypt your messages, a properly-configured LifeRadar using Matrix E2E Backup can too.

### 5.2 Implementation Path

**The SDK already supports this.** The Rust `matrix-sdk` has:

```rust
// Upload keys to homeserver E2E Backup
client.encryption().backup()

// Download keys from homeserver E2E Backup
client.encryption().load_backup()
```

**The flow would be:**

1. Fresh login (via `oauth-device-flow.mjs`) creates a new device
2. On first sync, the SDK generates Megolm session keys locally
3. SDK uploads keys to homeserver via E2E Backup (or the homeserver has a pre-existing backup from another client)
4. On subsequent sessions (after restart, re-deploy), SDK downloads keys from E2E Backup
5. No Beeper Desktop key export file needed

**The key verification problem:**
When you first log in on a new device, Matrix requires device verification to trust the crypto. The standard approach is emoji/SAS verification — the two devices each show a short code and you confirm they match.

For a fully automated sidecar, this is tricky. Options:
- **Watch and wait:** Use the Element desktop app to verify the new LifeRadar device (show the verification request in Element, approve it in Element's settings)
- **Key backup from existing session:** If LifeRadar loads a session that already has device keys (from the SDK SQLite store), it may be able to decrypt without verification if the session was previously verified
- **Skip verification for read-only:** If decryption fails without verification, fall back to HTTP plaintext sync (no E2EE) as a last resort, with a clear health metric indicating decryption failure

**Recommended approach:** For Phase B, implement the E2E Backup integration but keep the Beeper key export fallback for rooms where self-generated keys don't work. This gives a migration path: start with your existing Beeper keys, gradually the SDK's own key management takes over.

### 5.3 Phase B Implementation

Add to `matrix-rust-probe/src/main.rs` in the `ingest_live_history` flow:

```rust
// After restoring session, attempt E2E Backup restore
if client.encryption().is_enabled() {
    match client.encryption().load_backup().await {
        Ok(backup_version) => {
            eprintln!("E2E Backup restored: version {}", backup_version);
        }
        Err(err) => {
            eprintln!("E2E Backup restore failed (non-fatal): {}", err);
            // Continue with whatever keys we have (Beeper export or none)
        }
    }

    // Also ensure our own keys are backed up
    if let Err(err) = client.encryption().backup().await {
        eprintln!("E2E Backup upload failed (non-fatal): {}", err);
    }
}
```

The `maybe_import_room_keys` function becomes a **fallback** — if self-generated keys and E2E Backup don't decrypt a room, try importing from the Beeper export. The marker file (`matrix-rust-sdk-store/room-key-import-marker.json`) already handles caching to avoid re-importing.

**Health metric:** Add a `e2e_backup_version` field to the probe metadata, so you can see whether E2E Backup is being used vs Beeper export.

---

## 6. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Token refresh breaking (new session format) | Low | High | Verify `SessionFile` struct has `refresh_token` field before changing `SessionTokens` |
| Beeper key import breaking after rename | Low | Medium | Keep fallback to old env var names for one release |
| Matrix send breaking in API after flag change | Medium | High | Test send flow after changing `BEEPER_ENABLED` → `MATRIX_ENABLED` |
| E2E Backup not working for existing rooms | Medium | Medium | Keep Beeper key export fallback for Phase B |
| Session expiry causing gap in ingest | Low | Medium | Token refresh (Step A3) addresses this |

**Rollback plan:** All changes are additive (new env vars, renamed vars, new code paths). The only destructive change is removing the `LIFE_RADAR_BEEPER_ENABLED` gates — which would simply restore the pre-change behavior if reverted. Use feature flags in git for clean revert.

---

## 7. Testing Strategy

### Phase A Verification Checklist

After each step, verify:

- [ ] `docker compose up -d life-radar-worker` — container starts without `LIFE_RADAR_BEEPER_ENABLED` set
- [ ] `docker compose logs life-radar-worker | grep matrix` — matrix probe runs without the flag
- [ ] `docker compose exec life-radar-worker life-radar-matrix-rust-probe --mode=ingest_live_history` — ingest works with `LIFE_RADAR_MATRIX_ENABLED=true`
- [ ] Session token refresh: trigger a long-running sync (>1 hour) — verify token refresh occurs without re-auth
- [ ] `POST /messages/send` with `source='matrix'` — returns success when `LIFE_RADAR_MATRIX_ENABLED=true`
- [ ] `POST /messages/send` with `source='matrix'` — returns 501 when `LIFE_RADAR_MATRIX_ENABLED=false`
- [ ] `oauth-device-flow.mjs --homeserver https://element.example.com` — targets correct homeserver
- [ ] Probe metadata shows `matrix-compatible` not `beeper-matrix-compatible` after Step A7

### Phase B Verification Checklist

- [ ] New session (fresh `matrix-session.json`) — SDK generates own keys, uploads to E2E Backup
- [ ] After restart — SDK downloads keys from E2E Backup, decrypts messages
- [ ] Health metric shows `e2e_backup_version` populated
- [ ] Rooms not in E2E Backup still decrypt from Beeper export (fallback works)

---

## 8. Migration Path for Existing Deployments

For existing LifeRadar deployments (upgrading from OpenClaw era):

1. **No env var changes needed for Phase A** — if `LIFE_RADAR_BEEPER_ENABLED=true` is set, it implies `LIFE_RADAR_MATRIX_ENABLED=true`. Existing Matrix session, SDK store, and Beeper key export all continue to work.

2. **After Phase A**, the following env vars are available for new configurations:
   - `LIFE_RADAR_MATRIX_HOMESERVER_URL=https://your-homeserver.example.com`
   - `MATRIX_ROOM_KEYS_PATH=/path/to/your-export.txt` (any Megolm export, not just Beeper)
   - `LIFE_RADAR_MATRIX_ENABLED=false` to disable Matrix entirely

3. **For Phase B**, existing Beeper key export continues to work as fallback. New deployments can skip the export entirely and rely on E2E Backup.

---

## 9. Phase B — End State Architecture

After Phase A + Phase B, the architecture becomes:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  life-radar-worker                                                          │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │  run-probes.sh                                                         │ │
│  │  if [ "${LIFE_RADAR_MATRIX_ENABLED}" != "false" ]; then               │ │
│  │      life-radar-matrix-rust-probe --mode=ingest_live_history         │ │
│  │  fi                                                                    │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │  life-radar-matrix-rust-probe (ingest_live_history)                  │ │
│  │                                                                          │ │
│  │  1. build_client(cfg)  → Matrix SDK Client                              │ │
│  │     ├── Homeserver: from LIFE_RADAR_MATRIX_HOMESERVER_URL (any server)  │ │
│  │     ├── Session: from MATRIX_SESSION_PATH (OAuth device code flow)     │ │
│  │     └── Store: from MATRIX_RUST_STORE (SQLite)                         │ │
│  │                                                                          │ │
│  │  2. maybe_load_e2e_backup(client)  → Restore keys from homeserver     │ │
│  │     └── SDK: client.encryption().load_backup()                         │ │
│  │                                                                          │ │
│  │  3. maybe_import_room_keys(client)  → Fallback: Beeper/Element export  │ │
│  │                                                                          │ │
│  │  4. sync_once()  → Fetch new events, decrypt, ingest                  │ │
│  │                                                                          │ │
│  │  5. client.encryption().backup()  → Upload own keys to homeserver     │ │
│  │                                                                          │ │
│  │  6. save_sync_checkpoint()  → Persist next_batch to DB                 │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│  life-radar-identity (Docker named volume → /data/life-radar/identity)      │
│                                                                             │
│  matrix-session.json       ← OAuth session (access_token, refresh_token)   │
│  matrix-rust-sdk-store/   ← SDK state: crypto, room state, event cache    │
│                            ← E2E Backup keys stored here by SDK            │
│                                                                             │
│  (OPTIONAL) matrix-e2e-keys.txt ← Beeper/Element export fallback           │
│  (OPTIONAL) .e2e-keys-passphrase                                            │
│                                                                             │
│  Key insight: Beeper Desktop is no longer required for any of this.        │
└─────────────────────────────────────────────────────────────────────────────┘
```

**No Beeper Desktop. No Beeper-specific OAuth. No Beeper key export required.**

---

## 10. Conclusion

The current Beeper coupling is a historical artifact from the OpenClaw migration era. The existing `matrix-rust-probe` is well-engineered — it already uses the SDK correctly and handles E2EE properly. The coupling is primarily at the configuration layer (env vars, feature flags) and the OAuth home server URL.

**Phase A** (estimated 2.5 hours) removes all hard gates and makes Matrix first-class. The work is renaming, adding env vars, and enabling token refresh. No code rewrites.

**Phase B** (estimated 3-4 hours) eliminates the Beeper key export dependency by using Matrix E2E Backup, making LifeRadar a fully self-contained Matrix client.

**The path of least resistance is Phase A first** — it de-risks the migration and proves the plumbing works before tackling the more complex E2E Backup integration.