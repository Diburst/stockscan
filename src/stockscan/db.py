"""Database engine + session factory.

We use SQLAlchemy 2.x Core (not the ORM) — strong typing, expression-language
queries, and direct access to Postgres-specific features (TimescaleDB,
JSONB, full-text search) without ORM mapping ceremony.

Usage:

    from stockscan.db import session_scope

    with session_scope() as session:
        result = session.execute(text("SELECT 1")).scalar()
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from stockscan.config import settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Lazily-built engine, shared process-wide.

    Tuned for a single-host Mac mini deployment — modest pool sizes,
    pre-ping to detect dropped connections after laptop sleep, etc.
    """
    return create_engine(
        settings.database_url,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=3600,
        echo=False,
        future=True,
    )


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context-managed session with automatic commit/rollback."""
    session = _session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def healthcheck() -> dict[str, str | bool]:
    """Lightweight DB connectivity check used by the /health endpoint and CLI."""
    from sqlalchemy import text

    try:
        with session_scope() as s:
            version = s.execute(text("SELECT version();")).scalar_one()
            ts_ext = s.execute(
                text("SELECT extversion FROM pg_extension WHERE extname='timescaledb';")
            ).scalar()
        return {
            "ok": True,
            "postgres": str(version).split(",")[0],
            "timescaledb": str(ts_ext) if ts_ext else "(missing)",
        }
    except Exception as exc:  # noqa: BLE001 — surface any connection failure
        return {"ok": False, "error": str(exc)}
