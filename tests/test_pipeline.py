"""The refresh pipeline — step order, idempotency, fault tolerance and the
nightly summary text, with every I/O step patched on ``stockscan.jobs.pipeline``."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr

from stockscan.data.backfill import BulkRefreshResult, CatchUpResult
from stockscan.data.macro_refresh import DEFAULT_MACRO_SERIES
from stockscan.jobs import pipeline as pipeline_mod
from stockscan.jobs.pipeline import STEPS, PipelineResult, _send_summary, run_pipeline
from stockscan.proposals.settle import SettleResult
from stockscan.regime.store import MarketRegime
from stockscan.scan import ScanSummary

AS_OF = date(2026, 4, 27)


def _summary(name: str, passing: int, rejected: int, *, vol_scalar: float = 1.0, label="risk_on") -> ScanSummary:
    return ScanSummary(
        run_id=1,
        strategy_name=name,
        strategy_version="1.0.0",
        as_of_date=AS_OF,
        universe_size=500,
        signals_emitted=passing,
        rejected_count=rejected,
        regime_label=label,
        vol_scalar=vol_scalar,
    )


def _regime(label: str = "risk_on", *, gate_open=True, stress=False, vol_scalar="1.0", days=37) -> MarketRegime:
    return MarketRegime(
        as_of_date=AS_OF,
        regime=label,  # type: ignore[arg-type]
        trend_gate_open=gate_open,
        days_on_side=days,
        spy_close=Decimal("520.0"),
        spy_sma200=Decimal("480.0"),
        spy_sma200_slope_20d=Decimal("0.01"),
        realized_vol_20d=Decimal("0.18"),
        realized_vol_pct_rank=Decimal("0.7"),
        vol_scalar=Decimal(vol_scalar),
        hy_oas_level=Decimal("3.4"),
        hy_oas_pct_rank=Decimal("0.3"),
        credit_stress_flag=stress,
    )


def _result(**overrides) -> PipelineResult:
    base = dict(
        as_of=AS_OF, bars_upserted=0, caught_up=(), scans=[], scans_skipped=False, regime=None,
        trades_marked=0, trades_auto_closed=0, options_run_id=None, options_settled=0, feeds={},
        watchlist_alerts_fired=0, step_failures=[], duration_seconds=1.0,
    )
    base.update(overrides)
    return PipelineResult(**base)


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.name = "rec"

    def send(self, subject: str, body: str, *, priority: str = "normal") -> None:
        self.sent.append((subject, body))


# -----------------------------------------------------------------------
# _send_summary
# -----------------------------------------------------------------------
def test_summary_includes_all_strategies():
    rec = _Recorder()
    _send_summary(
        _result(bars_upserted=2400, scans=[_summary("rsi2_meanrev", 3, 5), _summary("momentum_52w_high", 1, 2)]),
        channels=[rec],
    )
    assert len(rec.sent) == 1
    subj, body = rec.sent[0]
    assert "stockscan" in subj
    assert "4 signals" in subj
    assert "rsi2_meanrev" in body
    assert "momentum_52w_high" in body
    assert "Refreshed bars: 2,400" in body
    assert "Strategies run: 2" in body


def test_summary_with_no_strategies_still_sends():
    rec = _Recorder()
    _send_summary(_result(), channels=[rec])
    assert len(rec.sent) == 1
    assert "No strategies" in rec.sent[0][1]


def test_summary_skipped_scans_say_so():
    rec = _Recorder()
    _send_summary(_result(scans_skipped=True), channels=[rec])
    subj, body = rec.sent[0]
    assert "0 signals" in subj
    assert "skipped — nothing new" in body
    assert "No strategies" not in body


def test_summary_zero_and_one_signal_use_singular():
    rec = _Recorder()
    _send_summary(_result(bars_upserted=100, scans=[_summary("s1", 0, 0)]), channels=[rec])
    assert "0 signals" in rec.sent[0][0]
    rec2 = _Recorder()
    _send_summary(_result(bars_upserted=100, scans=[_summary("s1", 1, 0)]), channels=[rec2])
    assert "1 signal ·" in rec2.sent[0][0]


def test_regime_label_and_gate_in_subject_and_body():
    rec = _Recorder()
    _send_summary(_result(scans=[_summary("rsi2_meanrev", 2, 1)], regime=_regime()), channels=[rec])
    subj, body = rec.sent[0]
    assert "risk on" in subj and "risk on" in body
    assert "gate open 37d" in body and "vol scalar 1.00" in body

    rec2 = _Recorder()
    _send_summary(
        _result(scans=[_summary("s1", 0, 0)], regime=_regime("risk_off", gate_open=False, days=4, vol_scalar="0.62")),
        channels=[rec2],
    )
    subj, body = rec2.sent[0]
    assert "risk off" in subj and "gate closed 4d" in body and "vol scalar 0.62" in body


def test_credit_stress_label():
    rec = _Recorder()
    _send_summary(_result(regime=_regime("credit_stress", stress=True)), channels=[rec])
    _, body = rec.sent[0]
    assert "credit stress" in body and "No strategies" in body


def test_per_strategy_line_shows_vol_scalar_only_when_applied():
    rec = _Recorder()
    _send_summary(
        _result(
            bars_upserted=100,
            scans=[_summary("momentum_52w_high", 2, 1, vol_scalar=0.5), _summary("rsi2_meanrev", 1, 0)],
            regime=_regime(vol_scalar="0.5"),
        ),
        channels=[rec],
    )
    _, body = rec.sent[0]
    lines = {ln.strip().split(" v1.0.0")[0].lstrip("• "): ln for ln in body.splitlines() if "v1.0.0" in ln}
    assert "[vol scalar x0.50]" in lines["momentum_52w_high"]
    assert "vol scalar x" not in lines["rsi2_meanrev"]
    assert "2 passing / 1 rejected (universe 500)" in lines["momentum_52w_high"]


def test_no_regime_shows_unknown():
    rec = _Recorder()
    _send_summary(_result(scans=[_summary("rsi2_meanrev", 1, 0)]), channels=[rec])
    subj, body = rec.sent[0]
    assert "unknown" in subj and "Market regime: unknown" in body and "gate" not in body


def test_watchlist_alerts_and_catch_up_lines_only_when_present():
    rec = _Recorder()
    _send_summary(
        _result(bars_upserted=49, caught_up=("AAOI",), scans=[_summary("s1", 0, 0)], watchlist_alerts_fired=2),
        channels=[rec],
    )
    body = rec.sent[0][1]
    assert "Watchlist alerts fired: 2" in body
    assert "Refreshed bars: 49 (caught up AAOI)" in body
    rec2 = _Recorder()
    _send_summary(_result(scans=[_summary("s1", 0, 0)]), channels=[rec2])
    assert "Watchlist alerts" not in rec2.sent[0][1] and "caught up" not in rec2.sent[0][1]


def test_summary_step_failures_mark_the_run_degraded():
    rec = _Recorder()
    _send_summary(
        _result(scans=[_summary("rsi2_meanrev", 2, 1)], step_failures=["sector composites: boom", "scan x: bad"]),
        channels=[rec],
    )
    subj, body = rec.sent[0]
    assert "DEGRADED" in subj and "Step failures" in body
    assert "sector composites: boom" in body and "scan x: bad" in body

    rec2 = _Recorder()
    _send_summary(_result(scans=[_summary("rsi2_meanrev", 2, 1)]), channels=[rec2])
    assert "DEGRADED" not in rec2.sent[0][0] and "Step failures" not in rec2.sent[0][1]

    rec3 = _Recorder()
    _send_summary(_result(step_failures=["bars refresh: 2 day(s) failed (2026-04-24, 2026-04-25)"]), channels=[rec3])
    assert "bars refresh: 2 day(s) failed" in rec3.sent[0][1]


# -----------------------------------------------------------------------
# run_pipeline — step order, idempotency and fault tolerance
# -----------------------------------------------------------------------
class _Flow:
    """Every I/O step patched; ``calls`` records the order they ran in."""

    def __init__(self, *, fred_key: str = "", regime=None, regime_error=None, macro_result=None,
                 bars_upserted: int = 1234, scans_covered: bool = False):
        self.calls: list[str] = []
        self.steps: list[str] = []
        self.rec = _Recorder()
        self.regime = regime
        self.regime_error = regime_error
        self.macro_result = macro_result if macro_result is not None else {c: 10 for c in DEFAULT_MACRO_SERIES}
        self.settings = SimpleNamespace(fred_api_key=SecretStr(fred_key), eodhd_api_key=SecretStr(""))
        self.fred_instance = MagicMock(name="fred")
        self.bars_upserted = bars_upserted
        self.scans_covered = scans_covered
        self.fired: list[int] = []
        self.alerts_error: Exception | None = None

    def _bars(self):
        self.calls.append("bars")
        return BulkRefreshResult(upserted=self.bars_upserted)

    def _catch_up(self):
        self.calls.append("catch_up")
        return CatchUpResult()

    def _macro(self, provider, series, start, end):
        self.calls.append("macro")
        assert provider is self.fred_instance
        assert tuple(series) == DEFAULT_MACRO_SERIES
        assert end == AS_OF and start.year == AS_OF.year - 2
        return self.macro_result

    def _detect(self, as_of, **kw):
        self.calls.append("regime")
        assert kw == {"force_recompute": True}
        if self.regime_error:
            raise self.regime_error
        return self.regime

    def _composites(self, start, end):
        self.calls.append("composites")
        return {}

    def _watchlist_composites(self):
        self.calls.append("watchlist_composites")
        return []

    def _scan(self, name, as_of):
        self.calls.append(f"scan:{name}")
        return _summary(name, 1, 0)

    def _trades(self):
        self.calls.append("trades")

    def _book(self, **kw):
        self.calls.append("book")
        assert kw == {"list_id": None, "as_of": AS_OF}
        return "book"

    def _save(self, run, list_id=None, *, replace=False):
        self.calls.append("save")
        assert run == "book" and list_id is None and replace is True
        return 7

    def _settle(self, as_of):
        self.calls.append("settle")
        assert as_of == AS_OF
        return SettleResult(settled=3, breached=1)

    def _feeds(self, failures):
        self.calls.append("feeds")
        return {"news": "12 articles"}

    def _alerts(self, channels=None):
        self.calls.append("alerts")
        if self.alerts_error:
            raise self.alerts_error
        return SimpleNamespace(fired=self.fired)

    def _progress(self, label, index, total):
        assert index == STEPS.index(label) + 1 and total == len(STEPS)
        self.steps.append(label)

    def run(self, **kw):
        fred_cls = MagicMock(name="FredProvider")
        fred_cls.return_value.__enter__.return_value = self.fred_instance
        runner = MagicMock()
        runner.run.side_effect = self._scan
        session = MagicMock()
        session.__enter__.return_value = session
        with (
            patch.object(pipeline_mod, "settings", self.settings),
            patch.object(pipeline_mod, "_refresh_recent_bars", side_effect=self._bars),
            patch.object(pipeline_mod, "_catch_up_watchlist", side_effect=self._catch_up),
            patch.object(pipeline_mod, "FredProvider", fred_cls),
            patch.object(pipeline_mod, "refresh_macro", side_effect=self._macro),
            patch.object(pipeline_mod, "detect_regime", side_effect=self._detect),
            patch("stockscan.sectors.store.refresh_sector_composites", side_effect=self._composites),
            patch.object(pipeline_mod, "backfill_all_lists", side_effect=self._watchlist_composites),
            patch.object(pipeline_mod, "latest_daily_bar_date", return_value=AS_OF),
            patch.object(pipeline_mod, "_scans_cover", return_value=self.scans_covered),
            patch.object(pipeline_mod, "ScanRunner", return_value=runner),
            patch.object(pipeline_mod, "session_scope", return_value=session),
            patch.object(pipeline_mod, "mark_to_market", side_effect=lambda **k: (self._trades(), 2)[1]),
            patch.object(pipeline_mod, "check_auto_close", return_value=[9]),
            patch.object(pipeline_mod, "generate_book", side_effect=self._book),
            patch.object(pipeline_mod, "save_run", side_effect=self._save),
            patch.object(pipeline_mod, "settle_expired", side_effect=self._settle),
            patch.object(pipeline_mod, "_refresh_feeds", side_effect=self._feeds),
            patch.object(pipeline_mod, "check_and_fire_alerts", side_effect=self._alerts),
        ):
            result = run_pipeline(AS_OF, notify_channels=[self.rec], progress=self._progress, **kw)
        self.fred_cls = fred_cls
        return result


def test_step_order_and_progress_callback():
    flow = _Flow(fred_key="abc", regime=_regime())
    result = flow.run()
    names = [f"scan:{n}" for n in pipeline_mod.STRATEGY_REGISTRY.names()]
    assert flow.calls == (
        ["bars", "catch_up", "macro", "regime", "composites", "watchlist_composites"]
        + names + ["trades", "book", "save", "settle", "feeds", "alerts"]
    )
    assert flow.steps == list(STEPS)
    assert result.bars_upserted == 1234 and result.scans_skipped is False
    assert result.trades_marked == 2 and result.trades_auto_closed == 1
    assert result.options_run_id == 7 and result.options_settled == 3
    assert result.feeds == {"news": "12 articles"}
    assert result.step_failures == [] and result.degraded is False
    flow.fred_cls.assert_called_once_with(api_key="abc")
    subj, body = flow.rec.sent[0]
    assert "gate open" in body and "DEGRADED" not in subj


def test_send_summary_false_sends_nothing():
    flow = _Flow(regime=_regime())
    flow.run(send_summary=False)
    assert flow.rec.sent == []


def test_scans_skipped_when_no_new_bars_and_runs_cover_the_latest_bar():
    flow = _Flow(regime=_regime(), bars_upserted=0, scans_covered=True)
    result = flow.run()
    assert result.scans_skipped is True and result.scans == []
    assert not any(c.startswith("scan:") for c in flow.calls)
    assert "skipped — nothing new" in flow.rec.sent[0][1]


def test_scans_run_when_bars_arrived_even_if_runs_cover():
    flow = _Flow(regime=_regime(), bars_upserted=5, scans_covered=True)
    result = flow.run()
    assert result.scans_skipped is False and len(result.scans) == len(pipeline_mod.STRATEGY_REGISTRY.names())


def test_missing_fred_key_warns_and_skips_macro_without_failure(caplog):
    flow = _Flow(fred_key="", regime=_regime())
    with caplog.at_level(logging.WARNING, logger="stockscan.jobs.pipeline"):
        result = flow.run()
    assert "macro" not in flow.calls
    assert result.step_failures == []
    assert any("FRED_API_KEY" in r.getMessage() for r in caplog.records)
    flow.fred_cls.assert_not_called()


def test_macro_series_failure_is_recorded_not_fatal():
    flow = _Flow(fred_key="abc", regime=_regime(), macro_result={"BAMLH0A0HYM2": None, "DGS1MO": 3, "DGS3MO": 3})
    result = flow.run()
    assert result.step_failures == ["macro refresh: BAMLH0A0HYM2"]
    assert "regime" in flow.calls


def test_regime_failure_is_recorded_and_the_run_continues():
    flow = _Flow(regime_error=RuntimeError("no SPY bars"))
    result = flow.run()
    assert result.regime is None
    assert result.step_failures == ["regime detection: no SPY bars"]
    assert "book" in flow.calls
    assert "Market regime: unknown" in flow.rec.sent[0][1]


def test_scan_failure_is_recorded_and_other_scans_run():
    flow = _Flow(regime=_regime())
    names = pipeline_mod.STRATEGY_REGISTRY.names()

    def _scan(name, as_of):
        flow.calls.append(f"scan:{name}")
        if name == names[0]:
            raise ValueError("bad data")
        return _summary(name, 1, 0)

    flow._scan = _scan
    result = flow.run()
    assert result.step_failures == [f"scan {names[0]}: bad data"]
    assert [s.strategy_name for s in result.scans] == names[1:]
    assert "DEGRADED" in flow.rec.sent[0][0]


def test_options_step_failure_is_recorded_and_feeds_still_run():
    flow = _Flow(regime=_regime())
    flow._save = MagicMock(side_effect=RuntimeError("db down"))
    result = flow.run()
    assert result.options_run_id is None
    assert result.step_failures == ["options: db down"]
    assert "feeds" in flow.calls


def test_bars_failed_days_and_catch_up_failures_recorded():
    flow = _Flow(regime=_regime())
    flow._bars = lambda: BulkRefreshResult(upserted=0, failed_days=[date(2026, 4, 24)])
    flow._catch_up = lambda: CatchUpResult(upserted=49, symbols_fetched=("AAOI",), failed=("XYZ",))
    result = flow.run()
    assert result.bars_upserted == 49 and result.caught_up == ("AAOI",)
    assert result.step_failures == [
        "bars refresh: 1 day(s) failed (2026-04-24)",
        "bars catch-up failed for XYZ",
    ]


def test_alerts_count_and_failure():
    flow = _Flow(regime=_regime())
    flow.fired = [1, 2, 3]
    result = flow.run()
    assert result.watchlist_alerts_fired == 3
    assert "Watchlist alerts fired: 3" in flow.rec.sent[0][1]

    failing = _Flow(regime=_regime())
    failing.alerts_error = RuntimeError("wl")
    result = failing.run()
    assert result.watchlist_alerts_fired == 0
    assert result.step_failures == ["watchlist alerts: wl"]


def test_bulk_filter_is_the_tracked_set_and_catch_up_is_watchlist_only():
    """The bulk filter must include watched names outside the index — an
    index-only filter is exactly the bug that left AAOI stale for weeks —
    and the per-symbol catch-up must run over the watchlist only."""
    provider = MagicMock()
    provider.__enter__.return_value = provider
    with (
        patch.object(pipeline_mod, "tracked_symbols", return_value={"SPY", "AAOI"}),
        patch.object(pipeline_mod, "missing_bulk_dates", return_value=[date(2026, 9, 18)]),
        patch.object(pipeline_mod, "_provider", return_value=provider),
        patch.object(pipeline_mod, "refresh_recent_days_bulk", return_value=BulkRefreshResult(upserted=2)) as bulk,
    ):
        assert pipeline_mod._refresh_recent_bars().upserted == 2
    assert bulk.call_args.kwargs["filter_to"] == {"SPY", "AAOI"}

    with (
        patch.object(pipeline_mod, "latest_daily_bar_date", return_value=date(2026, 9, 18)),
        patch.object(pipeline_mod, "watchlist_symbols", return_value={"AAOI"}),
        patch.object(pipeline_mod, "_provider", return_value=provider),
        patch.object(pipeline_mod, "catch_up_lagging_symbols", return_value=CatchUpResult(upserted=1)) as cu,
    ):
        assert pipeline_mod._catch_up_watchlist().upserted == 1
    assert cu.call_args.args[1] == {"AAOI"} and cu.call_args.kwargs["target"] == date(2026, 9, 18)


def test_bulk_makes_no_call_when_the_store_is_current():
    provider = MagicMock()
    with (
        patch.object(pipeline_mod, "tracked_symbols", return_value={"SPY"}),
        patch.object(pipeline_mod, "missing_bulk_dates", return_value=[]),
        patch.object(pipeline_mod, "_provider", return_value=provider),
    ):
        assert pipeline_mod._refresh_recent_bars().upserted == 0
    provider.__enter__.assert_not_called()


def test_feeds_skip_without_a_request_when_off_plan_or_on_cooldown():
    provider = MagicMock()
    provider.__enter__.return_value = provider
    provider.supports.side_effect = lambda f: f != "news"
    session = MagicMock()
    session.__enter__.return_value = session
    insider = SimpleNamespace(skipped_reason=None, skipped=True, error=None, transactions_upserted=0)
    failures: list[str] = []
    with (
        patch.object(pipeline_mod, "settings", SimpleNamespace(eodhd_api_key=SecretStr("k"))),
        patch.object(pipeline_mod, "_provider", return_value=provider),
        patch.object(pipeline_mod, "session_scope", return_value=session),
        patch.object(pipeline_mod, "watchlist_symbols", return_value={"AAOI"}),
        patch.object(pipeline_mod, "refresh_due", return_value=False),
        patch.object(pipeline_mod, "refresh_news") as news,
        patch.object(pipeline_mod, "refresh_economic_events") as econ,
        patch.object(pipeline_mod, "refresh_earnings") as earn,
        patch.object(pipeline_mod, "refresh_insider_for_watchlist", return_value=insider),
    ):
        out = pipeline_mod._refresh_feeds(failures)
    assert out == {"news": "not on plan", "macro calendar": "cooldown", "earnings": "cooldown", "insider": "cooldown"}
    assert failures == []
    news.assert_not_called(); econ.assert_not_called(); earn.assert_not_called()


def test_feeds_skip_entirely_without_an_api_key():
    with patch.object(pipeline_mod, "settings", SimpleNamespace(eodhd_api_key=SecretStr(""))):
        assert pipeline_mod._refresh_feeds([]) == {}


@pytest.mark.parametrize("failed", [[], ["x: y"]])
def test_result_shape(failed):
    r = _result(step_failures=failed)
    assert r.degraded is bool(failed)
