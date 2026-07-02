"""The delta-hedge MCP tools are registered read-only and shape data correctly.

These avoid the DB (Postgres) and fastmcp by testing the registration tuples,
the pure error/validation paths, and the service helpers against a hand-built
HedgePosition — the same split the rest of the hedge unit tests use.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from stockscan.hedge import service
from stockscan.hedge.store import HedgePosition
from stockscan.mcp import server
from stockscan.mcp.tools import hedge as t_hedge


def _position(**overrides) -> HedgePosition:
    """A short-call hedge position, ATM by default, with sensible fields."""
    base = dict(
        hedge_position_id=1,
        symbol="MU",
        option_kind="call",
        option_side="short",
        strike=Decimal("1300"),
        contracts=1,
        multiplier=100,
        expiry=datetime.now(UTC) + timedelta(days=30),
        premium=Decimal("2500"),
        iv_pct=Decimal("30"),
        rate_pct=Decimal("4"),
        band_policy={"mode": "whalley_wilmott"},
        status="active",
        held_shares=0,
        avg_cost=Decimal("0"),
        realized_hedge_pnl=Decimal("0"),
        last_spot=Decimal("1300"),
        last_delta=None,
        last_target_shares=None,
        last_hedge_spot=None,
        last_tick_at=None,
        iv_refreshed_on=None,
        created_at=datetime.now(UTC),
        closed_at=None,
        close_reason=None,
        settlement_spot=None,
        realized_pnl=None,
        notes=None,
    )
    base.update(overrides)
    return HedgePosition(**base)


# ---- registration invariants ----
def test_hedge_tools_registered_read_only():
    read_names = {fn.__name__ for fn in server.READ_TOOLS}
    assert {
        "list_hedges", "get_hedge", "get_hedge_adjustments", "hedge_daemon_status",
        "simulate_hedge", "hedge_monte_carlo", "hedge_sweep",
    } <= read_names


def test_no_hedge_write_tool_exists_even_with_writes_enabled():
    # Nothing from the hedge tools module may ever be a WRITE tool — agents can
    # never open, close, or steer a hedge.
    assert all(
        getattr(fn, "__module__", "") != "stockscan.mcp.tools.hedge" for fn in server.WRITE_TOOLS
    )
    # And the module deliberately exposes no open/close/toggle callables.
    for banned in ("open", "close", "toggle", "create", "pause", "delete"):
        assert not any(banned in name for name in dir(t_hedge) if not name.startswith("_"))


# ---- validation path (no DB needed) ----
def test_list_hedges_rejects_unknown_status():
    out = t_hedge.list_hedges(status="halfway")
    assert out["error"] == "unknown_status"
    assert "active" in out["valid"]


# ---- hedge_state reuses the daemon's decision ----
def test_hedge_state_atm_short_call_targets_about_half_a_contract():
    st = service.hedge_state(_position())
    assert st["available"] is True
    assert 40 <= st["target_shares"] <= 60  # ~0.5 delta ⇒ ~50 shares/contract
    assert st["would_rebalance"] is True  # held 0, far from target
    assert st["planned_fill_qty"] == st["target_shares"]
    assert st["band_half_width_shares"] >= 0
    assert st["is_hypothetical"] is False


def test_hedge_state_whatif_at_spot_moves_target_up_across_strike():
    pos = _position()
    atm = service.hedge_state(pos, spot=1300.0)["target_shares"]
    itm = service.hedge_state(pos, spot=1600.0)["target_shares"]  # deep ITM what-if
    assert itm > atm
    assert service.hedge_state(pos, spot=1600.0)["is_hypothetical"] is True


def test_hedge_state_unavailable_without_spot_or_vol():
    assert service.hedge_state(_position(last_spot=None)).get("available") is False
    assert service.hedge_state(_position(iv_pct=None)).get("available") is False


# ---- summary shape ----
def test_summary_has_pnl_and_option_block():
    s = t_hedge._summary(_position(last_spot=Decimal("1305"), held_shares=50, avg_cost=Decimal("1300")))
    assert s["symbol"] == "MU"
    assert s["option"]["side"] == "short" and s["option"]["strike"] == 1300.0
    assert set(s["pnl"]) == {"premium", "option_open_pnl", "hedge_pnl", "net_open_pnl"}


# ---- simulator tools (synthetic — no DB) ----
def test_simulate_hedge_tool_runs_and_guards_bad_scenario():
    out = t_hedge.simulate_hedge(spot=1300, annual_vol_pct=45, drift_pct=100, dte=30, seed=2)
    assert "summary" in out and out["summary"]["num_trades"] >= 0
    assert out["summary"]["net_pnl"] == pytest.approx(out["summary"]["net_pnl"])  # finite
    # No spot and no symbol → guarded error, not a crash.
    assert t_hedge.simulate_hedge().get("error") == "bad_scenario"


def test_simulate_hedge_tool_optional_series():
    out = t_hedge.simulate_hedge(spot=1300, annual_vol_pct=40, dte=20, seed=1, include_series=True)
    assert "series" in out and len(out["series"]) > 0


def test_monte_carlo_and_sweep_tools():
    mc = t_hedge.hedge_monte_carlo(spot=1300, annual_vol_pct=45, n_paths=30, dte=30, seed=0)
    assert mc["n_paths"] == 30 and 0.0 <= mc["win_rate"] <= 1.0
    sw = t_hedge.hedge_sweep(spot=1300, annual_vol_pct=45, values="0.005,0.05,0.5", seed=2)
    assert sw["count"] == 3
    assert t_hedge.hedge_sweep(spot=1300, values="not,numbers").get("error") == "bad_values"
