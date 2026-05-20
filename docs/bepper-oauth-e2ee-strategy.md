# LifeRadar OAuth + E2EE Strategy

> Historical note as of 2026-04-25: this document describes the earlier Matrix/OAuth/E2EE
> recovery strategy. LifeRadar's active messaging direction is now the Beeper Desktop sidecar +
> Go runtime path. Keep this document as background/reference material rather than the current
> messaging plan.

## Overview

Two-phase approach:
1. **One-time backfill** using Beeper Desktop's E2EE keys
2. **Ongoing OAuth** with Matrix E2E Backup for fresh keys

## Phase 1: One-Time Backfill

### Why Beeper Desktop Keys?
- Beeper Desktop has 3,274 Megolm sessions (vs 1,908 in Rust SDK)
- Covers 824 rooms (vs partial coverage elsewhere)
- Historical keys preserved

### Implementation
```
bin/backfill-from-beeper-desktop/
├── extract-keys.mjs          # Extract from BeeperTexts/account.db
├── convert-to-rust-format.mjs # Convert to Rust SDK importable format
└── run-backfill.sh          # Orchestrate the import
```

### Steps
1. Read `crypto_megolm_inbound_session` from `~/Library/Application Support/BeeperTexts/account.db`
2. Convert to Megolm session export format
3. Import into Rust SDK store using `/room_keys/backup` endpoint or direct store update
4. Run Matrix sync to fetch missing messages
5. Store final state for ongoing use

## Phase 2: OAuth + Matrix E2E Backup

### OAuth Flow (Beeper/Matrix)
```
1. User initiates login
2. Display device code + URL (https://matrix.beeper.com/_matrix/client/r0/auth/xxxx/ fallback)
3. User visits URL in browser, logs in
4. For new device: Emoji/SAS verification with existing device
5. Receive access_token + refresh_token
6. Store in runtime_metadata
```

### Beeper-Specific Considerations
- Beeper uses standard Matrix OAuth but may require:
  - Emoji verification for new devices
  - Beeper-specific homeserver URL (matrix.beeper.com)
  - Device ID persistence

### E2E Backup for Ongoing Keys
```
1. After OAuth, Rust SDK syncs
2. SDK fetches from /room_keys/backup
3. Keys auto-imported into crypto store
4. New messages decrypt automatically
```

## Implementation Files

### New Files
- `bin/oauth-beeper-login.mjs` - OAuth device code flow with emoji verification
- `bin/backfill-beeper-keys.mjs` - Extract and import Beeper keys
- `bin/ensure-e2e-backup.mjs` - Verify backup is working

### Modified Files
- `bin/bootstrap.sh` - Add backfill step
- `docker-compose.yaml` - Add OAuth service if needed
- `.env.example` - Add `BEEPER_HOMESERVER`, `OAUTH_DEVICE_CODE_URL`

## Environment Variables
```
BEEPER_HOMESERVER=https://matrix.beeper.com
BEEPER_CLIENT_ID=<optional - for Beeper-specific OAuth>
OAUTH_DEVICE_CODE_URL=https://matrix.beeper.com/_matrix/client/r0/auth/m.login.device

# For backup
E2E_BACKUP_KEY=<optional - if not using device keys>
```

## Security Considerations
- Store OAuth tokens encrypted (AES-256-GCM via pgcrypto)
- Refresh tokens need secure storage
- E2E backup key is the recovery mechanism

## References
- Matrix Device Code Flow: https://matrix.org/docs/client-server-api/#device-based-oauth
- Emoji Verification: https://matrix.org/docs/client-server-api/#key-verification-out-of-band
- E2E Backup: https://matrix.org/docs/client-server-api/#e2e-encrypted-room-backup
