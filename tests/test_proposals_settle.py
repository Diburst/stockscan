"""``settle_expired`` — outcome columns filled from synthetic bars.

No database: a fake ``Session`` answers the expired-proposals query from a
list of rows and records every settlement UPDATE; ``get_bars`` is patched
on ``stockscan.proposals.settle`` and answers per symbol.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from stockscan.proposals import settle as settle_mod
from stockscan.proposals.settle import settle_expired

RUN_AS_OF = date(2026, 6, 13)  # Saturday
EXPIRY = date(2026, 6, 19)  # Friday
AS_OF = date(2026, 6, 22)  # the Monday night after expiry


def _bars(rows: list[tuple[str, float, float, float]]) -> pd.DataFrame:
    """(day, high, low, close) rows on a UTC index, as ``get_bars`` returns."""
    idx = pd.to_datetime([r[0] for r in rows], utc=True)
    return pd.DataFrame(
        {
            "open": [r[3] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [1e6] * len(rows),
        },
        index=idx,
    )


def _proposal(pid: int, symbol: str, side: str, strike: float):
    return SimpleNamespace(
        proposal_id=pid, symbol=symbol, side=side, strike=Decimal(str(strike)),
        expiry_date=EXPIRY, as_of=RUN_AS_OF,
    )


class _FakeSession:
    def __init__(self, proposals):
        self.proposals = list(proposals)
        self.updates: dict[int, dict] = {}

    def execute(self, sql, params=None):
        text = str(sql)
        if "settled_at IS NULL" in text:
            assert params == {"as_of": AS_OF}
            return SimpleNamespace(all=lambda: list(self.proposals))
        if "UPDATE option_proposals" in text:
            self.updates[params["proposal_id"]] = dict(params)
            self.proposals = [p for p in self.proposals if p.proposal_id != params["proposal_id"]]
            return SimpleNamespace()
        raise AssertionError(f"unexpected SQL: {text[:60]}")


def _run(proposals, bars_by_symbol):
    session = _FakeSession(proposals)
    windows: dict[str, tuple[date, date]] = {}

    def fake_get_bars(symbol, start, end, *, session, adjust):
        assert adjust is False
        windows[symbol] = (start, end)
        return bars_by_symbol.get(symbol, pd.DataFrame())

    with patch.object(settle_mod, "get_bars", side_effect=fake_get_bars):
        result = settle_expired(AS_OF, session=session)
    return result, session, windows


def test_bars_window_runs_from_the_day_after_the_run_through_expiry():
    _, _, windows = _run(
        [_proposal(1, "AAA", "sell_put", 90)],
        {"AAA": _bars([("2026-06-15", 101, 99, 100)])},
    )
    assert windows["AAA"] == (RUN_AS_OF + timedelta(days=1), EXPIRY)


def test_put_breached_on_close():
    result, session, _ = _run(
        [_proposal(1, "AAA", "sell_put", 90)],
        {"AAA": _bars([
            ("2026-06-15", 101, 95, 96),
            ("2026-06-16", 96, 88, 89),   # closes through the strike
            ("2026-06-17", 92, 87, 91),   # back above; low is the worst
            ("2026-06-18", 93, 90, 92),
            ("2026-06-19", 94, 91, 93),
        ])},
    )
    assert result == settle_mod.SettleResult(settled=1, breached=1)
    row = session.updates[1]
    assert row["touched"] is True and row["breached"] is True
    assert row["breach_date"] == date(2026, 6, 16)
    assert row["close_at_expiry"] == 93.0
    assert row["max_adverse_pct"] == pytest.approx((87 - 90) / 90 * 100)


def test_put_touched_intraday_only():
    result, session, _ = _run(
        [_proposal(1, "AAA", "sell_put", 90)],
        {"AAA": _bars([
            ("2026-06-15", 101, 95, 96),
            ("2026-06-16", 97, 89.5, 92),  # low pierces the strike, close holds
            ("2026-06-19", 98, 94, 97),
        ])},
    )
    assert result.breached == 0
    row = session.updates[1]
    assert row["touched"] is True and row["breached"] is False
    assert row["breach_date"] is None
    assert row["max_adverse_pct"] == pytest.approx((89.5 - 90) / 90 * 100)


def test_put_untouched_reports_the_closest_approach_as_positive():
    _, session, _ = _run(
        [_proposal(1, "AAA", "sell_put", 90)],
        {"AAA": _bars([("2026-06-15", 101, 95, 96), ("2026-06-19", 99, 93, 97)])},
    )
    row = session.updates[1]
    assert row["touched"] is False and row["breached"] is False
    assert row["max_adverse_pct"] == pytest.approx((93 - 90) / 90 * 100)


def test_call_breached_on_close():
    result, session, _ = _run(
        [_proposal(2, "BBB", "sell_call", 110)],
        {"BBB": _bars([
            ("2026-06-15", 105, 99, 104),
            ("2026-06-16", 109, 103, 108),
            ("2026-06-17", 112, 107, 111),  # closes through the strike
            ("2026-06-18", 115, 110, 112),  # high is the worst
            ("2026-06-19", 113, 108, 109),
        ])},
    )
    assert result == settle_mod.SettleResult(settled=1, breached=1)
    row = session.updates[2]
    assert row["touched"] is True and row["breached"] is True
    assert row["breach_date"] == date(2026, 6, 17)
    assert row["close_at_expiry"] == 109.0
    assert row["max_adverse_pct"] == pytest.approx((110 - 115) / 110 * 100)


def test_expiry_on_a_holiday_settles_on_the_prior_bar():
    _, session, _ = _run(
        [_proposal(1, "AAA", "sell_put", 90)],
        {"AAA": _bars([("2026-06-17", 101, 95, 96), ("2026-06-18", 99, 94, 95)])},
    )
    row = session.updates[1]
    assert row["close_at_expiry"] == 95.0
    assert row["breached"] is False


def test_no_bars_leaves_the_proposal_unsettled_and_logs_once(caplog):
    with caplog.at_level(logging.WARNING, logger="stockscan.proposals.settle"):
        result, session, _ = _run(
            [_proposal(1, "AAA", "sell_put", 90), _proposal(2, "BBB", "sell_call", 110)],
            {},
        )
    assert result == settle_mod.SettleResult(settled=0, breached=0)
    assert session.updates == {}
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "2 proposal(s)" in warnings[0] and "AAA" in warnings[0] and "BBB" in warnings[0]


def test_second_run_settles_nothing():
    bars = {"AAA": _bars([("2026-06-15", 101, 95, 96), ("2026-06-19", 99, 93, 97)])}
    session = _FakeSession([_proposal(1, "AAA", "sell_put", 90)])
    with patch.object(settle_mod, "get_bars", return_value=bars["AAA"]):
        first = settle_expired(AS_OF, session=session)
        second = settle_expired(AS_OF, session=session)
    assert first.settled == 1
    assert second == settle_mod.SettleResult(settled=0, breached=0)
    assert list(session.updates) == [1]
