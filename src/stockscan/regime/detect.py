"""Market-regime detector — DESIGN §regime (v2 composite).

The detector produces two layers of output that get persisted in the
same row of ``market_regime``:

  * **Legacy label** (``trending_up`` / ``trending_down`` / ``choppy`` /
    ``transitioning``), classified from SPY ADX(14) + SMA(200). Kept for
    the dashboard banner and for back-compat with v1 callers.
  * **v2 composite** — four component scores in [0, 1] (vol, trend,
    breadth, credit), a credit-stress flag, and the underlying levels
    we pulled to compute them. Combined into a single composite score
    per the research doc weights (vol 0.40, trend 0.25, breadth 0.20,
    credit 0.15).

Failure modes are intentionally fine-grained — each v2 component fetches
and computes independently. If FRED is down, ``credit_score`` comes
back ``None`` but ``vol_score`` and ``trend_score`` still populate; the
composite is renormalized over what's available. The legacy label is
the only "hard" requirement: if SPY bars are missing or insufficient,
the function returns ``None`` and callers skip regime-aware sizing
rather than crashing.

Cache discipline: v2 rows are cached by ``as_of`` and reused on
subsequent calls. v1 cached rows (``methodology_version < 2``) are
treated as stale and re-detected so the upgrade path lands cleanly the
first time the new code runs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pandas as pd

from stockscan.data.macro_store import get_macro_series
from stockscan.data.store import get_bars
from stockscan.indicators import adx as compute_adx
from stockscan.indicators import sma
from stockscan.regime.composite import (
    BREADTH_LONG_WINDOW,
    DEFAULT_WINDOW,
    breadth_score,
    composite_score,
    credit_score,
    credit_stress_flag,
    hy_oas_zscore,
    trend_score,
    vol_score,
)
from stockscan.regime.store import (
    MarketRegime,
    RegimeLabel,
    get_regime,
    upsert_regime,
)

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# SPY is the S&P 500 proxy for the legacy label. Always required.
_BENCHMARK = "SPY"

# v2 instruments
_VIX_SYMBOL = "VIX"  # bars hypertable, fetched via EODHD .INDX
_RSP_SYMBOL = "RSP"  # bars hypertable, EODHD .US
HY_OAS_SERIES = "BAMLH0A0HYM2"  # macro_series, fetched via FRED

# ADX thresholds (canonical Wilder definitions).
_ADX_TREND_THRESHOLD = 25.0
_ADX_CHOP_THRESHOLD = 18.0

# Minimum SPY bars to compute the legacy label. SMA(200) dominates; add
# 2x ADX period for Wilder warmup + buffer.
_MIN_LEGACY_BARS = 230

# Lookback window for fetching all v2 inputs. The component math wants
# DEFAULT_WINDOW (252) trailing observations; we pull a 2-year window
# (≈ 504 trading days) to give the rolling functions warmup headroom.
_LOOKBACK_YEARS = 2

_METHODOLOGY_VERSION = 2


def classify_regime(adx_val: float, spy_close: float, spy_sma200: float) -> RegimeLabel:
    """Pure classification — no I/O. Useful for testing and backtest replay."""
    if adx_val > _ADX_TREND_THRESHOLD:
        return "trending_up" if spy_close > spy_sma200 else "trending_down"
    if adx_val < _ADX_CHOP_THRESHOLD:
        return "choppy"
    return "transitioning"


# ----------------------------------------------------------------------
# Helpers — each soft-fails to (None, ...) on missing/short data
# ----------------------------------------------------------------------
def _safe_last(series: pd.Series) -> float | None:
    """Return the last value of a Series as a float, or None if NaN/empty."""
    if series is None or series.empty:
        return None
    last = series.iloc[-1]
    if pd.isna(last):
        return None
    return float(last)


def _fetch_spy_bars(as_of: date, session: Session | None) -> pd.DataFrame | None:
    """SPY bars are mandatory for the legacy label; failure -> None."""
    start = as_of.replace(year=as_of.year - _LOOKBACK_YEARS)
    try:
        bars = get_bars(_BENCHMARK, start, as_of, session=session)
    except Exception as exc:
        log.warning("regime: could not fetch %s bars: %s", _BENCHMARK, exc)
        return None
    if bars is None or bars.empty:
        log.warning("regime: no %s bars in DB — run `stockscan refresh bars` first", _BENCHMARK)
        return None
    if hasattr(bars.index, "date"):
        bars = bars[bars.index.date <= as_of]
    if len(bars) < _MIN_LEGACY_BARS:
        log.warning(
            "regime: only %d %s bars available (need %d) — skipping",
            len(bars),
            _BENCHMARK,
            _MIN_LEGACY_BARS,
        )
        return None
    return bars


def _fetch_vix_close(as_of: date, session: Session | None) -> pd.Series | None:
    """Pull VIX close as a date-indexed Series, or None on any failure.

    VIX is stored in the bars hypertable under symbol="VIX" (fetched via
    EODHD's ``/eod/VIX.INDX`` endpoint). Missing data is non-fatal — the
    v2 composite can still be computed without ``vol_score``.
    """
    start = as_of.replace(year=as_of.year - _LOOKBACK_YEARS)
    try:
        bars = get_bars(_VIX_SYMBOL, start, as_of, session=session)
    except Exception as exc:
        log.warning("regime: VIX bars unavailable — vol_score skipped: %s", exc)
        return None
    if bars is None or bars.empty:
        log.warning("regime: no VIX bars stored — vol_score skipped")
        return None
    if hasattr(bars.index, "date"):
        bars = bars[bars.index.date <= as_of]
    if len(bars) < DEFAULT_WINDOW:
        log.warning(
            "regime: only %d VIX bars (need %d for percentile) — vol_score skipped",
            len(bars),
            DEFAULT_WINDOW,
        )
        return None
    return bars["close"].astype(float)


def _fetch_rsp_close(as_of: date, session: Session | None) -> pd.Series | None:
    """Pull RSP close. Used for the breadth proxy (RSP/SPY ratio)."""
    start = as_of.replace(year=as_of.year - _LOOKBACK_YEARS)
    try:
        bars = get_bars(_RSP_SYMBOL, start, as_of, session=session)
    except Exception as exc:
        log.warning("regime: RSP bars unavailable — breadth_score skipped: %s", exc)
        return None
    if bars is None or bars.empty:
        log.warning("regime: no RSP bars stored — breadth_score skipped")
        return None
    if hasattr(bars.index, "date"):
        bars = bars[bars.index.date <= as_of]
    if len(bars) < BREADTH_LONG_WINDOW:
        log.warning(
            "regime: only %d RSP bars (need %d for SMA) — breadth_score skipped",
            len(bars),
            BREADTH_LONG_WINDOW,
        )
        return None
    return bars["close"].astype(float)


def _fetch_hy_oas_series(as_of: date, session: Session | None) -> pd.Series | None:
    """Pull HY OAS from ``macro_series``. None on any failure / insufficient history."""
    start = as_of.replace(year=as_of.year - _LOOKBACK_YEARS)
    try:
        s = get_macro_series(HY_OAS_SERIES, start, as_of, session=session)
    except Exception as exc:
        log.warning("regime: HY OAS unavailable — credit_score skipped: %s", exc)
        return None
    if s is None or s.empty:
        log.warning("regime: no HY OAS rows in macro_series — credit_score skipped")
        return None
    if len(s) < DEFAULT_WINDOW:
        log.warning(
            "regime: only %d HY OAS observations (need %d) — credit_score skipped",
            len(s),
            DEFAULT_WINDOW,
        )
        return None
    return s


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------
def detect_regime(
    as_of: date,
    *,
    session: Session | None = None,
    force_recompute: bool = False,
) -> MarketRegime | None:
    """Return the v2 market regime for ``as_of``, computing and caching.

    Returns ``None`` only when SPY bars are missing or insufficient — the
    legacy label is the one hard prerequisite. Callers should skip
    regime-aware sizing in that case rather than blocking.

    A cached v2 row is reused as-is. A cached v1 row (rows persisted
    before migration 0010) is treated as stale and re-detected so the
    composite columns get backfilled the first time we see that date.
    Pass ``force_recompute=True`` to bypass the cache entirely (useful
    for backtest replay over historical dates whose underlying data has
    been refreshed).
    """
    if not force_recompute:
        cached = get_regime(as_of, session=session)
        if cached is not None and cached.methodology_version >= _METHODOLOGY_VERSION:
            return cached

    # ---- Legacy label (mandatory) ----
    spy_bars = _fetch_spy_bars(as_of, session=session)
    if spy_bars is None:
        return None

    spy_close = spy_bars["close"].astype(float)
    spy_high = spy_bars["high"].astype(float)
    spy_low = spy_bars["low"].astype(float)

    adx_series = compute_adx(spy_high, spy_low, spy_close, period=14)
    sma200_series = sma(spy_close, 200)

    adx_val = _safe_last(adx_series)
    sma200_val = _safe_last(sma200_series)
    close_val = _safe_last(spy_close)
    if adx_val is None or sma200_val is None or close_val is None:
        log.warning("regime: ADX or SMA(200) NaN for %s as of %s", _BENCHMARK, as_of)
        return None

    label = classify_regime(adx_val, close_val, sma200_val)

    # ---- v2 components (each may be None) ----

    # Trend score from SPY (always available since legacy succeeded).
    trend_val = _safe_last(trend_score(spy_close, sma200_series))

    # Vol score from VIX bars.
    vol_val: float | None = None
    vix_level_val: float | None = None
    vix_pct_rank_val: float | None = None
    vix_close = _fetch_vix_close(as_of, session=session)
    if vix_close is not None:
        vol_val = _safe_last(vol_score(vix_close))
        vix_level_val = _safe_last(vix_close)
        # vol_score = 1 - rank, so rank = 1 - vol when vol is computed.
        vix_pct_rank_val = 1.0 - vol_val if vol_val is not None else None

    # Breadth score from RSP/SPY ratio.
    breadth_val: float | None = None
    rsp_close = _fetch_rsp_close(as_of, session=session)
    if rsp_close is not None:
        # Inner-join on date so the ratio is computed only on shared bars.
        merged = pd.concat([rsp_close.rename("rsp"), spy_close.rename("spy")], axis=1).dropna()
        if len(merged) >= BREADTH_LONG_WINDOW:
            breadth_val = _safe_last(breadth_score(merged["rsp"], merged["spy"]))

    # Credit components from HY OAS.
    credit_val: float | None = None
    hy_oas_level_val: float | None = None
    hy_oas_pct_rank_val: float | None = None
    hy_oas_zscore_val: float | None = None
    stress_flag = False
    hy_series = _fetch_hy_oas_series(as_of, session=session)
    if hy_series is not None:
        credit_val = _safe_last(credit_score(hy_series))
        hy_oas_level_val = _safe_last(hy_series)
        hy_oas_pct_rank_val = 1.0 - credit_val if credit_val is not None else None
        hy_oas_zscore_val = _safe_last(hy_oas_zscore(hy_series))
        stress_series = credit_stress_flag(hy_series)
        if not stress_series.empty:
            stress_flag = bool(stress_series.iloc[-1])

    # Composite (renormalizes weights over non-None components).
    composite = composite_score(vol_val, trend_val, breadth_val, credit_val)

    log.info(
        "regime v2: %s as of %s | label=%s composite=%s "
        "vol=%s trend=%s breadth=%s credit=%s stress=%s",
        _BENCHMARK,
        as_of,
        label,
        f"{composite:.3f}" if composite is not None else "—",
        f"{vol_val:.3f}" if vol_val is not None else "—",
        f"{trend_val:.3f}" if trend_val is not None else "—",
        f"{breadth_val:.3f}" if breadth_val is not None else "—",
        f"{credit_val:.3f}" if credit_val is not None else "—",
        stress_flag,
    )

    return upsert_regime(
        as_of,
        label,
        adx=adx_val,
        spy_close=close_val,
        spy_sma200=sma200_val,
        composite_score=composite,
        vol_score=vol_val,
        trend_score=trend_val,
        breadth_score=breadth_val,
        credit_score=credit_val,
        vix_level=vix_level_val,
        vix_pct_rank=vix_pct_rank_val,
        hy_oas_level=hy_oas_level_val,
        hy_oas_pct_rank=hy_oas_pct_rank_val,
        hy_oas_zscore=hy_oas_zscore_val,
        credit_stress_flag=stress_flag,
        methodology_version=_METHODOLOGY_VERSION,
        session=session,
    )
