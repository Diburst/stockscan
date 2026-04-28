#!/usr/bin/env bash
# One-shot DB bootstrap: creates the timescaledb extension on a fresh database.
# Idempotent — safe to re-run.
set -euo pipefail

CONTAINER="${CONTAINER:-stockscan-db}"
DB="${DB:-stockscan}"
USER="${PGUSER:-stockscan}"

echo "→ Ensuring timescaledb extension exists in $DB..."
docker exec "$CONTAINER" psql -U "$USER" -d "$DB" -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"

echo "✓ Database ready. Run 'make db-migrate' to apply schema."
