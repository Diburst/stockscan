"""Regime sizing + diversification → the proposed book of N.

Takes the scored candidates from ``engine.propose_candidates`` and:

  * sizes each by the regime layer's vol scalar, halved again under credit
    stress — the book is short premium, so it keeps sizing down under stress
    rather than blocking the way the long-only swing runner does;
  * enforces diversification — one side per name, a cap per correlated cluster
    (e.g. the CoreWeave names), and a max book size.

Knobs are module constants.
"""

from __future__ import annotations

from dataclasses import replace

from stockscan.proposals._models import OptionProposal
from stockscan.regime import MarketRegime

# ---- knobs ----------------------------------------------------------------
MAX_BOOK = 30
MAX_PER_CLUSTER = 2
CREDIT_STRESS_MULT = 0.5

# Shared-counterparty clusters — correlated bets that shouldn't pack a book.
# v1 is a hand-maintained map; a fundamentals-driven version is a later upgrade.
CLUSTERS: dict[str, set[str]] = {
    "coreweave": {"CORZ", "APLD", "GLXY", "CRWV"},
}


def regime_size_multiplier(regime: MarketRegime | None) -> float:
    """The regime layer's vol scalar, halved while credit stress fires."""
    if regime is None:
        return 1.0
    stress_mult = CREDIT_STRESS_MULT if regime.credit_stress_flag else 1.0
    return regime.vol_multiplier * stress_mult


def _cluster_of(symbol: str) -> str | None:
    for name, members in CLUSTERS.items():
        if symbol in members:
            return name
    return None


def build_book(
    proposals: list[OptionProposal],
    regime: MarketRegime | None = None,
    *,
    n: int = MAX_BOOK,
    min_score: float = 0.0,
) -> list[OptionProposal]:
    """Size + diversify the ranked candidates into the proposed book.

    Args:
        proposals: scored candidates, sorted by score desc (from the engine).
        regime: MarketRegime for sizing; None = neutral (×1.0).
        n: max book size.
        min_score: drop candidates below this score.

    Returns:
        The selected proposals with ``size_weight`` filled, ranked.
    """
    regime_mult = regime_size_multiplier(regime)

    book: list[OptionProposal] = []
    seen: set[str] = set()
    cluster_counts: dict[str, int] = {}

    for p in proposals:
        if p.score < min_score or p.symbol in seen:
            continue
        cluster = _cluster_of(p.symbol)
        if cluster is not None and cluster_counts.get(cluster, 0) >= MAX_PER_CLUSTER:
            continue
        book.append(replace(p, size_weight=round(regime_mult, 3)))
        seen.add(p.symbol)
        if cluster is not None:
            cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1
        if len(book) >= n:
            break

    return book
