#!/usr/bin/env bash
set -euo pipefail

MODE="${LIFE_RADAR_SERVICE:-worker}"
echo "[life-radar] MODE=$MODE"

if [[ "$MODE" == "api" ]]; then
    echo "[life-radar] Starting API server (FastAPI + uvicorn)"
    export PYTHONPATH="/opt/life-radar:${PYTHONPATH:-}"
    cd /opt/life-radar
    echo "[life-radar] Verifying Python packages..."
    python -c "import fastapi, uvicorn, asyncpg, pydantic; print('  imports OK')"
    echo "[life-radar] Starting uvicorn on :8000..."
    exec python -m uvicorn api.main:app \
        --host 0.0.0.0 \
        --port 8000 \
        --log-level info
else
    echo "[life-radar] Starting worker probe loop"
    exec /opt/life-radar/bin/run-probes.sh
fi
