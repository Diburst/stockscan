"""Unit tests for EarningsTrend derived properties.

These cover the math-heavy bits of the revision-drift signal — the
``net_revisions_30d`` net-count and the ``trend_30d_change_pct`` walk
percentage. No DB needed; just constructs the dataclass directly.
"""

from __future__ import annotations

from datetime import date

import pytest

from stockscan.earnings.trends_store import EarningsTrend


def _make_trend(**overrides) -> EarningsTrend:
    """Build an EarningsTrend with all-None defaults; overrides patch specific fields."""
    defaults = {
        "trend_id": 1,
        "symbol": "TEST",
        "period_end": date(2026, 3, 31),
        "period": "+1q",
        "eps_estimate_avg": None,
        "eps_estimate_low": None,
        "eps_estimate_high": None,
        "eps_year_ago": None,
        "eps_growth": None,
        "eps_analyst_count": None,
        "rev_estimate_avg": None,
        "rev_estimate_low": None,
        "rev_estimate_high": None,
        "rev_year_ago": None,
        "rev_growth": None,
        "rev_analyst_count": None,
        "eps_trend_current": None,
        "eps_trend_7d_ago": None,
        "eps_trend_30d_ago": None,
        "eps_trend_60d_ago": None,
        "eps_trend_90d_ago": None,
        "eps_revisions_up_7d": None,
        "eps_revisions_up_30d": None,
        "eps_revisions_down_30d": None,
    }
    defaults.update(overrides)
    return EarningsTrend(**defaults)


def test_net_revisions_30d_positive() -> None:
    t = _make_trend(eps_revisions_up_30d=5, eps_revisions_down_30d=2)
    assert t.net_revisions_30d == 3


def test_net_revisions_30d_negative() -> None:
    t = _make_trend(eps_revisions_up_30d=1, eps_revisions_down_30d=4)
    assert t.net_revisions_30d == -3


def test_net_revisions_30d_all_none() -> None:
    t = _make_trend()
    assert t.net_revisions_30d is None


def test_net_revisions_30d_partial_none() -> None:
    """One side missing → treat the missing side as 0."""
    t = _make_trend(eps_revisions_up_30d=3, eps_revisions_down_30d=None)
    assert t.net_revisions_30d == 3
    t = _make_trend(eps_revisions_up_30d=None, eps_revisions_down_30d=2)
    assert t.net_revisions_30d == -2


def test_trend_30d_change_pct_positive() -> None:
    """Consensus walked from 2.00 → 2.10 = +5.0%."""
    t = _make_trend(eps_trend_current=2.10, eps_trend_30d_ago=2.00)
    assert t.trend_30d_change_pct == pytest.approx(5.0, abs=1e-9)


def test_trend_30d_change_pct_negative() -> None:
    t = _make_trend(eps_trend_current=1.80, eps_trend_30d_ago=2.00)
    assert t.trend_30d_change_pct == pytest.approx(-10.0, abs=1e-9)


def test_trend_30d_change_pct_zero_old_returns_none() -> None:
    """Division by zero guard — when old EPS is 0, percent change is undefined."""
    t = _make_trend(eps_trend_current=0.50, eps_trend_30d_ago=0.0)
    assert t.trend_30d_change_pct is None


def test_trend_30d_change_pct_missing_returns_none() -> None:
    """Either side missing → None."""
    assert _make_trend(eps_trend_current=2.0).trend_30d_change_pct is None
    assert _make_trend(eps_trend_30d_ago=2.0).trend_30d_change_pct is None


def test_trend_30d_change_pct_handles_negative_old() -> None:
    """Negative EPS (loss-making) — change% uses abs(old) so direction is sane."""
    # Old EPS = -0.10, current = -0.05: loss got smaller. That's +50% in the
    # 'improvement' direction, which is what the abs() denominator gives us.
    t = _make_trend(eps_trend_current=-0.05, eps_trend_30d_ago=-0.10)
    assert t.trend_30d_change_pct == pytest.approx(50.0, abs=1e-9)
