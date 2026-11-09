#!/bin/sh
set -eu
# Railway mounts persistent volumes as root at runtime, after Docker builds.
# Only the short startup phase runs as root; the web server and video pipeline
# use the unprivileged studio account.
mkdir -p /data
chown studio:studio /data
exec gosu studio "$@"
