"""Regime sizing + diversification → the proposed book of N.

Takes the scored candidates from ``engine.propose_candidates`` and:

  * sizes each by the SAME regime overlay the swing runner uses
    (``0.5 + 0.5·composite_score`` × credit-stress mult), with an extra haircut
    on short calls when breadth is weak (don't lean short-upside in a narrow
    tape);
  * enforces diversification — one side per name, a cap per correlated cluster
    (e.g. the CoreWeave names), and a max book size.

Knobs are module constants.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from stockscan.proposals._models import SELL_CALL, OptionProposal

# ---- knobs ----------------------------------------------------------------
MAX_BOOK = 30
MAX_PER_CLUSTER = 2
BREADTH_WEAK_THRESHOLD = 0.40
SHORT_CALL_BREADTH_HAIRCUT = 0.70  # extra size cut for short calls in weak breadth

# Shared-counterparty clusters — correlated bets that shouldn't pack a book.
# v1 is a hand-maintained map; a fundamentals-driven version is a later upgrade.
CLUSTERS: dict[str, set[str]] = {
    "coreweave": {"CORZ", "APLD", "GLXY", "CRWV"},
}


def regime_size_multiplier(regime: Any | None) -> float:
    """Reuse the swing runner's overlay: 0.5 + 0.5·composite × credit-stress."""
    if regime is None:
        return 1.0
    comp = getattr(regime, "composite_score", None)
    comp = float(comp) if comp is not None else None
    composite_mult = 0.5 + 0.5 * comp if comp is not None else 1.0
    stress_mult = 0.5 if getattr(regime, "credit_stress_flag", False) else 1.0
    return composite_mult * stress_mult


def _cluster_of(symbol: str) -> str | None:
    for name, members in CLUSTERS.items():
        if symbol in members:
            return name
    return None


def build_book(
    proposals: list[OptionProposal],
    regime: Any | None = None,
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
    breadth_weak = False
    if regime is not None:
        bs = getattr(regime, "breadth_score", None)
        breadth_weak = bs is not None and float(bs) < BREADTH_WEAK_THRESHOLD

    book: list[OptionProposal] = []
    seen: set[str] = set()
    cluster_counts: dict[str, int] = {}

    for p in proposals:
        if p.score < min_score or p.symbol in seen:
            continue
        cluster = _cluster_of(p.symbol)
        if cluster is not None and cluster_counts.get(cluster, 0) >= MAX_PER_CLUSTER:
            continue
        side_bias = (
            SHORT_CALL_BREADTH_HAIRCUT if (p.side == SELL_CALL and breadth_weak) else 1.0
        )
        book.append(replace(p, size_weight=round(regime_mult * side_bias, 3)))
        seen.add(p.symbol)
        if cluster is not None:
            cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1
        if len(book) >= n:
            break

    return book
