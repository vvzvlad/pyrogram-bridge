#!/bin/sh
# Container entrypoint.
#
# The container starts as ROOT on purpose (see Dockerfile: no `USER app`).
# Root is needed only for one thing at boot: fixing ownership of the mounted
# data volume. The image layer is chown'd to `app` at build time, but that only
# affects the image — a pre-existing named volume mounted at /app/data keeps
# whatever ownership it had on disk. Installs that started on the old root image
# (before #64) have root-owned files there (media_file_ids.db, pyro_bridge.session,
# cache/, tgcache/, media_digest.key). Once we drop to uid 1000 those become
# unwritable and SQLite fails with "attempt to write a readonly database" in
# init_db_sync during startup, crash-looping the container (issue #82).
#
# All writable runtime state lives under /app/data (DB, session, cache/, tgcache/,
# key), so a single recursive chown of that one directory migrates every existing
# install. It is idempotent and fast (a no-op walk) once ownership is already app.
set -e

# Migrate the data volume to the runtime uid. Only /app/data — never the whole
# /app image tree — so this stays a cheap targeted fixup on every start.
chown -R app:app /app/data

# Set HOME for the dropped user explicitly: setpriv does not change $HOME, so the app
# would otherwise inherit root's HOME from this boot. app's home (from `useradd -m`) is
# writable by uid 1000; pinning it avoids a latent "permission denied on $HOME" trap if a
# base image ever exports HOME=/root. The app itself uses an explicit workdir (data/), so
# this is defence-in-depth for any library that writes under $HOME.
export HOME=/home/app

# Drop root and exec the app as uid/gid 1000 (user `app`), preserving #64's
# non-root runtime intent. setpriv ships with util-linux (pinned in the Dockerfile).
# --init-groups sets the supplementary groups from app's /etc/group membership.
exec setpriv --reuid=app --regid=app --init-groups "$@"
