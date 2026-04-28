"""Base-rate analyzer for a (strategy, symbol) signal.

Walks the symbol's history; for every date the strategy's entry rule fires,
simulates the exit rules and records the round-trip outcome. Returns
aggregate statistics so the user can see whether *this kind of setup* has
historically worked.

We do NOT replay portfolio filters in this Phase 2 cut — that's the
"would have been rejected" cohort split called out in USER_STORIES Story 4.
The cohort split lands in v1.5; the rest of the report (win rate, expectancy,
distribution) is sufficient for the page to be useful immediately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pandas as pd

from stockscan.data.store import get_bars
from stockscan.metrics import TradeResult, performance_report
from stockscan.strategies import (
    PositionSnapshot,
    Strategy,
    StrategyParams,
)


@dataclass(frozen=True, slots=True)
class HistoricalSetup:
    entry_date: date
    entry_price: Decimal
    exit_date: date
    exit_price: Decimal
    exit_reason: str
    return_pct: float
    holding_days: int


@dataclass(frozen=True, slots=True)
class BaseRateReport:
    strategy_name: str
    strategy_version: str
    symbol: str
    as_of: date
    n_setups: int
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    profit_factor: float
    expectancy_pct: float
    avg_holding_days: float
    return_distribution: list[float] = field(default_factory=list)
    sample_size_warning: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "symbol": self.symbol,
            "as_of": str(self.as_of),
            "n_setups": self.n_setups,
            "win_rate": round(self.win_rate, 4),
            "avg_win_pct": round(self.avg_win_pct, 4),
            "avg_loss_pct": round(self.avg_loss_pct, 4),
            "profit_factor": round(self.profit_factor, 4),
            "expectancy_pct": round(self.expectancy_pct, 4),
            "avg_holding_days": round(self.avg_holding_days, 2),
            "return_distribution": self.return_distribution,
            "sample_size_warning": self.sample_size_warning,
            "note": self.note,
        }


def compute_base_rates(
    strategy_cls: type[Strategy],
    params: StrategyParams,
    symbol: str,
    as_of: date,
    *,
    bars: pd.DataFrame | None = None,
    history_years: int = 16,
) -> BaseRateReport:
    """Enumerate historical signals on `symbol` and aggregate outcomes."""
    strategy = strategy_cls(params)

    if bars is None:
        start = date(as_of.year - history_years, 1, 1)
        bars = get_bars(symbol, start, as_of)

    if bars is None or bars.empty:
        return BaseRateReport(
            strategy_name=strategy_cls.name,
            strategy_version=strategy_cls.version,
            symbol=symbol,
            as_of=as_of,
            n_setups=0,
            win_rate=0.0,
            avg_win_pct=0.0,
            avg_loss_pct=0.0,
            profit_factor=0.0,
            expectancy_pct=0.0,
            avg_holding_days=0.0,
            sample_size_warning=True,
            note="No bars available for this symbol",
        )

    bars = bars.sort_index()
    bars.attrs["symbol"] = symbol

    # Walk the history bar-by-bar. For each day, ask the strategy whether it would
    # have entered. If yes, simulate the exit forward.
    setups: list[HistoricalSetup] = []
    in_position_until: date | None = None  # avoid stacking concurrent setups
    required = strategy.required_history()

    for i in range(required, len(bars) - 1):
        row = bars.iloc[i]
        ts = bars.index[i]
        d = ts.date() if hasattr(ts, "date") else ts
        if in_position_until and d <= in_position_until:
            continue
        view = bars.iloc[: i + 1]
        view.attrs["symbol"] = symbol
        sigs = strategy.signals(view, as_of=d)
        if not sigs:
            continue
        sig = sigs[0]
        # Simulate: enter at next bar's open
        if i + 1 >= len(bars):
            break
        entry_row = bars.iloc[i + 1]
        entry_date = bars.index[i + 1].date()
        entry_price = Decimal(str(float(entry_row["open"])))

        # Step forward checking exit_rules each day until exit fires or run out of bars.
        snapshot = PositionSnapshot(
            symbol=symbol,
            qty=1,
            avg_cost=entry_price,
            opened_at=bars.index[i + 1],
            strategy=strategy_cls.name,
        )
        exit_date = entry_date
        exit_price = entry_price
        exit_reason = "open_at_end"
        for j in range(i + 1, len(bars)):
            view_j = bars.iloc[: j + 1]
            view_j.attrs["symbol"] = symbol
            d_j = bars.index[j].date()
            decision = strategy.exit_rules(snapshot, view_j, d_j)
            if decision is not None and j + 1 < len(bars):
                exit_date = bars.index[j + 1].date()
                exit_price = Decimal(str(float(bars.iloc[j + 1]["open"])))
                exit_reason = decision.reason
                break
            if j == len(bars) - 1:
                exit_date = d_j
                exit_price = Decimal(str(float(bars.iloc[j]["close"])))
                break

        ret = float((exit_price - entry_price) / entry_price)
        holding = (exit_date - entry_date).days
        setups.append(
            HistoricalSetup(
                entry_date=entry_date,
                entry_price=entry_price,
                exit_date=exit_date,
                exit_price=exit_price,
                exit_reason=exit_reason,
                return_pct=ret,
                holding_days=holding,
            )
        )
        in_position_until = exit_date

    if not setups:
        return BaseRateReport(
            strategy_name=strategy_cls.name,
            strategy_version=strategy_cls.version,
            symbol=symbol,
            as_of=as_of,
            n_setups=0,
            win_rate=0.0,
            avg_win_pct=0.0,
            avg_loss_pct=0.0,
            profit_factor=0.0,
            expectancy_pct=0.0,
            avg_holding_days=0.0,
            sample_size_warning=True,
            note="Strategy never fired on this symbol in the historical window",
        )

    # Convert to TradeResult so we reuse the metrics module.
    trades = [
        TradeResult(
            symbol=symbol,
            entry_date=s.entry_date,
            exit_date=s.exit_date,
            entry_price=s.entry_price,
            exit_price=s.exit_price,
            qty=1,
        )
        for s in setups
    ]
    # Synthetic equity curve for performance metrics.
    eq = pd.Series([100.0] + [100.0 * (1 + s.return_pct) for s in setups])
    eq.index = pd.to_datetime([setups[0].entry_date] + [s.exit_date for s in setups])
    report = performance_report(trades, eq)

    return BaseRateReport(
        strategy_name=strategy_cls.name,
        strategy_version=strategy_cls.version,
        symbol=symbol,
        as_of=as_of,
        n_setups=len(setups),
        win_rate=report.win_rate,
        avg_win_pct=report.avg_win_pct,
        avg_loss_pct=report.avg_loss_pct,
        profit_factor=report.profit_factor,
        expectancy_pct=report.expectancy_pct,
        avg_holding_days=sum(s.holding_days for s in setups) / len(setups),
        return_distribution=[s.return_pct for s in setups],
        sample_size_warning=len(setups) < 50,
    )
