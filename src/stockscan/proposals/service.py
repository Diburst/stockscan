"""The single entry point that assembles a proposed book.

Wires the per-symbol analysis + market regime into the engine and portfolio
constructor. MCP, CLI, and the web route all call ``generate_book`` so the
pipeline has one home.
"""

from __future__ import annotations

from datetime import date as _date
from typing import Any, NamedTuple

from stockscan.analysis import analyze_watchlist
from stockscan.proposals._models import OptionProposal
from stockscan.proposals.engine import propose_candidates
from stockscan.proposals.portfolio import MAX_BOOK, build_book
from stockscan.regime import get_regime


class ProposalRun(NamedTuple):
    as_of: _date
    regime: Any | None
    candidates: int  # how many cleared filters/triggers before diversification
    book: list[OptionProposal]


def generate_book(
    *,
    list_id: int | None = None,
    n: int = MAX_BOOK,
    min_score: float = 0.0,
    as_of: _date | None = None,
    session: Any | None = None,
) -> ProposalRun:
    """Run the full proposal pipeline and return the sized, diversified book.

    Args:
        list_id: Restrict to one watchlist list; None = all watched symbols.
        n: Max book size.
        min_score: Drop candidates below this attractiveness score.
        as_of: Analysis date; default today.
        session: Optional DB session (passed through to analysis/regime).

    Returns:
        ProposalRun(as_of, regime, candidates, book).
    """
    as_of = as_of or _date.today()
    analyses = analyze_watchlist(as_of=as_of, list_id=list_id, session=session)
    regime = get_regime(as_of, session=session)
    candidates = propose_candidates(analyses)
    book = build_book(candidates, regime, n=n, min_score=min_score)
    return ProposalRun(as_of=as_of, regime=regime, candidates=len(candidates), book=book)
