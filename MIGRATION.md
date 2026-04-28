# Migration Guide

How to move this project to a new location. Two scenarios are covered:

1. **Local relocation** — moving the working tree on the same Mac into your
   new git repo at `/Users/Thomas/Projects/Claude/stockscan/`. Same Postgres
   instance, same machine — fastest path is `rsync` of the working tree
   (including the live `pgdata/` volume).
2. **Mac mini transfer** — eventual move to a different machine. Postgres
   data is *not* directly portable across machines/filesystems, so we use a
   logical dump (`pg_dump --format=custom`) and `pg_restore` on the target.

---

## 1. Local relocation (same Mac, new path)

The new path is a fresh git repo (`git init` already done). The old path
is `/Users/Thomas/Documents/Claude/Projects/stock-scan/`.

### What gets copied vs left behind

| Item | Copy? | Why |
|---|---|---|
| `src/`, `tests/`, `migrations/`, `infra/` (configs), `Makefile`, `pyproject.toml`, etc. | ✅ | The project. |
| `infra/pgdata/` (Postgres data dir) | ✅ | Same Mac, same Postgres version → bit-for-bit portable. Saves a ~1 GB dump/restore round-trip. |
| `infra/db_password.secret` | ✅ | Gitignored, but the Postgres in `pgdata/` was initialised against this exact value. Skip it and the container won't authenticate. |
| `.env` | ✅ | Gitignored. Holds your EODHD key and DB URL. |
| `.venv/`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/` | ❌ | Python caches and virtualenv hardcode the source path. Recreate them at the new location with `uv sync`. |
| `.git/` from old project | ❌ | The new location is its own git repo. |
| `logs/`, `backups/` | ❌ (your call) | Optional. Backups are large; you probably don't need them in the new tree. |

### Step-by-step

```bash
# 1. Stop the running database (releases the pgdata directory cleanly).
cd /Users/Thomas/Documents/Claude/Projects/stock-scan
make db-down

# 2. rsync the project to the new repo. Excludes recreate-able caches and
#    the old .git/. -a preserves perms; --info=progress2 shows totals.
rsync -a --info=progress2 \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='.pytest_cache/' \
  --exclude='.mypy_cache/' \
  --exclude='.ruff_cache/' \
  --exclude='.git/' \
  --exclude='logs/' \
  --exclude='backups/' \
  /Users/Thomas/Documents/Claude/Projects/stock-scan/ \
  /Users/Thomas/Projects/Claude/stockscan/

# 3. Move into the new location.
cd /Users/Thomas/Projects/Claude/stockscan

# 4. Recreate the virtualenv (fast — uv reuses its package cache).
make install

# 5. Bring the database back up. docker-compose mounts ./infra/pgdata,
#    which is now the copied directory at the new path.
make db-up

# 6. Verify everything is intact:
make db-status        # all migrations marked applied
uv run stockscan universe count   # symbol count matches what you had
uv run pytest -q      # full test suite passes

# 7. Initial commit in the new repo.
git add .
git commit -m "Initial import of stockscan project"
```

### Cleaning up the old location

Once you've confirmed the new location works end-to-end (db queries,
backtests, web UI), you can safely delete the old tree:

```bash
rm -rf /Users/Thomas/Documents/Claude/Projects/stock-scan
```

Don't do this until the new location has been working for at least a
day — `pgdata/` only exists in one place at a time, and the rsync above
copied (didn't move) it, but cleanup is much safer with a known-good
target.

---

## 2. Mac mini transfer (different machine)

The Postgres data directory (`infra/pgdata/`) is **not** safe to copy
across machines. It depends on filesystem, page size, locale, and exact
PG binary build. Use a logical dump instead.

The two helper scripts in `infra/scripts/` handle the dump and restore:

- `migration_dump.sh` — runs `pg_dump --format=custom` against the running
  container and writes a single portable `.dump` file.
- `migration_restore.sh` — drops the target database, recreates it,
  installs the timescaledb extension, and runs `pg_restore`.

### On the source Mac

```bash
cd /Users/Thomas/Projects/Claude/stockscan

# 1. Make sure the database is running.
make db-up

# 2. Take a fresh dump. Output goes to migration_export/stockscan-YYYY-MM-DD.dump
bash infra/scripts/migration_dump.sh

# 3. Rsync the project to the Mac mini, excluding caches, venv, and pgdata.
#    pgdata is per-machine and will be regenerated empty on the mini.
rsync -a --info=progress2 \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='.pytest_cache/' \
  --exclude='.mypy_cache/' \
  --exclude='.ruff_cache/' \
  --exclude='infra/pgdata/' \
  --exclude='logs/' \
  --exclude='backups/' \
  -e ssh \
  /Users/Thomas/Projects/Claude/stockscan/ \
  thomas@macmini.local:/Users/thomas/Projects/Claude/stockscan/

# 4. SCP the dump file separately (rsync above excluded backups/, and the
#    dump is in migration_export/ which we did include — but if you want it
#    on a separate path, here's the explicit copy):
scp infra/migration_export/stockscan-*.dump \
    thomas@macmini.local:/Users/thomas/Projects/Claude/stockscan/infra/migration_export/
```

`db_password.secret` and `.env` ride along with the rsync. If you'd
rather generate fresh secrets on the mini, omit them from the rsync
(`--exclude='infra/db_password.secret' --exclude='.env'`) and re-create
them on the target before `make db-up`.

### On the Mac mini (target)

Prerequisites: Docker Desktop or OrbStack installed, `uv` available
(`make install` will fetch it), and the project tree at the new path.

```bash
cd /Users/thomas/Projects/Claude/stockscan

# 1. Install Python deps + create venv on this machine.
make install

# 2. Start a fresh empty Postgres. infra/pgdata/ doesn't exist yet — the
#    timescale image initialises a brand-new cluster on first boot.
make db-up

# 3. Restore from the dump. Pass the dump filename as the only argument.
bash infra/scripts/migration_restore.sh \
  infra/migration_export/stockscan-YYYY-MM-DD.dump

# 4. Sanity-check.
make db-status
uv run stockscan universe count
uv run pytest -q
```

### What about launchd?

The plists in `infra/launchd/` are templates with `{{PROJECT_DIR}}` and
`{{UV_BIN}}` placeholders. They have to be re-rendered and reloaded on
the new machine. The README has the exact commands; the short version
is: substitute `PROJECT_DIR=/Users/thomas/Projects/Claude/stockscan` and
`UV_BIN=$(which uv)` into each template, copy to `~/Library/LaunchAgents/`,
and `launchctl load` each one.

### What if the data is too large to dump+restore comfortably?

For the foreseeable future, the bars table for the S&P 500 going back
to 2007 fits in well under a gigabyte of compressed dump output —
`pg_dump --format=custom` applies zlib by default. If that ever stops
being true, the alternative is the Timescale-aware `pg_dump`/`pg_restore`
recipe documented in their migration guide (essentially: dump, restore
schema, replay data with parallelism). Cross that bridge if it appears.

---

## Quick reference: file inventory

Files Claude is creating for this migration:

- `MIGRATION.md` — this guide.
- `infra/scripts/migration_dump.sh` — produces a portable `.dump` file.
- `infra/scripts/migration_restore.sh` — replays a `.dump` into an empty DB.

Files that *don't* travel with the project (stay machine-local):

- `infra/pgdata/` — Postgres data dir. Recreated from a dump on the Mac mini.
- `.venv/`, all `__pycache__/`, all `.*_cache/` — Python build artifacts.
- `.git/` — the old repo's git history. The new repo at the destination
  starts fresh.

Files that travel via rsync but are not in git (re-create or re-copy if you
prefer fresh):

- `infra/db_password.secret`
- `.env`
