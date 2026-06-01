"""ensure_strategy_version upserts the strategy_versions row (no DB needed).

Regression for the backtest ForeignKeyViolation: a strategy that had only ever
been backtested (never scanned) had no strategy_versions row, so backtest_runs
failed its FK. The backtester now calls ensure_strategy_version too.
"""

from __future__ import annotations

from stockscan.strategies.reversal_swing import ReversalSwing
from stockscan.strategies.registration import ensure_strategy_version


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute(self, sql, params=None):  # noqa: ANN001
        self.calls.append((str(sql), params or {}))
        return None


def test_ensure_strategy_version_upserts_with_conflict_guard():
    fake = _FakeSession()
    ensure_strategy_version(ReversalSwing, session=fake)

    assert len(fake.calls) == 1
    sql, params = fake.calls[0]
    assert "INSERT INTO strategy_versions" in sql
    assert "ON CONFLICT (strategy_name, strategy_version) DO NOTHING" in sql
    assert params["n"] == "reversal_swing"
    assert params["v"] == ReversalSwing.version
    assert params["dn"]  # display_name present
    assert isinstance(params["t"], list)  # tags as a list (TEXT[])
    assert isinstance(params["schema"], str)  # JSON-serialized params schema
    assert params["fp"]  # code fingerprint present
