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
    echo "[life-radar] Starting MCP bridge server"
    export PYTHONPATH="/opt/life-radar:${PYTHONPATH:-}"
    cd /opt/life-radar
    echo "[life-radar] Installing dependencies..."
    pip3 install --break-system-packages httpx >/dev/null 2>&1
    # MCP mode: re-use API code but listen on different port (8090)
    # This exposes LifeRadar tools via HTTP for MCP clients
    export LIFE_RADAR_DB_HOST="g0gsgwskkc8k8cckc0kg8sc4"
    export LIFE_RADAR_DB_PASSWORD="Qp5CbGOzrDECdSySKVSMRL5ye9pavw2GzDLQzBWegKWJqoCv87IMPzw27eJ2SSlu"
    echo "[life-radar] Starting MCP bridge on :8090..."
    exec python3 -m uvicorn api.main:app \
        --host 0.0.0.0 \
        --port 8090 \
        --log-level info
else
    echo "[life-radar] Starting worker probe loop"
    exec /opt/life-radar/bin/run-probes.sh
fi
