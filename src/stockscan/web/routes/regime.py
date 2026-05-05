"""Market-regime routes.

Endpoint:
  POST /regime/refresh   — pull the latest SPY / VIX / RSP bars from the
                            provider, recompute the regime composite for
                            today, and return the freshly-rendered regime
                            card so HTMX can swap it in place on the
                            dashboard.

The refresh handler covers the common "indicators look stale" failure
mode: in single-user dev, the nightly cron may not have run, so
`market_regime` still has yesterday's row. Clicking the button bypasses
the daily timer — bars catch up, ``detect_regime`` re-runs with
``force_recompute=True``, and the card swaps in place.

HY OAS (the credit component) is FRED-side and lags ~2 trading days, so
it is NOT refreshed here — the nightly ``refresh macro`` job handles it.
"""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from stockscan.config import settings
from stockscan.data.backfill import refresh_recent_days_bulk, trading_days_since
from stockscan.data.providers.eodhd import EODHDError, EODHDProvider
from stockscan.regime import (
    build_strategy_factors,
    detect_regime,
    latest_regime,
)
from stockscan.strategies import STRATEGY_REGISTRY, discover_strategies
from stockscan.web.deps import (
    attach_hx_toast,
    get_session,
    rate_limit_check,
    render,
)

router = APIRouter(prefix="/regime")
log = logging.getLogger(__name__)

# Symbols the regime composite reads. Filtered server-side so the
# bulk-EOD response is small even though the endpoint pulls the full
# exchange.
_REGIME_US_SYMBOLS = {"SPY", "RSP"}
_REGIME_INDX_SYMBOLS = {"VIX"}

# How many recent trading days to refresh. 5 covers a long weekend +
# the typical "missed the nightly job" gap. detect_regime only needs
# today's bar but reading slightly more guards against partial-day
# upserts on the most recent few rows.
_REFRESH_DAYS_BACK = 5


@router.post("/refresh")
async def refresh_endpoint(
    request: Request,
    s: Session = Depends(get_session),
):
    """Refresh SPY/VIX/RSP bars and recompute the regime for today.

    HTMX swaps the response into ``#regime-card`` so the dashboard
    updates in place without a full page reload. The partial is the
    same one ``dashboard.html`` includes on initial load.
    """
    # Debounce — refusing to hit the upstream if the user just refreshed.
    cooldown_remaining = rate_limit_check("regime.refresh", cooldown_seconds=15)
    if cooldown_remaining is not None:
        return _render_card(
            request,
            s,
            error=None,
            summary=None,
            toast=(
                "warn",
                f"Just refreshed — try again in {int(cooldown_remaining) + 1}s",
            ),
        )

    discover_strategies()
    today = date.today()

    # Snapshot the previous as_of_date BEFORE any work so we can show
    # "advanced from X → Y" in the success banner.
    prev = latest_regime(session=s)
    previous_as_of = prev.as_of_date if prev is not None else None

    error: str | None = None
    bars_upserted = 0

    api_key = settings.eodhd_api_key.get_secret_value()
    if not api_key:
        error = "EODHD_API_KEY is not set. Add it to your .env to refresh bars."
    else:
        # ---- Phase 1: refresh recent SPY/RSP/VIX bars. ----
        # If the bars already cover today, the bulk endpoint upserts a
        # zero-or-near-zero number of new rows and detect_regime sees
        # the same data — that's fine. When the user has just resumed
        # the laptop after a weekend, this is where the regime catches up.
        try:
            with EODHDProvider(api_key=api_key) as provider:
                # trading_days_since takes (last_date, until); pass
                # today - N as last_date to get the trailing N-day window.
                from datetime import timedelta as _td

                window = trading_days_since(today - _td(days=_REFRESH_DAYS_BACK), today)
                if window:
                    bars_upserted += refresh_recent_days_bulk(
                        provider,
                        window,
                        exchange="US",
                        filter_to=_REGIME_US_SYMBOLS,
                    )
                    bars_upserted += refresh_recent_days_bulk(
                        provider,
                        window,
                        exchange="INDX",
                        filter_to=_REGIME_INDX_SYMBOLS,
                    )
        except EODHDError as exc:
            log.warning("regime refresh: provider error: %s", exc)
            error = f"Provider error: {exc}"
        except Exception as exc:
            log.exception("regime refresh: bar-refresh failed")
            error = f"Bar refresh failed: {exc}"

    # ---- Phase 2: recompute the regime for today. ----
    # Always attempted — even if the bar refresh failed (e.g., no API
    # key) the user may still want detect_regime to run against
    # whatever bars are already in the store. force_recompute=True so
    # we overwrite any stale row stamped earlier today by an aborted
    # nightly run.
    if error is None:
        try:
            detect_regime(today, session=s, force_recompute=True)
        except Exception as exc:
            log.exception("regime refresh: detect_regime failed")
            error = f"Recompute failed: {exc}"

    summary: dict[str, object] | None = None
    if error is None:
        summary = {
            "bars_upserted": bars_upserted,
            "previous_as_of": previous_as_of,
        }

    # Build the toast text based on what changed. If the as_of_date
    # did not advance, the user should know — that means EODHD has not
    # yet posted today's bar (typical pre-close on a trading day).
    toast: tuple[str, str] | None
    if error:
        toast = ("error", "Regime refresh failed")
    else:
        new_regime = latest_regime(session=s)
        new_as_of = new_regime.as_of_date if new_regime is not None else None
        if previous_as_of is not None and new_as_of == previous_as_of:
            toast = (
                "info",
                f"Regime recomputed — bars not yet available past {previous_as_of}",
            )
        elif new_as_of is not None:
            toast = ("success", f"Regime updated to {new_as_of}")
        else:
            toast = ("warn", "Regime recomputed but no row was persisted")

    return _render_card(request, s, error=error, summary=summary, toast=toast)


# ---- helpers -----------------------------------------------------------------


def _render_card(
    request: Request,
    session: Session,
    *,
    error: str | None,
    summary: dict[str, object] | None,
    toast: tuple[str, str] | None,
):
    """Render the regime card partial with the post-refresh context."""
    regime = latest_regime(session=session)
    strategy_factors = build_strategy_factors(regime, STRATEGY_REGISTRY.all())
    response = render(
        request,
        "_regime_card.html",
        regime=regime,
        strategy_factors=strategy_factors,
        regime_refresh_error=error,
        regime_refresh_summary=summary,
    )
    if toast is not None:
        kind, message = toast
        return attach_hx_toast(response, kind, message)
    return response
