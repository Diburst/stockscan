"""Options-proposal MCP tool — the ranked short-premium book."""

from __future__ import annotations

from typing import Any

from stockscan.mcp.serialize import jsonable
from stockscan.proposals import generate_book


def propose_options(
    list_id: int | None = None, n: int = 30, min_score: float = 0.0
) -> dict[str, Any]:
    """Propose a ranked, regime-sized, diversified book of short-premium options.

    Reads every watched name's options context and builds a book of short-put /
    short-call candidates: a green day suggests selling a call (only into
    resistance, never a breakout), a red day suggests selling a put (preferred
    with-trend). Drops anything with earnings inside the expiry, thin liquidity,
    or low IV; sizes each by the market regime (smaller in poor breadth / credit
    stress); and caps correlated names. IV is a realized-vol proxy, not live
    implied vol — treat scores as relative, and verify strikes on a live chain.

    Args:
        list_id: Restrict to one watchlist list (see list_watchlists); None = all.
        n: Max book size (default 30).
        min_score: Drop candidates below this 0–1 attractiveness score.

    Returns:
        {"as_of", "regime", "candidates", "book_size", "book": [{symbol, side,
        strike, dte, expiry, credit_per_contract, pct_otm, iv_pct, score,
        size_weight, day_move_pct, days_to_earnings, confluences,
        price_at_level (bool — current price is itself at the support/resistance
        it's selling against; context flag, not scored), rationale}, ...]}.
    """
    run = generate_book(list_id=list_id, n=n, min_score=min_score)
    reg = run.regime
    book = [
        {
            "symbol": p.symbol,
            "side": p.side,
            "strike": p.strike,
            "dte": p.days_to_expiry,
            "expiry": jsonable(p.expiry_date),
            "credit_per_contract": round(p.est_credit * 100, 2),
            "pct_otm": p.pct_otm,
            "iv_pct": p.iv_pct,
            "score": round(p.score, 3),
            "size_weight": p.size_weight,
            "day_move_pct": p.day_move_pct,
            "days_to_earnings": p.days_to_earnings,
            "confluences": p.confluence_count,
            "price_at_level": p.price_at_level,
            "rationale": p.rationale,
        }
        for p in run.book
    ]
    return {
        "as_of": jsonable(run.as_of),
        "regime": (
            None
            if reg is None
            else {"label": reg.regime, "composite": jsonable(reg.composite_score)}
        ),
        "candidates": run.candidates,
        "book_size": len(book),
        "book": book,
    }
