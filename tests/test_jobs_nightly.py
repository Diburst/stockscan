"""Nightly job orchestration — verifies the flow with stubs."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from stockscan.jobs.nightly import _send_summary
from stockscan.scan import ScanSummary


def _summary(name: str, passing: int, rejected: int) -> ScanSummary:
    return ScanSummary(
        run_id=1,
        strategy_name=name,
        strategy_version="1.0.0",
        as_of_date=date(2026, 4, 27),
        universe_size=500,
        signals_emitted=passing,
        rejected_count=rejected,
    )


def test_summary_includes_all_strategies(capsys):
    sent: list[tuple[str, str, str]] = []

    class Recorder:
        name = "rec"
        def send(self, subject, body, *, priority="normal"):
            sent.append((subject, body, priority))

    _send_summary(
        as_of=date(2026, 4, 27),
        bars_upserted=2400,
        scans=[
            _summary("rsi2_meanrev", passing=3, rejected=5),
            _summary("donchian_trend", passing=1, rejected=2),
        ],
        channels=[Recorder()],
    )
    assert len(sent) == 1
    subj, body, _prio = sent[0]
    assert "stockscan" in subj
    assert "4 signals" in subj  # 3 + 1
    assert "rsi2_meanrev" in body
    assert "donchian_trend" in body
    assert "Refreshed bars: 2,400" in body


def test_summary_with_no_strategies_still_sends():
    sent = []

    class Recorder:
        name = "rec"
        def send(self, subject, body, *, priority="normal"):
            sent.append((subject, body))

    _send_summary(
        as_of=date(2026, 4, 27),
        bars_upserted=0,
        scans=[],
        channels=[Recorder()],
    )
    assert len(sent) == 1
    assert "No strategies" in sent[0][1]


def test_summary_zero_signals_uses_singular():
    sent = []

    class Recorder:
        name = "rec"
        def send(self, subject, body, *, priority="normal"):
            sent.append((subject, body))

    _send_summary(
        as_of=date(2026, 4, 27),
        bars_upserted=100,
        scans=[_summary("s1", passing=0, rejected=0)],
        channels=[Recorder()],
    )
    assert "0 signals" in sent[0][0]
