#!/bin/sh
# Start as root just long enough to make /data writable, then run the server as
# PUID:PGID (Unraid's nobody:users, 99:100, by default) instead of root.
set -eu
PUID="${PUID:-99}"
PGID="${PGID:-100}"

mkdir -p /data/tmp
if [ "$(id -u)" = "0" ]; then
  # Only touch files that aren't already owned correctly, so restarts stay fast.
  find /data \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown "$PUID:$PGID" {} +
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups --inh-caps=-all \
    uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers --forwarded-allow-ips="$FORWARDED_ALLOW_IPS"
fi
exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers --forwarded-allow-ips="$FORWARDED_ALLOW_IPS"
