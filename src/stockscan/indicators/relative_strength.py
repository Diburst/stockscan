"""A stock's return relative to its sector composite.

The one primitive that needs *another symbol's* bars: the equal-weight,
point-in-time sector composite that :mod:`stockscan.sectors` builds and
stores as a synthetic ``$EWSECTOR:<CODE>`` symbol. The symbol being scored
is read from ``bars.attrs["symbol"]`` (set by the runner and the engine).

Why sector-relative: short-term reversal in large caps survives only in its
industry-residual form — the part of a stock's drop that is not the whole
sector dropping (Da, Liu & Schaumburg 2014; Blitz et al. 2023) — and
momentum's crash-prone component is likewise the part shared with the
sector (Blitz, Huij & Martens 2011). Two callers, two lookbacks:

* mean reversion ranks by the **1-month** stock-minus-sector return and
  vetoes names whose sector is itself in a downtrend;
* momentum tilts its ranking by the **12-month** residual.

Both are plain return differences — no bands, no blending.

Run-scoped caches: the sector map is static per run and there are only
~11 composites, so each is fetched once and sliced in memory. The backtest
engine calls :func:`clear_cache` per run; do the same after rebuilding
composites.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

_SECTOR_MAP: dict[str, str] | None = None  # symbol -> "$EWSECTOR:<CODE>"
_COMPOSITE_CLOSES: dict[str, pd.Series] = {}  # composite -> close, tz-naive midnight index


def clear_cache() -> None:
    """Drop the cached sector map + composite closes (per run / after refresh)."""
    global _SECTOR_MAP
    _SECTOR_MAP = None
    _COMPOSITE_CLOSES.clear()


def _composite_symbol_for(symbol: str) -> str | None:
    global _SECTOR_MAP
    if _SECTOR_MAP is None:
        from stockscan.sectors.composite import composite_symbol
        from stockscan.sectors.store import sector_map

        _SECTOR_MAP = {sym: composite_symbol(sec) for sym, sec in sector_map().items()}
    return _SECTOR_MAP.get(symbol)


def _composite_closes(composite: str, as_of: date) -> pd.Series | None:
    """Composite close series sliced to ≤ ``as_of`` (no look-ahead), cached
    per run and pre-normalized to a tz-naive midnight index so the slice is
    one ``searchsorted``."""
    closes = _COMPOSITE_CLOSES.get(composite)
    if closes is None:
        from stockscan.data.store import get_bars

        full = get_bars(composite, start=date(1990, 1, 1), end=date.today())
        if full.empty:
            closes = pd.Series(dtype=float)
        else:
            idx = full.index
            if getattr(idx, "tz", None) is not None:
                idx = idx.tz_convert(None)
            closes = pd.Series(full["close"].to_numpy(dtype=float), index=idx.normalize())
        _COMPOSITE_CLOSES[composite] = closes
    if closes.empty:
        return None
    bound = pd.Timestamp(as_of) + pd.Timedelta(days=1)
    sliced = closes.iloc[: closes.index.searchsorted(bound, side="left")]
    return sliced if not sliced.empty else None


def _symbol_of(bars: pd.DataFrame) -> str | None:
    sym = bars.attrs.get("symbol") if hasattr(bars, "attrs") else None
    if sym:
        return str(sym)
    if "symbol" in getattr(bars, "columns", []):
        return str(bars["symbol"].iloc[-1])
    return None


def _trailing_return(close: pd.Series, lookback: int) -> float | None:
    if len(close) <= lookback:
        return None
    start = float(close.iloc[-1 - lookback])
    end = float(close.iloc[-1])
    if start <= 0 or pd.isna(start) or pd.isna(end):
        return None
    return end / start - 1.0


def sector_return(bars: pd.DataFrame, as_of: date, *, lookback: int) -> float | None:
    """The stock's sector composite return over the last ``lookback`` bars,
    or None when the symbol has no sector or the composite is too short."""
    symbol = _symbol_of(bars)
    if symbol is None:
        return None
    composite = _composite_symbol_for(symbol)
    if composite is None:
        return None
    closes = _composite_closes(composite, as_of)
    if closes is None:
        return None
    return _trailing_return(closes, lookback)


def sector_relative_return(bars: pd.DataFrame, as_of: date, *, lookback: int) -> float | None:
    """Stock return minus sector return over the last ``lookback`` bars
    (``adj_close`` based), or None when either side is unavailable."""
    sector = sector_return(bars, as_of, lookback=lookback)
    if sector is None:
        return None
    stock = _trailing_return(bars["adj_close"].astype(float), lookback)
    if stock is None:
        return None
    return stock - sector
