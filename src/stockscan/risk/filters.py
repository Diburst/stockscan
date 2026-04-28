"""Filter chain (DESIGN §4.7).

Each filter inspects a candidate signal + portfolio context and either
passes (returns None) or rejects with a human-readable reason. The chain
runs filters in order; the first rejection short-circuits.

Filters are pure functions of (signal, context). `PortfolioContext` is
populated by the scanner from current positions, equity, and reference
data (earnings calendar, sector mappings, ADV).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from stockscan.strategies import RawSignal


@dataclass
class PortfolioContext:
    """Snapshot of portfolio state at scan time."""

    as_of: date
    equity: Decimal
    high_water_mark: Decimal
    open_positions: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    # symbol -> {"qty": Decimal, "notional": Decimal, "strategy": str, "sector": str}
    sector_exposure: dict[str, Decimal] = field(default_factory=dict)  # sector -> $
    earnings_within_5d: set[str] = field(default_factory=set)  # symbols
    avg_dollar_volume_20d: dict[str, Decimal] = field(default_factory=dict)
    sectors: dict[str, str] = field(default_factory=dict)  # symbol -> sector


@dataclass(frozen=True, slots=True)
class FilterResult:
    passed: bool
    reason: str | None = None


# A filter takes (signal, suggested_qty, context) and returns FilterResult.
Filter = Callable[[RawSignal, int, PortfolioContext], FilterResult]


def filter_earnings_5d(signal: RawSignal, qty: int, ctx: PortfolioContext) -> FilterResult:
    if signal.symbol in ctx.earnings_within_5d:
        return FilterResult(False, "earnings_within_5_trading_days")
    return FilterResult(True)


def filter_already_in_position(
    signal: RawSignal, qty: int, ctx: PortfolioContext
) -> FilterResult:
    if signal.symbol in ctx.open_positions:
        existing = ctx.open_positions[signal.symbol]
        return FilterResult(False, f"already_in_position_via_{existing.get('strategy')}")
    return FilterResult(True)


def make_max_positions_filter(limit: int) -> Filter:
    def _f(signal: RawSignal, qty: int, ctx: PortfolioContext) -> FilterResult:
        if len(ctx.open_positions) >= limit:
            return FilterResult(False, f"max_concurrent_positions_{limit}")
        return FilterResult(True)

    return _f


def make_max_position_pct_filter(max_pct: Decimal) -> Filter:
    def _f(signal: RawSignal, qty: int, ctx: PortfolioContext) -> FilterResult:
        notional = signal.suggested_entry * qty
        cap = ctx.equity * max_pct
        if notional > cap:
            return FilterResult(False, f"position_exceeds_{max_pct:.0%}_of_equity")
        return FilterResult(True)

    return _f


def make_max_sector_pct_filter(max_pct: Decimal) -> Filter:
    def _f(signal: RawSignal, qty: int, ctx: PortfolioContext) -> FilterResult:
        sector = ctx.sectors.get(signal.symbol)
        if not sector:
            return FilterResult(True)  # unknown sector — don't block
        notional = signal.suggested_entry * qty
        current = ctx.sector_exposure.get(sector, Decimal(0))
        cap = ctx.equity * max_pct
        if (current + notional) > cap:
            return FilterResult(
                False,
                f"sector_{sector}_would_exceed_{max_pct:.0%}_of_equity",
            )
        return FilterResult(True)

    return _f


def make_max_adv_pct_filter(max_pct: Decimal) -> Filter:
    """Cap qty so notional <= max_pct * 20-day average dollar volume."""

    def _f(signal: RawSignal, qty: int, ctx: PortfolioContext) -> FilterResult:
        adv = ctx.avg_dollar_volume_20d.get(signal.symbol)
        if adv is None or adv <= 0:
            return FilterResult(True)  # unknown ADV — don't block
        notional = signal.suggested_entry * qty
        cap = adv * max_pct
        if notional > cap:
            return FilterResult(False, f"position_exceeds_{max_pct:.0%}_of_20d_adv")
        return FilterResult(True)

    return _f


def make_drawdown_circuit_breaker(max_dd: Decimal) -> Filter:
    def _f(signal: RawSignal, qty: int, ctx: PortfolioContext) -> FilterResult:
        if ctx.high_water_mark <= 0:
            return FilterResult(True)
        dd = (ctx.high_water_mark - ctx.equity) / ctx.high_water_mark
        if dd > max_dd:
            return FilterResult(
                False,
                f"drawdown_circuit_breaker_at_{dd:.1%}",
            )
        return FilterResult(True)

    return _f


@dataclass
class FilterChain:
    filters: list[Filter]

    @classmethod
    def default(
        cls,
        *,
        max_positions: int,
        max_position_pct: Decimal,
        max_sector_pct: Decimal,
        max_adv_pct: Decimal,
        max_drawdown: Decimal,
    ) -> FilterChain:
        return cls(
            filters=[
                make_drawdown_circuit_breaker(max_drawdown),
                filter_already_in_position,
                filter_earnings_5d,
                make_max_positions_filter(max_positions),
                make_max_position_pct_filter(max_position_pct),
                make_max_sector_pct_filter(max_sector_pct),
                make_max_adv_pct_filter(max_adv_pct),
            ]
        )

    def evaluate(
        self, signal: RawSignal, qty: int, ctx: PortfolioContext
    ) -> FilterResult:
        for f in self.filters:
            r = f(signal, qty, ctx)
            if not r.passed:
                return r
        return FilterResult(True)

    def evaluate_all(
        self, signals: Iterable[tuple[RawSignal, int]], ctx: PortfolioContext
    ) -> list[tuple[RawSignal, int, FilterResult]]:
        return [(s, q, self.evaluate(s, q, ctx)) for s, q in signals]
