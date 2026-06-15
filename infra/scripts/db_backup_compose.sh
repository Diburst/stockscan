#!/usr/bin/env bash
# Nightly logical backup for the compose deployment. Runs inside the
# scheduler container (which has pg_dump via postgresql-client) against
# the db service, writing rotated dumps to the `backups` volume.
#
# Env:
#   PG_DUMP_URL  — plain libpq URL (set by docker-compose.yml)
#   BACKUP_DIR   — default /backups
#   KEEP_DAILY   — how many daily dumps to retain (default 14)
#
# Restore (into a fresh db service):
#   docker compose exec -T db pg_restore -U stockscan -d stockscan --clean < dump
# or follow MIGRATION.md §2 / infra/scripts/migration_restore.sh.

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups}"
KEEP_DAILY="${KEEP_DAILY:-14}"
STAMP="$(date +%Y%m%d)"
OUT="${BACKUP_DIR}/stockscan-${STAMP}.dump"

mkdir -p "${BACKUP_DIR}"

echo "db_backup: dumping to ${OUT}"
pg_dump --format=custom --dbname="${PG_DUMP_URL}" --file="${OUT}"
echo "db_backup: wrote $(du -h "${OUT}" | cut -f1)"

# Rotate: keep the newest KEEP_DAILY dumps, delete the rest.
ls -1t "${BACKUP_DIR}"/stockscan-*.dump 2>/dev/null \
  | tail -n "+$((KEEP_DAILY + 1))" \
  | xargs -r rm -f

echo "db_backup: retention pass done ($(ls -1 "${BACKUP_DIR}"/stockscan-*.dump | wc -l) dumps kept)"
