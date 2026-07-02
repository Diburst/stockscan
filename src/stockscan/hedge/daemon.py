"""The delta-hedge daemon — a single asyncio process that hedges every active
position in real time.

Shape
-----
One process, one price feed (see :mod:`hedge.feed`), N hedge positions. All
state lives in Postgres; the daemon holds only a small in-memory mirror it
rebuilds from the DB on startup, so a crash-and-restart resumes exactly where it
left off (crash-safety = "the ledger is the truth").

Two concurrent tasks:
  * **refresh loop** (slow, every ``refresh_interval_s``) — reloads the active
    positions, reconciles the feed's subscription set, seeds simulated prices,
    refreshes σ once per day, settles anything past expiry, writes the heartbeat.
  * **tick loop** (fast) — consumes ``feed.stream()`` and, per tick, recomputes
    the target hedge for each position on that symbol and trades if we've fallen
    out of the no-transaction band.

The per-tick decision is factored into the pure :func:`plan_tick` so it can be
unit-tested without a DB or an event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from datetime import UTC, date, datetime

from stockscan.analysis import black_scholes
from stockscan.config import settings
from stockscan.hedge import accounting, service, store, vol
from stockscan.hedge.feed import EodhdWebsocketFeed, PriceFeed, PriceTick, SimulatedFeed
from stockscan.hedge.policy import HedgePolicy

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TickPlan:
    """The daemon's decision for one (position, spot) pair."""

    expired: bool
    delta: float  # signed option-position delta, share-equivalents
    gamma: float  # signed option-position gamma
    target_shares: int
    rebalance: bool
    fill_qty: int  # signed shares to trade (0 if not rebalancing)


def plan_tick(
    *,
    held_shares: int,
    option_kind: str,
    option_side: str,
    strike: float,
    contracts: int,
    multiplier: int,
    iv_pct: float,
    rate_pct: float,
    expiry: datetime,
    policy: HedgePolicy,
    spot: float,
    last_hedge_spot: float | None,
    now: datetime,
) -> TickPlan:
    """Pure per-tick decision: target shares + whether to trade. No I/O."""
    if now >= expiry:
        return TickPlan(expired=True, delta=0.0, gamma=0.0, target_shares=held_shares,
                        rebalance=False, fill_qty=0)

    t = black_scholes.years_to_expiry(expiry, now)
    sigma = iv_pct / 100.0
    r = rate_pct / 100.0
    delta = black_scholes.position_delta(
        spot, strike, t, r, sigma, option_kind, option_side, contracts, multiplier=multiplier
    )
    gamma = black_scholes.position_gamma(
        spot, strike, t, r, sigma, option_kind, option_side, contracts, multiplier=multiplier
    )
    target = round(-delta)
    rebalance = policy.should_rebalance(
        held_shares=held_shares,
        target_shares=target,
        spot=spot,
        position_gamma=gamma,
        last_hedge_spot=last_hedge_spot,
    )
    return TickPlan(
        expired=False,
        delta=delta,
        gamma=gamma,
        target_shares=target,
        rebalance=rebalance,
        fill_qty=(target - held_shares) if rebalance else 0,
    )


@dataclass
class _Runtime:
    """In-memory mirror of a position's mutable hedge state."""

    position: store.HedgePosition
    held_shares: int
    avg_cost: float
    realized_hedge_pnl: float
    last_hedge_spot: float | None
    iv_pct: float
    rate_pct: float
    policy: HedgePolicy
    last_write_ts: float = 0.0


class HedgeDaemon:
    """Owns the feed + the hedge loop for every active position."""

    def __init__(
        self,
        *,
        feed: PriceFeed | None = None,
        refresh_interval_s: float = 15.0,
        tick_write_throttle_s: float = 3.0,
    ) -> None:
        self._feed = feed or self._build_default_feed()
        self._refresh_interval = refresh_interval_s
        self._throttle = tick_write_throttle_s
        self._runtime: dict[int, _Runtime] = {}
        self._by_symbol: dict[str, list[int]] = {}
        self._stop = asyncio.Event()
        self._feed_kind = type(self._feed).__name__

    # ---- feed selection ----
    @staticmethod
    def _build_default_feed() -> PriceFeed:
        token = settings.eodhd_api_key.get_secret_value()
        if token:
            log.info("hedge daemon: using live EODHD websocket feed")
            return EodhdWebsocketFeed(token)
        log.warning("hedge daemon: no EODHD_API_KEY — falling back to SimulatedFeed")
        return SimulatedFeed()

    # ---- lifecycle ----
    async def run(self) -> None:
        log.info("hedge daemon starting (pid=%d, feed=%s)", os.getpid(), self._feed_kind)
        self._install_signal_handlers()
        store.write_heartbeat(pid=os.getpid(), feed_kind=self._feed_kind, active_symbols=0,
                              status="starting", started=True)
        await self._reload_positions(initial=True)

        refresh_task = asyncio.create_task(self._refresh_loop(), name="hedge-refresh")
        tick_task = asyncio.create_task(self._tick_loop(), name="hedge-ticks")
        stop_task = asyncio.create_task(self._stop.wait(), name="hedge-stop")
        try:
            await asyncio.wait({refresh_task, tick_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._stop.set()
            for tsk in (refresh_task, tick_task):
                tsk.cancel()
            await asyncio.gather(refresh_task, tick_task, return_exceptions=True)
            await self._feed.close()
            store.write_heartbeat(pid=os.getpid(), feed_kind=self._feed_kind,
                                  active_symbols=len(self._by_symbol), status="stopped")
            log.info("hedge daemon stopped")

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError):
                pass  # e.g. non-main thread / Windows — rely on KeyboardInterrupt.

    def stop(self) -> None:
        self._stop.set()

    # ---- slow loop: reconcile positions, symbols, σ, expiry, heartbeat ----
    async def _refresh_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._reload_positions()
                await self._settle_expired()
                store.write_heartbeat(
                    pid=os.getpid(), feed_kind=self._feed_kind,
                    active_symbols=len(self._by_symbol), status="running",
                )
            except Exception:  # noqa: BLE001 - never let the loop die.
                log.exception("hedge daemon: refresh loop error")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._refresh_interval)
            except TimeoutError:
                pass

    async def _reload_positions(self, *, initial: bool = False) -> None:
        active = store.list_active_hedge_positions()
        seen: set[int] = set()
        by_symbol: dict[str, list[int]] = {}
        for pos in active:
            seen.add(pos.hedge_position_id)
            by_symbol.setdefault(pos.symbol, []).append(pos.hedge_position_id)
            rt = self._runtime.get(pos.hedge_position_id)
            if rt is None:
                # New (or recovered-after-restart) position — hydrate from DB.
                iv = float(pos.iv_pct) if pos.iv_pct is not None else None
                if iv is None:
                    iv = await self._resolve_iv(pos)
                rt = _Runtime(
                    position=pos,
                    held_shares=pos.held_shares,
                    avg_cost=float(pos.avg_cost),
                    realized_hedge_pnl=float(pos.realized_hedge_pnl),
                    last_hedge_spot=float(pos.last_hedge_spot) if pos.last_hedge_spot else None,
                    iv_pct=iv or 0.0,
                    rate_pct=float(pos.rate_pct) if pos.rate_pct is not None else settings.risk_free_rate * 100.0,
                    policy=HedgePolicy.from_dict(pos.band_policy),
                )
                self._runtime[pos.hedge_position_id] = rt
                # Seed the simulated feed off the last known / last-close price.
                if isinstance(self._feed, SimulatedFeed):
                    seed = float(pos.last_spot) if pos.last_spot else await self._seed_price(pos.symbol)
                    if seed:
                        self._feed.seed(pos.symbol, seed)
            else:
                rt.position = pos  # refresh static fields (expiry, premium, etc.)
        # Drop positions that are no longer active (paused/closed).
        for pid in list(self._runtime):
            if pid not in seen:
                del self._runtime[pid]
        self._by_symbol = by_symbol
        await self._feed.set_symbols(set(by_symbol))
        # Daily σ refresh.
        await self._refresh_iv_daily()

    async def _seed_price(self, symbol: str) -> float | None:
        return await asyncio.to_thread(self._last_close, symbol)

    @staticmethod
    def _last_close(symbol: str) -> float | None:
        try:
            from datetime import timedelta

            from stockscan.data.store import get_bars

            end = datetime.now(UTC)
            bars = get_bars(symbol, end - timedelta(days=10), end)
            if bars is not None and not bars.empty:
                return float(bars["close"].iloc[-1])
        except Exception as exc:  # noqa: BLE001
            log.debug("hedge daemon: seed price lookup failed for %s: %s", symbol, exc)
        return None

    async def _resolve_iv(self, pos: store.HedgePosition) -> float | None:
        iv = await asyncio.to_thread(vol.realized_vol_pct, pos.symbol)
        if iv is not None:
            store.refresh_iv(pos.hedge_position_id, iv, date.today())
        return iv

    async def _refresh_iv_daily(self) -> None:
        today = date.today()
        for rt in self._runtime.values():
            if rt.position.iv_refreshed_on == today:
                continue
            iv = await asyncio.to_thread(vol.realized_vol_pct, rt.position.symbol)
            if iv is not None:
                rt.iv_pct = iv
                store.refresh_iv(rt.position.hedge_position_id, iv, today)

    async def _settle_expired(self) -> None:
        now = datetime.now(UTC)
        for pid in list(self._runtime):
            rt = self._runtime.get(pid)
            if rt is None or now < rt.position.expiry:
                continue
            spot = float(rt.position.last_spot) if rt.position.last_spot else rt.avg_cost or float(rt.position.strike)
            fresh = store.get_hedge_position(pid)
            if fresh is not None and fresh.status == "active":
                await asyncio.to_thread(service.settle_and_close, fresh, spot=spot, reason="expiry_settle")
            self._runtime.pop(pid, None)

    # ---- fast loop: consume ticks, hedge ----
    async def _tick_loop(self) -> None:
        try:
            async for tick in self._feed.stream():
                if self._stop.is_set():
                    break
                await self._on_tick(tick)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("hedge daemon: tick loop crashed")
            self._stop.set()

    async def _on_tick(self, tick: PriceTick) -> None:
        pids = self._by_symbol.get(tick.symbol)
        if not pids:
            return
        now = datetime.now(UTC)
        for pid in pids:
            rt = self._runtime.get(pid)
            if rt is None or rt.iv_pct <= 0:
                continue
            plan = plan_tick(
                held_shares=rt.held_shares,
                option_kind=rt.position.option_kind,
                option_side=rt.position.option_side,
                strike=float(rt.position.strike),
                contracts=rt.position.contracts,
                multiplier=rt.position.multiplier,
                iv_pct=rt.iv_pct,
                rate_pct=rt.rate_pct,
                expiry=rt.position.expiry,
                policy=rt.policy,
                spot=tick.price,
                last_hedge_spot=rt.last_hedge_spot,
                now=now,
            )
            if plan.expired:
                await self._settle_expired()
                continue
            if plan.rebalance and plan.fill_qty != 0:
                await self._execute_fill(rt, plan, tick.price, now)
            else:
                await self._maybe_write_mark(rt, plan, tick.price, now)

    async def _execute_fill(self, rt: _Runtime, plan: TickPlan, spot: float, now: datetime) -> None:
        held_before = rt.held_shares
        fill = accounting.apply_fill(
            held_shares=rt.held_shares,
            avg_cost=rt.avg_cost,
            realized_pnl=rt.realized_hedge_pnl,
            fill_qty=plan.fill_qty,
            fill_price=spot,
        )
        await asyncio.to_thread(
            store.apply_adjustment,
            rt.position.hedge_position_id,
            fill_qty=plan.fill_qty,
            fill_price=spot,
            spot=spot,
            option_delta=plan.delta,
            target_shares=plan.target_shares,
            held_before=held_before,
            held_after=fill.held_shares,
            new_avg_cost=fill.avg_cost,
            new_realized_hedge_pnl=fill.realized_pnl,
            realized_pnl_delta=fill.realized_delta,
            reason="band_breach",
            tick_at=now,
        )
        rt.held_shares = fill.held_shares
        rt.avg_cost = fill.avg_cost
        rt.realized_hedge_pnl = fill.realized_pnl
        rt.last_hedge_spot = spot
        rt.last_write_ts = now.timestamp()
        log.info(
            "hedge #%d %s %+d %s @ %.2f → held=%d (target=%d, Δ=%.1f)",
            rt.position.hedge_position_id,
            "BUY" if plan.fill_qty > 0 else "SELL",
            plan.fill_qty, rt.position.symbol, spot, fill.held_shares,
            plan.target_shares, plan.delta,
        )

    async def _maybe_write_mark(self, rt: _Runtime, plan: TickPlan, spot: float, now: datetime) -> None:
        if now.timestamp() - rt.last_write_ts < self._throttle:
            return
        rt.last_write_ts = now.timestamp()
        await asyncio.to_thread(
            store.update_tick_state,
            rt.position.hedge_position_id,
            last_spot=spot,
            last_delta=plan.delta,
            last_target_shares=plan.target_shares,
            last_tick_at=now,
        )


def run_daemon() -> None:
    """Blocking entry point (used by the CLI ``stockscan hedge run``)."""
    daemon = HedgeDaemon()
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        log.info("hedge daemon: interrupted")
