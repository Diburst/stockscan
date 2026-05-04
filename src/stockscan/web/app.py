"""FastAPI application factory.

Phase 0: /health
Phase 2: dashboard, signals, trades, backtests, base-rates, strategies, manual
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from stockscan import __version__
from stockscan.db import healthcheck
from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies
from stockscan.web.routes import (
    analysis,
    backtests,
    base_rates,
    dashboard,
    manual,
    news,
    signals,
    strategies,
    trades,
    watchlist,
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

    return app


app = create_app()
