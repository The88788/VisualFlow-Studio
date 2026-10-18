#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${MYFLOW_APP_DIR:-/opt/myflow}"
BACKUP_DIR="${MYFLOW_BACKUP_DIR:-/var/backups/myflow}"
CONTAINER="${MYFLOW_CONTAINER:-myflow}"
SNAPSHOT="${APP_DIR}/data/veoapi.snapshot.db"
STAMP="$(date +%F-%H%M%S)"
ARCHIVE="${BACKUP_DIR}/myflow-${STAMP}.tar.gz"

exec 9>/run/lock/myflow-backup.lock
flock -n 9 || exit 0

cleanup() {
    rm -f "${SNAPSHOT}"
}
trap cleanup EXIT

install -d -m 0700 "${BACKUP_DIR}"

docker exec "${CONTAINER}" python -c \
    "import sqlite3; src=sqlite3.connect('/app/data/veoapi.db'); dst=sqlite3.connect('/app/data/veoapi.snapshot.db'); src.backup(dst); dst.close(); src.close()"

docker exec "${CONTAINER}" python -c \
    "import sqlite3; db=sqlite3.connect('/app/data/veoapi.snapshot.db'); result=db.execute('PRAGMA integrity_check').fetchone()[0]; db.close(); assert result == 'ok', result"

tar -C "${APP_DIR}" -czf "${ARCHIVE}" \
    config \
    browser_profiles \
    gflow_profiles \
    data/veoapi.snapshot.db \
    docker-compose.prod.yml \
    .dockerignore

chmod 600 "${ARCHIVE}"
find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'myflow-*.tar.gz' -mtime +14 -delete
