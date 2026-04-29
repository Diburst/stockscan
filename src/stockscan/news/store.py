"""Persistence layer for all four news tables.

Public surface:
  upsert_articles()     — idempotent bulk-insert / update articles + symbols
  recent_general()      — latest N articles matching the general-market config
  recent_for_symbol()   — latest N articles mentioning a symbol
  fts_search()          — full-text search across title + snippet
  get_feed_config()     — read news_feed_config row (auto-seeds if absent)
  save_feed_config()    — write news_feed_config row
  mark_alert_sent()     — record a (article, channel) pair as notified
  alerts_already_sent() — filter articles already notified on a channel
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import session_scope

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class NewsArticle:
    article_id: str
    published_at: datetime
    title: str
    snippet: str | None
    link: str
    source: str | None
    sentiment_polarity: Decimal | None
    sentiment_pos: Decimal | None
    sentiment_neg: Decimal | None
    sentiment_neu: Decimal | None
    tags: list[str]
    symbols: list[str]  # populated by queries that join news_article_symbols


@dataclass(frozen=True, slots=True)
class NewsFeedConfig:
    symbols: list[str]
    tags: list[str]
    sentiment_alert_enabled: bool
    sentiment_threshold: Decimal


# ---------------------------------------------------------------------------
# article_id generation
# ---------------------------------------------------------------------------

def make_article_id(link: str) -> str:
    """SHA-256 of the article URL — stable, URL-safe, collision-proof for our scale."""
    return hashlib.sha256(link.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Article CRUD
# ---------------------------------------------------------------------------

_UPSERT_ARTICLE_SQL = text(
    """
    INSERT INTO news_articles
        (article_id, published_at, title, snippet, link, source,
         sentiment_polarity, sentiment_pos, sentiment_neg, sentiment_neu, tags)
    VALUES
        (:aid, :pub, :title, :snippet, :link, :source,
         :pol, :pos, :neg, :neu, :tags)
    ON CONFLICT (article_id) DO UPDATE SET
        published_at       = EXCLUDED.published_at,
        title              = EXCLUDED.title,
        snippet            = EXCLUDED.snippet,
        source             = EXCLUDED.source,
        sentiment_polarity = EXCLUDED.sentiment_polarity,
        sentiment_pos      = EXCLUDED.sentiment_pos,
        sentiment_neg      = EXCLUDED.sentiment_neg,
        sentiment_neu      = EXCLUDED.sentiment_neu,
        tags               = EXCLUDED.tags,
        fetched_at         = NOW();
    """
)

_UPSERT_SYMBOL_SQL = text(
    """
    INSERT INTO news_article_symbols (article_id, symbol)
    VALUES (:aid, :sym)
    ON CONFLICT (article_id, symbol) DO NOTHING;
    """
)


def upsert_articles(
    articles: list[NewsArticle],
    *,
    session: Session | None = None,
) -> int:
    """Bulk-upsert articles and their symbol associations. Returns inserted count."""
    if not articles:
        return 0

    def _run(s: Session) -> int:
        count = 0
        for a in articles:
            s.execute(
                _UPSERT_ARTICLE_SQL,
                {
                    "aid": a.article_id,
                    "pub": a.published_at,
                    "title": a.title,
                    "snippet": a.snippet,
                    "link": a.link,
                    "source": a.source,
                    "pol": a.sentiment_polarity,
                    "pos": a.sentiment_pos,
                    "neg": a.sentiment_neg,
                    "neu": a.sentiment_neu,
                    "tags": a.tags,
                },
            )
            for sym in a.symbols:
                s.execute(_UPSERT_SYMBOL_SQL, {"aid": a.article_id, "sym": sym})
            count += 1
        return count

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def _rows_to_articles(rows: list) -> list[NewsArticle]:
    def _dec(v: object) -> Decimal | None:
        return Decimal(str(v)) if v is not None else None

    return [
        NewsArticle(
            article_id=r.article_id,
            published_at=r.published_at,
            title=r.title,
            snippet=r.snippet,
            link=r.link,
            source=r.source,
            sentiment_polarity=_dec(r.sentiment_polarity),
            sentiment_pos=_dec(r.sentiment_pos),
            sentiment_neg=_dec(r.sentiment_neg),
            sentiment_neu=_dec(r.sentiment_neu),
            tags=list(r.tags or []),
            symbols=list(r.symbols or []),
        )
        for r in rows
    ]


def recent_for_symbol(
    symbol: str,
    limit: int = 5,
    *,
    session: Session | None = None,
) -> list[NewsArticle]:
    """Return the N most-recent articles mentioning `symbol`."""
    sql = text(
        """
        SELECT a.article_id, a.published_at, a.title, a.snippet, a.link,
               a.source, a.sentiment_polarity, a.sentiment_pos,
               a.sentiment_neg, a.sentiment_neu, a.tags,
               array_agg(DISTINCT s2.symbol) FILTER (WHERE s2.symbol IS NOT NULL)
                   AS symbols
        FROM news_article_symbols s
        JOIN news_articles a ON a.article_id = s.article_id
        LEFT JOIN news_article_symbols s2 ON s2.article_id = a.article_id
        WHERE s.symbol = :sym
        GROUP BY a.article_id
        ORDER BY a.published_at DESC
        LIMIT :lim
        """
    )

    def _run(sess: Session) -> list[NewsArticle]:
        rows = sess.execute(sql, {"sym": symbol, "lim": limit}).all()
        return _rows_to_articles(rows)

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def recent_general(
    limit: int = 10,
    *,
    config: NewsFeedConfig | None = None,
    session: Session | None = None,
) -> list[NewsArticle]:
    """Return the N most-recent articles matching the general-market feed config.

    Matches articles whose symbol set overlaps config.symbols OR whose tags
    overlap config.tags.
    """
    def _run(sess: Session) -> list[NewsArticle]:
        cfg = config or get_feed_config(session=sess)
        if not cfg.symbols and not cfg.tags:
            return []
        sql = text(
            """
            SELECT DISTINCT ON (a.published_at, a.article_id)
                   a.article_id, a.published_at, a.title, a.snippet, a.link,
                   a.source, a.sentiment_polarity, a.sentiment_pos,
                   a.sentiment_neg, a.sentiment_neu, a.tags,
                   array_agg(DISTINCT s.symbol) FILTER (WHERE s.symbol IS NOT NULL)
                       OVER (PARTITION BY a.article_id) AS symbols
            FROM news_articles a
            LEFT JOIN news_article_symbols s ON s.article_id = a.article_id
            WHERE (
                EXISTS (
                    SELECT 1 FROM news_article_symbols ns
                    WHERE ns.article_id = a.article_id
                      AND ns.symbol = ANY(:syms)
                )
                OR a.tags && :tags
            )
            ORDER BY a.published_at DESC, a.article_id
            LIMIT :lim
            """
        )
        rows = sess.execute(sql, {
            "syms": cfg.symbols,
            "tags": cfg.tags,
            "lim": limit,
        }).all()
        return _rows_to_articles(rows)

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def fts_search(
    query: str,
    limit: int = 25,
    symbol: str | None = None,
    tag: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    *,
    session: Session | None = None,
) -> list[NewsArticle]:
    """Full-text search across title + snippet.

    Returns articles ranked by ts_rank, filtered by optional symbol, tag,
    and date range.
    """
    clauses = ["a.search_vec @@ plainto_tsquery('english', :q)"]
    params: dict[str, object] = {"q": query, "lim": limit}

    if symbol:
        clauses.append(
            "EXISTS (SELECT 1 FROM news_article_symbols ns "
            "WHERE ns.article_id = a.article_id AND ns.symbol = :sym)"
        )
        params["sym"] = symbol
    if tag:
        clauses.append(":tag = ANY(a.tags)")
        params["tag"] = tag
    if from_date:
        clauses.append("a.published_at >= :from_dt")
        params["from_dt"] = from_date
    if to_date:
        clauses.append("a.published_at <= :to_dt")
        params["to_dt"] = to_date

    where = " AND ".join(clauses)
    sql = text(
        f"""
        SELECT a.article_id, a.published_at, a.title, a.snippet, a.link,
               a.source, a.sentiment_polarity, a.sentiment_pos,
               a.sentiment_neg, a.sentiment_neu, a.tags,
               array_agg(DISTINCT s.symbol) FILTER (WHERE s.symbol IS NOT NULL)
                   AS symbols,
               ts_rank(a.search_vec, plainto_tsquery('english', :q)) AS rank
        FROM news_articles a
        LEFT JOIN news_article_symbols s ON s.article_id = a.article_id
        WHERE {where}
        GROUP BY a.article_id
        ORDER BY rank DESC, a.published_at DESC
        LIMIT :lim
        """
    )

    def _run(sess: Session) -> list[NewsArticle]:
        rows = sess.execute(sql, params).all()
        return _rows_to_articles(rows)

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def list_articles(
    limit: int = 50,
    symbol: str | None = None,
    tag: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    *,
    session: Session | None = None,
) -> list[NewsArticle]:
    """Chronological feed with optional filters (no FTS ranking)."""
    clauses: list[str] = []
    params: dict[str, object] = {"lim": limit}

    if symbol:
        clauses.append(
            "EXISTS (SELECT 1 FROM news_article_symbols ns "
            "WHERE ns.article_id = a.article_id AND ns.symbol = :sym)"
        )
        params["sym"] = symbol
    if tag:
        clauses.append(":tag = ANY(a.tags)")
        params["tag"] = tag
    if from_date:
        clauses.append("a.published_at >= :from_dt")
        params["from_dt"] = from_date
    if to_date:
        clauses.append("a.published_at <= :to_dt")
        params["to_dt"] = to_date

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = text(
        f"""
        SELECT a.article_id, a.published_at, a.title, a.snippet, a.link,
               a.source, a.sentiment_polarity, a.sentiment_pos,
               a.sentiment_neg, a.sentiment_neu, a.tags,
               array_agg(DISTINCT s.symbol) FILTER (WHERE s.symbol IS NOT NULL)
                   AS symbols
        FROM news_articles a
        LEFT JOIN news_article_symbols s ON s.article_id = a.article_id
        {where}
        GROUP BY a.article_id
        ORDER BY a.published_at DESC
        LIMIT :lim
        """
    )

    def _run(sess: Session) -> list[NewsArticle]:
        rows = sess.execute(sql, params).all()
        return _rows_to_articles(rows)

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


# ---------------------------------------------------------------------------
# news_feed_config
# ---------------------------------------------------------------------------

_DEFAULT_SYMBOLS = [
    "SPY", "QQQ", "DIA", "IWM",
    "XLK", "SOXX", "SMH",
    "AAPL", "MSFT", "NVDA", "GOOGL",
    "META", "AMZN", "AMD", "TSM", "ASML",
]
_DEFAULT_TAGS = [
    "monetary-policy",
    "economic-indicators",
    "earnings",
    "artificial-intelligence",
]

_GET_CONFIG_SQL = text(
    "SELECT symbols, tags, sentiment_alert_enabled, sentiment_threshold "
    "FROM news_feed_config WHERE id = 1"
)
_UPSERT_CONFIG_SQL = text(
    """
    INSERT INTO news_feed_config (id, symbols, tags, sentiment_alert_enabled, sentiment_threshold)
    VALUES (1, :syms, :tags, :enabled, :threshold)
    ON CONFLICT (id) DO UPDATE SET
        symbols                 = EXCLUDED.symbols,
        tags                    = EXCLUDED.tags,
        sentiment_alert_enabled = EXCLUDED.sentiment_alert_enabled,
        sentiment_threshold     = EXCLUDED.sentiment_threshold,
        updated_at              = NOW();
    """
)


def get_feed_config(*, session: Session | None = None) -> NewsFeedConfig:
    """Return the feed config, seeding defaults if none exists."""
    def _run(sess: Session) -> NewsFeedConfig:
        row = sess.execute(_GET_CONFIG_SQL).first()
        if row is None:
            return NewsFeedConfig(
                symbols=_DEFAULT_SYMBOLS,
                tags=_DEFAULT_TAGS,
                sentiment_alert_enabled=False,
                sentiment_threshold=Decimal("0.70"),
            )
        return NewsFeedConfig(
            symbols=list(row.symbols or []),
            tags=list(row.tags or []),
            sentiment_alert_enabled=bool(row.sentiment_alert_enabled),
            sentiment_threshold=Decimal(str(row.sentiment_threshold)),
        )

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def save_feed_config(
    cfg: NewsFeedConfig,
    *,
    session: Session | None = None,
) -> None:
    def _run(sess: Session) -> None:
        sess.execute(
            _UPSERT_CONFIG_SQL,
            {
                "syms": cfg.symbols,
                "tags": cfg.tags,
                "enabled": cfg.sentiment_alert_enabled,
                "threshold": cfg.sentiment_threshold,
            },
        )

    if session is not None:
        _run(session)
        return
    with session_scope() as s:
        _run(s)


# ---------------------------------------------------------------------------
# news_alerts_sent
# ---------------------------------------------------------------------------

def mark_alert_sent(
    article_id: str,
    channel: str,
    *,
    session: Session | None = None,
) -> None:
    sql = text(
        """
        INSERT INTO news_alerts_sent (article_id, channel)
        VALUES (:aid, :ch)
        ON CONFLICT (article_id, channel) DO NOTHING;
        """
    )

    def _run(sess: Session) -> None:
        sess.execute(sql, {"aid": article_id, "ch": channel})

    if session is not None:
        _run(session)
        return
    with session_scope() as s:
        _run(s)


def alerts_already_sent(
    article_ids: list[str],
    channel: str,
    *,
    session: Session | None = None,
) -> set[str]:
    """Return the subset of article_ids already sent on this channel."""
    if not article_ids:
        return set()
    sql = text(
        "SELECT article_id FROM news_alerts_sent "
        "WHERE article_id = ANY(:ids) AND channel = :ch"
    )

    def _run(sess: Session) -> set[str]:
        rows = sess.execute(sql, {"ids": article_ids, "ch": channel}).all()
        return {r.article_id for r in rows}

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)
