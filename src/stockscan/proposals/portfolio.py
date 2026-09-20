"""Regime sizing + diversification → the proposed book of N.

Takes the ranked candidates from ``engine.propose_candidates`` and:

  * sets the BOOK MULTIPLIER from the regime layer — the vol scalar, halved
    again under credit stress (the engine already skipped every put-sale
    under stress, so the halving lands on call-sales). The book is short
    premium: it sizes down under stress rather than blocking the way the
    long-only swing runner does;
  * sizes each row in CONTRACTS against live equity —
    ``floor(equity × OPTIONS_RISK_PCT × book_mult / (strike × 100 × 2 × σ_tenor))``
    with ``σ_tenor = hv × √(dte/252)``: risk OPTIONS_RISK_PCT of equity to a
    two-sigma move of the tenor on the cash-secured notional;
  * enforces DIVERSIFICATION — one side per name, at most MAX_PER_SECTOR per
    sector (the same map the runner's sector cap reads), at most
    MAX_PER_CLUSTER per hand-maintained cross-sector cluster (the CoreWeave
    names the sector map cannot see), and a max book size.

Knobs are module constants.
"""

from __future__ import annotations

from dataclasses import replace
from math import floor, sqrt

from stockscan.proposals._models import OptionProposal
from stockscan.regime import MarketRegime

# ---- knobs ----------------------------------------------------------------
MAX_BOOK = 30
MAX_PER_SECTOR = 2
MAX_PER_CLUSTER = 2
CREDIT_STRESS_MULT = 0.5      # call-sales only; the engine skips put-sales under stress
OPTIONS_RISK_PCT = 0.005      # of equity per trade, against a 2σ tenor move

# Cross-sector clusters the sector map cannot see — correlated bets that
# shouldn't pack a book.
CLUSTERS: dict[str, set[str]] = {
    "coreweave": {"CORZ", "APLD", "GLXY", "CRWV"},
}


def book_multiplier(regime: MarketRegime | None) -> float:
    """The regime layer's vol scalar, halved while credit stress fires."""
    if regime is None:
        return 1.0
    stress_mult = CREDIT_STRESS_MULT if regime.credit_stress_flag else 1.0
    return regime.vol_multiplier * stress_mult


def contracts_for(p: OptionProposal, *, equity: float, book_mult: float) -> int | None:
    """Per-trade size in contracts, or None when the tenor's σ cannot be formed."""
    if p.days_to_expiry <= 0 or p.hv_pct <= 0 or p.strike <= 0:
        return None
    sigma_tenor = p.hv_pct / 100.0 * sqrt(p.days_to_expiry / 252.0)
    risk_dollars = equity * OPTIONS_RISK_PCT * book_mult
    return floor(risk_dollars / (p.strike * 100.0 * 2.0 * sigma_tenor))


def _cluster_of(symbol: str) -> str | None:
    for name, members in CLUSTERS.items():
        if symbol in members:
            return name
    return None


def build_book(
    proposals: list[OptionProposal],
    regime: MarketRegime | None = None,
    *,
    equity: float,
    sectors: dict[str, str] | None = None,
    n: int = MAX_BOOK,
) -> list[OptionProposal]:
    """Size + diversify the ranked candidates into the proposed book.

    Args:
        proposals: ranked candidates (from the engine), best first.
        regime: MarketRegime for the book multiplier; None = neutral (×1.0).
        equity: live account equity the contracts are sized against.
        sectors: ``{symbol: sector}`` for the per-sector cap; None = no cap.
        n: max book size.

    Returns:
        The selected proposals with ``size_weight`` and ``contracts`` filled, ranked.
    """
    book_mult = round(book_multiplier(regime), 3)
    sectors = sectors or {}

    book: list[OptionProposal] = []
    seen: set[str] = set()
    sector_counts: dict[str, int] = {}
    cluster_counts: dict[str, int] = {}

    for p in proposals:
        if p.symbol in seen:
            continue
        sector = sectors.get(p.symbol)
        if sector is not None and sector_counts.get(sector, 0) >= MAX_PER_SECTOR:
            continue
        cluster = _cluster_of(p.symbol)
        if cluster is not None and cluster_counts.get(cluster, 0) >= MAX_PER_CLUSTER:
            continue
        book.append(
            replace(
                p,
                size_weight=book_mult,
                contracts=contracts_for(p, equity=equity, book_mult=book_mult),
            )
        )
        seen.add(p.symbol)
        if sector is not None:
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
        if cluster is not None:
            cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1
        if len(book) >= n:
            break

    return book
