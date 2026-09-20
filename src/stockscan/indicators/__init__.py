"""Technical primitives used by strategies and the regime layer.

Hand-rolled, dependency-light, NaN-safe. Each function answers one question
about a bar series: a moving average, RSI, ATR, dollar volume, realized
volatility, or a stock's return relative to its sector composite. They are
state descriptors and normalizers — strategies compose them; none is an
entry oracle on its own.

All functions accept a price/bar Series or DataFrame and return a Series
aligned to the input index. NaN at the start (insufficient history) is
intentional — never silently filled.
"""

from stockscan.indicators.relative_strength import (
    sector_relative_return,
    sector_return,
)
from stockscan.indicators.ta import (
    atr,
    avg_dollar_volume,
    ema,
    rsi,
    sma,
    true_range,
    yang_zhang_volatility,
    yang_zhang_volatility_ewm,
)

__all__ = [
    "atr",
    "avg_dollar_volume",
    "ema",
    "rsi",
    "sector_relative_return",
    "sector_return",
    "sma",
    "true_range",
    "yang_zhang_volatility",
    "yang_zhang_volatility_ewm",
]
