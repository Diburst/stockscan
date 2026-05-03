"""Orchestrator + bundle for the Index Structure dashboard card.

Public entry point: :func:`compute_index_structure`. Pulls SPY bars
once, dispatches to each sub-state, soft-fails per indicator, returns
a fully-populated :class:`IndexStructureState`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as _date
from typing import TYPE_CHECKING

from stockscan.data.store import get_bars
from stockscan.db import session_scope
from stockscan.structure.adx import AdxState, compute_adx_state
from stockscan.structure.bollinger import (
    BollingerState,
    compute_bollinger_state,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# 2 years of SPY history is plenty for ADX(14), BB(20), and the
# six-month BB-width percentile. Keep this lean — these indicators
# don't need the 25-year window the seasonality stuff does.
_SPY_LOOKBACK_YEARS = 2


@dataclass(frozen=True, slots=True)
class IndexStructureState:
    as_of: _date
    spy_symbol: str
    adx: AdxState
    bollinger: BollingerState
    failures: list[str] = field(default_factory=list)


def compute_index_structure(
    as_of: _date | None = None,
    *,
    session: Session | None = None,
    benchmark_symbol: str = "SPY",
) -> IndexStructureState:
    """Compute every Index Structure indicator. Soft-fails per indicator.

    Parameters
    ----------
    as_of:
        Anchor date. Default = today.
    session:
        Optional caller-managed DB session.
    benchmark_symbol:
        Defaults to SPY. Could in principle be QQQ or another index
        ETF if you want a tech-heavy read instead — same indicators
        apply to any liquid index proxy.
    """
    if as_of is None:
        as_of = _date.today()

    if session is None:
        with session_scope() as s:
            return _compute(as_of, s, benchmark_symbol)
    return _compute(as_of, session, benchmark_symbol)


def _compute(
    as_of: _date,
    session: Session,
    benchmark_symbol: str,
) -> IndexStructureState:
    failures: list[str] = []

    # Pull a clean window of SPY bars. 2 years gives us ADX warmup +
    # six-month BB-width percentile + plenty of margin for missing
    # bars on holidays etc.
    try:
        spy_start = as_of.replace(year=as_of.year - _SPY_LOOKBACK_YEARS)
    except ValueError:
        # Feb 29 on a non-leap year — step to Feb 28.
        spy_start = as_of.replace(
            year=as_of.year - _SPY_LOOKBACK_YEARS, month=2, day=28
        )
    try:
        spy_bars = get_bars(benchmark_symbol, spy_start, as_of, session=session)
    except Exception as exc:
        log.warning("structure: SPY bars fetch failed: %s", exc)
        spy_bars = None
        failures.append("spy_bars")

    adx = _safe(
        failures,
        "adx",
        lambda: compute_adx_state(spy_bars, as_of),
        AdxState.unavailable(),
    )
    bollinger = _safe(
        failures,
        "bollinger",
        lambda: compute_bollinger_state(spy_bars, as_of),
        BollingerState.unavailable(),
    )

    return IndexStructureState(
        as_of=as_of,
        spy_symbol=benchmark_symbol,
        adx=adx,
        bollinger=bollinger,
        failures=failures,
    )


def _safe(failures: list[str], name: str, fn, fallback):
    try:
        return fn()
    except Exception as exc:
        log.warning("structure/%s: failed: %s", name, exc)
        failures.append(name)
        return fallback
