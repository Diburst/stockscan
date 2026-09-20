"""Compute and cache the day's market regime from SPY bars and HY OAS.

:func:`detect_regime` is the one entry point. It pulls two years of SPY
closes (enough warmup for the 200-day SMA and the 252-day vol rank) and
the HY OAS series, evaluates :func:`stockscan.regime.rules.regime_frame`,
and persists the last row. Rows are cached by date; rows written under
an older ``methodology_version`` are recomputed on first touch.

SPY bars are the one hard requirement — without them the function returns
``None`` and callers size neutrally. HY OAS is optional: when the FRED
series is missing the credit-stress flag is simply ``False``.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd
from sqlalchemy.orm import Session

from stockscan.data.macro_store import get_macro_series
from stockscan.data.store import get_bars
from stockscan.regime.rules import (
    CREDIT_RANK_WINDOW,
    TREND_SMA_WINDOW,
    VOL_RANK_WINDOW,
    VOL_WINDOW,
    regime_frame,
)
from stockscan.regime.store import (
    METHODOLOGY_VERSION,
    MarketRegime,
    get_regime,
    upsert_regime,
)

log = logging.getLogger(__name__)

BENCHMARK = "SPY"
HY_OAS_SERIES = "BAMLH0A0HYM2"  # ICE BofA US High Yield OAS, via FRED

# Bars needed before every column of the regime frame is defined.
MIN_BENCHMARK_BARS = VOL_RANK_WINDOW + VOL_WINDOW
_LOOKBACK_YEARS = 2


def _fetch_spy_close(as_of: date, session: Session | None) -> pd.Series | None:
    start = as_of.replace(year=as_of.year - _LOOKBACK_YEARS)
    try:
        bars = get_bars(BENCHMARK, start, as_of, session=session)
    except Exception as exc:
        log.warning("regime: could not fetch %s bars: %s", BENCHMARK, exc)
        return None
    if bars is None or bars.empty:
        log.warning("regime: no %s bars in DB — run `stockscan refresh bars %s` first", BENCHMARK, BENCHMARK)
        return None
    bars = bars[bars.index.date <= as_of]
    if len(bars) < max(MIN_BENCHMARK_BARS, TREND_SMA_WINDOW):
        log.warning(
            "regime: only %d %s bars available (need %d) — skipping",
            len(bars),
            BENCHMARK,
            MIN_BENCHMARK_BARS,
        )
        return None
    return bars["close"].astype(float)


def _fetch_hy_oas(as_of: date, session: Session | None) -> pd.Series | None:
    start = as_of.replace(year=as_of.year - _LOOKBACK_YEARS)
    try:
        series = get_macro_series(HY_OAS_SERIES, start, as_of, session=session)
    except Exception as exc:
        log.warning("regime: HY OAS unavailable — credit-stress flag off: %s", exc)
        return None
    if series is None or series.empty or len(series) < CREDIT_RANK_WINDOW:
        log.warning(
            "regime: %d HY OAS observations (need %d) — credit-stress flag off; "
            "run `stockscan refresh macro`",
            0 if series is None else len(series),
            CREDIT_RANK_WINDOW,
        )
        return None
    return series


def _last_float(series: pd.Series) -> float | None:
    value = series.iloc[-1]
    return None if pd.isna(value) else float(value)


def detect_regime(
    as_of: date,
    *,
    session: Session | None = None,
    force_recompute: bool = False,
) -> MarketRegime | None:
    """The market regime for ``as_of``, computed and cached.

    ``force_recompute`` bypasses the cache (after a bar or macro refresh).
    """
    if not force_recompute:
        cached = get_regime(as_of, session=session)
        if cached is not None and cached.methodology_version >= METHODOLOGY_VERSION:
            return cached

    spy_close = _fetch_spy_close(as_of, session)
    if spy_close is None:
        return None
    frame = regime_frame(spy_close, _fetch_hy_oas(as_of, session))
    today = frame.iloc[-1]
    if pd.isna(today["sma200"]):
        log.warning("regime: SMA(%d) undefined for %s as of %s", TREND_SMA_WINDOW, BENCHMARK, as_of)
        return None

    row = upsert_regime(
        as_of,
        trend_gate_open=bool(today["trend_gate_open"]),
        days_on_side=int(today["days_on_side"]),
        spy_close=float(spy_close.iloc[-1]),
        spy_sma200=float(today["sma200"]),
        spy_sma200_slope_20d=_last_float(frame["sma200_slope_20d"]),
        realized_vol_20d=_last_float(frame["realized_vol"]),
        realized_vol_pct_rank=_last_float(frame["vol_pct_rank"]),
        vol_scalar=_last_float(frame["vol_scalar"]),
        hy_oas_level=_last_float(frame["hy_oas"]),
        hy_oas_pct_rank=_last_float(frame["hy_oas_pct_rank"]),
        credit_stress_flag=bool(today["credit_stress_flag"]),
        session=session,
    )
    log.info(
        "regime: %s | gate %s (%d days on side) | vol scalar %s (rank %s) | stress %s",
        as_of,
        "open" if row.trend_gate_open else "closed",
        row.days_on_side,
        f"{row.vol_multiplier:.2f}",
        f"{float(row.realized_vol_pct_rank):.2f}" if row.realized_vol_pct_rank is not None else "—",
        row.credit_stress_flag,
    )
    return row
