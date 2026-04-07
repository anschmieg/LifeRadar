#!/usr/bin/env bash
set -euo pipefail

MODE="${LIFE_RADAR_SERVICE:-worker}"
echo "[life-radar] MODE=$MODE"

if [[ "$MODE" == "api" ]]; then
    echo "[life-radar] Starting API server (FastAPI + uvicorn)"
    export PYTHONPATH="/opt/life-radar:${PYTHONPATH:-}"
    cd /opt/life-radar
    echo "[life-radar] Verifying Python packages..."
    python3 -c "import fastapi, uvicorn, asyncpg, pydantic; print('  imports OK')"
    echo "[life-radar] Starting uvicorn on :8000..."
    exec python3 -m uvicorn api.main:app \
        --host 0.0.0.0 \
        --port 8000 \
        --log-level info
elif [[ "$MODE" == "mcp" ]]; then
    echo "[life-radar] Starting MCP SSE bridge server"
    export PYTHONPATH="/opt/life-radar:${PYTHONPATH:-}"
    cd /opt/life-radar
    echo "[life-radar] Starting MCP SSE server on :8090..."
    exec python3 mcp-server/server.py
else
    echo "[life-radar] Starting worker probe loop"
    exec /opt/life-radar/bin/run-probes.sh
fi
