"""Nightly job orchestration — the step order and the summary text, with
every I/O step patched on ``stockscan.jobs.nightly``."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr

from stockscan.data.backfill import BulkRefreshResult
from stockscan.data.macro_refresh import DEFAULT_MACRO_SERIES
from stockscan.jobs import nightly as nightly_mod
from stockscan.jobs.nightly import _send_summary, run_nightly_scan
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
        AS_OF,
        2400,
        [_summary("rsi2_meanrev", 3, 5), _summary("momentum_52w_high", 1, 2)],
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
    _send_summary(AS_OF, 0, [], channels=[rec])
    assert len(rec.sent) == 1
    assert "No strategies" in rec.sent[0][1]


def test_summary_zero_signals_uses_singular():
    rec = _Recorder()
    _send_summary(AS_OF, 100, [_summary("s1", 0, 0)], channels=[rec])
    assert "0 signals" in rec.sent[0][0]


def test_summary_one_signal_uses_singular():
    rec = _Recorder()
    _send_summary(AS_OF, 100, [_summary("s1", 1, 0)], channels=[rec])
    assert "1 signal ·" in rec.sent[0][0]


def test_regime_label_and_gate_open_in_subject_and_body():
    rec = _Recorder()
    _send_summary(AS_OF, 100, [_summary("rsi2_meanrev", 2, 1)], regime=_regime(), channels=[rec])
    subj, body = rec.sent[0]
    assert "risk on" in subj
    assert "risk on" in body
    assert "gate open 37d" in body
    assert "vol scalar 1.00" in body


def test_gate_closed_in_body():
    rec = _Recorder()
    _send_summary(
        AS_OF, 0, [_summary("s1", 0, 0)],
        regime=_regime("risk_off", gate_open=False, days=4, vol_scalar="0.62"),
        channels=[rec],
    )
    subj, body = rec.sent[0]
    assert "risk off" in subj
    assert "gate closed 4d" in body
    assert "vol scalar 0.62" in body


def test_credit_stress_label():
    rec = _Recorder()
    _send_summary(AS_OF, 0, [], regime=_regime("credit_stress", stress=True), channels=[rec])
    _, body = rec.sent[0]
    assert "credit stress" in body
    assert "No strategies" in body


def test_per_strategy_line_shows_vol_scalar_only_when_applied():
    rec = _Recorder()
    _send_summary(
        AS_OF, 100,
        [_summary("momentum_52w_high", 2, 1, vol_scalar=0.5), _summary("rsi2_meanrev", 1, 0)],
        regime=_regime(vol_scalar="0.5"),
        channels=[rec],
    )
    _, body = rec.sent[0]
    lines = {ln.strip().split(" v1.0.0")[0].lstrip("• ") : ln for ln in body.splitlines() if "v1.0.0" in ln}
    assert "[vol scalar x0.50]" in lines["momentum_52w_high"]
    assert "vol scalar x" not in lines["rsi2_meanrev"]
    assert "2 passing / 1 rejected (universe 500)" in lines["momentum_52w_high"]


def test_no_regime_shows_unknown():
    rec = _Recorder()
    _send_summary(AS_OF, 0, [_summary("rsi2_meanrev", 1, 0)], regime=None, channels=[rec])
    subj, body = rec.sent[0]
    assert "unknown" in subj
    assert "Market regime: unknown" in body
    assert "gate" not in body


def test_watchlist_alerts_line_only_when_fired():
    rec = _Recorder()
    _send_summary(AS_OF, 0, [_summary("s1", 0, 0)], watchlist_alerts=2, channels=[rec])
    assert "Watchlist alerts fired: 2" in rec.sent[0][1]
    rec2 = _Recorder()
    _send_summary(AS_OF, 0, [_summary("s1", 0, 0)], watchlist_alerts=0, channels=[rec2])
    assert "Watchlist alerts" not in rec2.sent[0][1]


def test_summary_includes_step_failures():
    rec = _Recorder()
    _send_summary(
        AS_OF, 2400, [_summary("rsi2_meanrev", 2, 1)],
        failures=["sector composites: boom", "scan momentum_52w_high: bad data"],
        channels=[rec],
    )
    subj, body = rec.sent[0]
    assert "DEGRADED" in subj
    assert "Step failures" in body
    assert "sector composites: boom" in body
    assert "scan momentum_52w_high: bad data" in body


def test_summary_clean_run_has_no_failure_block():
    rec = _Recorder()
    _send_summary(AS_OF, 2400, [_summary("rsi2_meanrev", 2, 1)], channels=[rec])
    subj, body = rec.sent[0]
    assert "DEGRADED" not in subj
    assert "Step failures" not in body


def test_summary_no_strategies_still_reports_failures():
    rec = _Recorder()
    _send_summary(
        AS_OF, 0, [],
        failures=["bars refresh: 2 day(s) failed (2026-04-24, 2026-04-25)"],
        channels=[rec],
    )
    assert "bars refresh: 2 day(s) failed" in rec.sent[0][1]


# -----------------------------------------------------------------------
# run_nightly_scan — step order and fault tolerance
# -----------------------------------------------------------------------


class _Flow:
    """Every I/O step patched; ``calls`` records the order they ran in."""

    def __init__(self, *, fred_key: str = "", regime=None, regime_error=None, macro_result=None):
        self.calls: list[str] = []
        self.rec = _Recorder()
        self.regime = regime
        self.regime_error = regime_error
        self.macro_result = macro_result if macro_result is not None else {c: 10 for c in DEFAULT_MACRO_SERIES}
        self.settings = SimpleNamespace(fred_api_key=SecretStr(fred_key), eodhd_api_key=SecretStr(""))
        self.fred_instance = MagicMock(name="fred")

    def _bars(self, as_of):
        self.calls.append("bars")
        return BulkRefreshResult(upserted=1234)

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

    def _scan(self, name, as_of):
        self.calls.append(f"scan:{name}")
        return _summary(name, 1, 0)

    def run(self, **kw):
        fred_cls = MagicMock(name="FredProvider")
        fred_cls.return_value.__enter__.return_value = self.fred_instance
        runner = MagicMock()
        runner.run.side_effect = self._scan
        with (
            patch.object(nightly_mod, "settings", self.settings),
            patch.object(nightly_mod, "_refresh_recent_bars", side_effect=self._bars),
            patch.object(nightly_mod, "FredProvider", fred_cls),
            patch.object(nightly_mod, "refresh_macro", side_effect=self._macro),
            patch.object(nightly_mod, "detect_regime", side_effect=self._detect),
            patch("stockscan.sectors.store.refresh_sector_composites", side_effect=self._composites),
            patch.object(nightly_mod, "ScanRunner", return_value=runner),
            patch.object(nightly_mod, "check_and_fire_alerts", return_value=SimpleNamespace(fired=[])),
        ):
            result = run_nightly_scan(AS_OF, notify_channels=[self.rec], **kw)
        self.fred_cls = fred_cls
        return result


def test_step_order_macro_then_regime_before_scans():
    flow = _Flow(fred_key="abc", regime=_regime())
    result = flow.run()
    names = [c for c in flow.calls if c.startswith("scan:")]
    assert flow.calls[:4] == ["bars", "macro", "regime", "composites"]
    assert flow.calls[4:] == names
    assert len(names) == len(result.scans) >= 1
    assert result.bars_upserted == 1234
    assert result.step_failures == []
    flow.fred_cls.assert_called_once_with(api_key="abc")
    subj, body = flow.rec.sent[0]
    assert "gate open" in body
    assert "vol scalar" in body
    assert "DEGRADED" not in subj


def test_missing_fred_key_warns_and_skips_macro_without_failure(caplog):
    flow = _Flow(fred_key="", regime=_regime())
    with caplog.at_level(logging.WARNING, logger="stockscan.jobs.nightly"):
        result = flow.run()
    assert "macro" not in flow.calls
    assert flow.calls[:2] == ["bars", "regime"]
    assert result.step_failures == []
    assert any("FRED_API_KEY" in r.getMessage() for r in caplog.records)
    flow.fred_cls.assert_not_called()
    assert "DEGRADED" not in flow.rec.sent[0][0]


def test_macro_series_failure_is_recorded_not_fatal():
    flow = _Flow(fred_key="abc", regime=_regime(), macro_result={"BAMLH0A0HYM2": None, "DGS1MO": 5, "DGS3MO": 5})
    result = flow.run()
    assert result.step_failures == ["macro refresh: BAMLH0A0HYM2"]
    assert "regime" in flow.calls
    assert "DEGRADED" in flow.rec.sent[0][0]


def test_macro_exception_is_recorded_and_regime_still_runs():
    flow = _Flow(fred_key="abc", regime=_regime())
    flow._macro = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fred 500"))
    result = flow.run()
    assert result.step_failures == ["macro refresh: fred 500"]
    assert "regime" in flow.calls


def test_regime_failure_is_recorded_and_scans_still_run():
    flow = _Flow(regime_error=RuntimeError("no SPY"))
    result = flow.run()
    assert result.step_failures == ["regime detection: no SPY"]
    assert any(c.startswith("scan:") for c in flow.calls)
    _, body = flow.rec.sent[0]
    assert "Market regime: unknown" in body
    assert "regime detection: no SPY" in body


def test_regime_none_is_not_a_failure():
    flow = _Flow(regime=None)
    result = flow.run()
    assert result.step_failures == []
    assert "Market regime: unknown" in flow.rec.sent[0][1]


def test_scan_failure_is_recorded_and_other_scans_run():
    flow = _Flow(regime=_regime())
    real_scan = flow._scan

    def flaky(name, as_of):
        if name == "momentum_52w_high":
            raise RuntimeError("bad bars")
        return real_scan(name, as_of)

    flow._scan = flaky
    result = flow.run()
    assert result.step_failures == ["scan momentum_52w_high: bad bars"]
    assert all(s.strategy_name != "momentum_52w_high" for s in result.scans)
    assert len(result.scans) >= 1


def test_bars_refresh_failed_days_recorded():
    flow = _Flow(regime=_regime())
    flow._bars = lambda as_of: BulkRefreshResult(upserted=0, failed_days=[date(2026, 4, 24)])
    result = flow.run()
    assert result.step_failures == ["bars refresh: 1 day(s) failed (2026-04-24)"]


def test_watchlist_alert_count_and_failure():
    flow = _Flow(regime=_regime())
    with patch.object(nightly_mod, "check_and_fire_alerts", side_effect=RuntimeError("wl")):
        # Inner patch is overridden by _Flow.run's own patch; drive it via the flow instead.
        pass
    fired = SimpleNamespace(fired=[1, 2, 3])
    with patch.object(nightly_mod, "check_and_fire_alerts", return_value=fired):
        result = _run_with_alerts(flow)
    assert result.watchlist_alerts_fired == 3
    assert "Watchlist alerts fired: 3" in flow.rec.sent[0][1]


def _run_with_alerts(flow: _Flow):
    """Same as ``_Flow.run`` but leaves ``check_and_fire_alerts`` to the caller."""
    fred_cls = MagicMock(name="FredProvider")
    runner = MagicMock()
    runner.run.side_effect = flow._scan
    with (
        patch.object(nightly_mod, "settings", flow.settings),
        patch.object(nightly_mod, "_refresh_recent_bars", side_effect=flow._bars),
        patch.object(nightly_mod, "FredProvider", fred_cls),
        patch.object(nightly_mod, "detect_regime", side_effect=flow._detect),
        patch("stockscan.sectors.store.refresh_sector_composites", side_effect=flow._composites),
        patch.object(nightly_mod, "ScanRunner", return_value=runner),
    ):
        return run_nightly_scan(AS_OF, notify_channels=[flow.rec])


@pytest.mark.parametrize("failed", [True, False])
def test_result_shape(failed):
    flow = _Flow(regime=_regime(), regime_error=RuntimeError("x") if failed else None)
    result = flow.run()
    assert result.as_of == AS_OF
    assert isinstance(result.scans, list)
    assert bool(result.step_failures) is failed
