"""Cross-sectional relative strength vs the stock's sector composite.

Signal-scoring spec §4.3. This is the first technical indicator that needs
*another symbol's* bars (the equal-weight sector composite built by
``stockscan.sectors``), so it reads the symbol it's scoring from the bars frame
and fetches the composite by symbol — exactly the benchmark-fetch pattern
``donchian_trend._relative_strength`` already uses (``get_bars`` is lazy-imported
to avoid an import cycle, and the DB layer caches the repeated composite hits
across a scan run, so the per-symbol cost is negligible).

Sign convention (reversal score): **positive = bottom/bullish.** The thesis is
pure relative *momentum* — buy the relative outperformer (a sector **leader**),
not the laggard expecting catch-up. So a positive reading reinforces a *bottom*
(fade the dip in a resilient leader) and a negative reading reinforces a *top*
(fade the rip in a laggard). For mean-reversion strategies the contribution is
dampened (it's a quality tilt, not a trigger); see ``score``.

The raw measurement is the 63-day return spread (stock − sector), saturated at
±``rs_band``, blended 70/30 with the slope of the RS line (is relative strength
still improving?). No look-ahead: only the trailing window is read; the pure
math lives in :func:`_rs_values` and is unit-tested without a DB.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pandas as pd
from pydantic import Field

from stockscan.technical.indicators.base import (
    TechnicalIndicator,
    TechnicalIndicatorParams,
)

if TYPE_CHECKING:
    from stockscan.strategies.base import Strategy


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ----------------------------------------------------------------------
# Run-scoped caches. Without these, sector_rs does ~2 DB round-trips per
# (symbol, day) — a sector lookup + a composite-bars fetch — which is the
# dominant cost in a multi-year, full-universe backtest (millions of queries).
# The sector map is static per run and there are only ~11 composites, so we
# fetch each exactly once and slice in memory. Call clear_cache() at the start
# of a run (the backtest engine does) or after rebuilding composites.
# ----------------------------------------------------------------------
_SECTOR_MAP: dict[str, str] | None = None       # symbol -> "$EWSECTOR:<CODE>"
_COMPOSITE_BARS: dict[str, pd.DataFrame] = {}    # composite symbol -> full bars


def clear_cache() -> None:
    """Drop the cached sector map + composite bars (call per run / after refresh)."""
    global _SECTOR_MAP
    _SECTOR_MAP = None
    _COMPOSITE_BARS.clear()


def _composite_symbol_for(symbol: str) -> str | None:
    """Cached symbol → composite-symbol lookup (the sector map is static per run)."""
    global _SECTOR_MAP
    if _SECTOR_MAP is None:
        try:
            from stockscan.sectors.composite import composite_symbol
            from stockscan.sectors.store import sector_map

            _SECTOR_MAP = {sym: composite_symbol(sec) for sym, sec in sector_map().items()}
        except Exception:
            _SECTOR_MAP = {}
    return _SECTOR_MAP.get(symbol)


def _composite_closes(composite: str, as_of: date) -> pd.Series | None:
    """Cached composite close series sliced to ≤ as_of (no look-ahead).

    Fetches each composite's full series exactly once per run, then slices in
    memory — one DB query per composite instead of one per (symbol, day)."""
    full = _COMPOSITE_BARS.get(composite)
    if full is None:
        try:
            from stockscan.data.store import get_bars

            full = get_bars(composite, start=date(1990, 1, 1), end=date.today())
        except Exception:
            full = pd.DataFrame()
        _COMPOSITE_BARS[composite] = full
    if full is None or full.empty or "close" not in full.columns:
        return None
    sliced = full.loc[full.index.date <= as_of, "close"]
    return sliced if not sliced.empty else None


def _rs_values(
    stock_close: pd.Series,
    sec_close: pd.Series,
    *,
    look: int,
    band: float,
    slope_window: int,
    slope_band: float,
) -> dict[str, float] | None:
    """Pure relative-strength math from two adjusted-close series.

    ``stock_close`` and ``sec_close`` are both indexed by date (the composite
    shares the trading-day calendar). Returns the value dict, or ``None`` when
    there isn't enough clean data. Causal: only the trailing window is read, so
    the result is the same on a truncated prefix.
    """
    if stock_close is None or sec_close is None:
        return None
    sc = stock_close.dropna()
    if len(sc) <= look:
        return None

    # Align the composite onto the stock's dates (shared calendar); forward-fill
    # tolerates a slightly-stale composite without inventing future data.
    sec_on = sec_close.reindex(sc.index).ffill()
    sec_now = sec_on.iloc[-1]
    sec_then = sec_on.iloc[-1 - look]
    if pd.isna(sec_now) or pd.isna(sec_then):
        return None

    stock_now = float(sc.iloc[-1])
    stock_then = float(sc.iloc[-1 - look])
    sec_now = float(sec_now)
    sec_then = float(sec_then)
    if stock_then <= 0 or sec_then <= 0:
        return None

    stock_ret = stock_now / stock_then - 1.0
    sec_ret = sec_now / sec_then - 1.0
    spread = stock_ret - sec_ret
    rs = _clip(spread / band)

    # RS-line slope: is relative strength still improving? (acceleration, not level)
    slope_n = 0.0
    rs_line = (sc / sec_on).dropna()
    if len(rs_line) >= slope_window * 2:
        rs_sma = rs_line.rolling(slope_window).mean()
        a = rs_sma.iloc[-1]
        b = rs_sma.iloc[-1 - slope_window]
        if pd.notna(a) and pd.notna(b) and b != 0:
            slope_n = _clip((a / b - 1.0) / slope_band)

    return {
        "stock_ret": stock_ret,
        "sector_ret": sec_ret,
        "spread": spread,
        "rs": rs,
        "slope_n": slope_n,
    }


class SectorRSParams(TechnicalIndicatorParams):
    rs_window: int = Field(63, ge=5, le=252, description="Return-spread lookback (trading days).")
    rs_band: float = Field(0.15, gt=0, description="Spread that saturates rs to ±1.")
    slope_window: int = Field(20, ge=2, le=100, description="RS-line SMA + slope window.")
    slope_band: float = Field(0.05, gt=0, description="RS-line slope that saturates to ±1.")
    rs_weight: float = Field(0.7, ge=0, le=1, description="Weight on the spread level.")
    slope_weight: float = Field(0.3, ge=0, le=1, description="Weight on the RS-line slope.")
    mr_dampen: float = Field(
        0.6, ge=0, le=1, description="Dampening for mean-reversion tags (context, not trigger)."
    )
    fetch_buffer_days: int = Field(
        45, ge=10, description="Extra calendar days padded onto the composite fetch."
    )


class TechnicalSectorRS(TechnicalIndicator):
    name = "sector_rs"
    description = (
        "Cross-sectional relative strength vs the stock's equal-weight sector "
        "composite. Positive = relative leader (reinforces a bottom); negative = "
        "relative laggard (reinforces a top). Dampened for mean-reversion tags."
    )
    params_model = SectorRSParams
    weight = 0.20  # context tilt in the v2 reversal composite (spec §6)

    # ------------------------------------------------------------------
    def values(self, bars: pd.DataFrame, as_of: date) -> dict[str, float] | None:
        p: SectorRSParams = self.params  # type: ignore[assignment]
        if "close" not in bars.columns or len(bars) <= p.rs_window:
            return None

        symbol = self._symbol_of(bars)
        if not symbol:
            return None

        composite = _composite_symbol_for(symbol)
        if not composite:
            return None  # no sector mapping → abstain (composite scorer skips us)

        sec_close = _composite_closes(composite, as_of)  # cached full series, sliced ≤ as_of
        if sec_close is None:
            return None

        return _rs_values(
            bars["close"],
            sec_close,
            look=p.rs_window,
            band=p.rs_band,
            slope_window=p.slope_window,
            slope_band=p.slope_band,
        )

    # ------------------------------------------------------------------
    def score(self, values: dict[str, float], strategy: type[Strategy] | None) -> float:
        p: SectorRSParams = self.params  # type: ignore[assignment]
        raw = self.clamp(p.rs_weight * values["rs"] + p.slope_weight * values["slope_n"])

        if strategy is None:
            return raw  # neutral / watchlist: full signed RS

        tags = set(getattr(strategy, "tags", ()))
        if tags & {"trend_following", "breakout", "momentum"}:
            return raw  # leading the sector confirms a continuation/breakout long
        if "mean_reversion" in tags:
            # Reversal context tilt, not a trigger: same sign, dampened. Favors
            # fading dips in relative leaders (+) and rips in laggards (−).
            return self.clamp(p.mr_dampen * raw)
        return raw

    # ------------------------------------------------------------------
    @staticmethod
    def _symbol_of(bars: pd.DataFrame) -> str | None:
        """The symbol being scored. Runner sets ``bars.attrs['symbol']``; the
        watchlist passes a raw ``get_bars`` frame that still carries a ``symbol``
        column — support both."""
        sym = bars.attrs.get("symbol") if hasattr(bars, "attrs") else None
        if sym:
            return str(sym)
        if "symbol" in getattr(bars, "columns", []):
            try:
                return str(bars["symbol"].iloc[-1])
            except Exception:
                return None
        return None
