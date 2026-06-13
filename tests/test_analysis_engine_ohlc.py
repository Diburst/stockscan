"""Engine ohlc_history contract — powers the interactive candlestick
mini-charts on the /analysis hub.

No DB: bars are passed directly and the session is mocked. The sub-modules
that touch the session (options_context) soft-fail and don't affect the
ohlc_history slice, which is built purely from the bars frame.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pandas as pd

from stockscan.analysis.engine import _CHART_HISTORY_DAYS, analyze_symbol


def _bars(n: int) -> pd.DataFrame:
    idx = pd.date_range("2022-01-03", periods=n, freq="B")
    rng = np.random.default_rng(11)
    closes = 100 + np.cumsum(rng.normal(0, 1, n))
    return pd.DataFrame(
        {
            "open": closes + rng.normal(0, 0.3, n),
            "high": closes + 1.0,
            "low": closes - 1.0,
            "close": closes,
            "adj_close": closes,
            "volume": rng.integers(100_000, 1_000_000, n),
        },
        index=idx,
    )


def test_ohlc_history_shape():
    bars = _bars(300)
    a = analyze_symbol("TEST", bars=bars, session=MagicMock())
    assert a.available
    assert a.ohlc_history, "ohlc_history should be populated"
    rec = a.ohlc_history[-1]
    for k in ("time", "open", "high", "low", "close", "volume"):
        assert k in rec, f"ohlc record missing {k}"
    # Lightweight-Charts time is the YYYY-MM-DD string.
    assert isinstance(rec["time"], str) and rec["time"][4] == "-"
    # Chronological: last record is the most recent close.
    assert rec["close"] == float(bars["close"].iloc[-1])


def test_ohlc_history_capped():
    n = _CHART_HISTORY_DAYS + 150
    a = analyze_symbol("TEST", bars=_bars(n), session=MagicMock())
    assert len(a.ohlc_history) == _CHART_HISTORY_DAYS
    # closes_history shares the same cap.
    assert len(a.closes_history) == _CHART_HISTORY_DAYS
