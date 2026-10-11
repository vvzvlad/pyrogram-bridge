#!/bin/sh
# Container entrypoint.
#
# Starts as root (the Dockerfile has no `USER app`) only to chown the mounted data volume:
# a named volume created by an older root-based image keeps root-owned files, which uid 1000
# cannot write (SQLite: "attempt to write a readonly database"). All writable state lives
# under /app/data, so one recursive chown covers it; it is a no-op once ownership is right.
set -e

# Started as non-root (compose `user:` pinned): chown and the privilege drop below both
# need root, so skip them and run the app as the user we were given.
if [ "$(id -u)" -ne 0 ]; then
    exec "$@"
fi

# Migrate the data volume to the runtime uid. Only /app/data — never the whole
# /app image tree — so this stays a cheap targeted fixup on every start.
chown -R app:app /app/data

# Set HOME for the dropped user explicitly: setpriv does not change $HOME, so the app
# would otherwise inherit root's HOME from this boot. app's home (from `useradd -m`) is
# writable by uid 1000; pinning it avoids a latent "permission denied on $HOME" trap if a
# base image ever exports HOME=/root. The app itself uses an explicit workdir (data/), so
# this is defence-in-depth for any library that writes under $HOME.
export HOME=/home/app

# Drop root and exec the app as uid/gid 1000 (user `app`), preserving the
# non-root runtime intent. setpriv ships with util-linux (pinned in the Dockerfile).
# --init-groups sets the supplementary groups from app's /etc/group membership.
exec setpriv --reuid=app --regid=app --init-groups "$@"
