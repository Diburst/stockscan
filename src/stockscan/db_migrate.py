"""Lightweight SQL migration runner.

Replaces Alembic. We use raw SQL migrations (kept that way deliberately
because the schema uses Postgres + TimescaleDB-specific features that don't
roundtrip through ORM autogenerate cleanly), so a 150-line runner gives us
everything Alembic would and one fewer dependency.

Conventions:
  - Migrations live in `<repo>/migrations/` as `NNNN_descriptive_name.sql`.
  - Versions sort lexicographically (NNNN is zero-padded — stable order forever).
  - Migrations run in **AUTOCOMMIT mode**, NOT a wrapping transaction.
    Why: TimescaleDB continuous aggregates (`CREATE MATERIALIZED VIEW ...
    WITH (timescaledb.continuous)`) cannot run inside a transaction block,
    nor can `CREATE INDEX CONCURRENTLY`, `VACUUM`, etc. This matches the
    behavior of Flyway, sqitch, and most other production migration tools.
  - Trade-off: a migration that fails partway through leaves the schema in
    a partial state. Recovery is `make db-reset` — re-running with the same
    file is also safe if every statement is idempotent.
  - Applied migrations are tracked in `_migrations` (version, name,
    applied_at, checksum). The tracking row is inserted only after the
    migration's SQL fully completes. If the SQL fails, no row is inserted
    and the migration will be retried on the next `db-migrate`.
  - `verify_checksums()` detects on-disk drift; manual review only.

Usage from CLI:
    stockscan db migrate         # apply pending
    stockscan db status          # show applied + pending
    stockscan db verify          # detect checksum drift
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text

from stockscan.db import get_engine, session_scope

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
VERSION_TABLE = "_migrations"

_VERSION_RE = re.compile(r"^(\d{4})_(.+)\.sql$")

# Match line comments (-- ... \n) and block comments (/* ... */).
_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


def _split_sql_statements(sql: str) -> list[str]:
    """Split a SQL script into individual statements.

    Strips line comments and block comments, then splits on top-level
    semicolons. Does NOT handle semicolons inside string literals or
    dollar-quoted strings — our migrations don't use those. If a future
    migration needs them, switch to a real SQL parser (e.g., sqlparse).
    """
    cleaned = _COMMENT_RE.sub("", sql)
    return [s.strip() for s in cleaned.split(";") if s.strip()]


@dataclass(frozen=True, slots=True)
class Migration:
    version: str
    name: str
    path: Path
    sql: str
    checksum: str


def _checksum(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Return all migration files in version order."""
    out: list[Migration] = []
    if not directory.exists():
        return out
    for path in sorted(directory.glob("*.sql")):
        m = _VERSION_RE.match(path.name)
        if not m:
            log.warning("skipping non-versioned migration file: %s", path.name)
            continue
        version, name = m.group(1), m.group(2)
        sql = path.read_text(encoding="utf-8")
        out.append(Migration(version, name, path, sql, _checksum(sql)))
    return out


def _ensure_version_table() -> None:
    with session_scope() as s:
        s.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS {VERSION_TABLE} (
                    version    TEXT        PRIMARY KEY,
                    name       TEXT        NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    checksum   TEXT        NOT NULL
                );
                """
            )
        )


def applied_versions() -> dict[str, dict[str, str]]:
    """{version: {'name', 'checksum', 'applied_at'}} for everything applied."""
    _ensure_version_table()
    sql = text(
        f"SELECT version, name, checksum, applied_at::text AS applied_at "
        f"FROM {VERSION_TABLE} ORDER BY version"
    )
    with session_scope() as s:
        return {
            r.version: {"name": r.name, "checksum": r.checksum, "applied_at": r.applied_at}
            for r in s.execute(sql)
        }


def pending_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    discovered = discover_migrations(directory)
    applied = applied_versions()
    return [m for m in discovered if m.version not in applied]


def apply_pending(directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply all pending migrations in version order. Returns versions applied.

    Each migration's SQL is split into individual statements, then each
    statement is executed under AUTOCOMMIT (statement-level commit). This
    is required because some DDL — notably TimescaleDB continuous
    aggregates — cannot run inside a transaction block, and psycopg
    implicitly opens one when given a multi-statement script.

    After every statement in the file succeeds, the tracking row is
    inserted to mark the migration applied. If any statement fails,
    earlier statements are NOT rolled back; the migration is not marked
    applied and will be retried on the next run. Use `make db-reset` if
    you need a clean slate after a partial failure.
    """
    pending = pending_migrations(directory)
    applied: list[str] = []
    engine = get_engine()
    for m in pending:
        log.info("applying migration %s_%s", m.version, m.name)
        statements = _split_sql_statements(m.sql)
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            for i, stmt in enumerate(statements, start=1):
                try:
                    conn.exec_driver_sql(stmt)
                except Exception as exc:
                    log.error(
                        "migration %s failed on statement %d/%d: %s",
                        m.version, i, len(statements), str(exc).split("\n")[0],
                    )
                    raise
            conn.execute(
                text(
                    f"INSERT INTO {VERSION_TABLE} (version, name, checksum) "
                    f"VALUES (:v, :n, :c)"
                ),
                {"v": m.version, "n": m.name, "c": m.checksum},
            )
        applied.append(m.version)
    return applied


def current_version() -> str | None:
    av = applied_versions()
    return max(av) if av else None


def verify_checksums() -> list[tuple[str, str, str]]:
    """Returns [(version, applied_checksum, current_checksum)] for any drift."""
    discovered = {m.version: m for m in discover_migrations()}
    applied = applied_versions()
    drift: list[tuple[str, str, str]] = []
    for ver, info in applied.items():
        if ver in discovered and discovered[ver].checksum != info["checksum"]:
            drift.append((ver, str(info["checksum"]), discovered[ver].checksum))
    return drift
