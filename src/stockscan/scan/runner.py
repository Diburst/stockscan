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

import dataclasses
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
from stockscan.ml.predict import score_signal as ml_score_signal
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
class _RegimeShim:
    """Duck-typed stand-in for ``MarketRegime`` used by the meta-label
    feature builder.

    The full ``MarketRegime`` row carries a dozen percentile-rank
    columns that ``build_features`` doesn't read. Rather than
    round-trip the DB inside the scoring loop, we forward the three
    attributes the feature block actually consumes (regime label,
    composite score, credit-stress flag) using the same attribute
    names. The feature builder uses ``getattr(..., default)`` so any
    extra attributes a real ``MarketRegime`` would have stay dormant.
    """

    regime: str
    composite_score: float | None
    credit_stress_flag: bool


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
        #
        # Two passes by design:
        #
        #   Pass A — generate + size: for each symbol, generate signals
        #     and apply the cheap, signal-local checks (pre-reject from
        #     strategy metadata, qty_zero, credit_stress_long_block,
        #     regime_zero_size). Anything killed at this stage is
        #     specific to one signal and doesn't depend on what other
        #     candidates exist, so order doesn't matter here.
        #
        #   Pass B — filter chain: sort the surviving (sig, qty) list
        #     by sig.score DESCENDING, then run the FilterChain. The
        #     chain enforces portfolio-level caps (max_positions,
        #     max_sector_pct, etc.) where contention exists between
        #     candidates — and the strongest-scoring candidates should
        #     win the slot, not the alphabetically-first ones. Stable
        #     sort keeps ties deterministic.
        bars_cache: dict[str, pd.DataFrame] = {}
        passing: list[tuple[RawSignal, int]] = []
        rejected: list[tuple[RawSignal, int, str]] = []
        # Eligible-for-filter-chain queue, populated in Pass A and
        # sorted before Pass B.
        chain_eligible: list[tuple[RawSignal, int]] = []
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
                # Strategy-emitted pre-rejection. Strategies that need
                # to surface a contextual rejection reason (e.g., the
                # Turtle 1L skip-after-winner filter, which depends on
                # PRIOR signal outcomes the FilterChain can't see)
                # populate ``metadata['_strategy_reject_reason']`` on the
                # emitted signal. We route those straight to the
                # rejected list so they appear in the dashboard's
                # Rejected Signals card with the strategy's own reason.
                pre_reject = (sig.metadata or {}).get("_strategy_reject_reason")
                if pre_reject:
                    rejected.append((sig, 0, str(pre_reject)))
                    continue

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

                # Pass-A survivor — defer filter-chain evaluation to
                # the sorted Pass B below.
                chain_eligible.append((sig, qty))

        # ---- Pass B: score-sort, then filter-chain. ----
        # Highest-scoring candidates first. ``None`` scores sink to
        # the bottom (treated as -inf). Ties fall back to insertion
        # order (alphabetical from the universe iteration), keeping
        # behavior deterministic.
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
            else:
                rejected.append((sig, qty, result.reason or "filter_rejected"))

        # 4b. Meta-label scoring pass (advisory only — never rejects).
        #     Loads the trained XGBoost model for this strategy (None
        #     if none exists yet), scores each passing signal, and
        #     attaches the probability into ``signal.metadata`` under
        #     ``meta_label_proba``. RawSignal is frozen so we rebuild
        #     each entry via ``dataclasses.replace`` rather than mutate.
        #
        #     Soft-fails per signal: a model exception logs at warning
        #     level and the signal is persisted without a score. The
        #     score-only integration mode means we never block a trade
        #     on a missing or low score — operators decide based on
        #     accumulated data once they're ready to flip on hard
        #     filtering (TODO: meta-label rejection threshold).
        passing = self._meta_score_pass(passing, bars_cache, as_of, strategy_cls, regime_factor)

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

    def _meta_score_pass(
        self,
        passing: list[tuple[RawSignal, int]],
        bars_cache: dict[str, pd.DataFrame],
        as_of: date,
        strategy_cls: type[Strategy],
        regime_factor: RegimeFactor,
    ) -> list[tuple[RawSignal, int]]:
        """Attach meta-label probability to each passing signal's metadata.

        Score-only integration: never rejects, never alters qty. The
        probability is round-tripped through ``signal.metadata`` so the
        downstream persistence layer stores it under the JSONB
        ``metadata.meta_label_proba`` key for inspection in the
        Signals UI and for later threshold tuning.

        We piggy-back on the regime row already fetched in
        ``_resolve_regime_factor`` rather than re-querying — see
        :meth:`_regime_for_scoring`. If the model isn't trained yet
        :func:`ml_score_signal` returns ``None`` and we skip the
        metadata patch entirely, leaving the signal unchanged.
        """
        if not passing:
            return passing
        regime = self._regime_for_scoring(as_of, regime_factor)
        out: list[tuple[RawSignal, int]] = []
        scored = 0
        for sig, qty in passing:
            bars = bars_cache.get(sig.symbol)
            if bars is None or bars.empty:
                out.append((sig, qty))
                continue
            try:
                proba = ml_score_signal(
                    strategy_name=strategy_cls.name,
                    bars=bars,
                    as_of=as_of,
                    signal_metadata=sig.metadata,
                    signal_score=float(sig.score) if sig.score is not None else None,
                    regime=regime,
                )
            except Exception as exc:  # never break a scan on ML
                log.warning(
                    "meta-score: %s/%s scoring raised: %s",
                    strategy_cls.name,
                    sig.symbol,
                    exc,
                )
                out.append((sig, qty))
                continue
            if proba is None:
                out.append((sig, qty))
                continue
            new_md = {**sig.metadata, "meta_label_proba": round(proba, 4)}
            new_sig = dataclasses.replace(sig, metadata=new_md)
            out.append((new_sig, qty))
            scored += 1
        if scored:
            log.info(
                "meta-score: %s — %d/%d signals scored",
                strategy_cls.name,
                scored,
                len(passing),
            )
        return out

    @staticmethod
    def _regime_for_scoring(_as_of: date, factor: RegimeFactor) -> Any:
        """Return a MarketRegime-shaped object for the meta-label features.

        ``RegimeFactor`` carries the multiplier and label/composite/
        credit-stress fields that the feature builder needs. We pack
        them into a duck-typed shim with the attribute names the
        feature builder expects (``regime``, ``composite_score``,
        ``credit_stress_flag``) so we don't have to round-trip the
        DB. The shim doesn't claim to be a full ``MarketRegime`` and
        :func:`build_features` only reads the attributes it cares about.

        ``_as_of`` is intentionally accepted but unused for now — when
        we promote scoring to read the per-day regime row from the DB
        rather than reusing the once-per-scan ``RegimeFactor``, this
        argument carries the lookup key.
        """
        return _RegimeShim(
            regime=factor.label or "",
            composite_score=factor.composite_score,
            credit_stress_flag=factor.credit_stress_flag,
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
