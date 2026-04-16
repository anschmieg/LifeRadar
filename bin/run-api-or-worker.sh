#!/usr/bin/env bash
set -euo pipefail

MODE="${LIFERADAR_SERVICE:-worker}"
echo "[liferadar] MODE=$MODE"

if [[ "$MODE" == "api" ]]; then
    echo "[liferadar] Starting API server (FastAPI + uvicorn)"
    export PYTHONPATH="/opt/liferadar:${PYTHONPATH:-}"
    cd /opt/liferadar
    echo "[liferadar] Verifying Python packages..."
    python3 -c "import fastapi, uvicorn, asyncpg, pydantic; print('  imports OK')"
    echo "[liferadar] Starting uvicorn on :8000..."
    exec python3 -m uvicorn api.main:app \
        --host 0.0.0.0 \
        --port 8000 \
        --log-level info
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
