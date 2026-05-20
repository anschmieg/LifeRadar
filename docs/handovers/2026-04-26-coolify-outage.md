# Handoff Report: Coolify Deployment Outage Investigation

**Date:** 2026-04-26  
**Agents Involved:** Codex (this agent), another Claude agent (prior session)  
**Status:** Partially resolved — root cause identified, fix not yet applied

---

## What We Were Doing

Investigating why `*.nothing.pink` routes return `503 no available server` after the Oracle server moved to a new public IP (`79.76.97.124`).

---

## Current Symptom

```
curl -s https://liferadar.nothing.pink/health  →  "no available server"
curl -s https://vwswgg0s48kggs0cwkkc0k8o.nothing.pink/health  →  "no available server"
```

This is a **Coolify-specific error message** — traffic IS reaching Coolify/Traefik, but Traefik can't route to backend containers.

---

## What Works

| Check | Result |
|-------|--------|
| Server reachable at new IP | ✅ `curl -vk https://79.76.97.124/ping` connects (TLS handshake succeeds) |
| Coolify/Traefik healthy | ✅ Traefik process alive, port 80/443 listening |
| cloudflared tunnels connected | ✅ 3 tunnels running (hermes-lxc, root, hermes) |
| Backend liferadar-api reachable from host | ✅ `curl http://10.0.13.3:8000/health` → `200` |
| coolify-proxy on vwswgg network | ✅ IP `10.0.13.4` in `vwswgg0s48kggs0cwkkc0k8o_default` |
| coolify-proxy on vos0okss network | ✅ IP `10.0.3.2` in `vos0okssswwsos88ckg88wk8` |
| Traefik dynamic config exists | ✅ `/data/coolify/proxy/dynamic/coolify.yaml` present |
| Docker network connectivity | ✅ Oracle confirms `curl http://10.0.13.3:8000/health` returns `200` |

**The problem is NOT connectivity — it's routing configuration.**

---

## Root Cause (Suspected)

Traefik's dynamic config (`/data/coolify/proxy/dynamic/coolify.yaml`) is either:
1. Missing route entries for LifeRadar containers, OR
2. Pointing to outdated/renamed container hostnames

---

## Key Diagnostic Findings

### Docker Networks

- `coolify-proxy` endpoint in `vwswgg0s48kggs0cwkkc0k8o_default`: `10.0.13.4/24`
- `liferadar-api-vwswgg0s48kggs0cwkkc0k8o-194523872083`: `10.0.13.3/24`
- Both containers ARE on the same Docker network (no network isolation issue)

### Container Health Status

```
liferadar-api                          Up 11 hours (unhealthy)    8000/tcp
liferadar-mcp                          Up 11 hours (healthy)      8090/tcp
liferadar-chat-gateway                 Up 11 hours (healthy)      8020/tcp
liferadar-matrix-bridge                Up 11 hours (healthy)      8010/tcp
liferadar-mcp-vwswgg0s48kggs0cwkkc0k8o-194523881696   Up About an hour (healthy)   8090/tcp
liferadar-api-vwswgg0s48kggs0cwkkc0k8o-194523872083   Up About an hour (healthy)   8000/tcp
```

Note: The original `liferadar-api` container (port 8000) shows **(unhealthy)** while the new `liferadar-api-vwswgg...` shows **(healthy)**. This could be the actual problem — the old container is unhealthy.

### Traefik Logs

Only showing routes to `http://10.0.2.17:8081` — not to any LifeRadar containers. This suggests LifeRadar routes aren't registered in Traefik at all.

### Cloudflared Tunnels

3 cloudflared processes, all running. The `hermes-lxc.yml` routes `hermes.nothing.pink/webhook` to `http://10.99.244.100:8644`. The other two tunnels likely serve `*.nothing.pink` routes.

---

## Commands to Run for Diagnosis

```bash
# On oracle server:
sudo cat /data/coolify/proxy/dynamic/coolify.yaml

# Check if liferadar routes exist in config
grep -i liferadar /data/coolify/proxy/dynamic/coolify.yaml

# Compare container names in config vs actual running containers
docker ps --format '{{.Names}}' | grep liferadar
```

---

## What Didn't Work

- Tried killing old cloudflared processes — already dead (PIDs shown were stale)
- Tried restarting cloudflared with wrong command syntax — failed (token split across lines)
- DNS resolution for `*.nothing.pink` resolved to Cloudflare IPs, not local (expected — correct behavior)

---

## My Nitpicks / Observations

1. **Stale process PIDs**: The process list showed cloudflared PIDs that were already dead — user had to `sudo kill` them and got "no such process". This happens when processes restart. Be careful trusting old PID values.

2. **Command line splitting**: When pasting multi-line commands with line breaks, the shell interprets each line as a separate command. The cloudflared token got parsed as a separate command and failed. Always paste as a single line or use `&&` to chain lines.

3. **Network naming is chaotic**: Coolify creates Docker networks with random names like `vwswgg0s48kggs0cwkkc0k8o_default` and `vos0okssswwsos88ckg88wk8`. This makes manual debugging harder but is expected behavior.

4. **The (unhealthy) container might be the real issue**: The original `liferadar-api` container shows unhealthy. If Traefik is still trying to route to it (and it's dead), that would explain the 503. The new container (`liferadar-api-vwswgg...`) is healthy. Check which container the Traefik config points to.

5. **Cloudflared config for other tunnels**: The `/home/hermes/.cloudflared/config.yml` file doesn't exist. Two of the three running cloudflared processes use tokens directly (not config files). This is fine but less maintainable.

---

## LifeRadar Branch Context

- **Branch:** `codex/go-api-beeper-rewrite`
- **Latest commits:** `72750e1` (proxy), `715ac5e` (APPID/Xvfb), `e8456e1` (ARM64), `407e725` (network), `5ca40dc` (compose), `232ad13` (build defaults), `076240f` (docs), `8585266` (shell/APPID fix), `61830d2` (APPID export), `d903306` (Telegram gate)
- **Remote is ahead by 3 commits** — these changes haven't been pushed yet

**What was being tested before outage:**
- Beeper Desktop sidecar WebSocket with `requestID` fix deployed
- Sidecar proxy was working (`GET /v1/info` → 122 bytes, websocket upgrade → 101)
- `subscriptions.set` returned empty events — native desktop websocket also returned empty events
- **Suspected next blocker:** token type mismatch

---

## Open Issues

1. **Coolify 503 (Critical)**: Traefik can't route to LifeRadar containers
   - Likely: stale/missing routes in `coolify.yaml` pointing to wrong container names
   - Fix: redeploy Coolify app OR manually fix Traefik dynamic config
   - Verify: after fix, `curl https://liferadar.nothing.pink/health` should return `200`

2. **Unhealthy liferadar-api container**: The original container shows unhealthy status
   - Fix: restart it or let Coolify's health check recover it

3. **3 unpushed commits on `codex/go-api-beeper-rewrite`**
   - Decide whether to push before next session

---

## Next Steps (Priority Order)

1. **Read the Traefik config** on oracle:
   ```bash
   ssh oracle "sudo cat /data/coolify/proxy/dynamic/coolify.yaml"
   ```
   Look for any entries referencing LifeRadar containers and verify container name/IP match.

2. **Compare container names** — the dynamic config may reference `liferadar-api` (old name) while the actual running container is `liferadar-api-vwswgg...`.

3. **Redeploy the Coolify app** if the config is stale. Coolify regenerates the Traefik config on deploy.

4. **Test** once routes are fixed:
   ```bash
   curl -s https://liferadar.nothing.pink/health
   curl -s https://vwswgg0s48kggs0cwkkc0k8o.nothing.pink/health
   ```

5. **Check the unhealthy container** — if it's the real target, health check it manually and restart if needed.

---

## Infrastructure Summary

| Service | Host | IP/Network |
|---------|------|-----------|
| Oracle server | `oracle` (SSH alias) | `79.76.97.124` (public), `10.0.13.x` (internal) |
| Traefik/coolify-proxy | oracle | `0.0.0.0:80,443,8080` |
| liferadar-api | oracle (Docker) | `10.0.13.3:8000` (vwswgg network) |
| liferadar-mcp | oracle (Docker) | `10.0.13.x:8090` |
| cloudflared (hermes-lxc) | oracle (user=ubuntu) | tunnel to `hermes.nothing.pink/webhook` → `10.99.244.100:8644` |
| cloudflared (root) | oracle (user=root) | tunnel for `*.nothing.pink` routes |
| cloudflared (hermes) | oracle (user=hermes) | tunnel `8d7bf53d-...` |

---

*Report generated: 2026-04-26*
