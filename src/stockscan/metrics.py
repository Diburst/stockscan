"""Trading-performance metrics.

Used by both the backtester (Phase 1) and the live trade journal (Phase 2).
All functions take a list of TradeResult dataclasses (round-trip trades)
and/or an equity curve as a pandas Series indexed by date.

Annualization assumes 252 trading days/year for US equities.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd


TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True, slots=True)
class TradeResult:
    """Closed round-trip trade — minimal shape for metrics."""

    symbol: str
    entry_date: date
    exit_date: date
    entry_price: Decimal
    exit_price: Decimal
    qty: int
    commission: Decimal = Decimal(0)
    # Optional — present when the trade originated from a strategy that
    # specified a hard-stop level at entry. Required to compute r_multiple.
    entry_stop: Decimal | None = None
    # Reason the strategy gave for the exit (e.g. 'macd_below_zero',
    # 'hard_stop', 'time_stop'). 'end_of_backtest' for trades closed by the
    # force-close at the end of the run.
    exit_reason: str | None = None
    # Snapshot of strategy.signals() metadata at entry time — RSI value,
    # MACD histogram, etc. Populated by the engine, displayed in the UI.
    entry_metadata: dict[str, Any] | None = None

    @property
    def pnl(self) -> Decimal:
        return (self.exit_price - self.entry_price) * self.qty - self.commission

    @property
    def return_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return float((self.exit_price - self.entry_price) / self.entry_price)

    @property
    def holding_days(self) -> int:
        return (self.exit_date - self.entry_date).days

    @property
    def r_multiple(self) -> float | None:
        """Return on risk: (exit − entry) / (entry − stop).

        +1R = made the planned risk amount.
        −1R = stop hit cleanly.
        +3R = made 3× the planned risk.
        Returns None if no stop was recorded at entry, or if stop ≥ entry
        (which would make planned risk zero or negative — a strategy bug).
        """
        if self.entry_stop is None:
            return None
        risk = self.entry_price - self.entry_stop
        if risk <= 0:
            return None
        return float((self.exit_price - self.entry_price) / risk)


@dataclass(frozen=True, slots=True)
class PerformanceReport:
    num_trades: int
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    profit_factor: float
    expectancy_pct: float
    total_pnl: Decimal
    total_return_pct: float
    cagr: float
    sharpe: float
    sortino: float
    max_drawdown_pct: float
    max_drawdown_days: int
    exposure_pct: float

    def to_dict(self) -> dict[str, float | int | str]:
        return {
            "num_trades": self.num_trades,
            "win_rate": round(self.win_rate, 4),
            "avg_win_pct": round(self.avg_win_pct, 4),
            "avg_loss_pct": round(self.avg_loss_pct, 4),
            "profit_factor": round(self.profit_factor, 4),
            "expectancy_pct": round(self.expectancy_pct, 4),
            "total_pnl": str(self.total_pnl),
            "total_return_pct": round(self.total_return_pct, 4),
            "cagr": round(self.cagr, 4),
            "sharpe": round(self.sharpe, 4),
            "sortino": round(self.sortino, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "max_drawdown_days": self.max_drawdown_days,
            "exposure_pct": round(self.exposure_pct, 4),
        }


# ---------------------------------------------------------------------
# Trade-level metrics
# ---------------------------------------------------------------------
def win_rate(trades: list[TradeResult]) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t.pnl > 0)
    return wins / len(trades)


def avg_win_pct(trades: list[TradeResult]) -> float:
    wins = [t.return_pct for t in trades if t.pnl > 0]
    return float(np.mean(wins)) if wins else 0.0


def avg_loss_pct(trades: list[TradeResult]) -> float:
    losses = [t.return_pct for t in trades if t.pnl < 0]
    return float(np.mean(losses)) if losses else 0.0


def profit_factor(trades: list[TradeResult]) -> float:
    gross_win = sum(float(t.pnl) for t in trades if t.pnl > 0)
    gross_loss = -sum(float(t.pnl) for t in trades if t.pnl < 0)
    if gross_loss <= 0:
        return float("inf") if gross_win > 0 else 0.0
    return gross_win / gross_loss


def expectancy_pct(trades: list[TradeResult]) -> float:
    if not trades:
        return 0.0
    return float(np.mean([t.return_pct for t in trades]))


# ---------------------------------------------------------------------
# Equity-curve metrics
# ---------------------------------------------------------------------
def cagr(equity: pd.Series) -> float:
    if len(equity) < 2:
        return 0.0
    start = float(equity.iloc[0])
    end = float(equity.iloc[-1])
    if start <= 0 or end <= 0:
        return 0.0
    days = (equity.index[-1] - equity.index[0]).days
    if days <= 0:
        return 0.0
    years = days / 365.25
    return (end / start) ** (1 / years) - 1


def sharpe_ratio(equity: pd.Series, risk_free_rate: float = 0.0) -> float:
    """Annualized Sharpe of daily returns. risk_free_rate is annualized."""
    if len(equity) < 2:
        return 0.0
    rets = equity.pct_change().dropna()
    if rets.std() == 0:
        return 0.0
    rf_daily = risk_free_rate / TRADING_DAYS_PER_YEAR
    excess = rets - rf_daily
    return float(excess.mean() / excess.std() * math.sqrt(TRADING_DAYS_PER_YEAR))


def sortino_ratio(equity: pd.Series, risk_free_rate: float = 0.0) -> float:
    """Annualized Sortino — Sharpe but using downside deviation only."""
    if len(equity) < 2:
        return 0.0
    rets = equity.pct_change().dropna()
    rf_daily = risk_free_rate / TRADING_DAYS_PER_YEAR
    excess = rets - rf_daily
    downside = excess[excess < 0]
    if downside.empty or downside.std() == 0:
        return 0.0
    return float(excess.mean() / downside.std() * math.sqrt(TRADING_DAYS_PER_YEAR))


def max_drawdown(equity: pd.Series) -> tuple[float, int]:
    """Returns (max_dd_pct, max_dd_duration_days).

    max_dd_pct is negative (e.g., -0.123 = 12.3% drawdown).
    Duration is the longest stretch from peak to recovery (or to end of series).
    """
    if equity.empty:
        return 0.0, 0
    peak = equity.cummax()
    dd = (equity - peak) / peak
    max_dd_pct = float(dd.min())
    # Drawdown duration: longest run of underwater days
    underwater = (equity < peak).astype(int)
    longest = 0
    cur = 0
    for v in underwater:
        if v:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 0
    return max_dd_pct, longest


def exposure_pct(equity: pd.Series, positions_value: pd.Series) -> float:
    """Fraction of days where positions_value > 0, weighted by allocation."""
    if equity.empty:
        return 0.0
    aligned = positions_value.reindex(equity.index).fillna(0)
    return float((aligned / equity).clip(lower=0, upper=1).mean())


# ---------------------------------------------------------------------
# Top-level report
# ---------------------------------------------------------------------
def performance_report(
    trades: list[TradeResult],
    equity: pd.Series,
    positions_value: pd.Series | None = None,
) -> PerformanceReport:
    """Compute the full performance report for a backtest or live period."""
    starting = float(equity.iloc[0]) if len(equity) else 0.0
    ending = float(equity.iloc[-1]) if len(equity) else 0.0
    total_pnl = Decimal(str(ending - starting))
    total_return_pct = (ending / starting - 1) if starting > 0 else 0.0
    dd_pct, dd_days = max_drawdown(equity)
    pos_val = positions_value if positions_value is not None else pd.Series(dtype=float)

    return PerformanceReport(
        num_trades=len(trades),
        win_rate=win_rate(trades),
        avg_win_pct=avg_win_pct(trades),
        avg_loss_pct=avg_loss_pct(trades),
        profit_factor=profit_factor(trades),
        expectancy_pct=expectancy_pct(trades),
        total_pnl=total_pnl,
        total_return_pct=total_return_pct,
        cagr=cagr(equity),
        sharpe=sharpe_ratio(equity),
        sortino=sortino_ratio(equity),
        max_drawdown_pct=dd_pct,
        max_drawdown_days=dd_days,
        exposure_pct=exposure_pct(equity, pos_val),
    )
