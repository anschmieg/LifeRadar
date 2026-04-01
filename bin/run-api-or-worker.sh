#!/usr/bin/env bash
set -euo pipefail

MODE="${LIFE_RADAR_SERVICE:-worker}"

if [[ "$MODE" == "api" ]]; then
    echo "[life-radar] Starting API server (FastAPI + uvicorn)"
    export PYTHONPATH="/opt/life-radar:${PYTHONPATH:-}"
    cd /opt/life-radar
    exec python -m uvicorn api.main:app \
        --host 0.0.0.0 \
        --port 8000 \
        --log-level info
else
    echo "[life-radar] Starting worker probe loop"
    exec /opt/life-radar/bin/run-probes.sh
fi
