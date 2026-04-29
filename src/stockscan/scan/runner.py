"""Live / backdated scanner runner.

Bridges the strategy plugin system, the data store, the risk engine, and
persistence. Reuses the SAME `Strategy.signals()` and `FilterChain` code
the backtester uses — no separate code path.

Workflow per run:
  1. Resolve `as_of` (default = today). Determine universe via historical
     S&P 500 membership.
  2. Look up the strategy's active config (creates a default config if the
     strategy is brand new — strategy_versions row is also created).
  3. Build a PortfolioContext from current DB state (positions, equity,
     earnings calendar, etc.).
  4. For each symbol with sufficient history: run strategy.signals().
  5. Size each raw signal via the risk module.
  6. Run the filter chain. Passing signals get status='new'; failing signals
     get status='rejected' with `rejected_reason`.
  7. Persist a strategy_runs row + one signals row per emitted candidate
     (passing AND rejected, so the UI can show both).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.config import settings
from stockscan.data.store import get_bars
from stockscan.db import session_scope
from stockscan.regime import detect_regime
from stockscan.risk.filters import FilterChain, PortfolioContext
from stockscan.risk.sizer import position_size
from stockscan.strategies import (
    STRATEGY_REGISTRY,
    RawSignal,
    Strategy,
    discover_strategies,
)
from stockscan.technical import compute_technical_score, upsert_score
from stockscan.universe import current_constituents, members_as_of

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RegimeFactor:
    """Soft-sizing inputs derived from the current market regime.

    Replaces the v1 ``regime_skipped`` hard gate. Computed once per scan
    invocation and applied to every signal's base qty:

        final_qty = round(base_qty * multiplier)

    ``multiplier`` is the product of three terms:
      * The strategy's affinity for the current discrete regime label.
      * The composite-score health multiplier (``0.5 + 0.5 * composite``,
        per the research doc's conservative recommendation — never zeros
        out exposure entirely on the composite alone).
      * A 0.5x credit-stress override (research doc §Tier 0(b)) when the
        circuit-breaker fires.

    ``block_new_longs`` short-circuits long entries under credit stress
    regardless of the multiplier — sized-down longs would still bleed in
    a rolling drawdown.
    """

    multiplier: float
    label: str | None  # None when regime data unavailable
    composite_score: float | None
    credit_stress_flag: bool
    block_new_longs: bool


@dataclass(frozen=True, slots=True)
class ScanSummary:
    run_id: int  # always >= 0 under soft sizing; -1 only on hard data outage
    strategy_name: str
    strategy_version: str
    as_of_date: date
    universe_size: int
    signals_emitted: int
    rejected_count: int
    # Kept for back-compat with v1 dashboards. With soft sizing nothing
    # is skipped on regime grounds, so the field stays but always reports
    # False — callers that surface it should migrate to ``regime_multiplier``.
    regime_skipped: bool = False
    # Effective regime multiplier applied to base sizing (1.0 = neutral,
    # 0.0 = no exposure on regime grounds).
    regime_multiplier: float = 1.0


class ScanRunner:
    """One scanner invocation. Stateful per-instance for clarity."""

    def __init__(self, session: Session | None = None) -> None:
        self._owns_session = session is None
        self._session = session

    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    def _run_in_session(
        self,
        s: Session,
        strategy_cls: type[Strategy],
        as_of: date,
        symbols: list[str] | None,
    ) -> ScanSummary:
        # 1. Ensure strategy_versions + active strategy_configs row exist.
        config_id, params = self._ensure_strategy_config(s, strategy_cls)
        strategy = strategy_cls(params)

        # 1b. Resolve the regime soft-sizing multiplier. Replaces the v1
        #     hard gate: instead of skipping the strategy when the regime
        #     doesn't match, we compute a multiplier (0.0..1.0+) that scales
        #     each signal's base qty. ``block_new_longs`` short-circuits
        #     long entries when credit stress fires.
        regime_factor = self._resolve_regime_factor(s, strategy_cls, as_of)

        # 2. Resolve universe.
        if symbols is None:
            symbols = members_as_of(as_of, session=s) or current_constituents(session=s)
        log.info(
            "scanning %s v%s on %d symbols as of %s (regime mult=%.3f)",
            strategy_cls.name,
            strategy_cls.version,
            len(symbols),
            as_of,
            regime_factor.multiplier,
        )

        # 3. Build portfolio context (positions, equity, earnings).
        ctx = self._portfolio_context(s, as_of)
        chain = FilterChain.default(
            max_positions=settings.max_positions,
            max_position_pct=settings.max_position_pct,
            max_sector_pct=settings.max_sector_pct,
            max_adv_pct=settings.max_adv_pct,
            max_drawdown=settings.drawdown_circuit_breaker,
        )

        # 4. Run signals + sizer + filters per symbol.
        # We cache bars per symbol so the technical-score step can reuse them
        # without a second DB roundtrip per signal.
        bars_cache: dict[str, pd.DataFrame] = {}
        passing: list[tuple[RawSignal, int]] = []
        rejected: list[tuple[RawSignal, int, str]] = []
        for symbol in symbols:
            try:
                bars = get_bars(symbol, as_of.replace(year=as_of.year - 5), as_of, session=s)
            except Exception as exc:
                log.debug("skip %s — bars query failed: %s", symbol, exc)
                continue
            if bars.empty or len(bars) < strategy.required_history():
                continue
            # Stamp the symbol so strategy.signals() can extract it.
            bars.attrs["symbol"] = symbol
            bars_cache[symbol] = bars
            raw_sigs = strategy.signals(bars, as_of)
            for sig in raw_sigs:
                base_qty = self._size(sig)
                if base_qty <= 0:
                    rejected.append((sig, 0, "qty_zero"))
                    continue

                # Credit-stress override: hard-block new long entries
                # regardless of the multiplier (research doc §Tier 0(b)).
                if regime_factor.block_new_longs and sig.side == "long":
                    rejected.append((sig, base_qty, "credit_stress_long_block"))
                    continue

                # Soft sizing: scale base qty by the regime multiplier.
                # round() with one arg already returns int in Py3.
                qty = max(0, round(base_qty * regime_factor.multiplier))
                if qty <= 0:
                    rejected.append((sig, 0, "regime_zero_size"))
                    continue
                result = chain.evaluate(sig, qty, ctx)
                if result.passed:
                    passing.append((sig, qty))
                else:
                    rejected.append((sig, qty, result.reason or "filter_rejected"))

        # 5. Persist signals, then technical scores for each.
        run_id = self._persist_run(
            s,
            strategy_cls,
            config_id,
            as_of,
            len(symbols),
            len(passing),
            len(rejected),
        )
        for sig, qty in passing:
            self._persist_signal(s, run_id, strategy_cls, config_id, as_of, sig, qty, "new", None)
            self._persist_tech_score(s, strategy_cls, sig.symbol, as_of, bars_cache.get(sig.symbol))
        for sig, qty, reason in rejected:
            self._persist_signal(
                s, run_id, strategy_cls, config_id, as_of, sig, qty, "rejected", reason
            )
            self._persist_tech_score(s, strategy_cls, sig.symbol, as_of, bars_cache.get(sig.symbol))

        return ScanSummary(
            run_id=run_id,
            strategy_name=strategy_cls.name,
            strategy_version=strategy_cls.version,
            as_of_date=as_of,
            universe_size=len(symbols),
            signals_emitted=len(passing),
            rejected_count=len(rejected),
            regime_multiplier=regime_factor.multiplier,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _ensure_strategy_config(
        self,
        s: Session,
        strategy_cls: type[Strategy],
    ) -> tuple[int, Any]:
        """Make sure strategy_versions + strategy_configs rows exist; return config_id + params."""
        # version row
        s.execute(
            text(
                """
                INSERT INTO strategy_versions
                    (strategy_name, strategy_version, display_name, description, tags,
                     params_json_schema, code_fingerprint)
                VALUES
                    (:n, :v, :dn, :d, :t,
                     CAST(:schema AS JSONB), :fp)
                ON CONFLICT (strategy_name, strategy_version) DO NOTHING;
                """
            ),
            {
                "n": strategy_cls.name,
                "v": strategy_cls.version,
                "dn": strategy_cls.display_name,
                "d": strategy_cls.description,
                "t": list(strategy_cls.tags),
                "schema": json.dumps(strategy_cls.params_json_schema()),
                "fp": strategy_cls.code_fingerprint(),
            },
        )

        # active config — if none exists, create with defaults
        existing = s.execute(
            text(
                "SELECT config_id, params_json FROM strategy_configs "
                "WHERE strategy_name = :n AND active = TRUE LIMIT 1"
            ),
            {"n": strategy_cls.name},
        ).first()
        if existing:
            params = strategy_cls.params_model(**existing.params_json)
            return int(existing.config_id), params

        params = strategy_cls.params_model()
        params_json = params.model_dump(mode="json")
        params_hash = strategy_cls.hash_params(params)
        row = s.execute(
            text(
                """
                INSERT INTO strategy_configs
                    (strategy_name, strategy_version, params_json, params_hash,
                     created_by, note)
                VALUES (:n, :v, CAST(:p AS JSONB), :h, 'system', 'auto-created defaults')
                RETURNING config_id;
                """
            ),
            {
                "n": strategy_cls.name,
                "v": strategy_cls.version,
                "p": json.dumps(params_json),
                "h": params_hash,
            },
        ).one()
        return int(row.config_id), params

    def _size(self, signal: RawSignal) -> int:
        # Quick pass through the sizer using config defaults.
        sizing = position_size(
            equity=Decimal("1000000"),  # placeholder; live equity wired in via context
            entry_price=signal.suggested_entry,
            stop_price=signal.suggested_stop,
            risk_pct=Decimal(str(settings.default_risk_pct)),
            max_position_pct=Decimal(str(settings.max_position_pct)),
        )
        return sizing.qty

    def _portfolio_context(self, s: Session, as_of: date) -> PortfolioContext:
        # Latest NAV
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
        equity = Decimal(str(eq_row.total_equity)) if eq_row else Decimal("1000000")
        hwm = Decimal(str(eq_row.high_water_mark)) if eq_row else equity

        # Open positions
        pos_rows = s.execute(
            text(
                """
                SELECT symbol, strategy, qty, avg_cost
                FROM positions
                """
            )
        ).all()
        open_positions = {
            r.symbol: {
                "qty": Decimal(r.qty),
                "notional": Decimal(str(r.qty)) * Decimal(str(r.avg_cost)),
                "strategy": r.strategy,
            }
            for r in pos_rows
        }

        # Earnings within 5 trading days (calendar approximation = 7 calendar days)
        earnings_rows = s.execute(
            text(
                """
                SELECT DISTINCT symbol FROM earnings_calendar
                WHERE report_date BETWEEN :d AND :d + INTERVAL '7 days'
                """
            ),
            {"d": as_of},
        ).all()
        earnings = {r.symbol for r in earnings_rows}

        return PortfolioContext(
            as_of=as_of,
            equity=equity,
            high_water_mark=hwm,
            open_positions=open_positions,
            earnings_within_5d=earnings,
        )

    def _resolve_regime_factor(
        self,
        s: Session,
        strategy_cls: type[Strategy],
        as_of: date,
    ) -> RegimeFactor:
        """Compute the soft-sizing multiplier for this strategy.

        Replaces the v1 ``_check_regime`` hard gate. Multiplier is the
        product of:

          * ``strategy_cls.affinity_for(label)`` — strategy's preference
            for the current discrete regime label.
          * ``0.5 + 0.5 * composite_score`` — continuous health
            multiplier (research doc §6.1's conservative form; never
            zeros out exposure on the composite alone).
          * ``0.5`` if ``credit_stress_flag`` is set, else ``1.0``
            (research doc §Tier 0(b)).

        Soft-fails to a neutral 1.0 multiplier on any failure path so
        a regime-data outage doesn't block the strategy from running.
        """
        # Default = neutral. Used when regime detection is degraded or fails.
        neutral = RegimeFactor(
            multiplier=1.0,
            label=None,
            composite_score=None,
            credit_stress_flag=False,
            block_new_longs=False,
        )

        try:
            regime_obj = detect_regime(as_of, session=s)
        except Exception as exc:
            log.warning("regime detection failed — using neutral multiplier: %s", exc)
            return neutral

        if regime_obj is None:
            log.info("regime: no row for %s — using neutral multiplier", as_of)
            return neutral

        label = regime_obj.regime
        affinity = strategy_cls.affinity_for(label)
        composite_dec = regime_obj.composite_score
        composite = float(composite_dec) if composite_dec is not None else None
        composite_mult = 0.5 + 0.5 * composite if composite is not None else 1.0
        stress = bool(regime_obj.credit_stress_flag)
        stress_mult = 0.5 if stress else 1.0

        multiplier = affinity * composite_mult * stress_mult
        log.info(
            "regime sizing: %s @ %s | affinity=%.2f composite_mult=%.2f stress_mult=%.2f -> %.3f%s",
            strategy_cls.name,
            label,
            affinity,
            composite_mult,
            stress_mult,
            multiplier,
            " [block_new_longs]" if stress else "",
        )

        return RegimeFactor(
            multiplier=multiplier,
            label=label,
            composite_score=composite,
            credit_stress_flag=stress,
            block_new_longs=stress,
        )

    def _persist_run(
        self,
        s: Session,
        strategy_cls: type[Strategy],
        config_id: int,
        as_of: date,
        universe_size: int,
        n_pass: int,
        n_reject: int,
    ) -> int:
        row = s.execute(
            text(
                """
                INSERT INTO strategy_runs
                    (strategy_name, strategy_version, config_id, as_of_date,
                     universe_size, signals_emitted, rejected_count)
                VALUES (:n, :v, :c, :d, :u, :s, :r)
                RETURNING run_id;
                """
            ),
            {
                "n": strategy_cls.name,
                "v": strategy_cls.version,
                "c": config_id,
                "d": as_of,
                "u": universe_size,
                "s": n_pass,
                "r": n_reject,
            },
        ).one()
        return int(row.run_id)

    def _persist_tech_score(
        self,
        s: Session,
        strategy_cls: type[Strategy],
        symbol: str,
        as_of: date,
        bars: pd.DataFrame | None,
    ) -> None:
        """Compute and upsert the technical confirmation score for one signal."""
        if bars is None or bars.empty:
            return
        try:
            score = compute_technical_score(strategy_cls, bars, as_of)
        except Exception as exc:
            log.debug("tech score failed for %s: %s", symbol, exc)
            return
        if score is None:
            return
        try:
            upsert_score(symbol, as_of, strategy_cls.name, score, session=s)
        except Exception as exc:
            log.error("tech score upsert failed for %s: %s", symbol, exc)

    def _persist_signal(
        self,
        s: Session,
        run_id: int,
        strategy_cls: type[Strategy],
        config_id: int,
        as_of: date,
        sig: RawSignal,
        qty: int,
        status: str,
        rejected_reason: str | None,
    ) -> None:
        s.execute(
            text(
                """
                INSERT INTO signals
                    (run_id, strategy_name, strategy_version, config_id,
                     symbol, side, score, as_of_date,
                     suggested_entry, suggested_stop, suggested_target, suggested_qty,
                     rejected_reason, metadata, status)
                VALUES (:run_id, :n, :v, :c,
                        :symbol, :side, :score, :as_of,
                        :entry, :stop, :target, :qty,
                        :reason, CAST(:meta AS JSONB), :status);
                """
            ),
            {
                "run_id": run_id,
                "n": strategy_cls.name,
                "v": strategy_cls.version,
                "c": config_id,
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
