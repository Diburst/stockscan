#!/usr/bin/env bash
# Restore a stockscan database from a `pg_dump --format=custom` dump file
# produced by migration_dump.sh.
#
# Usage:
#   bash infra/scripts/migration_restore.sh path/to/stockscan-YYYY-MM-DD.dump
#
# What it does:
#   1. Refuses to run if the target DB has user data already (safety net).
#   2. Drops + recreates the stockscan database.
#   3. Installs the timescaledb extension (must exist before restoring data).
#   4. Runs pg_restore against the empty database.
#   5. Reports row counts on the major tables for sanity.
#
# Requires: stockscan-db container running. Start it with `make db-up`.

set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 path/to/stockscan-YYYY-MM-DD.dump" >&2
  exit 64
fi

DUMP_FILE="$1"
if [ ! -f "$DUMP_FILE" ]; then
  echo "✗ Dump file not found: $DUMP_FILE" >&2
  exit 66
fi

CONTAINER="${CONTAINER:-stockscan-db}"
DB="${DB:-stockscan}"
USER="${PGUSER:-stockscan}"

# Sanity check: container must be running.
if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "✗ Container '$CONTAINER' is not running. Start it with: make db-up" >&2
  exit 1
fi

# Safety net: if the database has bars in it, refuse without --force.
EXISTING_BARS=$(docker exec "$CONTAINER" psql -U "$USER" -d "$DB" -tAc \
  "SELECT COUNT(*) FROM information_schema.tables WHERE table_name='bars';" 2>/dev/null || echo "0")

if [ "$EXISTING_BARS" != "0" ] && [ "${FORCE:-0}" != "1" ]; then
  ROW_COUNT=$(docker exec "$CONTAINER" psql -U "$USER" -d "$DB" -tAc \
    "SELECT COUNT(*) FROM bars;" 2>/dev/null || echo "0")
  if [ "$ROW_COUNT" -gt 0 ]; then
    echo "✗ Target database '$DB' already contains $ROW_COUNT bar rows." >&2
    echo "  Refusing to overwrite. Re-run with FORCE=1 to override:" >&2
    echo "    FORCE=1 $0 $DUMP_FILE" >&2
    exit 1
  fi
fi

echo "→ Dropping + recreating database '$DB'..."
docker exec "$CONTAINER" psql -U "$USER" -d postgres \
  -c "DROP DATABASE IF EXISTS $DB;"
docker exec "$CONTAINER" psql -U "$USER" -d postgres \
  -c "CREATE DATABASE $DB;"

echo "→ Installing timescaledb extension..."
docker exec "$CONTAINER" psql -U "$USER" -d "$DB" \
  -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"

echo "→ Restoring from $DUMP_FILE..."
SIZE=$(du -h "$DUMP_FILE" | cut -f1)
echo "  Source size: $SIZE"

# Stream the dump file into pg_restore inside the container. -j for parallel
# data-load workers; the server can handle it. Errors-as-warnings let the
# restore continue past benign messages about extensions that already exist.
docker exec -i "$CONTAINER" pg_restore \
  -U "$USER" -d "$DB" \
  --no-owner \
  --no-acl \
  --jobs=4 \
  --verbose \
  < "$DUMP_FILE" 2> "$DUMP_FILE.restore.log" || {
    echo "⚠ pg_restore reported errors. Review $DUMP_FILE.restore.log" >&2
    echo "  (Some 'already exists' warnings are expected and harmless.)" >&2
}

echo ""
echo "→ Verifying row counts on major tables..."
docker exec "$CONTAINER" psql -U "$USER" -d "$DB" <<'SQL'
\echo ----- migrations applied -----
SELECT version, applied_at FROM schema_migrations ORDER BY version;
\echo ----- table row counts -----
SELECT 'bars' AS table, COUNT(*) AS rows FROM bars
UNION ALL SELECT 'symbols', COUNT(*) FROM symbols
UNION ALL SELECT 'universe_history', COUNT(*) FROM universe_history
UNION ALL SELECT 'fundamentals_snapshot', COUNT(*) FROM fundamentals_snapshot
UNION ALL SELECT 'signals', COUNT(*) FROM signals
UNION ALL SELECT 'backtests', COUNT(*) FROM backtests
UNION ALL SELECT 'backtest_trades', COUNT(*) FROM backtest_trades
UNION ALL SELECT 'watchlist', COUNT(*) FROM watchlist
ORDER BY 1;
SQL

echo ""
echo "✓ Restore complete."
echo "  Verbose log: $DUMP_FILE.restore.log"
echo ""
echo "Next steps:"
echo "  - make db-status         (should show all migrations applied)"
echo "  - make test              (full unit-test suite)"
echo "  - uv run stockscan universe count"
