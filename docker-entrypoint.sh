#!/bin/sh
set -e

if [ "$(id -u)" != "0" ]; then
  exec "$@"
fi

# Le volume ./data peut avoir été créé par Docker ou par une version
# précédente qui tournait en root.
chown -R "$PUID:$PGID" /app/data
exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
