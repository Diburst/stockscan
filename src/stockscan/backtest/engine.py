"""Event-driven backtest engine (DESIGN §4.4).

Loop structure (one trading day at a time):

  1. For each open position, run strategy.exit_rules() with bars[≤today].
     If exit triggered → enqueue MARKET_ON_OPEN sell for tomorrow.
  2. Run strategy.signals() over the day's universe.
     Apply the filter chain (risk module). Size each passing signal.
     For passing+sized signals → enqueue MARKET_ON_OPEN buy for tomorrow.
  3. Mark-to-market end-of-day equity using today's close.
  4. Advance to tomorrow → fill enqueued orders at tomorrow's open with
     slippage. Reject buys that violate cash availability.

The engine never reads from `bars` past `as_of` — strategies and risk
filters receive a sliced view. This is the same code path the live
scanner uses; the only difference is fills are simulated.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd

from stockscan.backtest.slippage import FixedBpsSlippage, SlippageModel
from stockscan.data.store import get_bars
from stockscan.metrics import (
    PerformanceReport,
    TradeResult,
    performance_report,
)
from stockscan.risk.filters import FilterChain, PortfolioContext
from stockscan.risk.sizer import position_size
from stockscan.strategies import (
    PositionSnapshot,
    RawSignal,
    Strategy,
    StrategyParams,
)
from stockscan.universe import members_as_of

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BacktestConfig:
    strategy_cls: type[Strategy]
    params: StrategyParams
    start_date: date
    end_date: date
    starting_capital: Decimal = Decimal("1000000")
    risk_pct: Decimal = Decimal("0.01")
    commission_per_trade: Decimal = Decimal("0")
    slippage: SlippageModel = field(default_factory=FixedBpsSlippage)
    universe: list[str] | None = None  # None = use historical S&P 500 membership
    max_positions: int = 15
    max_position_pct: Decimal = Decimal("0.08")
    max_sector_pct: Decimal = Decimal("0.25")
    max_adv_pct: Decimal = Decimal("0.05")
    max_drawdown: Decimal = Decimal("0.15")


# ---------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------
@dataclass
class _Position:
    symbol: str
    qty: int
    avg_cost: Decimal
    opened_at: datetime
    high_water_close: Decimal  # for MFE
    low_water_close: Decimal  # for MAE
    entry_index: int  # index into the bar history (for holding-day calcs)
    # Strategy's suggested stop at entry — preserved through the trade so we
    # can compute R-multiple at close. None for trades opened without one.
    entry_stop: Decimal | None = None
    # Snapshot of the originating signal's metadata — RSI/MACD/SMA values
    # that fired the entry. Used by the UI's trade log + chart hovers.
    entry_metadata: dict | None = None


@dataclass
class _PendingOrder:
    symbol: str
    side: str  # 'buy' or 'sell'
    qty: int
    reason: str  # for sells: ExitDecision.reason; for buys: 'entry_signal'
    # Carry the strategy's suggested stop into the position at fill time.
    suggested_stop: Decimal | None = None
    # For buys: snapshot of signal.metadata — strategy's indicator values.
    entry_metadata: dict | None = None


@dataclass(frozen=True, slots=True)
class BacktestResult:
    config: BacktestConfig
    trades: list[TradeResult]
    equity_curve: pd.Series
    positions_value: pd.Series
    report: PerformanceReport


# ---------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------
class BacktestEngine:
    """One backtest run. Stateful — instantiate per run."""

    def __init__(self, config: BacktestConfig, *, bars_loader=None) -> None:
        self.config = config
        self.strategy = config.strategy_cls(config.params)
        self.cash: Decimal = config.starting_capital
        self.positions: dict[str, _Position] = {}
        self.pending_orders: list[_PendingOrder] = []
        self.closed_trades: list[TradeResult] = []
        self.equity_history: list[tuple[date, Decimal, Decimal]] = []
        # (date, total_equity, positions_value)
        self.high_water: Decimal = config.starting_capital

        # bars_loader signature: (symbol, start, end) -> DataFrame indexed by ts (UTC)
        self._bars_loader = bars_loader or get_bars
        self._bars_cache: dict[str, pd.DataFrame] = {}

        self.filter_chain = FilterChain.default(
            max_positions=config.max_positions,
            max_position_pct=config.max_position_pct,
            max_sector_pct=config.max_sector_pct,
            max_adv_pct=config.max_adv_pct,
            max_drawdown=config.max_drawdown,
        )

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------
    def run(self) -> BacktestResult:
        trading_days = self._trading_days()
        if not trading_days:
            raise ValueError(
                f"No trading days found in [{self.config.start_date}, "
                f"{self.config.end_date}]. Are bars loaded for the universe?"
            )

        for i, today in enumerate(trading_days):
            tomorrow = trading_days[i + 1] if i + 1 < len(trading_days) else None

            # 1. Fill any pending orders at TODAY's open (queued on the prior day).
            self._fill_pending(today)

            # 2. Generate exit decisions on currently-open positions (using bars≤today).
            self._evaluate_exits(today)

            # 3. Generate entry signals + filter + size (using bars≤today).
            #    Only enqueue if there's a tomorrow to fill on.
            if tomorrow is not None:
                self._evaluate_entries(today)

            # 4. Mark-to-market end-of-day.
            self._record_equity(today)

        # Close any remaining open positions at the final close.
        self._force_close_remaining(trading_days[-1])

        equity_series = self._equity_series()
        positions_series = self._positions_series()
        report = performance_report(self.closed_trades, equity_series, positions_series)
        return BacktestResult(
            config=self.config,
            trades=self.closed_trades,
            equity_curve=equity_series,
            positions_value=positions_series,
            report=report,
        )

    # ---------------------------------------------------------------------
    # Core loop steps
    # ---------------------------------------------------------------------
    def _evaluate_exits(self, today: date) -> None:
        for symbol, pos in list(self.positions.items()):
            bars = self._bars(symbol, today)
            if bars.empty:
                continue
            snapshot = PositionSnapshot(
                symbol=symbol,
                qty=pos.qty,
                avg_cost=pos.avg_cost,
                opened_at=pos.opened_at,
                strategy=self.strategy.name,
            )
            decision = self.strategy.exit_rules(snapshot, bars, today)
            if decision is not None:
                self.pending_orders.append(
                    _PendingOrder(
                        symbol=symbol,
                        side="sell",
                        qty=decision.qty,
                        reason=decision.reason,
                    )
                )

    def _evaluate_entries(self, today: date) -> None:
        universe = self._daily_universe(today)
        if not universe:
            return

        signals: list[tuple[RawSignal, int]] = []
        for symbol in universe:
            if symbol in self.positions:
                continue  # filter handles this too, but skip the scan call entirely
            bars = self._bars(symbol, today)
            if bars.empty or len(bars) < self.strategy.required_history():
                continue
            raw = self.strategy.signals(bars, today)
            for sig in raw:
                qty = self._size(sig)
                if qty > 0:
                    signals.append((sig, qty))

        if not signals:
            return

        ctx = self._portfolio_context(today)
        for sig, qty in signals:
            result = self.filter_chain.evaluate(sig, qty, ctx)
            if result.passed:
                self.pending_orders.append(
                    _PendingOrder(
                        symbol=sig.symbol,
                        side="buy",
                        qty=qty,
                        reason="entry_signal",
                        suggested_stop=sig.suggested_stop,
                        entry_metadata=dict(sig.metadata) if sig.metadata else None,
                    )
                )
            # Rejected signals are still discoverable via the live scanner;
            # the backtester logs them at debug for inspection.
            else:
                log.debug("backtest: rejected %s — %s", sig.symbol, result.reason)

    def _fill_pending(self, today: date) -> None:
        if not self.pending_orders:
            return
        remaining: list[_PendingOrder] = []
        for order in self.pending_orders:
            bars = self._bars(order.symbol, today)
            if bars.empty or bars.index[-1].date() != today:
                # No bar today — order expires (rare; usually means delisted).
                continue
            today_open = Decimal(str(float(bars.iloc[-1]["open"])))
            fill = self.config.slippage.adjust(order.side, today_open, order.qty)
            if order.side == "buy":
                cost = fill * order.qty + self.config.commission_per_trade
                if cost > self.cash:
                    log.debug(
                        "backtest: insufficient cash for %s buy %d @ %s",
                        order.symbol, order.qty, fill,
                    )
                    continue
                self.cash -= cost
                self.positions[order.symbol] = _Position(
                    symbol=order.symbol,
                    qty=order.qty,
                    avg_cost=fill,
                    opened_at=datetime(today.year, today.month, today.day, tzinfo=timezone.utc),
                    high_water_close=fill,
                    low_water_close=fill,
                    entry_index=0,
                    entry_stop=order.suggested_stop,
                    entry_metadata=order.entry_metadata,
                )
            else:  # sell
                if order.symbol not in self.positions:
                    continue
                pos = self.positions[order.symbol]
                proceeds = fill * order.qty - self.config.commission_per_trade
                self.cash += proceeds
                trade = TradeResult(
                    symbol=order.symbol,
                    entry_date=pos.opened_at.date(),
                    exit_date=today,
                    entry_price=pos.avg_cost,
                    exit_price=fill,
                    qty=order.qty,
                    commission=self.config.commission_per_trade * 2,
                    entry_stop=pos.entry_stop,
                    exit_reason=order.reason,
                    entry_metadata=pos.entry_metadata,
                )
                self.closed_trades.append(trade)
                if order.qty >= pos.qty:
                    del self.positions[order.symbol]
                else:
                    pos.qty -= order.qty
        self.pending_orders = remaining

    def _record_equity(self, today: date) -> None:
        positions_value = Decimal(0)
        for symbol, pos in self.positions.items():
            bars = self._bars(symbol, today)
            if bars.empty:
                # Mark at last known close.
                last_close = pos.avg_cost
            else:
                last_close = Decimal(str(float(bars.iloc[-1]["close"])))
                # Track MFE / MAE on the position itself (could persist later).
                if last_close > pos.high_water_close:
                    pos.high_water_close = last_close
                if last_close < pos.low_water_close:
                    pos.low_water_close = last_close
            positions_value += last_close * pos.qty
        total = self.cash + positions_value
        if total > self.high_water:
            self.high_water = total
        self.equity_history.append((today, total, positions_value))

    def _force_close_remaining(self, last_day: date) -> None:
        for symbol, pos in list(self.positions.items()):
            bars = self._bars(symbol, last_day)
            if bars.empty:
                continue
            close = Decimal(str(float(bars.iloc[-1]["close"])))
            self.closed_trades.append(
                TradeResult(
                    symbol=symbol,
                    entry_date=pos.opened_at.date(),
                    exit_date=last_day,
                    entry_price=pos.avg_cost,
                    exit_price=close,
                    qty=pos.qty,
                    commission=self.config.commission_per_trade,
                    entry_stop=pos.entry_stop,
                    exit_reason="end_of_backtest",
                    entry_metadata=pos.entry_metadata,
                )
            )
            self.cash += close * pos.qty
            del self.positions[symbol]

    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------
    def _size(self, signal: RawSignal) -> int:
        # Use the strategy's default risk override if present, else config.
        risk_pct = Decimal(str(self.strategy.default_risk_pct))
        if self.config.risk_pct:
            risk_pct = self.config.risk_pct
        equity = self.cash + sum(
            Decimal(0) for _ in self.positions.values()
        )  # cash + (live mark-to-market handled in _record_equity)
        # For sizing we use last-known equity; computing market value here
        # would force a bars lookup per signal which is wasteful. Use the
        # most-recent recorded equity instead.
        if self.equity_history:
            equity = self.equity_history[-1][1]
        result = position_size(
            equity=equity,
            entry_price=signal.suggested_entry,
            stop_price=signal.suggested_stop,
            risk_pct=risk_pct,
            max_position_pct=self.config.max_position_pct,
        )
        return result.qty

    def _portfolio_context(self, today: date) -> PortfolioContext:
        last_eq = self.equity_history[-1][1] if self.equity_history else self.cash
        open_positions = {
            s: {
                "qty": Decimal(p.qty),
                "notional": p.avg_cost * p.qty,
                "strategy": self.strategy.name,
            }
            for s, p in self.positions.items()
        }
        return PortfolioContext(
            as_of=today,
            equity=last_eq,
            high_water_mark=self.high_water,
            open_positions=open_positions,
        )

    def _trading_days(self) -> list[date]:
        # Use AAPL (or first universe symbol) as the "calendar".
        sample = self.config.universe[0] if self.config.universe else "AAPL"
        bars = self._bars(sample, self.config.end_date)
        if bars.empty:
            # Fall back to scanning every symbol in the configured universe.
            for sym in (self.config.universe or []):
                b = self._bars(sym, self.config.end_date)
                if not b.empty:
                    bars = b
                    break
        if bars.empty:
            return []
        days = sorted({ts.date() for ts in bars.index})
        return [
            d for d in days if self.config.start_date <= d <= self.config.end_date
        ]

    def _daily_universe(self, today: date) -> list[str]:
        if self.config.universe is not None:
            return self.config.universe
        return members_as_of(today)

    def _bars(self, symbol: str, as_of: date) -> pd.DataFrame:
        cached = self._bars_cache.get(symbol)
        if cached is None:
            # Pull a generous window once per symbol — full backfill from
            # config.start_date minus warmup, to config.end_date.
            warmup = max(250, self.strategy.required_history()) + 30
            start = self.config.start_date - timedelta(days=warmup * 2)  # weekends/holidays
            cached = self._bars_loader(symbol, start, self.config.end_date)
            if not cached.empty:
                cached = cached.sort_index()
            self._bars_cache[symbol] = cached
        if cached.empty:
            return cached
        return cached[cached.index.date <= as_of]

    def _equity_series(self) -> pd.Series:
        if not self.equity_history:
            return pd.Series(dtype=float)
        idx = pd.DatetimeIndex([d for d, _, _ in self.equity_history])
        return pd.Series([float(e) for _, e, _ in self.equity_history], index=idx, name="equity")

    def _positions_series(self) -> pd.Series:
        if not self.equity_history:
            return pd.Series(dtype=float)
        idx = pd.DatetimeIndex([d for d, _, _ in self.equity_history])
        return pd.Series(
            [float(p) for _, _, p in self.equity_history], index=idx, name="positions_value"
        )
