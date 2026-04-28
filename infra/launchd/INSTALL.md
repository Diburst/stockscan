# launchd installation

These plist templates schedule the nightly scan, keep the FastAPI server alive,
and run the daily database backup. They use placeholder paths — replace before
installing.

## One-time install

1. **Replace the placeholders** in each plist (`{{PROJECT_DIR}}` and `{{UV_BIN}}`):
   ```bash
   cd /path/to/stock-scan
   PROJECT_DIR="$(pwd)"
   UV_BIN="$(which uv)"

   for plist in infra/launchd/*.plist; do
     sed -e "s|{{PROJECT_DIR}}|$PROJECT_DIR|g" \
         -e "s|{{UV_BIN}}|$UV_BIN|g" \
         "$plist" > ~/Library/LaunchAgents/$(basename "$plist")
   done

   chmod +x infra/scripts/db_backup.sh
   mkdir -p logs backups/daily backups/weekly
   ```

2. **Load the agents** with launchctl:
   ```bash
   launchctl load ~/Library/LaunchAgents/com.stockscan.web.plist
   launchctl load ~/Library/LaunchAgents/com.stockscan.nightly-scan.plist
   launchctl load ~/Library/LaunchAgents/com.stockscan.db-backup.plist
   ```

3. **Verify they're loaded:**
   ```bash
   launchctl list | grep stockscan
   ```

## Triggering manually (for testing)

```bash
launchctl start com.stockscan.nightly-scan
tail -f logs/nightly-scan.out.log logs/nightly-scan.err.log
```

## Schedule

| Job | When | What it does |
|---|---|---|
| `com.stockscan.web` | At login, kept alive | Serves the FastAPI dashboard on port 8000 |
| `com.stockscan.nightly-scan` | M–F 20:00 ET | Bulk-refresh bars, run all strategies, send notification |
| `com.stockscan.db-backup` | Daily 02:00 ET | `pg_dump` to `backups/daily/`, weekly retention on Sundays |

## Disabling temporarily

```bash
launchctl unload ~/Library/LaunchAgents/com.stockscan.nightly-scan.plist
# … work / debug …
launchctl load   ~/Library/LaunchAgents/com.stockscan.nightly-scan.plist
```

## Phase 4 (E*TRADE) will add

- `com.stockscan.place-orders.plist` — M–F 09:25 ET, transmits queued orders
- `com.stockscan.reconcile.plist` — M–F 16:05 ET, diffs broker positions vs local

These plist templates will be added when Phase 4 lands.

## Troubleshooting

- **Job loads but doesn't run.** Check `launchctl list | grep stockscan` for the
  exit code. Non-zero = check the `*.err.log` file. Common issues: incorrect
  `UV_BIN` path, `PROJECT_DIR` not absolute, `logs/` directory doesn't exist.
- **Plist parse error on `launchctl load`.** Validate with
  `plutil -lint ~/Library/LaunchAgents/com.stockscan.*.plist`.
- **launchd ignores `EnvironmentVariables`.** Add explicit `PATH` so the job
  finds `docker`, `uv`, etc. The templates already do this.
