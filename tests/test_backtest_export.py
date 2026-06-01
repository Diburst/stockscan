"""DB-free coverage for the backtest export shape.

The full export_run() reads from Postgres and is exercised by hand against
real runs. These tests pin the serialisation contract for the parts that take
Python dicts as input — summary statistics, the winners/losers per-input
contribution table, and the helper formatters — so the shape can't drift
silently between refactors.
"""

from __future__ import annotations

from stockscan.backtest.export import (
    _dec,
    _iso,
    _round_or_none,
    _score_input_breakdown,
    _summary_stats,
    _trade_capsule,
)


# ---------------------------------------------------------------------------
# Tiny formatter helpers
# ---------------------------------------------------------------------------
def test_dec_renders_decimal_losslessly():
    from decimal import Decimal
    assert _dec(Decimal("100.1234")) == "100.1234"
    assert _dec(None) is None


def test_iso_handles_date_and_datetime():
    from datetime import date, datetime, timezone
    assert _iso(date(2024, 3, 4)) == "2024-03-04"
    s = _iso(datetime(2024, 3, 4, 16, 0, tzinfo=timezone.utc))
    assert s is not None and s.startswith("2024-03-04T16:00:00")
    assert _iso(None) is None


def test_round_or_none_tolerates_strings_and_none():
    assert _round_or_none("1.23456", 2) == 1.23
    assert _round_or_none(None, 4) is None
    assert _round_or_none("not a number", 2) is None


# ---------------------------------------------------------------------------
# Trades fixture — three winners, two losers, one open trade
# ---------------------------------------------------------------------------
def _trade(
    trade_id: int, symbol: str, pnl: float, r: float | None,
    exit_reason: str = "reversal_top", holding_days: int | None = 6,
    score: float | None = 0.40, breakdown_score: float = 0.40,
    rev_trig: float = 0.9, piv: float = 0.4, sec: float = 0.2,
    trend: float = 0.3, vol_mult: float = 0.9,
    closed: bool = True,
) -> dict:
    return {
        "trade_id": trade_id,
        "symbol": symbol,
        "side": "long",
        "qty": 10,
        "entry_date": "2024-03-01",
        "entry_price": "100",
        "stop_price": "95",
        "exit_date": "2024-03-08" if closed else None,
        "exit_price": "108" if closed else None,
        "exit_reason": exit_reason if closed else None,
        "commission": "0",
        "slippage": "0",
        "realized_pnl": str(pnl) if closed else None,
        "return_pct": "0.08" if closed else None,
        "r_multiple": str(r) if r is not None else None,
        "holding_days": holding_days if closed else None,
        "mfe_pct": "0.10",
        "mae_pct": "-0.02",
        "entry_metadata": {
            "reversal_score": score,
            "D": breakdown_score, "C": vol_mult,
            "score_breakdown": {
                "reversal_trigger": {"score": rev_trig, "weight": 0.35},
                "pivot_proximity":  {"score": piv,      "weight": 0.30},
                "sector_rs":        {"score": sec,      "weight": 0.20},
                "trend_location":   {"score": trend,    "weight": 0.15},
                "volume_confirm":   {"multiplier": vol_mult},
                "_meta": {"D": breakdown_score, "C": vol_mult, "score": breakdown_score * vol_mult,
                          "methodology_version": 2},
            },
        },
    }


def _sample_trades() -> list[dict]:
    return [
        _trade(1, "AAA",  pnl= 200.0, r= 2.0, score=0.55,  rev_trig=0.95, piv=0.50, sec=0.30),
        _trade(2, "BBB",  pnl= 150.0, r= 1.5, score=0.45,  rev_trig=0.90, piv=0.45, sec=0.20),
        _trade(3, "CCC",  pnl=  50.0, r= 0.5, score=0.32,  rev_trig=0.85, piv=0.20, sec=0.10),
        _trade(4, "DDD",  pnl=-100.0, r=-1.0, score=0.28,
               exit_reason="hard_stop",  rev_trig=0.60, piv=-0.10, sec=-0.20),
        _trade(5, "EEE",  pnl= -30.0, r=-0.3, score=0.30,
               exit_reason="time_stop", holding_days=20,
               rev_trig=0.70, piv=0.10, sec=0.00),
        _trade(6, "FFF",  pnl=  0.0,  r=None,
               exit_reason=None, closed=False),
    ]


# ---------------------------------------------------------------------------
# Summary stats
# ---------------------------------------------------------------------------
def test_summary_stats_counts_and_win_rate():
    s = _summary_stats(_sample_trades())
    assert s["n_trades"] == 6
    assert s["n_closed"] == 5
    assert s["n_winners"] == 3
    assert s["n_losers"] == 2
    assert s["win_rate"] == 0.6


def test_summary_stats_exit_reason_mix():
    s = _summary_stats(_sample_trades())
    mix = s["exit_reason_mix"]
    assert mix.get("reversal_top") == 3
    assert mix.get("hard_stop") == 1
    assert mix.get("time_stop") == 1


def test_summary_stats_r_multiple_distribution():
    s = _summary_stats(_sample_trades())
    r = s["r_multiple"]
    assert r["min"] == -1.0
    assert r["max"] == 2.0
    assert r["median"] == 0.5
    # Mean over [2.0, 1.5, 0.5, -1.0, -0.3] = 0.54
    assert r["mean"] == 0.54


def test_summary_stats_best_and_worst_trade():
    s = _summary_stats(_sample_trades())
    assert s["best_trade"]["symbol"] == "AAA" and s["best_trade"]["r_multiple"] == "2.0"
    assert s["worst_trade"]["symbol"] == "DDD" and s["worst_trade"]["r_multiple"] == "-1.0"


def test_summary_stats_empty_list_doesnt_crash():
    assert _summary_stats([]) == {"n_trades": 0}


# ---------------------------------------------------------------------------
# Score-input contribution (winners vs losers)
# ---------------------------------------------------------------------------
def test_score_input_breakdown_separates_winners_and_losers():
    trades = _sample_trades()
    closed = [t for t in trades if t["exit_date"]]
    winners = [t for t in closed if float(t["realized_pnl"]) > 0]
    losers  = [t for t in closed if float(t["realized_pnl"]) <= 0]
    out = _score_input_breakdown(winners, losers)

    # reversal_trigger should be HIGHER on winners than losers in the fixture
    assert out["reversal_trigger"]["winners_n"] == 3
    assert out["reversal_trigger"]["losers_n"] == 2
    assert out["reversal_trigger"]["winners_mean"] > out["reversal_trigger"]["losers_mean"]
    # delta should be positive (winners > losers) and the sign should match
    assert out["reversal_trigger"]["delta"] > 0

    # sector_rs should also separate (winners +mean, losers near zero / negative)
    assert out["sector_rs"]["winners_mean"] > out["sector_rs"]["losers_mean"]


def test_trade_capsule_carries_score_from_metadata():
    t = _sample_trades()[0]
    cap = _trade_capsule(t)
    assert cap["symbol"] == "AAA"
    assert cap["score"] == 0.55
