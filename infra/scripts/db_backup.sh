#!/usr/bin/env bash
# Nightly logical backup of the stockscan database.
# Keeps:
#   - Last 14 daily dumps (rotating)
#   - Last 8 weekly dumps (taken on Sunday, separate retention)
# Layout under $PROJECT_DIR/backups/:
#   daily/stockscan-YYYY-MM-DD.dump
#   weekly/stockscan-YYYY-MM-DD.dump

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
CONTAINER="${CONTAINER:-stockscan-db}"
DB="${DB:-stockscan}"
USER="${PGUSER:-stockscan}"

DAILY_DIR="$PROJECT_DIR/backups/daily"
WEEKLY_DIR="$PROJECT_DIR/backups/weekly"
mkdir -p "$DAILY_DIR" "$WEEKLY_DIR"

DATE=$(date +%Y-%m-%d)
DAY_OF_WEEK=$(date +%u)  # 1=Mon..7=Sun
DAILY_FILE="$DAILY_DIR/stockscan-$DATE.dump"

echo "→ Dumping $DB to $DAILY_FILE"
docker exec "$CONTAINER" pg_dump -U "$USER" -d "$DB" --format=custom --no-owner --no-acl \
  > "$DAILY_FILE.tmp"
mv "$DAILY_FILE.tmp" "$DAILY_FILE"
SIZE=$(du -h "$DAILY_FILE" | cut -f1)
echo "✓ daily backup: $DAILY_FILE ($SIZE)"

# Sunday → also retain a weekly copy
if [ "$DAY_OF_WEEK" = "7" ]; then
  WEEKLY_FILE="$WEEKLY_DIR/stockscan-$DATE.dump"
  cp "$DAILY_FILE" "$WEEKLY_FILE"
  echo "✓ weekly backup: $WEEKLY_FILE"
fi

# Retention: keep 14 daily, 8 weekly
ls -1t "$DAILY_DIR"/*.dump 2>/dev/null | tail -n +15 | xargs -I {} rm -- {} || true
ls -1t "$WEEKLY_DIR"/*.dump 2>/dev/null | tail -n +9 | xargs -I {} rm -- {} || true

echo "→ Retention applied. Daily count: $(ls -1 "$DAILY_DIR" | wc -l), weekly: $(ls -1 "$WEEKLY_DIR" | wc -l)"
