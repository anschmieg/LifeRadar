# LifeRadar Coolify Deployment Guide

## Overview

This guide fixes the two critical infrastructure issues:
1. Docker Runtime Broken (OCI sysctl permission denied)
2. Coolify path-based MCP exposure via the main LifeRadar host

## Prerequisites

- SSH access to Oracle Cloud server
- sudo privileges
- DNS control for nothing.pink domain
- Coolify dashboard access

## Step 1: Fix Docker Runtime

### 1.1 SSH to the server
```bash
ssh ubuntu@YOUR_ORACLE_CLOUD_IP
```

### 1.2 Run the Docker fix script
```bash
cd /tmp
curl -O https://raw.githubusercontent.com/anschmieg/LifeRadar/main/bin/coolify-fix/fix-docker-runtime.sh
sudo bash fix-docker-runtime.sh
```

### 1.3 Verify Docker is working
```bash
docker run --rm hello-world
docker ps
```

Expected output: "Hello from Docker!"

## Step 2: Configure DNS

Add these DNS A/AAAA records:

| Record | Type | Value |
|--------|------|-------|
| liferadar.nothing.pink | A | YOUR_SERVER_IP |

Wait for DNS propagation (check with: dig liferadar.nothing.pink)

## Step 3: Update docker-compose.yaml

The repository now contains the fixed docker-compose.yaml with path-based MCP exposure:

- `liferadar.nothing.pink` → API service (port 8000)
- `liferadar.nothing.pink/mcp` → MCP proxy path on the API service

## Step 4: Deploy via Coolify

### 4.1 Delete existing stack (if broken)
In Coolify dashboard:
1. Go to Projects → liferadar-stack
2. Delete the current deployment
3. Wait for cleanup

### 4.2 Create new deployment
1. Click "New Resource" → "Docker Compose"
2. Select your GitHub repository (anschmieg/LifeRadar)
3. Set FQDN: `liferadar.nothing.pink`
4. Branch: main
5. Compose file: docker-compose.yaml
6. Click Deploy

### 4.3 Configure environment variables
Add these in Coolify → Environment Variables:

``` 
LIFERADAR_DB_HOST=YOUR_COOLIFY_DB_HOST
LIFERADAR_DB_PORT=5432
LIFERADAR_DB_NAME=life_radar
LIFERADAR_DB_USER=life_radar
LIFERADAR_DB_PASSWORD=YOUR_DB_PASSWORD
LIFERADAR_API_KEY=generate_random_key
LIFERADAR_MATRIX_ENABLED=true
LIFERADAR_MATRIX_HOMESERVER_URL=https://your-homeserver.example.com
LIFERADAR_MATRIX_BRIDGE_URL=http://life-radar-matrix-bridge:8010
LIFERADAR_MATRIX_RUST_RECOVER_HTTP_ON_FAILURE=0
MATRIX_RUST_SESSION_PATH=/path/to/matrix-session.json
MATRIX_RUST_STORE=/path/to/matrix-rust-sdk-store
MATRIX_ROOM_KEYS_PATH=/path/to/matrix-e2e-keys.txt
MATRIX_ROOM_KEYS_PASSPHRASE_PATH=/path/to/.e2ee-export-passphrase
MATRIX_RUST_KEY_IMPORT_MARKER=/path/to/room-key-import-marker.json
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
```

### 4.4 Deploy
Click "Deploy" and wait for the build to complete.

## Step 5: Verify Deployment

### Check services are running
```bash
# On the server
docker ps

# Should show:
# - life-radar-api
# - life-radar-mcp
# - life-radar-worker
```

### Check Traefik routing
```bash
# Check Traefik logs
docker logs coolify-proxy | grep liferadar

# Verify routers are registered
curl -s http://localhost:8080/api/http/routers | grep liferadar
```

### Test endpoints
```bash
# Test API
curl https://liferadar.nothing.pink/health

# Test authenticated send path shape (replace key/uuid)
curl -X POST https://liferadar.nothing.pink/messages/send \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"conversation_id":"00000000-0000-0000-0000-000000000000","content_text":"test"}'

# Test MCP health through the main host
curl https://liferadar.nothing.pink/mcp/health
```

## Troubleshooting

### Issue: Containers not starting
Check Docker logs:
```bash
docker logs life-radar-api
docker logs life-radar-mcp
```

### Issue: 404/502 errors from Traefik
1. Verify DNS records point to server
2. Check Coolify proxy is running:
   docker ps | grep coolify-proxy
3. Verify containers are on coolify network:
   docker network inspect coolify

### Issue: SSL certificate errors
1. Verify DNS is propagated
2. Check Traefik logs for ACME errors:
   docker logs coolify-proxy 2>&1 | grep -i acme
3. Wait 1-2 minutes for certificate provisioning

### Issue: Database connection errors
1. Verify Coolify database is running:
   docker ps | grep coolify-database
2. Check database host/port in env vars
3. Verify network connectivity:
   docker exec life-radar-api ping -c 3 YOUR_DB_HOST

## Key Changes Made

1. **Docker Runtime**: Disabled iptables manipulation in Docker daemon config
2. **Routing**: MCP is exposed at `liferadar.nothing.pink/mcp`, not a separate subdomain
3. **Network**: All services use external `coolify` network managed by Coolify
4. **SSL**: Let Coolify handle SSL via certresolver=letsencrypt
5. **Matrix Send/Auth**: API calls an internal Matrix bridge service and write/MCP access can be protected with `LIFERADAR_API_KEY`

## Rollback Plan

If issues persist:
1. Delete Coolify resource
2. Restore original docker-compose.yaml: `git checkout docker-compose.yaml`
3. Revert Docker changes: `sudo rm /etc/docker/daemon.json &amp;&amp; sudo systemctl restart docker`
4. Verify the API proxy exposes `/mcp` on the main host (advanced)
