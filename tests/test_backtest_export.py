"""DB-free coverage for the backtest export shape.

The full export_run() reads from Postgres and is exercised by hand against
real runs. These tests pin the serialisation contract for the parts that take
Python dicts as input — summary statistics, the winners/losers entry-metadata
table, and the helper formatters — so the shape can't drift silently between
refactors.
"""

from __future__ import annotations

from stockscan.backtest.export import (
    _dec,
    _iso,
    _metadata_breakdown,
    _round_or_none,
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
# Trades fixture — three winners, two losers, one open trade. The metadata
# mirrors what momentum_52w_high writes on its signals: a flat dict of
# numeric inputs, one of which may be None.
# ---------------------------------------------------------------------------
def _trade(
    trade_id: int, symbol: str, pnl: float, r: float | None,
    exit_reason: str = "trend_break", holding_days: int | None = 6,
    closeness: float = 0.95, slope_quality: float = 0.7,
    residual: float | None = 0.10, realized_vol: float = 0.30,
    closed: bool = True,
) -> dict:
    return {
        "trade_id": trade_id,
        "symbol": symbol,
        "side": "long",
        "qty": 10,
        "entry_date": "2024-03-01",
        "entry_price": "100",
        "stop_price": "85",
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
            "closeness_52w": closeness,
            "slope_quality": slope_quality,
            "residual_return_12m": residual,
            "realized_vol_1y": realized_vol,
            "review_day": "wednesday",
        },
    }


def _sample_trades() -> list[dict]:
    return [
        _trade(1, "AAA", pnl=200.0, r=2.0, closeness=0.99, slope_quality=0.90, residual=0.30),
        _trade(2, "BBB", pnl=150.0, r=1.5, closeness=0.97, slope_quality=0.80, residual=0.20),
        _trade(3, "CCC", pnl=50.0, r=0.5, closeness=0.95, slope_quality=0.70, residual=None),
        _trade(4, "DDD", pnl=-100.0, r=-1.0, exit_reason="stop_loss",
               closeness=0.91, slope_quality=0.40, residual=-0.10),
        _trade(5, "EEE", pnl=-30.0, r=-0.3, exit_reason="left_the_set", holding_days=20,
               closeness=0.92, slope_quality=0.50, residual=0.00),
        _trade(6, "FFF", pnl=0.0, r=None, exit_reason=None, closed=False),
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
    assert mix.get("trend_break") == 3
    assert mix.get("stop_loss") == 1
    assert mix.get("left_the_set") == 1


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


def test_summary_stats_carries_metadata_breakdown():
    s = _summary_stats(_sample_trades())
    assert set(s["entry_metadata"]) == {
        "closeness_52w", "slope_quality", "residual_return_12m", "realized_vol_1y",
    }


# ---------------------------------------------------------------------------
# Entry-metadata averages (winners vs losers)
# ---------------------------------------------------------------------------
def _split(trades: list[dict]) -> tuple[list[dict], list[dict]]:
    closed = [t for t in trades if t["exit_date"]]
    winners = [t for t in closed if float(t["realized_pnl"]) > 0]
    losers = [t for t in closed if float(t["realized_pnl"]) <= 0]
    return winners, losers


def test_metadata_breakdown_separates_winners_and_losers():
    out = _metadata_breakdown(*_split(_sample_trades()))

    assert out["slope_quality"]["winners_n"] == 3
    assert out["slope_quality"]["losers_n"] == 2
    assert out["slope_quality"]["winners_mean"] == 0.8
    assert out["slope_quality"]["losers_mean"] == 0.45
    assert out["slope_quality"]["delta"] == 0.35


def test_metadata_breakdown_skips_none_and_non_numeric():
    out = _metadata_breakdown(*_split(_sample_trades()))

    # CCC's residual is None → only two winners contribute.
    assert out["residual_return_12m"]["winners_n"] == 2
    assert out["residual_return_12m"]["winners_mean"] == 0.25
    # A string-valued key is not averaged at all.
    assert "review_day" not in out


def test_metadata_breakdown_handles_missing_side():
    winners, _ = _split(_sample_trades())
    out = _metadata_breakdown(winners, [])
    assert out["closeness_52w"]["losers_n"] == 0
    assert out["closeness_52w"]["losers_mean"] is None
    assert out["closeness_52w"]["delta"] is None
    assert _metadata_breakdown([], []) == {}


def test_trade_capsule_carries_entry_metadata():
    t = _sample_trades()[0]
    cap = _trade_capsule(t)
    assert cap["symbol"] == "AAA"
    assert cap["entry_metadata"]["closeness_52w"] == 0.99
