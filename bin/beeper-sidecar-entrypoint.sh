#!/usr/bin/env bash
set -euo pipefail

: "${DISPLAY:=:99}"
: "${BEEPER_VNC_ENABLED:=false}"
: "${BEEPER_NOVNC_ENABLED:=false}"
: "${BEEPER_DISABLE_GPU:=true}"

mkdir -p /tmp/.X11-unix /data/beeper-home
export HOME=/data/beeper-home
export APPIMAGE_EXTRACT_AND_RUN=1

Xvfb "$DISPLAY" -screen 0 1280x800x24 -ac +extension RANDR &
XVFB_PID=$!

if command -v fluxbox >/dev/null 2>&1; then
  fluxbox >/tmp/fluxbox.log 2>&1 &
fi

if [[ "${BEEPER_VNC_ENABLED,,}" == "true" ]]; then
  x11vnc -display "$DISPLAY" -forever -shared -nopw -listen 0.0.0.0 -rfbport "${BEEPER_VNC_PORT:-5900}" >/tmp/x11vnc.log 2>&1 &
fi

if [[ "${BEEPER_NOVNC_ENABLED,,}" == "true" ]]; then
  sleep 1
  websockify --web=/usr/share/novnc/ "${BEEPER_NOVNC_PORT:-6080}" "127.0.0.1:${BEEPER_VNC_PORT:-5900}" >/tmp/novnc.log 2>&1 &
fi

ARGS=()
ARGS+=("--no-sandbox" "--disable-dev-shm-usage")
if [[ "${BEEPER_DISABLE_GPU,,}" == "true" ]]; then
  ARGS+=("--disable-gpu")
fi

trap 'kill $XVFB_PID 2>/dev/null || true; exit 0' INT TERM EXIT

while true; do
  /opt/beeper/Beeper.AppImage --appimage-extract-and-run "${ARGS[@]}" &
  BEEPER_PID=$!
  wait "$BEEPER_PID" || true
  echo "[beeper-sidecar] Beeper exited; restarting in 10 seconds" >&2
  sleep 10
done
