"""Cross-sectional relative strength vs the stock's sector composite.

Signal-scoring spec §4.3. This is the primitive that needs *another symbol's*
bars (the equal-weight sector composite built by ``stockscan.sectors``): it reads
the symbol it is scoring from the bars frame and fetches the composite by symbol.
``get_bars`` is lazy-imported to avoid an import cycle, and the DB layer caches
the repeated composite hits across a scan run, so the per-symbol cost is
negligible.

Sign convention (reversal score): **positive = bottom/bullish.** The thesis is
pure relative *momentum* — buy the relative outperformer (a sector **leader**),
not the laggard expecting catch-up. So a positive reading reinforces a *bottom*
(fade the dip in a resilient leader) and a negative reading reinforces a *top*
(fade the rip in a laggard).

The raw measurement is the 63-day return spread (stock − sector), saturated at
±``band``, blended 70/30 with the slope of the RS line (is relative strength
still improving?). No look-ahead: only the trailing window is read; the pure math
lives in :func:`relative_strength_values` and is unit-tested without a DB.

Strategy-agnostic: the function returns an intrinsic signed read. Earlier this
primitive dampened its contribution for ``mean_reversion`` strategies; that
strategy-intent branch has been removed — a strategy or composite that wants to
down-weight relative strength does so via its own weight, not here.
"""

from __future__ import annotations

from datetime import date

import pandas as pd


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ----------------------------------------------------------------------
# Run-scoped caches. Without these, relative strength does ~2 DB round-trips
# per (symbol, day) — a sector lookup + a composite-bars fetch — which is the
# dominant cost in a multi-year, full-universe backtest (millions of queries).
# The sector map is static per run and there are only ~11 composites, so we
# fetch each exactly once and slice in memory. Call clear_cache() at the start
# of a run (the backtest engine does) or after rebuilding composites.
#
# Second-level cache (added after the v2 backtest profile): the composite's
# close series gets pre-normalized to tz-naive midnight ONCE when first
# fetched. Without this, every scoring call's ``_by_date`` rebuilds the same
# normalized index from scratch — ~50% of the relative-strength wall-clock
# in the 10-symbol × 1-year profile, even with the bytes already in memory.
# ----------------------------------------------------------------------
_SECTOR_MAP: dict[str, str] | None = None        # symbol -> "$EWSECTOR:<CODE>"
_COMPOSITE_BARS: dict[str, pd.DataFrame] = {}     # composite symbol -> full bars
_COMPOSITE_CLOSES_BY_DATE: dict[str, pd.Series] = {}  # composite -> close, index normalized


def clear_cache() -> None:
    """Drop the cached sector map + composite bars (call per run / after refresh)."""
    global _SECTOR_MAP
    _SECTOR_MAP = None
    _COMPOSITE_BARS.clear()
    _COMPOSITE_CLOSES_BY_DATE.clear()


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

    Two levels of caching:
      1. The full bars DataFrame is fetched once per composite per run
         (one DB query per composite, not per (symbol, day)).
      2. The close column is pre-normalized to tz-naive midnight once per
         composite — composites have static indexes per run, so doing this
         once and slicing is strictly cheaper than re-normalizing every
         call inside ``_by_date`` (a hot path: ~50% of relative_strength's
         wall-clock in the profile).

    Slice is via ``searchsorted`` on the (sorted) datetime index — O(log n)
    and returns a view, vs the old ``full.index.date <= as_of`` mask which
    built a full Python ``date`` object array (the ``datetimes.date`` line
    that surfaced as 0.86s / 5587 calls in the profile).
    """
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

    # Build (or fetch) the pre-normalized close series for this composite.
    by_date = _COMPOSITE_CLOSES_BY_DATE.get(composite)
    if by_date is None:
        idx = full.index
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_convert(None)
        by_date = pd.Series(full["close"].to_numpy(), index=idx.normalize())
        _COMPOSITE_CLOSES_BY_DATE[composite] = by_date

    # No-look-ahead slice via searchsorted on the (now tz-naive) midnight index.
    # ``as_of`` is a date; one day after it bounds the half-open right end.
    bound = pd.Timestamp(as_of) + pd.Timedelta(days=1)
    pos = by_date.index.searchsorted(bound, side="left")
    sliced = by_date.iloc[:pos]
    return sliced if not sliced.empty else None


def _symbol_of(bars: pd.DataFrame) -> str | None:
    """The symbol being scored. The runner sets ``bars.attrs['symbol']``; the
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


def relative_strength_values(
    stock_close: pd.Series,
    sec_close: pd.Series,
    *,
    look: int = 63,
    band: float = 0.15,
    slope_window: int = 20,
    slope_band: float = 0.05,
    rs_weight: float = 0.7,
    slope_weight: float = 0.3,
) -> dict[str, float] | None:
    """Pure relative-strength math from two adjusted-close series.

    ``stock_close`` and ``sec_close`` are both indexed by date (the composite
    shares the trading-day calendar). Returns a value dict (including the signed
    ``raw`` = clip(rs_weight·rs + slope_weight·slope_n)), or ``None`` when there
    isn't enough clean data. Causal: only the trailing window is read, so the
    result is the same on a truncated prefix.
    """
    if stock_close is None or sec_close is None:
        return None
    sc = stock_close.dropna()
    if len(sc) <= look:
        return None

    # Normalise both series to tz-naive midnight so alignment is by *calendar
    # date*, not by intraday timestamp. Stock bars from EODHD store bar_ts at
    # NY-close → UTC (e.g. 20–21:00 UTC); sector composites are written at
    # midnight UTC. Without this normalisation, ``reindex`` would find zero
    # matching timestamps even on overlapping dates → ``ffill`` has nothing to
    # propagate → ``sec_on`` is all NaN → silent abstain on every call. This
    # was the dominant reason sector_rs contributed to 0/13 (bt20) and 0/9
    # (bt21) trades despite the composite having thousands of bars.
    #
    # Performance note: ``sec_close`` is normalized once when the composite
    # is first cached (see ``_composite_closes``); only the per-call stock
    # series needs normalization here. The ``getattr`` guard makes this
    # idempotent for callers that pass in an already-normalized series
    # (e.g. the unit tests).
    def _by_date(s: pd.Series) -> pd.Series:
        idx = s.index
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_convert(None)
        return pd.Series(s.values, index=idx.normalize())
    sc = _by_date(sc)
    if getattr(sec_close.index, "tz", None) is not None:
        sec_close = _by_date(sec_close)

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
        "raw": _clip(rs_weight * rs + slope_weight * slope_n),
    }


def sector_relative_strength(
    bars: pd.DataFrame,
    as_of: date,
    *,
    rs_window: int = 63,
    rs_band: float = 0.15,
    slope_window: int = 20,
    slope_band: float = 0.05,
    rs_weight: float = 0.7,
    slope_weight: float = 0.3,
) -> dict[str, float] | None:
    """Resolve the stock's sector composite and return its relative-strength read.

    Returns None (abstains) when the symbol can't be identified, has no sector
    mapping, the composite has no data, or there's insufficient history. The
    composite scorer treats None as "this input abstains".
    """
    if "close" not in bars.columns or len(bars) <= rs_window:
        return None
    symbol = _symbol_of(bars)
    if not symbol:
        return None
    composite = _composite_symbol_for(symbol)
    if not composite:
        return None  # no sector mapping → abstain
    sec_close = _composite_closes(composite, as_of)  # cached full series, sliced ≤ as_of
    if sec_close is None:
        return None
    return relative_strength_values(
        bars["close"],
        sec_close,
        look=rs_window,
        band=rs_band,
        slope_window=slope_window,
        slope_band=slope_band,
        rs_weight=rs_weight,
        slope_weight=slope_weight,
    )
