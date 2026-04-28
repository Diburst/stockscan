#!/usr/bin/env bash
# Produce a portable logical dump of the stockscan database for cross-machine
# migration (e.g., Mac → Mac mini via SCP).
#
# Output: infra/migration_export/stockscan-YYYY-MM-DD-HHMMSS.dump
#
# Format: pg_dump --format=custom (compressed, supports parallel restore,
# allows selective restore of objects). NOT compatible with `psql -f` —
# use `pg_restore` (or our migration_restore.sh helper) on the target.
#
# Requires: stockscan-db container running. Start it with `make db-up`.

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
CONTAINER="${CONTAINER:-stockscan-db}"
DB="${DB:-stockscan}"
USER="${PGUSER:-stockscan}"

EXPORT_DIR="$PROJECT_DIR/infra/migration_export"
mkdir -p "$EXPORT_DIR"

TS=$(date +%Y-%m-%d-%H%M%S)
OUT="$EXPORT_DIR/stockscan-$TS.dump"

# Sanity check: container must be running.
if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "✗ Container '$CONTAINER' is not running. Start it with: make db-up" >&2
  exit 1
fi

echo "→ Dumping $DB → $OUT"
echo "  (custom format, compressed, no-owner/no-acl for cross-machine portability)"

# --no-owner / --no-acl: strip role and grant info. The target machine will
# have its own 'stockscan' role created by the timescale image; we don't want
# the dump to insist on a specific OID or permission set.
docker exec "$CONTAINER" pg_dump \
  -U "$USER" -d "$DB" \
  --format=custom \
  --no-owner \
  --no-acl \
  --verbose \
  > "$OUT.tmp" 2> "$OUT.log"
mv "$OUT.tmp" "$OUT"

SIZE=$(du -h "$OUT" | cut -f1)
echo "✓ Dump complete: $OUT ($SIZE)"
echo "  Verbose log:    $OUT.log"
echo ""
echo "Next steps:"
echo "  1. Rsync the project to the target machine (excluding infra/pgdata/)."
echo "     The dump file in $EXPORT_DIR/ travels with the project."
echo "  2. On the target: bash infra/scripts/migration_restore.sh \\"
echo "       infra/migration_export/stockscan-$TS.dump"
