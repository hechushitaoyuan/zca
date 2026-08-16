#!/bin/sh
set -eu

export DISPLAY="${DISPLAY:-:99}"
Xvfb "$DISPLAY" -screen 0 1280x800x24 -nolisten tcp -ac &

if [ "${ZCODE_NOVNC_ENABLED:-1}" = "1" ]; then
  sleep 0.2
  x11vnc -display "$DISPLAY" -forever -shared -nopw -localhost -rfbport 5900 -quiet \
    >/tmp/zca-x11vnc.log 2>&1 &
  websockify --web=/usr/share/novnc/ 6080 localhost:5900 \
    >/tmp/zca-websockify.log 2>&1 &
fi

exec "$@"
