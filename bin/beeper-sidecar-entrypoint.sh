#!/usr/bin/env bash
set -euo pipefail

: "${DISPLAY:=:99}"
: "${BEEPER_VNC_ENABLED:=false}"
: "${BEEPER_NOVNC_ENABLED:=false}"
: "${BEEPER_DISABLE_GPU:=false}"

mkdir -p /tmp/.X11-unix /data/beeper-home
export HOME=/data/beeper-home
export APPIMAGE_EXTRACT_AND_RUN=1

Xvfb "$DISPLAY" -screen 0 1280x800x24 -ac +extension RANDR &
XVFB_PID=$!

for _ in $(seq 1 50); do
  if xdpyinfo -display "$DISPLAY" >/tmp/xdpyinfo.log 2>&1; then
    break
  fi
  sleep 0.2
done

touch /tmp/beeper.log /tmp/x11vnc.log /tmp/novnc.log /tmp/fluxbox.log /tmp/xterm.log /tmp/xclock.log

mkdir -p "$HOME/.fluxbox"
cat >"$HOME/.fluxbox/init" <<'EOF'
session.screen0.rootCommand:
session.screen0.toolbar.visible: true
session.styleFile: /usr/share/fluxbox/styles/bloe
EOF

if command -v fluxbox >/dev/null 2>&1; then
  fluxbox >/tmp/fluxbox.log 2>&1 &
  (sleep 2; pkill xmessage >/dev/null 2>&1 || true) &
fi

if command -v xsetroot >/dev/null 2>&1; then
  xsetroot -solid '#263238' || true
fi

if command -v xclock >/dev/null 2>&1; then
  xclock -digital -strftime 'LifeRadar Beeper sidecar - %H:%M:%S' -geometry 480x40+24+24 >/tmp/xclock.log 2>&1 &
fi

if command -v xterm >/dev/null 2>&1; then
  xterm -geometry 74x24+820+96 -title "LifeRadar Beeper sidecar logs" -e tail -F /tmp/beeper.log /tmp/x11vnc.log /tmp/novnc.log /tmp/fluxbox.log /tmp/xclock.log >/tmp/xterm.log 2>&1 &
fi

if [[ "${BEEPER_VNC_ENABLED,,}" == "true" ]]; then
  x11vnc -display "$DISPLAY" -forever -shared -nopw -listen 0.0.0.0 -rfbport "${BEEPER_VNC_PORT:-5900}" >/tmp/x11vnc.log 2>&1 &
fi

if [[ "${BEEPER_NOVNC_ENABLED,,}" == "true" ]]; then
  sleep 1
  websockify --web=/usr/share/novnc/ "${BEEPER_NOVNC_PORT:-6080}" "127.0.0.1:${BEEPER_VNC_PORT:-5900}" >/tmp/novnc.log 2>&1 &
fi

ARGS=()
ARGS+=("--no-sandbox" "--disable-dev-shm-usage" "--ozone-platform=x11")
if [[ "${BEEPER_DISABLE_GPU,,}" == "true" ]]; then
  ARGS+=("--disable-gpu")
fi

trap 'kill $XVFB_PID 2>/dev/null || true; exit 0' INT TERM EXIT

while true; do
  /opt/beeper/Beeper.AppImage --appimage-extract-and-run "${ARGS[@]}" >>/tmp/beeper.log 2>&1 &
  BEEPER_PID=$!
  wait "$BEEPER_PID" || true
  echo "[beeper-sidecar] Beeper exited; restarting in 10 seconds" | tee -a /tmp/beeper.log >&2
  sleep 10
done
