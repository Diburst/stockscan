"""News routes — on-demand article reader.

Endpoints:
  GET  /news/{article_id}/content — re-fetch a single article's body
                                    on demand, return as an HTML
                                    fragment for HTMX inline expansion.

The dedicated ``/news`` listing page (chronological feed + filters +
FTS search) is on the TODO list — once it ships, add the GET handler
here.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from stockscan.config import settings
from stockscan.data.providers.eodhd import EODHDError, EODHDProvider
from stockscan.news import fetch_article_content, get_article
from stockscan.web.deps import get_session, render

router = APIRouter(prefix="/news")
log = logging.getLogger(__name__)


@router.get("/{article_id}/content")
def article_content(
    article_id: str,
    request: Request,
    s: Session = Depends(get_session),
):
    """Render an article's full body as an HTML fragment.

    Lazy-loaded by HTMX when the user expands a ``<details>`` row in
    the news card. The body is **not persisted** — we re-issue a
    narrow ``/news`` query against EODHD using the article's
    symbol/tag and a date window around its ``published_at``, then
    pluck out the matching item by article-id hash.

    Misses (no symbols/tags, provider error, no match in window,
    empty content) all degrade to the "view on source" fallback that
    ``_article_body.html`` already renders. We surface unexpected
    exceptions in ``error`` rather than 5xx-ing — the row stays in
    the dashboard, the user just sees a small inline warning.
    """
    article = get_article(article_id, session=s)
    if article is None:
        raise HTTPException(status_code=404, detail="article not found")

    content: str | None = None
    error: str | None = None

    api_key = settings.eodhd_api_key.get_secret_value()
    if not api_key:
        error = "EODHD_API_KEY is not set."
    else:
        try:
            with EODHDProvider(api_key=api_key) as provider:
                content = fetch_article_content(provider, article)
        except EODHDError as exc:
            log.warning("article_content: provider error for %s: %s", article_id, exc)
            error = f"Provider error: {exc}"
        except Exception as exc:
            log.exception("article_content: unexpected error for %s", article_id)
            error = f"Couldn't fetch: {exc}"

    return render(
        request,
        "_article_body.html",
        article=article,
        content=content,
        error=error,
    )
