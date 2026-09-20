"""Event-driven backtest engine (DESIGN §4.4).

Loop structure (one trading day at a time):

  1. For each open position, run strategy.exit_rules() with bars[≤today].
     Exits — stops included — are the strategy's decision; the engine
     applies none of its own. Triggered exits fill at tomorrow's open.
  2. Run strategy.signals() over the day's universe. Refuse new longs
     while the regime blocks them (trend gate closed / credit stress),
     size each signal with the strategy's rule × the regime vol scalar,
     then apply the filter chain. Survivors fill at tomorrow's open.
  3. Mark-to-market end-of-day equity using today's close.

The regime is evaluated once per run from SPY bars and HY OAS with the
same :func:`stockscan.regime.rules.regime_frame` the live detector uses,
and sizing goes through the same :func:`size_for_strategy`, so a backtest
measures exactly what trades live. The engine never reads from ``bars``
past ``as_of``.
"""

from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd

from stockscan.backtest.slippage import FixedBpsSlippage, SlippageModel
from stockscan.data.macro_store import get_macro_series
from stockscan.data.store import get_bars
from stockscan.metrics import (
    PerformanceReport,
    TradeResult,
    performance_report,
)
from stockscan.regime.detect import BENCHMARK, HY_OAS_SERIES
from stockscan.regime.rules import regime_frame
from stockscan.risk.filters import FilterChain, PortfolioContext
from stockscan.risk.sizer import size_for_strategy
from stockscan.scan.runner import avg_dollar_volume
from stockscan.sectors.store import sector_map
from stockscan.strategies import PositionSnapshot, RawSignal, Strategy
from stockscan.universe import members_as_of

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BacktestConfig:
    strategy_cls: type[Strategy]
    start_date: date
    end_date: date
    starting_capital: Decimal = Decimal("100000")
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
    # Strategy's suggested stop at entry — kept so the trade's R-multiple
    # can be computed at close. None for strategies that trade without one.
    entry_stop: Decimal | None = None
    # Snapshot of the originating signal's metadata — the indicator values
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
        self.strategy = config.strategy_cls()
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
        # Regime controls per SPY bar date, built lazily on the first entry
        # evaluation (one SPY load + one FRED query per run).
        self._regime: pd.DataFrame | None = None
        self._regime_missing = False
        # Point-in-time membership cache: members_as_of() is one DB query per
        # trading day; cache it so a multi-year run pays it once per date.
        self._members_cache: dict[date, list[str]] = {}
        self._sectors: dict[str, str] | None = None

        # Reset relative-strength's run-scoped caches so this run fetches fresh
        # composites (and doesn't reuse a prior run's). Eliminates ~2 DB
        # round-trips per (symbol, day) — the dominant backtest cost.
        try:
            from stockscan.indicators.relative_strength import clear_cache as _clear_rs
            _clear_rs()
        except Exception:
            pass

        self.filter_chain = FilterChain.default(
            max_positions=config.max_positions,
            max_position_pct=config.max_position_pct,
            max_sector_pct=config.max_sector_pct,
            max_adv_pct=config.max_adv_pct,
            max_drawdown=config.max_drawdown,
            strategy_max_positions=config.strategy_cls.max_open_positions,
        )

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------
    def run(self) -> BacktestResult:
        """Execute the day-by-day event loop and return the full result.

        Logs a one-line summary at start and finish (strategy, span,
        duration, trade count, ending equity) so long runs are traceable
        in the job logs without per-day spam.
        """
        trading_days = self._trading_days()
        if not trading_days:
            raise ValueError(
                f"No trading days found in [{self.config.start_date}, "
                f"{self.config.end_date}]. Are bars loaded for the universe?"
            )

        started = _time.perf_counter()
        log.info(
            "backtest start: %s v%s | universe=%s | %s..%s (%d trading days)",
            self.strategy.name,
            self.strategy.version,
            len(self.config.universe) if self.config.universe else "S&P500-historical",
            trading_days[0],
            trading_days[-1],
            len(trading_days),
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
        log.info(
            "backtest done: %s v%s | %d trades | final equity %.0f | %.1fs",
            self.strategy.name,
            self.strategy.version,
            len(self.closed_trades),
            float(equity_series.iloc[-1]) if len(equity_series) else 0.0,
            _time.perf_counter() - started,
        )
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

            # ----- Strategy-level exit rules -----
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

        block_new_longs, vol_scalar = self._regime_today(today)
        if block_new_longs:
            return

        ctx = self._portfolio_context(today)
        signals: list[tuple[RawSignal, int]] = []
        for symbol in universe:
            if symbol in self.positions:
                continue  # filter handles this too, but skip the scan call entirely
            bars = self._bars(symbol, today)
            if bars.empty or len(bars) < self.strategy.required_history():
                continue
            bars.attrs["symbol"] = symbol
            raw = self.strategy.signals(bars, today)
            if not raw:
                continue
            adv = avg_dollar_volume(bars)
            if adv is not None:
                ctx.avg_dollar_volume_20d[symbol] = adv
            for sig in raw:
                sizing = size_for_strategy(
                    self.config.strategy_cls,
                    ctx.equity,
                    sig.suggested_entry,
                    sig.suggested_stop,
                    vol_scalar=vol_scalar,
                    max_position_pct=self.config.max_position_pct,
                )
                if sizing.qty > 0:
                    signals.append((sig, sizing.qty))

        if not signals:
            return

        # Sort signals by strategy-emitted score (descending). Without
        # this sort, signals are processed in symbol-alphabetical
        # order (the order they were collected from `universe`). When
        # the filter chain's max_positions / sector caps create
        # contention for limited slots, alphabetically-first symbols
        # would consistently win — which has nothing to do with signal
        # quality and silently biases backtest results. Sorting puts
        # the strongest candidates at the front so they claim the open
        # slots first.
        #
        # Stable sort: ties (same score) fall back to insertion order
        # (alphabetical from `universe`), keeping behavior
        # deterministic. Signals with no numeric score sink to the
        # bottom by treating ``None`` as -inf.
        signals.sort(
            key=lambda pair: (
                float(pair[0].score) if pair[0].score is not None else float("-inf")
            ),
            reverse=True,
        )

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
                # Count the queued buy against the caps for the rest of the
                # day so the chain sees the book it is building.
                ctx.open_positions[sig.symbol] = {
                    "qty": Decimal(qty),
                    "notional": sig.suggested_entry * qty,
                    "strategy": self.strategy.name,
                }
                sector = ctx.sectors.get(sig.symbol)
                if sector:
                    ctx.sector_exposure[sector] = (
                        ctx.sector_exposure.get(sector, Decimal(0)) + sig.suggested_entry * qty
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
    def _portfolio_context(self, today: date) -> PortfolioContext:
        last_eq = self.equity_history[-1][1] if self.equity_history else self.cash
        if self._sectors is None:
            self._sectors = sector_map()
        open_positions: dict[str, dict[str, Decimal]] = {}
        sector_exposure: dict[str, Decimal] = {}
        for symbol, p in self.positions.items():
            notional = p.avg_cost * p.qty
            open_positions[symbol] = {
                "qty": Decimal(p.qty),
                "notional": notional,
                "strategy": self.strategy.name,
            }
            sector = self._sectors.get(symbol)
            if sector:
                sector_exposure[sector] = sector_exposure.get(sector, Decimal(0)) + notional
        return PortfolioContext(
            as_of=today,
            equity=last_eq,
            high_water_mark=self.high_water,
            open_positions=open_positions,
            sector_exposure=sector_exposure,
            sectors=self._sectors,
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
        cached = self._members_cache.get(today)
        if cached is None:
            cached = members_as_of(today)
            self._members_cache[today] = cached
        return cached

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
        # Slice to bars on/before `as_of` via O(log n) searchsorted on the sorted
        # index, returning a view. The old `cached[cached.index.date <= as_of]`
        # built a Python-date object array and a boolean-mask copy on EVERY call
        # (millions of times in a full-universe run) — the single biggest CPU sink.
        bound = pd.Timestamp(as_of) + pd.Timedelta(days=1)  # midnight after as_of
        if cached.index.tz is not None:
            bound = bound.tz_localize(cached.index.tz)
        pos = cached.index.searchsorted(bound, side="left")
        return cached.iloc[:pos]

    # ---------------------------------------------------------------------
    # Regime
    # ---------------------------------------------------------------------
    def _regime_today(self, today: date) -> tuple[bool, float]:
        """``(block_new_longs, vol_scalar)`` for ``today``.

        Neutral (no block, scalar 1.0) when SPY bars are missing, so an
        absent benchmark never silently empties a backtest — the run log
        says so once.
        """
        if self._regime is None and not self._regime_missing:
            # Two calendar years of warmup covers the 252-bar vol rank and
            # seeds the trend gate's state well before the first trade.
            start = self.config.start_date - timedelta(days=800)
            spy = self._bars_loader(BENCHMARK, start, self.config.end_date)
            if spy.empty:
                self._regime_missing = True
                log.warning("backtest: no %s bars — regime controls disabled for this run", BENCHMARK)
            else:
                spy = spy.sort_index()
                try:
                    oas = get_macro_series(HY_OAS_SERIES, start, self.config.end_date)
                except Exception as exc:
                    log.warning("backtest: HY OAS unavailable — credit-stress flag off: %s", exc)
                    oas = None
                frame = regime_frame(spy["close"], oas if oas is not None and not oas.empty else None)
                frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index).date)
                self._regime = frame
        if self._regime is None:
            return False, 1.0
        try:
            row = self._regime.loc[pd.Timestamp(today)]
        except KeyError:
            return False, 1.0
        blocked = bool(row["credit_stress_flag"]) or not bool(row["trend_gate_open"])
        scalar = float(row["vol_scalar"]) if not pd.isna(row["vol_scalar"]) else 1.0
        return blocked, scalar

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
