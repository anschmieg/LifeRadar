#!/usr/bin/env bash
set -euo pipefail

# Script to push new E2EE keys to oracle and trigger full re-import + ingest
# Usage: ./bin/heal-decryption-on-oracle.sh

WORKER="life-radar-worker-vos0okssswwsos88ckg88wk8-210144309091"
IDENTITY="/app/identity"

echo "=== Step 1: Copy new key export and passphrase to oracle ==="
scp "$PWD/matrix-rust-sdk-store/element-e2ee-keys-latest.txt" oracle:/tmp/element-e2ee-keys-latest.txt
scp "$PWD/.e2ee-export-passphrase" oracle:/tmp/e2ee-passphrase

echo "=== Step 2: Copy files into the worker container ==="
ssh oracle "docker cp /tmp/element-e2ee-keys-latest.txt ${WORKER}:${IDENTITY}/element-e2ee-keys-latest.txt"
ssh oracle "docker cp /tmp/e2ee-passphrase ${WORKER}:${IDENTITY}/.e2ee-export-passphrase"

echo "=== Step 3: Verify files ==="
ssh oracle "docker exec ${WORKER} wc -l ${IDENTITY}/element-e2ee-keys-latest.txt"
ssh oracle "docker exec ${WORKER} wc -c ${IDENTITY}/.e2ee-export-passphrase"

echo "=== Step 4: Remove old import marker to force re-import ==="
ssh oracle "docker exec ${WORKER} rm -f ${IDENTITY}/matrix-rust-sdk-store/room-key-import-marker.json"

echo "=== Step 5: Update E2EE export path to point to new keys ==="
# The container has MATRIX_E2EE_EXPORT_PATH=/app/identity/beeper-e2e-keys.txt
# We need to either overwrite that file or change the env var.
# Safest: copy the new file as the expected filename
ssh oracle "docker exec ${WORKER} cp ${IDENTITY}/element-e2ee-keys-latest.txt ${IDENTITY}/beeper-e2e-keys.txt"

echo "=== Step 6: Restart the worker to trigger key import + ingest ==="
ssh oracle "docker restart ${WORKER}"

echo "=== Step 7: Wait and check logs ==="
echo "Waiting 30s for worker to start and import keys..."
sleep 30
ssh oracle "docker logs --tail 50 ${WORKER} 2>&1 | grep -i 'key_import\|imported\|decrypt\|undecrypt\|room_key\|error'"

echo ""
echo "=== Done! ==="
echo "The worker should now import the new keys and re-process undecrypted messages."
echo "Check full logs with: ssh oracle docker logs --tail 200 ${WORKER}"