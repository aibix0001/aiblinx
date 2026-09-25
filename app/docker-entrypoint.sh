#!/bin/sh
set -e

# Bind-mounted host dirs are created root-owned, but the service runs unprivileged.
# When started as root, make the data dir writable by the app user, then drop
# privileges with setpriv and exec the command. If already running unprivileged
# (e.g. a compose `user:` override), just exec as-is.
if [ "$(id -u)" = "0" ]; then
    mkdir -p "${DATA_DIR:-/data}"
    chown -R app:app "${DATA_DIR:-/data}"
    exec setpriv --reuid=app --regid=app --init-groups "$@"
fi

exec "$@"
