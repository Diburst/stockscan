"""Financial news integration (DESIGN §news / TODO §news).

Module surface, in dependency order:

  * ``store``    — DB CRUD: upsert articles, recent feeds, FTS search,
                   feed config, alert dedup, ``last_fetched_at``.
  * ``refresh``  — orchestrate provider pulls into the store. Idempotent:
                   re-running on the same day is safe.

The CLI command ``stockscan refresh news`` and the web ``POST /news/refresh``
endpoint both call :func:`refresh.refresh_news`.
"""

from __future__ import annotations

from stockscan.news.refresh import RefreshResult, fetch_article_content, refresh_news
from stockscan.news.store import (
    NewsArticle,
    NewsFeedConfig,
    alerts_already_sent,
    fts_search,
    get_article,
    get_feed_config,
    last_fetched_at,
    list_articles,
    make_article_id,
    mark_alert_sent,
    recent_for_symbol,
    recent_general,
    save_feed_config,
    upsert_articles,
)

__all__ = [
    "NewsArticle",
    "NewsFeedConfig",
    "RefreshResult",
    "alerts_already_sent",
    "fetch_article_content",
    "fts_search",
    "get_article",
    "get_feed_config",
    "last_fetched_at",
    "list_articles",
    "make_article_id",
    "mark_alert_sent",
    "recent_for_symbol",
    "recent_general",
    "refresh_news",
    "save_feed_config",
    "upsert_articles",
]
