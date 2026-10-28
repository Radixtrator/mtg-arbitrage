#!/bin/sh
# Started as root (the default): hand the volumes to PUID:PGID (Unraid's nobody:users) and drop privileges.
# Started as another user (docker run --user): just run.
set -e
if [ "$(id -u)" = "0" ]; then
  chown -R "${PUID:-99}:${PGID:-100}" /app/.cache /app/data
  exec setpriv --reuid="${PUID:-99}" --regid="${PGID:-100}" --clear-groups python3 app.py "$@"
fi
exec python3 app.py "$@"
