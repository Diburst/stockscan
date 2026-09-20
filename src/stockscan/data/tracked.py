"""The symbols the store keeps daily bars for.

Two sources: every symbol that has ever been in the S&P 500 (the backtest
universe, kept whole so old runs stay survivorship-free) and every symbol
on a watchlist (names outside the index that are analyzed and traded).
Every bars refresh path — the nightly job, Fetch Latest, ``refresh bars``
and ``refresh daily`` — filters the bulk endpoint's output to this set,
so a watched name outside the index is never discarded.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from stockscan.universe import all_known_symbols
from stockscan.watchlist.store import watchlist_symbols


def tracked_symbols(*, session: Session | None = None) -> set[str]:
    return set(all_known_symbols(session=session)) | watchlist_symbols(session=session)
