"""ensure_strategy_version upserts the strategy_versions row (no DB needed).

Regression for the backtest ForeignKeyViolation: a strategy that had only ever
been backtested (never scanned) had no strategy_versions row, so backtest_runs
failed its FK. The backtester now calls ensure_strategy_version too.
"""

from __future__ import annotations

import json

import pytest

from stockscan.strategies.momentum_52w import Momentum52WeekHigh
from stockscan.strategies.registration import ensure_strategy_version
from stockscan.strategies.rsi2_meanrev import RSI2MeanReversion


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute(self, sql, params=None):  # noqa: ANN001
        self.calls.append((str(sql), params or {}))
        return None


@pytest.mark.parametrize("strategy_cls", [RSI2MeanReversion, Momentum52WeekHigh])
def test_ensure_strategy_version_upserts_with_conflict_guard(strategy_cls):
    fake = _FakeSession()
    ensure_strategy_version(strategy_cls, session=fake)

    assert len(fake.calls) == 1
    sql, params = fake.calls[0]
    assert "INSERT INTO strategy_versions" in sql
    assert "ON CONFLICT (strategy_name, strategy_version) DO NOTHING" in sql
    assert params["n"] == strategy_cls.name
    assert params["v"] == strategy_cls.version
    assert params["dn"] == strategy_cls.display_name
    assert params["t"] == list(strategy_cls.tags)  # tags as a list (TEXT[])
    # The schema column carries the knob dict, JSON-serialised.
    assert json.loads(params["schema"]) == strategy_cls.knobs()
    assert params["fp"] == strategy_cls.code_fingerprint()
