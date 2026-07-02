"""Pure math for equal-weight, daily-rebalanced total-return sector composites.

No DB, no I/O, no look-ahead. Every function here is a stateless transform from
pandas inputs to pandas outputs, so the store layer (and the tests) can replay
them over historical data without touching Postgres or the providers — the same
discipline as :mod:`stockscan.regime.composite`. **Keep this module free of any
DB / config imports** so its tests run without infrastructure.

**No-look-ahead is the load-bearing invariant.** A sector's level at date ``t``
is a function only of constituent prices at ``t`` and ``t-1`` and of index
membership as of ``t``. Recomputing on a truncated prefix ``≤ t`` must reproduce
the live value at ``t`` — there is a truncation-invariance property test in
``tests/test_sector_composite.py`` and reviewers should treat it as the canonical
safety net for this module (mirrors ``tests/test_regime_composite.py``).

Method (spec §8.1):

    ret(t, S)   = equal-weight mean over members(t, S) of
                  (adj_close[m, t] / adj_close[m, t-1] - 1)
    level(t, S) = level(t-1, S) * (1 + ret(t, S)),   base 100 at the series start

Equal-weight (not cap-weight) is deliberate: it represents the *median peer*,
which is the right benchmark for relative strength, and it avoids importing the
latest-snapshot market-cap look-ahead documented for fundamentals.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

import pandas as pd

# Reserved synthetic-symbol prefix for composites written into the bars table.
# The '$' keeps them out of the scan universe (which is driven by
# universe_history, never "all symbols in bars").
COMPOSITE_PREFIX = "$EWSECTOR:"

DEFAULT_BASE = 100.0
DEFAULT_MIN_MEMBERS = 1  # production callers pass a higher floor (see store.py)


def sector_code(sector_name: str) -> str:
    """Deterministic, stable code for a sector name, used in the synthetic
    composite symbol ``$EWSECTOR:<code>``.

    Slugify: uppercase, collapse runs of non-alphanumerics to a single
    underscore, trim. e.g. ``"Financial Services"`` → ``"FINANCIAL_SERVICES"``.
    The same slug is used both when *building* a composite and when resolving a
    stock → its composite symbol, so they always agree.
    """
    return re.sub(r"[^0-9A-Za-z]+", "_", sector_name.strip()).strip("_").upper()


def composite_symbol(sector: str, *, is_code: bool = False) -> str:
    """``$EWSECTOR:<CODE>`` for a sector name (or an already-computed code)."""
    code = sector if is_code else sector_code(sector)
    return f"{COMPOSITE_PREFIX}{code}"


def daily_returns(closes: pd.DataFrame) -> pd.DataFrame:
    """Per-symbol 1-day simple returns from a wide adjusted-close frame.

    ``closes``: index = sorted ``DatetimeIndex`` of trading dates, columns =
    symbols, values = adjusted close (``NaN`` where the symbol has no bar that
    day). ``fill_method=None`` means a return is ``NaN`` whenever *either*
    endpoint is missing, so a symbol with a data gap is cleanly excluded that
    day rather than booking a multi-day jump as a single-day return.
    """
    return closes.sort_index().pct_change(fill_method=None)


def chain_to_levels(returns: pd.Series, *, base: float = DEFAULT_BASE) -> pd.Series:
    """Compound a daily-return series into an index level, ``base`` at day one.

    ``NaN`` returns (no data, or too few members that day) are treated as 0
    (flat), so the level carries forward across gaps without a discontinuity.
    Causal: ``level_t`` depends only on returns ``≤ t`` (``cumprod`` of a prefix
    equals the prefix of the full ``cumprod``), which is what makes the whole
    pipeline truncation-invariant.
    """
    if returns.empty:
        return returns.astype(float)
    r = returns.fillna(0.0)
    return (base * (1.0 + r).cumprod()).rename(returns.name)


def weighted_composite(
    closes: pd.DataFrame,
    weights: pd.DataFrame | None = None,
    *,
    base: float = DEFAULT_BASE,
    min_members: int = DEFAULT_MIN_MEMBERS,
) -> pd.Series:
    """Collapse a wide adjusted-close frame into a single base-``base`` index level.

    This is the generic engine behind the watchlist composites (the sector
    builder above is the equal-weight, per-sector specialization). Same causal,
    no-look-ahead discipline: ``level_t`` depends only on prices ``≤ t``.

    ``weights=None`` → **equal weight**: each day's index return is the mean of
    its members' 1-day returns (NaN-skipped), so it rebalances to equal weights
    daily — the "median peer" benchmark.

    ``weights`` given (a frame aligned to ``closes``: per-symbol, per-day weight,
    e.g. market cap = raw_close x shares) → **weighted**: each day's return is the
    weight-weighted mean of member returns, using *prior-day* weights
    (start-of-period, the standard convention) renormalized over the members that
    have a valid return AND a positive weight that day. A name with a data gap or
    missing weight is cleanly dropped from that day's average rather than
    distorting it.

    A day with fewer than ``min_members`` valid members yields ``NaN`` and the
    level carries flat across it (no discontinuity).

    Fully vectorized (no per-cell pandas loops — see orientation principle #6).
    """
    closes = closes.sort_index()
    returns = daily_returns(closes)
    if returns.shape[1] == 0:
        return pd.Series(dtype="float64")

    if weights is None:
        valid = returns.notna()
        count = valid.sum(axis=1)
        daily = returns.mean(axis=1)  # skipna mean over available members
    else:
        # Prior-day weights aligned to today's return (start-of-period weighting).
        w = weights.reindex(index=closes.index, columns=closes.columns).shift(1)
        mask = returns.notna() & w.notna() & (w > 0)
        w_masked = w.where(mask)
        r_masked = returns.where(mask)
        w_sum = w_masked.sum(axis=1)
        daily = (w_masked * r_masked).sum(axis=1) / w_sum
        daily = daily.where(w_sum > 0)
        count = mask.sum(axis=1)

    daily = daily.where(count >= min_members)
    return chain_to_levels(daily.rename(None), base=base)


def sector_daily_returns(
    returns: pd.DataFrame,
    sector_of: Mapping[str, str],
    *,
    membership: pd.DataFrame | None = None,
    min_members: int = DEFAULT_MIN_MEMBERS,
    use_code: bool = True,
) -> pd.DataFrame:
    """Equal-weight daily return per sector. Columns = sector codes.

    ``sector_of`` maps symbol → sector name. ``membership`` (optional) is a
    boolean frame aligned to ``returns`` (``True`` where the symbol is an index
    member that day); when given, non-member cells are masked out before
    averaging, which is what makes the composite point-in-time correct. A day
    with fewer than ``min_members`` valid members yields ``NaN`` (the level
    stays flat that day rather than being defined by one or two names).
    """
    groups: dict[str, list[str]] = {}
    for sym in returns.columns:
        name = sector_of.get(sym)
        if not name:
            continue
        code = sector_code(name) if use_code else name
        groups.setdefault(code, []).append(sym)

    out: dict[str, pd.Series] = {}
    for code, cols in groups.items():
        sub = returns[cols]
        if membership is not None:
            mask = membership.reindex(index=returns.index, columns=cols).fillna(False)
            sub = sub.where(mask)
        count = sub.notna().sum(axis=1)
        daily = sub.mean(axis=1)  # skipna=True: mean over available members only
        out[code] = daily.where(count >= min_members)

    if not out:
        return pd.DataFrame(index=returns.index)
    return pd.DataFrame(out, index=returns.index).sort_index(axis=1)


def build_sector_composites(
    closes: pd.DataFrame,
    sector_of: Mapping[str, str],
    *,
    membership: pd.DataFrame | None = None,
    base: float = DEFAULT_BASE,
    min_members: int = DEFAULT_MIN_MEMBERS,
) -> dict[str, pd.Series]:
    """Wide adjusted-close frame + sector map (+ optional point-in-time
    membership mask) → ``{sector_code: level series}``.

    Pure, causal, no look-ahead. This is the function the store layer feeds
    DB-loaded data into and the tests exercise directly.
    """
    returns = daily_returns(closes)
    sec_ret = sector_daily_returns(
        returns, sector_of, membership=membership, min_members=min_members
    )
    return {code: chain_to_levels(sec_ret[code], base=base) for code in sec_ret.columns}
