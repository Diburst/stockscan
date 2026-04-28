"""Event-driven backtester (DESIGN §4.4).

Shares the Strategy code path with the live engine — same `signals()` and
`exit_rules()` functions are called from both contexts. The same risk
filters apply. Only the persistence layer differs (separate backtest_*
tables) and the broker is replaced with PaperBroker fills against historical
bars at next-day open.
"""

from stockscan.backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from stockscan.backtest.slippage import (
    FixedBpsSlippage,
    NoSlippage,
    SlippageModel,
    VolumeBasedSlippage,
)

__all__ = [
    "BacktestConfig",
    "BacktestEngine",
    "BacktestResult",
    "FixedBpsSlippage",
    "NoSlippage",
    "SlippageModel",
    "VolumeBasedSlippage",
]
