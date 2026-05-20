#!/usr/bin/env bash
set -euo pipefail

MODE="${LIFERADAR_SERVICE:-worker}"
echo "[liferadar] MODE=$MODE"

if [[ "$MODE" == "api" ]]; then
    echo "[liferadar] Starting Go API server"
    exec /usr/local/bin/liferadar-api
elif [[ "$MODE" == "mcp" ]]; then
    echo "[liferadar] Starting MCP SSE bridge server"
    export PYTHONPATH="/opt/liferadar:${PYTHONPATH:-}"
    cd /opt/liferadar
    echo "[liferadar] Starting MCP SSE server on :8090..."
    exec python3 mcp-server/server.py
else
    echo "[liferadar] Starting worker probe loop"
    exec /opt/liferadar/bin/run-probes.sh
fi
