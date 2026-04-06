# LifeRadar Coolify Deployment Guide

## Overview

This guide fixes the two critical infrastructure issues:
1. Docker Runtime Broken (OCI sysctl permission denied)
2. Coolify Traefik Path-Based Routing Shadowed

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
| mcp.liferadar.nothing.pink | A | YOUR_SERVER_IP |

Wait for DNS propagation (check with: dig liferadar.nothing.pink)

## Step 3: Update docker-compose.yaml

The repository now contains the fixed docker-compose.yaml with subdomain routing:

- `liferadar.nothing.pink` → API service (port 8000)
- `mcp.liferadar.nothing.pink` → MCP service (port 8090)

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
LIFE_RADAR_DB_HOST=YOUR_COOLIFY_DB_HOST
LIFE_RADAR_DB_PORT=5432
LIFE_RADAR_DB_NAME=liferadar
LIFE_RADAR_DB_USER=liferadar
LIFE_RADAR_DB_PASSWORD=YOUR_DB_PASSWORD
LIFE_RADAR_API_KEY=generate_random_key
MATRIX_HOMESERVER=https://matrix.org
MATRIX_USER_ID=@youruser:matrix.org
MATRIX_ACCESS_TOKEN=your_token
MATRIX_DEVICE_ID=your_device
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

# Test MCP
curl https://mcp.liferadar.nothing.pink/
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
2. **Routing**: Changed from `PathPrefix(/mcp)` to `Host(mcp.liferadar.nothing.pink)`
3. **Network**: All services use external `coolify` network managed by Coolify
4. **SSL**: Let Coolify handle SSL via certresolver=letsencrypt

## Rollback Plan

If issues persist:
1. Delete Coolify resource
2. Restore original docker-compose.yaml: `git checkout docker-compose.yaml`
3. Revert Docker changes: `sudo rm /etc/docker/daemon.json &amp;&amp; sudo systemctl restart docker`
4. Try path-based routing with manual Traefik config (advanced)
