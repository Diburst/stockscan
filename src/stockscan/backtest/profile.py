"""Profile a real backtest run under cProfile.

Wraps :meth:`BacktestEngine.run` in ``cProfile.Profile``, then dumps a
ranked stats report (cumtime / tottime / ncalls) and optionally a raw
``.prof`` file for snakeviz / pyinstrument / SpeedScope.

The point is **diagnosis, not benchmarking**: identify which functions
actually dominate the wall clock so the next perf change is targeted
instead of guessed. The DB layer is already heavily cached (DESIGN
§4.4.1); the working hypothesis going in is that pandas indicator
recomputation in the per-(symbol, day) loop is the new ceiling.

Two entry points consume this module:

  - ``stockscan backtest profile STRAT ...`` — the typer subcommand
    in ``stockscan.cli``.
  - ``python tools/profile_backtest.py STRAT ...`` — a standalone
    shim kept for dev / ad-hoc invocation outside the venv-installed
    CLI script.

Both share the same arg shape and call into :func:`run_profile` here.
"""

from __future__ import annotations

import cProfile
import io
import pstats
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from stockscan.backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    BacktestResult,
)
from stockscan.backtest.slippage import FixedBpsSlippage


# Small liquid universe used as the profile default when ``-s/--symbol`` is
# omitted. Full S&P 500 profiles take ~40 minutes under cProfile's
# instrumentation overhead — too slow for iterative perf work, which is
# what this tool exists for. Use ``--sp500`` for the rare full-scale run.
DEV_SYMBOLS: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "TSLA", "AMD",
    "META", "GOOGL", "AMZN", "JPM", "WMT",
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ProfileOptions:
    """Knobs that don't shape the backtest itself, only how it's profiled."""

    top: int = 30          # how many ranked lines to print
    sort: str = "cumtime"  # pstats sort key (cumtime / tottime / ncalls)
    callers: str | None = None  # optional regex; print top callers of matches
    out_path: str | None = None  # optional .prof file to dump
    strip_dirs: bool = True       # basenames only in the report


def build_profile_config(
    *,
    strategy_cls: type,
    start: date,
    end: date,
    symbols: Iterable[str] | None = None,
    sp500: bool = False,
    capital: Decimal = Decimal("1000000"),
    risk_pct: Decimal = Decimal("0.01"),
    slippage_bps: Decimal = Decimal("5"),
) -> BacktestConfig:
    """Assemble a :class:`BacktestConfig` from profile-tool arguments.

    Mirrors what ``stockscan backtest run`` builds, with one
    profile-specific default: when ``symbols`` is omitted AND ``sp500``
    is False, fall back to :data:`DEV_SYMBOLS` (the small liquid set).
    Pass ``sp500=True`` to explicitly request historical S&P 500
    membership (the engine's default when ``universe is None``).
    """
    if symbols is not None and sp500:
        raise ValueError("specify EITHER --symbol(s) OR --sp500, not both.")

    if sp500:
        universe: list[str] | None = None  # engine uses historical S&P 500 membership
    elif symbols:
        universe = [s.strip().upper() for s in symbols if s and s.strip()]
        if not universe:
            universe = list(DEV_SYMBOLS)
    else:
        universe = list(DEV_SYMBOLS)

    return BacktestConfig(
        strategy_cls=strategy_cls,
        params=strategy_cls.params_model() if strategy_cls.params_model is not None else None,
        start_date=start,
        end_date=end,
        starting_capital=capital,
        risk_pct=risk_pct,
        slippage=FixedBpsSlippage(bps=slippage_bps),
        universe=universe,
    )


def run_profile(config: BacktestConfig, options: ProfileOptions) -> BacktestResult:
    """Run the backtest under cProfile and print the ranked hotspot report.

    Returns the :class:`BacktestResult` so callers (the typer command,
    the shim, tests) can also report trade counts or persist if needed.
    Pure side-effect-on-stdout otherwise — no DB writes.
    """
    print(
        f"profiling {config.strategy_cls.name} on "
        f"{len(config.universe) if config.universe else 'historical S&P 500'} symbols, "
        f"{config.start_date} → {config.end_date}",
        file=sys.stderr,
    )

    engine = BacktestEngine(config)
    profiler = cProfile.Profile()
    profiler.enable()
    try:
        result = engine.run()
    finally:
        profiler.disable()

    print(
        f"backtest complete: {len(result.trades)} trades, "
        f"ending equity ${float(result.equity_curve.iloc[-1]):,.0f}",
        file=sys.stderr,
    )

    _print_report(profiler, options)
    return result


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _print_report(profiler: cProfile.Profile, opts: ProfileOptions) -> None:
    buf = io.StringIO()
    stats = pstats.Stats(profiler, stream=buf)
    if opts.strip_dirs:
        stats.strip_dirs()
    stats.sort_stats(opts.sort)
    stats.print_stats(opts.top)
    print(f"\n=== Top functions (sorted by {opts.sort}) ===\n")
    print(buf.getvalue())

    if opts.callers:
        cbuf = io.StringIO()
        stats.stream = cbuf
        stats.print_callers(opts.callers)
        print(f"\n=== Callers of /{opts.callers}/ ===\n")
        print(cbuf.getvalue())

    if opts.out_path:
        stats.dump_stats(opts.out_path)
        print(f"raw stats written to {opts.out_path!r} — open with: snakeviz {opts.out_path}")
