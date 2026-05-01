"""News refresh orchestration.

EODHD's ``/news`` endpoint accepts ONE filter per call (symbol OR tag),
so the general-market feed and per-watchlist coverage both require
multiple pulls. This module owns that loop: walking the
``news_feed_config`` symbols + tags, plus any extra watchlisted
symbols, deduping, and bulk-upserting via :mod:`stockscan.news.store`.

Soft-fails on a per-call basis — one bad symbol or tag won't kill the
whole refresh. Failures are logged at warning level.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from stockscan.news.store import (
    NewsArticle,
    NewsFeedConfig,
    get_feed_config,
    last_fetched_at,
    make_article_id,
    upsert_articles,
)

# How many days on either side of an article's published_at to widen the
# re-fetch window when looking up its full content. EODHD's `from`/`to`
# bounds are inclusive at day granularity, so a non-zero pad lets us
# tolerate timezone drift and same-day-but-different-second mismatches.
_CONTENT_FETCH_PAD_DAYS = 1
# Per-page cap on the re-fetch query. The article we want is almost
# always near the top of the result set (filtered by symbol+date), so
# a modest cap keeps API cost bounded.
_CONTENT_FETCH_LIMIT = 100

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Summary of one refresh invocation."""

    articles_upserted: int
    api_calls: int
    failures: int
    started_at: datetime
    finished_at: datetime
    last_fetched_at: datetime | None  # AFTER the upserts


def _parse_date(raw: str) -> datetime | None:
    """Tolerantly parse EODHD's ``date`` field.

    EODHD returns either ``"YYYY-MM-DD HH:MM:SS"`` or ISO-8601 with TZ
    or just ``"YYYY-MM-DD"`` in old payloads. Be liberal in what we accept.
    """
    if not raw:
        return None
    candidates = [
        # Full datetime variants
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in candidates:
        try:
            dt = datetime.strptime(raw[:25], fmt)
            # Force UTC if naive — EODHD stamps publish time in UTC.
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except ValueError:
            continue
    log.debug("news: unparseable date %r", raw)
    return None


def _strip_us_suffix(symbols: Iterable[str]) -> list[str]:
    """``['AAPL.US', 'MSFT.US']`` -> ``['AAPL', 'MSFT']``. Other suffixes
    (e.g., .INDX, .L) get retained as-is so we don't accidentally
    collide with US tickers."""
    out: list[str] = []
    for s in symbols:
        if not s:
            continue
        out.append(s.split(".", 1)[0] if s.endswith(".US") else s)
    return out


def _payload_to_article(item: dict[str, Any]) -> NewsArticle | None:
    """Map one EODHD news item to our canonical :class:`NewsArticle`.

    Returns None when the item is missing the load-bearing fields
    (link/title/date) — caller should drop it.
    """
    link = item.get("link") or item.get("url")
    title = item.get("title")
    raw_date = item.get("date") or item.get("published_at")
    if not link or not title or not raw_date:
        return None
    pub = _parse_date(str(raw_date))
    if pub is None:
        return None

    content = (item.get("content") or "").strip()
    snippet = content[:500] if content else None

    sentiment = item.get("sentiment") or {}

    def _opt_dec(key: str) -> Decimal | None:
        v = sentiment.get(key)
        if v is None:
            return None
        try:
            return Decimal(str(v))
        except (ArithmeticError, ValueError):
            return None

    raw_symbols = item.get("symbols") or []
    raw_tags = item.get("tags") or []

    return NewsArticle(
        article_id=make_article_id(link),
        published_at=pub,
        title=str(title),
        snippet=snippet,
        link=str(link),
        source=item.get("source"),
        sentiment_polarity=_opt_dec("polarity"),
        sentiment_pos=_opt_dec("pos"),
        sentiment_neg=_opt_dec("neg"),
        sentiment_neu=_opt_dec("neu"),
        tags=[str(t) for t in raw_tags if t],
        symbols=_strip_us_suffix(raw_symbols),
    )


def refresh_news(
    provider: Any,
    *,
    days_back: int = 7,
    watchlist_symbols: Iterable[str] = (),
    config: NewsFeedConfig | None = None,
    per_call_limit: int = 50,
    session: Session | None = None,
) -> RefreshResult:
    """Pull recent news for the general feed + each watchlist symbol.

    The general feed comes from ``news_feed_config`` (auto-seeded with
    sensible defaults if not configured). ``watchlist_symbols`` is
    additive — symbols already in the general feed are not pulled twice.

    The provider must expose ``get_news(symbol=..., tag=..., from_date,
    to_date, limit)`` — see :class:`stockscan.data.providers.eodhd.EODHDProvider`.

    Idempotent: re-running on the same day re-upserts articles (newer
    fetched_at, same content). Returns a :class:`RefreshResult` with
    counts and the post-refresh ``last_fetched_at`` for the dashboard.
    """
    started = datetime.now(UTC)
    cfg = config if config is not None else get_feed_config(session=session)
    end = date.today()
    start = end - timedelta(days=days_back)

    raw: list[dict[str, Any]] = []
    api_calls = 0
    failures = 0

    def _safe_pull(**kwargs: Any) -> None:
        nonlocal api_calls, failures
        api_calls += 1
        try:
            items = provider.get_news(
                from_date=start,
                to_date=end,
                limit=per_call_limit,
                **kwargs,
            )
        except Exception as exc:  # provider exceptions vary; soft-fail.
            failures += 1
            log.warning("news refresh: pull failed (%s): %s", kwargs, exc)
            return
        if items:
            raw.extend(items)

    # General feed: one call per symbol, one per tag.
    for sym in cfg.symbols:
        _safe_pull(symbol=sym)
    for tag in cfg.tags:
        _safe_pull(tag=tag)

    # Watchlist symbols not already in the general feed.
    in_feed = set(cfg.symbols)
    for sym in watchlist_symbols:
        if sym in in_feed:
            continue
        _safe_pull(symbol=sym)

    # Map raw items to canonical articles, dedupe by article_id.
    by_id: dict[str, NewsArticle] = {}
    for item in raw:
        art = _payload_to_article(item)
        if art is None:
            continue
        # If the same article shows up under multiple filters, merge
        # symbol lists rather than overwriting.
        existing = by_id.get(art.article_id)
        if existing is not None:
            merged_syms = sorted({*existing.symbols, *art.symbols})
            merged_tags = sorted({*existing.tags, *art.tags})
            by_id[art.article_id] = NewsArticle(
                article_id=existing.article_id,
                published_at=existing.published_at,
                title=existing.title,
                snippet=existing.snippet,
                link=existing.link,
                source=existing.source,
                sentiment_polarity=existing.sentiment_polarity,
                sentiment_pos=existing.sentiment_pos,
                sentiment_neg=existing.sentiment_neg,
                sentiment_neu=existing.sentiment_neu,
                tags=merged_tags,
                symbols=merged_syms,
            )
        else:
            by_id[art.article_id] = art

    deduped = list(by_id.values())
    upserted = upsert_articles(deduped, session=session)

    finished = datetime.now(UTC)
    log.info(
        "news refresh: %d unique articles upserted from %d raw items (%d api calls, %d failures, took %.1fs)",
        upserted,
        len(raw),
        api_calls,
        failures,
        (finished - started).total_seconds(),
    )

    return RefreshResult(
        articles_upserted=upserted,
        api_calls=api_calls,
        failures=failures,
        started_at=started,
        finished_at=finished,
        last_fetched_at=last_fetched_at(session=session),
    )


def fetch_article_content(
    provider: Any,
    article: NewsArticle,
    *,
    days_window: int = _CONTENT_FETCH_PAD_DAYS,
) -> str | None:
    """Re-fetch an article's full body from the provider on demand.

    This is the read-side counterpart to :func:`refresh_news`. It is
    deliberately *not persisted*: stockscan stores headlines, snippets,
    sentiment, and links — but never the article body itself, so we
    don't end up holding redacted-paywall content. When the user expands
    an article in the dashboard, we re-issue an EODHD ``/news`` query
    scoped tightly enough to find this one item, then return its
    ``content`` field as a transient HTML/plain-text string.

    Strategy:

    1. Pick the narrowest available filter — first one of the article's
       symbols (cheaper, more relevant), falling back to one of its tags
       if it has none.
    2. Query a small date window around ``published_at`` (±``days_window``
       days, default 1) to absorb timezone drift between EODHD's stamp
       and ours.
    3. Hash each result's ``link`` with :func:`make_article_id` and
       compare against the requested ``article.article_id``. The hash
       round-trip is the source of truth — same URL → same id, no
       string-normalization fragility.

    Returns the content string on a hit, or ``None`` on any miss
    (no symbols/tags, provider failure, no matching article in window,
    or the matched item has empty content). Caller decides how to
    render the missing case (we surface a "Couldn't load — view on
    source" link in the template).
    """
    if not article.link:
        return None

    # Pick a filter. Prefer a symbol (more specific); EODHD requires
    # exactly one of {symbol, tag} per call, so we don't combine.
    symbol: str | None = article.symbols[0] if article.symbols else None
    tag: str | None = article.tags[0] if (not symbol and article.tags) else None
    if not symbol and not tag:
        log.info(
            "fetch_article_content: %s has no symbols or tags; "
            "no filter to query — caller should fall back to source link",
            article.article_id,
        )
        return None

    pub_date = article.published_at.date()
    start = pub_date - timedelta(days=days_window)
    end = pub_date + timedelta(days=days_window)

    try:
        items = provider.get_news(
            symbol=symbol,
            tag=tag,
            from_date=start,
            to_date=end,
            limit=_CONTENT_FETCH_LIMIT,
        )
    except Exception as exc:  # provider exceptions vary; soft-fail.
        log.warning(
            "fetch_article_content: provider error for %s (sym=%s tag=%s): %s",
            article.article_id,
            symbol,
            tag,
            exc,
        )
        return None

    for item in items or []:
        link = item.get("link") or item.get("url")
        if not link:
            continue
        if make_article_id(str(link)) != article.article_id:
            continue
        content = item.get("content")
        if not content:
            log.info(
                "fetch_article_content: matched %s but content was empty",
                article.article_id,
            )
            return None
        return str(content).strip() or None

    log.info(
        "fetch_article_content: no match for %s in window %s..%s "
        "(filter sym=%s tag=%s, %d items returned)",
        article.article_id,
        start,
        end,
        symbol,
        tag,
        len(items or []),
    )
    return None
