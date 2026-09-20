"""Options-proposal MCP tool — the ranked short-premium book."""

from __future__ import annotations

from typing import Any

from stockscan.mcp.serialize import jsonable
from stockscan.proposals import generate_book


def propose_options(list_id: int | None = None, n: int = 30) -> dict[str, Any]:
    """Propose a ranked, regime-sized, diversified book of short-premium options.

    Reads every watched name's options context and builds a book of short-put /
    short-call candidates. The trigger is today's move in units of the name's
    own daily vol: a sector-residual red day of at least 1σ suggests selling a
    put (never in a strong_down name, never under credit stress, demoted to
    counter-trend while the trend gate is closed); a raw green day of at least
    1σ suggests selling a call (never into a strong_up breakout). Hard filters
    drop earnings inside the expiry (when the date is known — unknown is
    carried as ``earnings_known: false``), HV percentile below 25, 20-day ADV
    below $25M and price below $10. Rows are ranked by
    ``rank_key = |move_sigma| × trend_align``; the book multiplier is the
    regime's vol scalar, halved under credit stress, and ``contracts`` sizes
    each row against live equity. HV is realized vol, not a live chain —
    verify strikes on a chain.

    Args:
        list_id: Restrict to one watchlist list (see list_watchlists); None = all.
        n: Max book size (default 30).

    Returns:
        {"as_of", "regime": {label, trend_gate_open, vol_scalar,
        credit_stress_flag} | None, "book_mult", "macro_events": ["CPI Thu", ...]
        (high-importance US events inside the expiry), "candidates",
        "book_size", "book": [{symbol, side, strike, dte, expiry,
        credit_per_contract, pct_otm, sigma_distance, credit_yield_ann, hv_pct,
        hv_percentile, move_sigma, trend_align, rank_key, contracts,
        day_move_pct, day_move_residual_pct, days_to_earnings, earnings_known,
        trend_bucket, confluences (key EMAs within 0.5×ATR of the strike, a
        displayed fact, not ranked), rationale}, ...]}.
    """
    run = generate_book(list_id=list_id, n=n)
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
            "sigma_distance": p.sigma_distance,
            "credit_yield_ann": p.credit_yield_ann,
            "hv_pct": p.hv_pct,
            "hv_percentile": p.hv_percentile,
            "move_sigma": p.move_sigma,
            "trend_align": p.trend_align,
            "rank_key": p.rank_key,
            "contracts": p.contracts,
            "day_move_pct": p.day_move_pct,
            "day_move_residual_pct": p.day_move_residual_pct,
            "days_to_earnings": p.days_to_earnings,
            "earnings_known": p.earnings_known,
            "trend_bucket": p.trend_bucket,
            "confluences": list(p.confluences),
            "rationale": p.rationale,
        }
        for p in run.book
    ]
    return {
        "as_of": jsonable(run.as_of),
        "regime": (
            None
            if reg is None
            else {
                "label": reg.regime,
                "trend_gate_open": reg.trend_gate_open,
                "vol_scalar": reg.vol_multiplier,
                "credit_stress_flag": reg.credit_stress_flag,
            }
        ),
        "book_mult": run.book_mult,
        "macro_events": run.macro_events,
        "candidates": run.candidates,
        "book_size": len(book),
        "book": book,
    }
