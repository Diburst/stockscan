"""FastAPI application factory.

Phase 0: /health
Phase 2: dashboard, signals, trades, backtests, base-rates, strategies, manual
"""

from __future__ import annotations

import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from stockscan import __version__
from stockscan.db import healthcheck
from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies
from stockscan.web.deps import render
from stockscan.web.routes import (
    analysis,
    backtests,
    base_rates,
    dashboard,
    manual,
    news,
    regime,
    signals,
    strategies,
    trades,
    watchlist,
)

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Exception handlers — render the error.html template for HTML routes
# (anything that isn't an /api-* or /health endpoint), JSON for the rest.
# Falls back to FastAPI's default if anything goes wrong rendering the
# error page itself, so we never recurse into a 500-rendering-a-500.
# ----------------------------------------------------------------------

_API_PREFIXES = ("/api-docs", "/api-redoc", "/api-openapi.json", "/health")


def _wants_html(request: Request) -> bool:
    """Decide whether to render error.html or JSON.

    True for normal browser nav (HTML pages); false for API + JSON routes
    and for any path that asks for application/json explicitly.
    """
    path = request.url.path
    if any(path.startswith(p) for p in _API_PREFIXES):
        return False
    accept = request.headers.get("accept", "").lower()
    if "application/json" in accept and "text/html" not in accept:
        return False
    return True


def _status_label(code: int) -> str:
    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return "Error"


def _friendly_message(code: int, fallback: str = "") -> str:
    if code == 404:
        return "We couldn't find that page. The link may have been moved or removed."
    if code == 403:
        return "You don't have access to that page."
    if code == 400:
        return "The request didn't look right. Try again, or head back to the dashboard."
    if code == 405:
        return "That action isn't allowed on this URL."
    if code == 422:
        return "Some of the form fields didn't validate. Check the values and try again."
    if 500 <= code < 600:
        return (
            "Something went wrong on our end. The error has been logged; "
            "the dashboard is still available."
        )
    return fallback or "An unexpected error occurred."


def _render_error(
    request: Request,
    *,
    status_code: int,
    detail: str | None = None,
) -> JSONResponse | object:
    label = _status_label(status_code)
    message = _friendly_message(status_code, fallback=detail or "")
    if not _wants_html(request):
        return JSONResponse(
            {"status": label, "code": status_code, "detail": detail or message},
            status_code=status_code,
        )
    try:
        response = render(
            request,
            "error.html",
            status_code=status_code,
            status_label=label,
            message=message,
            detail=detail if detail and detail != message else None,
            path=request.url.path,
            method=request.method,
        )
        # render() returns a TemplateResponse with status 200 by default;
        # surface the real status so caches and clients see it correctly.
        response.status_code = status_code
        return response
    except Exception as render_exc:  # noqa: BLE001 - last-resort fallback
        log.exception("error.html render failed: %s", render_exc)
        return JSONResponse(
            {"status": label, "code": status_code, "detail": detail or message},
            status_code=status_code,
        )


def create_app() -> FastAPI:
    # FastAPI's auto-Swagger has been moved from /docs to /api-docs so the
    # /docs path can host the in-app documentation hub (rendered markdown +
    # CLI reference). The OpenAPI JSON moves to /api-openapi.json for the
    # same reason.
    app = FastAPI(
        title="stockscan",
        version=__version__,
        description="Personal swing-trading scanner, backtester, and position manager",
        docs_url="/api-docs",
        redoc_url="/api-redoc",
        openapi_url="/api-openapi.json",
    )

    @app.on_event("startup")
    async def _startup() -> None:
        # Auto-discover strategies on boot.
        discover_strategies()

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> object:
        # 404, 405, 403, etc. — render the friendly page when the
        # browser asked for HTML, JSON otherwise.
        return _render_error(
            request, status_code=exc.status_code, detail=str(exc.detail)
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> object:
        # Log the full traceback then degrade to a generic 500 page so
        # the user sees something usable instead of a stack trace.
        log.exception("unhandled exception on %s %s", request.method, request.url.path)
        return _render_error(request, status_code=500, detail=str(exc))

    @app.get("/health")
    async def health() -> JSONResponse:
        db = healthcheck()
        ok = bool(db.get("ok"))
        body = {
            "status": "ok" if ok else "degraded",
            "version": __version__,
            "strategies": STRATEGY_REGISTRY.names(),
            "db": db,
        }
        return JSONResponse(body, status_code=200 if ok else 503)

    # ------------------------------------------------------------------
    # Phase 2 routes
    # ------------------------------------------------------------------
    app.include_router(dashboard.router)
    app.include_router(signals.router)
    app.include_router(trades.router)
    app.include_router(backtests.router)
    app.include_router(base_rates.router)
    app.include_router(strategies.router)
    app.include_router(watchlist.router)
    app.include_router(news.router)
    app.include_router(manual.router)
    app.include_router(analysis.router)
    app.include_router(regime.router)

    return app


app = create_app()
