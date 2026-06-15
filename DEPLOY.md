# Deploying stockscan

Two supported paths:

1. **Docker Compose (primary)** — one command brings up the database, runs
   migrations, starts the web UI, and schedules the nightly jobs. Works on
   any Linux or macOS host with Docker. This is the portable path.
2. **macOS bare-metal (legacy)** — uv venv + the DB-only compose file +
   launchd plists. Documented in README.md and `infra/launchd/INSTALL.md`;
   still fully supported on the Mac mini.

Moving data between hosts (pg_dump / pg_restore) is covered by
[MIGRATION.md](./MIGRATION.md) — that part is identical for both paths.

---

## 1. Docker Compose deployment

### What you get

| Service | Role |
|---|---|
| `db` | TimescaleDB (Postgres 16). Compose-network only — no host port. |
| `migrate` | One-shot: applies pending SQL migrations, then exits. |
| `web` | uvicorn serving the UI on port 8000. Healthchecked via `/health`. |
| `scheduler` | supercronic running `infra/crontab` in ET: nightly scan (M–F 20:00), DB backup (02:00), weekly fundamentals refresh (Sun 03:00). |

Named volumes: `pgdata` (database), `logs` (rotating app logs), `models`
(ML pickles), `backups` (rotated pg_dump output).

### Fresh host, step by step

```bash
# 0. Prereqs: Docker Engine + the compose plugin (Linux) or Docker
#    Desktop / OrbStack (macOS). git or rsync to get the tree there.

# 1. Get the project onto the host.
git clone <your-repo> stockscan && cd stockscan

# 2. Configure. Compose reads .env for both app config AND the DB password.
cp .env.example .env
#    Edit .env and set, at minimum:
#      STOCKSCAN_DB_PASSWORD=<openssl rand -base64 32 | tr -d '+/='>
#      EODHD_API_KEY=<your key>
#      STOCKSCAN_ENV=prod
#    Optional but recommended: FRED_API_KEY, DISCORD_WEBHOOK_URL or the
#    email settings. The app logs a WARNING at startup for anything
#    missing that degrades a capability.

# 3. Build + start everything.
docker compose up -d --build

# 4. Verify.
docker compose ps                  # all services healthy; migrate Exited (0)
curl -s localhost:8000/health | python3 -m json.tool
docker compose logs migrate        # "applied N migrations"

# 5. Seed data (first run only — same commands as the README, but in-container).
docker compose exec web stockscan refresh universe
docker compose exec web stockscan refresh bars          # ~15-45 min full universe
docker compose exec web stockscan refresh fundamentals --current-only
docker compose exec web stockscan refresh macro
docker compose exec web stockscan health
```

The UI is now at `http://<host>:8000`. To keep it LAN/VPN-private, either
set `STOCKSCAN_WEB_BIND=<lan-ip>` in `.env`, or front it with your existing
reverse proxy / WireGuard setup (the app itself does no auth — same trust
model as the launchd deployment).

### Restoring an existing database

Follow MIGRATION.md §2 to produce a dump on the source machine, then:

```bash
docker compose up -d db
docker compose cp stockscan-YYYY-MM-DD.dump db:/tmp/restore.dump
docker compose exec db bash -c \
  'pg_restore -U stockscan -d stockscan --clean --if-exists /tmp/restore.dump'
docker compose up -d        # migrate brings the schema current if needed
```

### Day-2 operations

```bash
docker compose logs -f web                  # request log (timing per request)
docker compose logs -f scheduler            # cron output: nightly scan, backups
docker compose exec web stockscan health    # DB + strategies + key status
docker compose exec scheduler ls -lh /backups   # rotated pg_dump files
docker compose build && docker compose up -d    # deploy a code update
```

Scheduled jobs are defined in `infra/crontab` (times in ET — the scheduler
container runs with `TZ=America/New_York`). Edit + `docker compose restart
scheduler` to apply.

### Host access to Postgres (optional)

The DB intentionally exposes no host port. When you want psql or a GUI:

```bash
cp docker-compose.override.example.yml docker-compose.override.yml
docker compose up -d db    # now also on 127.0.0.1:5432 (loopback only)
```

### Knobs

| `.env` key | Default | Meaning |
|---|---|---|
| `STOCKSCAN_DB_PASSWORD` | (required) | Postgres password — compose's single source of truth |
| `STOCKSCAN_WEB_BIND` / `STOCKSCAN_WEB_PORT` | `0.0.0.0` / `8000` | Web UI bind address / host port |
| `STOCKSCAN_DB_TUNE_MEMORY` / `_CPUS` | `4GB` / `4` | timescaledb-tune sizing for the DB container |
| `STOCKSCAN_LOG_LEVEL` | `INFO` | App log level |
| `STOCKSCAN_SLOW_REQUEST_MS` | `750` | Threshold for `[slow]` request warnings |

ML extra (XGBoost meta-labeling) isn't installed by default; build with:

```bash
docker compose build --build-arg INSTALL_EXTRAS="--extra ml"
```

### Constraints worth knowing

- **Single web worker by design.** The background Fetch-Latest job keeps
  its state in-process (`stockscan/scan/refresh_job.py`). Don't add
  `--workers N` to the web service without first moving that state to the
  database.
- **`uv.lock` is committed** and the image builds with `--frozen` — a
  deploy installs exactly the dependency set you tested. Update deps with
  `uv lock` locally, test, commit.
- **Static assets are self-hosted** (`src/stockscan/web/static/`): the UI
  needs no internet. After changing templates or `tailwind.config.js`,
  run `make css` (the Docker build also recompiles it in its assets stage).

---

## 2. macOS bare-metal (legacy path)

Unchanged: README.md covers setup (uv venv + `infra/docker-compose.yml`
for the DB only + `make db-init`), `infra/launchd/INSTALL.md` covers the
plist templates for nightly-scan / web KeepAlive / db-backup. The compose
file at `infra/` and the secret-file password flow remain exactly as they
were — nothing about the Docker path changes the dev workflow.
