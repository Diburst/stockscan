"""Filter chain tests."""

from datetime import date
from decimal import Decimal

from stockscan.risk.filters import FilterChain, PortfolioContext
from stockscan.strategies import RawSignal


def _signal(symbol: str = "AAPL", entry: str = "100", stop: str = "95") -> RawSignal:
    return RawSignal(
        strategy_name="test",
        strategy_version="1.0.0",
        symbol=symbol,
        side="long",
        score=Decimal("1"),
        suggested_entry=Decimal(entry),
        suggested_stop=Decimal(stop),
    )


def _ctx(**overrides) -> PortfolioContext:
    base = dict(
        as_of=date(2026, 4, 27),
        equity=Decimal("1000000"),
        high_water_mark=Decimal("1000000"),
    )
    base.update(overrides)
    return PortfolioContext(**base)


def _chain() -> FilterChain:
    return FilterChain.default(
        max_positions=15,
        max_position_pct=Decimal("0.08"),
        max_sector_pct=Decimal("0.25"),
        max_adv_pct=Decimal("0.05"),
        max_drawdown=Decimal("0.15"),
    )


def test_passes_when_no_constraints_violated() -> None:
    r = _chain().evaluate(_signal(), qty=100, ctx=_ctx())
    assert r.passed
    assert r.reason is None


def test_earnings_within_5d_rejects() -> None:
    ctx = _ctx(earnings_within_5d={"AAPL"})
    r = _chain().evaluate(_signal(), qty=100, ctx=ctx)
    assert not r.passed
    assert r.reason == "earnings_within_5_trading_days"


def test_already_in_position_rejects() -> None:
    ctx = _ctx(open_positions={"AAPL": {"strategy": "rsi2"}})
    r = _chain().evaluate(_signal(), qty=100, ctx=ctx)
    assert not r.passed
    assert "already_in_position_via_rsi2" in (r.reason or "")


def test_drawdown_circuit_breaker_blocks() -> None:
    # Equity 800k, HWM 1M → 20% drawdown > 15% breaker
    ctx = _ctx(equity=Decimal("800000"), high_water_mark=Decimal("1000000"))
    r = _chain().evaluate(_signal(), qty=100, ctx=ctx)
    assert not r.passed
    assert "drawdown_circuit_breaker" in (r.reason or "")


def test_position_pct_cap_blocks_large_position() -> None:
    # 1000 shares @ $100 = $100k = 10% of $1M; cap is 8%
    r = _chain().evaluate(_signal(), qty=1000, ctx=_ctx())
    assert not r.passed
    assert "8%" in (r.reason or "")


def test_max_positions_blocks_at_cap() -> None:
    ctx = _ctx(open_positions={f"S{i}": {"strategy": "x"} for i in range(15)})
    r = _chain().evaluate(_signal(symbol="ZZZ"), qty=10, ctx=ctx)
    assert not r.passed
    assert "max_concurrent" in (r.reason or "")
