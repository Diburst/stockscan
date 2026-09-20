"""Live / backdated scanner runner.

Bridges the strategy plugin system, the data store, the regime layer, the
risk engine, and persistence. Reuses the SAME ``Strategy.signals()``,
``size_for_strategy`` and ``FilterChain`` code the backtester uses — one
code path, two callers.

Workflow per run:
  1. Resolve ``as_of`` (default = today). Determine the universe via
     point-in-time S&P 500 membership.
  2. Ensure the strategy's version row exists and instantiate it.
  3. Read the day's market regime (trend gate, vol scalar, credit stress).
  4. Build a PortfolioContext from DB state: equity, open positions,
     sector map and exposure, the earnings calendar, 20-day dollar volume.
  5. For each symbol with sufficient history: run ``strategy.signals()``,
     size each signal (strategy rule × vol scalar), refuse new longs while
     the regime blocks them.
  6. Run the filter chain over the survivors, best score first.
  7. Persist a strategy_runs row + one signals row per candidate (passing
     AND rejected, so the UI can show both).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.config import settings
from stockscan.data.store import get_bars
from stockscan.db import session_scope
from stockscan.regime import MarketRegime, detect_regime
from stockscan.risk.filters import FilterChain, PortfolioContext
from stockscan.risk.sizer import size_for_strategy
from stockscan.sectors.store import sector_map
from stockscan.strategies import (
    STRATEGY_REGISTRY,
    RawSignal,
    Strategy,
    discover_strategies,
)
from stockscan.strategies.registration import ensure_strategy_version
from stockscan.universe import current_constituents, members_as_of

log = logging.getLogger(__name__)

ADV_WINDOW = 20


@dataclass(frozen=True, slots=True)
class ScanSummary:
    run_id: int
    strategy_name: str
    strategy_version: str
    as_of_date: date
    universe_size: int
    signals_emitted: int
    rejected_count: int
    regime_label: str | None
    vol_scalar: float


def avg_dollar_volume(bars: pd.DataFrame, window: int = ADV_WINDOW) -> Decimal | None:
    """Mean of close × volume over the last ``window`` bars, or None if short."""
    if len(bars) < window:
        return None
    tail = bars.iloc[-window:]
    value = float((tail["close"].astype(float) * tail["volume"].astype(float)).mean())
    return Decimal(str(round(value, 2)))


class ScanRunner:
    """One scanner invocation. Stateful per-instance for clarity."""

    def __init__(self, session: Session | None = None) -> None:
        self._session = session

    def run(
        self,
        strategy_name: str,
        as_of: date | None = None,
        *,
        symbols: list[str] | None = None,
    ) -> ScanSummary:
        as_of = as_of or date.today()
        discover_strategies()
        strategy_cls = STRATEGY_REGISTRY.get(strategy_name)

        if self._session is not None:
            return self._run_in_session(self._session, strategy_cls, as_of, symbols)
        with session_scope() as s:
            return self._run_in_session(s, strategy_cls, as_of, symbols)

    def _run_in_session(
        self,
        s: Session,
        strategy_cls: type[Strategy],
        as_of: date,
        symbols: list[str] | None,
    ) -> ScanSummary:
        ensure_strategy_version(strategy_cls, session=s)
        strategy = strategy_cls()

        regime = self._regime(s, as_of)
        vol_scalar = regime.vol_multiplier if regime is not None else 1.0
        block_reason = _block_reason(regime)

        if symbols is None:
            symbols = members_as_of(as_of, session=s) or current_constituents(session=s)
        scan_started = time.perf_counter()
        log.info(
            "scanning %s v%s on %d symbols as of %s (regime=%s, vol scalar=%.2f)",
            strategy_cls.name,
            strategy_cls.version,
            len(symbols),
            as_of,
            regime.regime if regime is not None else "unavailable",
            vol_scalar,
        )

        ctx = self._portfolio_context(s, as_of)
        chain = FilterChain.default(
            max_positions=settings.max_positions,
            max_position_pct=settings.max_position_pct,
            max_sector_pct=settings.max_sector_pct,
            max_adv_pct=settings.max_adv_pct,
            max_drawdown=settings.drawdown_circuit_breaker,
            strategy_max_positions=strategy_cls.max_open_positions,
        )

        # Pass A — generate + size. Signal-local checks only, so order does
        # not matter. Pass B — filter chain over the survivors, strongest
        # score first, so the best candidates claim contended slots.
        passing: list[tuple[RawSignal, int]] = []
        rejected: list[tuple[RawSignal, int, str]] = []
        chain_eligible: list[tuple[RawSignal, int]] = []
        for symbol in symbols:
            try:
                bars = get_bars(symbol, as_of.replace(year=as_of.year - 5), as_of, session=s)
            except Exception as exc:
                log.debug("skip %s — bars query failed: %s", symbol, exc)
                continue
            if bars.empty or len(bars) < strategy.required_history():
                continue
            bars.attrs["symbol"] = symbol
            try:
                raw_sigs = strategy.signals(bars, as_of)
            except Exception:
                log.exception(
                    "scan %s: signals() raised on %s as of %s — symbol skipped",
                    strategy_cls.name,
                    symbol,
                    as_of,
                )
                continue
            if not raw_sigs:
                continue
            adv = avg_dollar_volume(bars)
            if adv is not None:
                ctx.avg_dollar_volume_20d[symbol] = adv
            for sig in raw_sigs:
                if block_reason is not None and sig.side == "long":
                    rejected.append((sig, 0, block_reason))
                    continue
                sizing = size_for_strategy(
                    strategy_cls,
                    ctx.equity,
                    sig.suggested_entry,
                    sig.suggested_stop,
                    vol_scalar=vol_scalar,
                    max_position_pct=Decimal(str(settings.max_position_pct)),
                )
                if sizing.qty <= 0:
                    rejected.append((sig, 0, sizing.rejected_reason or "qty_zero"))
                    continue
                chain_eligible.append((sig, sizing.qty))

        chain_eligible.sort(
            key=lambda pair: (
                float(pair[0].score) if pair[0].score is not None else float("-inf")
            ),
            reverse=True,
        )
        for sig, qty in chain_eligible:
            result = chain.evaluate(sig, qty, ctx)
            if result.passed:
                passing.append((sig, qty))
                # A passing candidate counts against the caps for the rest
                # of the pass, so the chain sees the book it is building.
                ctx.open_positions[sig.symbol] = {
                    "qty": Decimal(qty),
                    "notional": sig.suggested_entry * qty,
                    "strategy": strategy_cls.name,
                }
                sector = ctx.sectors.get(sig.symbol)
                if sector:
                    ctx.sector_exposure[sector] = (
                        ctx.sector_exposure.get(sector, Decimal(0)) + sig.suggested_entry * qty
                    )
            else:
                rejected.append((sig, qty, result.reason or "filter_rejected"))

        run_id = self._persist_run(s, strategy_cls, as_of, len(symbols), len(passing), len(rejected))
        for sig, qty in passing:
            self._persist_signal(s, run_id, strategy_cls, as_of, sig, qty, "new", None)
        for sig, qty, reason in rejected:
            self._persist_signal(s, run_id, strategy_cls, as_of, sig, qty, "rejected", reason)

        log.info(
            "scan done: %s v%s | %d passing / %d rejected | run_id=%d | %.1fs",
            strategy_cls.name,
            strategy_cls.version,
            len(passing),
            len(rejected),
            run_id,
            time.perf_counter() - scan_started,
        )
        return ScanSummary(
            run_id=run_id,
            strategy_name=strategy_cls.name,
            strategy_version=strategy_cls.version,
            as_of_date=as_of,
            universe_size=len(symbols),
            signals_emitted=len(passing),
            rejected_count=len(rejected),
            regime_label=regime.regime if regime is not None else None,
            vol_scalar=vol_scalar,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _regime(s: Session, as_of: date) -> MarketRegime | None:
        """Today's regime row, or None (neutral sizing, no entry block) when
        the benchmark data is missing — a data outage must not silently
        stop the scanner."""
        try:
            regime = detect_regime(as_of, session=s)
        except Exception as exc:
            log.warning("regime detection failed — sizing neutrally: %s", exc)
            return None
        if regime is None:
            log.warning("regime: no row for %s — sizing neutrally", as_of)
        return regime

    def _portfolio_context(self, s: Session, as_of: date) -> PortfolioContext:
        eq_row = s.execute(
            text(
                """
                SELECT total_equity, high_water_mark
                FROM equity_history
                WHERE as_of_date <= :d
                ORDER BY as_of_date DESC
                LIMIT 1
                """
            ),
            {"d": as_of},
        ).first()
        if eq_row is not None:
            equity = Decimal(str(eq_row.total_equity))
            hwm = Decimal(str(eq_row.high_water_mark))
        else:
            equity = Decimal(str(settings.starting_equity))
            hwm = equity
            log.warning(
                "no equity_history row on or before %s — sizing against "
                "STOCKSCAN_STARTING_EQUITY=%s",
                as_of,
                equity,
            )

        sectors = sector_map(session=s)
        pos_rows = s.execute(text("SELECT symbol, strategy, qty, avg_cost FROM positions")).all()
        open_positions: dict[str, dict[str, Decimal]] = {}
        sector_exposure: dict[str, Decimal] = {}
        for r in pos_rows:
            notional = Decimal(str(r.qty)) * Decimal(str(r.avg_cost))
            open_positions[r.symbol] = {
                "qty": Decimal(r.qty),
                "notional": notional,
                "strategy": r.strategy,
            }
            sector = sectors.get(r.symbol)
            if sector:
                sector_exposure[sector] = sector_exposure.get(sector, Decimal(0)) + notional

        # Earnings within 5 trading days (calendar approximation = 7 days).
        # Empty when the data plan does not refresh the calendar.
        earnings_rows = s.execute(
            text(
                """
                SELECT DISTINCT symbol FROM earnings_calendar
                WHERE report_date BETWEEN :d AND :d + INTERVAL '7 days'
                """
            ),
            {"d": as_of},
        ).all()

        return PortfolioContext(
            as_of=as_of,
            equity=equity,
            high_water_mark=hwm,
            open_positions=open_positions,
            sector_exposure=sector_exposure,
            earnings_within_5d={r.symbol for r in earnings_rows},
            sectors=sectors,
        )

    def _persist_run(
        self,
        s: Session,
        strategy_cls: type[Strategy],
        as_of: date,
        universe_size: int,
        n_pass: int,
        n_reject: int,
    ) -> int:
        row = s.execute(
            text(
                """
                INSERT INTO strategy_runs
                    (strategy_name, strategy_version, as_of_date,
                     universe_size, signals_emitted, rejected_count)
                VALUES (:n, :v, :d, :u, :s, :r)
                RETURNING run_id;
                """
            ),
            {
                "n": strategy_cls.name,
                "v": strategy_cls.version,
                "d": as_of,
                "u": universe_size,
                "s": n_pass,
                "r": n_reject,
            },
        ).one()
        return int(row.run_id)

    def _persist_signal(
        self,
        s: Session,
        run_id: int,
        strategy_cls: type[Strategy],
        as_of: date,
        sig: RawSignal,
        qty: int,
        status: str,
        rejected_reason: str | None,
    ) -> None:
        # ON CONFLICT on the natural key so re-running the same strategy for
        # the same date refreshes the row instead of duplicating it.
        s.execute(
            text(
                """
                INSERT INTO signals
                    (run_id, strategy_name, strategy_version,
                     symbol, side, score, as_of_date,
                     suggested_entry, suggested_stop, suggested_target, suggested_qty,
                     rejected_reason, metadata, status)
                VALUES (:run_id, :n, :v,
                        :symbol, :side, :score, :as_of,
                        :entry, :stop, :target, :qty,
                        :reason, CAST(:meta AS JSONB), :status)
                ON CONFLICT (symbol, strategy_name, strategy_version, as_of_date)
                DO UPDATE SET
                    run_id          = EXCLUDED.run_id,
                    side            = EXCLUDED.side,
                    score           = EXCLUDED.score,
                    suggested_entry = EXCLUDED.suggested_entry,
                    suggested_stop  = EXCLUDED.suggested_stop,
                    suggested_target = EXCLUDED.suggested_target,
                    suggested_qty   = EXCLUDED.suggested_qty,
                    rejected_reason = EXCLUDED.rejected_reason,
                    metadata        = EXCLUDED.metadata,
                    status          = EXCLUDED.status;
                """
            ),
            {
                "run_id": run_id,
                "n": strategy_cls.name,
                "v": strategy_cls.version,
                "symbol": sig.symbol,
                "side": sig.side,
                "score": sig.score,
                "as_of": as_of,
                "entry": sig.suggested_entry,
                "stop": sig.suggested_stop,
                "target": sig.suggested_target,
                "qty": qty,
                "reason": rejected_reason,
                "meta": json.dumps(sig.metadata) if sig.metadata else None,
                "status": status,
            },
        )


def _block_reason(regime: MarketRegime | None) -> str | None:
    """Why new longs are refused today, or None when they are allowed."""
    if regime is None:
        return None
    if regime.credit_stress_flag:
        return "credit_stress_long_block"
    if not regime.trend_gate_open:
        return "trend_gate_closed"
    return None
