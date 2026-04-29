"""Market-regime detector — DESIGN §regime.

Classifies the broad market (proxied by SPY) into one of four states using
ADX(14) and SMA(200) on the most-recent EOD bars:

  trending_up   — ADX > 25 AND close > SMA(200)
  trending_down — ADX > 25 AND close < SMA(200)
  choppy        — ADX < 18  (range-bound, no directional conviction)
  transitioning — ADX 18-25 (ambiguous; wait-and-see)

Results are cached in the `market_regime` table so every call after the first
is a cheap DB lookup rather than a recomputation.

Failure modes are soft: if SPY bars are missing or insufficient, the function
returns None and callers skip regime-based filtering rather than crashing.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd
from sqlalchemy.orm import Session

from stockscan.data.store import get_bars
from stockscan.indicators import adx as compute_adx
from stockscan.indicators import sma
from stockscan.regime.store import MarketRegime, RegimeLabel, get_regime, upsert_regime

log = logging.getLogger(__name__)

# SPY is the S&P 500 proxy for regime detection.
_BENCHMARK = "SPY"

# ADX thresholds (canonical Wilder definitions).
_ADX_TREND_THRESHOLD = 25.0
_ADX_CHOP_THRESHOLD = 18.0

# Bars needed: SMA(200) dominates; add 2x ADX period for Wilder warmup + buffer.
_MIN_BARS = 230


def classify_regime(adx_val: float, spy_close: float, spy_sma200: float) -> RegimeLabel:
    """Pure classification — no I/O. Useful for testing and backtest replay."""
    if adx_val > _ADX_TREND_THRESHOLD:
        return "trending_up" if spy_close > spy_sma200 else "trending_down"
    if adx_val < _ADX_CHOP_THRESHOLD:
        return "choppy"
    return "transitioning"


def detect_regime(
    as_of: date,
    *,
    session: Session | None = None,
) -> MarketRegime | None:
    """Return the market regime for `as_of`, computing and caching if needed.

    Returns None when SPY bars are unavailable or insufficient — callers
    should skip regime-based filtering in that case rather than blocking.
    """
    # Fast path: already stored.
    cached = get_regime(as_of, session=session)
    if cached is not None:
        return cached

    # Fetch SPY bars up to as_of.  Use a 5-year window; we need only ~230 bars
    # but this keeps the call consistent with how the scanner fetches bars.
    start = as_of.replace(year=as_of.year - 2)
    try:
        bars = get_bars(_BENCHMARK, start, as_of, session=session)
    except Exception as exc:
        log.warning("regime: could not fetch %s bars: %s", _BENCHMARK, exc)
        return None

    if bars is None or bars.empty:
        log.warning("regime: no %s bars in DB — run `stockscan refresh bars` first", _BENCHMARK)
        return None

    # Filter to as_of (no look-ahead).
    if hasattr(bars.index, "date"):
        bars = bars[bars.index.date <= as_of]

    if len(bars) < _MIN_BARS:
        log.warning(
            "regime: only %d %s bars available (need %d) — skipping regime detection",
            len(bars),
            _BENCHMARK,
            _MIN_BARS,
        )
        return None

    close = bars["close"].astype(float)
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)

    adx_series = compute_adx(high, low, close, period=14)
    sma200_series = sma(close, 200)

    adx_val = float(adx_series.iloc[-1])
    sma200_val = float(sma200_series.iloc[-1])
    close_val = float(close.iloc[-1])

    if pd.isna(adx_val) or pd.isna(sma200_val):
        log.warning("regime: ADX or SMA(200) is NaN for %s as of %s", _BENCHMARK, as_of)
        return None

    label = classify_regime(adx_val, close_val, sma200_val)
    log.info(
        "regime: %s as of %s — ADX=%.1f, close=%.2f, SMA200=%.2f → %s",
        _BENCHMARK, as_of, adx_val, close_val, sma200_val, label,
    )

    return upsert_regime(
        as_of,
        label,
        adx=adx_val,
        spy_close=close_val,
        spy_sma200=sma200_val,
        session=session,
    )
