#!/bin/sh
set -eu

export DISPLAY="${DISPLAY:-:99}"
oauth_display="${ZCODE_OAUTH_DISPLAY:-:100}"

# A persistent Chromium profile can retain these process-singleton markers when
# the container is restarted while a manual captcha window is open. No browser
# exists yet at entrypoint time, so any markers here necessarily belong to the
# previous container and would otherwise prevent Chromium from starting.
chromium_profile_dir="${ZCODE_CHROMIUM_PROFILE_DIR:-/data/chromium-profile}"
rm -f -- \
  "$chromium_profile_dir/SingletonCookie" \
  "$chromium_profile_dir/SingletonLock" \
  "$chromium_profile_dir/SingletonSocket"

Xvfb "$DISPLAY" -screen 0 1280x800x24 -nolisten tcp -ac &
Xvfb "$oauth_display" -screen 0 1280x800x24 -nolisten tcp -ac &

if [ "${ZCODE_NOVNC_ENABLED:-1}" = "1" ]; then
  sleep 0.2
  x11vnc -display "$DISPLAY" -forever -shared -nopw -localhost -rfbport 5900 -quiet \
    >/tmp/zca-x11vnc.log 2>&1 &
  websockify --web=/usr/share/novnc/ 6080 localhost:5900 \
    >/tmp/zca-websockify.log 2>&1 &
  x11vnc -display "$oauth_display" -forever -shared -nopw -localhost -rfbport 5901 -quiet \
    >/tmp/zca-oauth-x11vnc.log 2>&1 &
  websockify --web=/usr/share/novnc/ 6081 localhost:5901 \
    >/tmp/zca-oauth-websockify.log 2>&1 &
fi

exec "$@"
