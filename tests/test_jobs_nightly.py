"""Nightly job orchestration — verifies the flow with stubs."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from stockscan.jobs.nightly import _send_summary
from stockscan.regime.store import MarketRegime
from stockscan.scan import ScanSummary


def _summary(name: str, passing: int, rejected: int, *, skipped: bool = False) -> ScanSummary:
    return ScanSummary(
        run_id=-1 if skipped else 1,
        strategy_name=name,
        strategy_version="1.0.0",
        as_of_date=date(2026, 4, 27),
        universe_size=0 if skipped else 500,
        signals_emitted=passing,
        rejected_count=rejected,
        regime_skipped=skipped,
    )


def _regime(label: str) -> MarketRegime:
    return MarketRegime(
        as_of_date=date(2026, 4, 27),
        regime=label,  # type: ignore[arg-type]
        adx=Decimal("28.5"),
        spy_close=Decimal("520.0"),
        spy_sma200=Decimal("480.0"),
    )


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.name = "rec"

    def send(self, subject: str, body: str, *, priority: str = "normal") -> None:
        self.sent.append((subject, body))


# -----------------------------------------------------------------------
# Existing tests (updated to pass regime kwarg)
# -----------------------------------------------------------------------

def test_summary_includes_all_strategies():
    rec = _Recorder()
    _send_summary(
        as_of=date(2026, 4, 27),
        bars_upserted=2400,
        scans=[
            _summary("rsi2_meanrev", passing=3, rejected=5),
            _summary("donchian_trend", passing=1, rejected=2),
        ],
        channels=[rec],
    )
    assert len(rec.sent) == 1
    subj, body = rec.sent[0]
    assert "stockscan" in subj
    assert "4 signals" in subj  # 3 + 1
    assert "rsi2_meanrev" in body
    assert "donchian_trend" in body
    assert "Refreshed bars: 2,400" in body


def test_summary_with_no_strategies_still_sends():
    rec = _Recorder()
    _send_summary(
        as_of=date(2026, 4, 27),
        bars_upserted=0,
        scans=[],
        channels=[rec],
    )
    assert len(rec.sent) == 1
    assert "No strategies" in rec.sent[0][1]


def test_summary_zero_signals_uses_singular():
    rec = _Recorder()
    _send_summary(
        as_of=date(2026, 4, 27),
        bars_upserted=100,
        scans=[_summary("s1", passing=0, rejected=0)],
        channels=[rec],
    )
    assert "0 signals" in rec.sent[0][0]


# -----------------------------------------------------------------------
# Regime-in-email tests
# -----------------------------------------------------------------------

def test_regime_label_appears_in_subject():
    rec = _Recorder()
    _send_summary(
        as_of=date(2026, 4, 27),
        bars_upserted=100,
        scans=[_summary("rsi2_meanrev", passing=2, rejected=1)],
        regime=_regime("trending_up"),
        channels=[rec],
    )
    subj, body = rec.sent[0]
    assert "trending up" in subj
    assert "trending up" in body


def test_regime_adx_appears_in_body():
    rec = _Recorder()
    _send_summary(
        as_of=date(2026, 4, 27),
        bars_upserted=0,
        scans=[_summary("rsi2_meanrev", passing=1, rejected=0)],
        regime=_regime("choppy"),
        channels=[rec],
    )
    _, body = rec.sent[0]
    assert "ADX" in body
    assert "28.5" in body


def test_skipped_strategy_appears_in_body_not_in_totals():
    rec = _Recorder()
    _send_summary(
        as_of=date(2026, 4, 27),
        bars_upserted=100,
        scans=[
            _summary("rsi2_meanrev", passing=3, rejected=1),
            _summary("donchian_trend", passing=0, rejected=0, skipped=True),
        ],
        regime=_regime("choppy"),
        channels=[rec],
    )
    subj, body = rec.sent[0]
    # Totals should only count the non-skipped strategy
    assert "3 signals" in subj
    # Body must mention both strategies
    assert "rsi2_meanrev" in body
    assert "donchian_trend" in body
    # The skipped one should show the paused marker
    assert "paused" in body
    # Strategies run count should note the pause
    assert "paused by regime" in body


def test_no_regime_shows_unknown():
    rec = _Recorder()
    _send_summary(
        as_of=date(2026, 4, 27),
        bars_upserted=0,
        scans=[_summary("rsi2_meanrev", passing=1, rejected=0)],
        regime=None,
        channels=[rec],
    )
    # No crash; subject/body should still be sent
    assert len(rec.sent) == 1


def test_no_strategies_with_regime():
    rec = _Recorder()
    _send_summary(
        as_of=date(2026, 4, 27),
        bars_upserted=0,
        scans=[],
        regime=_regime("trending_down"),
        channels=[rec],
    )
    _, body = rec.sent[0]
    assert "trending down" in body
    assert "No strategies" in body
