#!/bin/sh
# Ensure volume mounts are writable by the archiver user.
# Docker creates bind-mount dirs as root when they don't exist on the host.
set -e
chown -R archiver:archiver /archive /config 2>/dev/null || true
exec gosu archiver "$@"
