"""Input sanitization + form-default helpers for the hedge web routes.

Pure helpers only (no DB / no FastAPI), covering the validation that keeps
malformed user input — stray commas, `$`, letters, out-of-range numbers — out
of the store and the templates.
"""

from __future__ import annotations

from datetime import date

import pytest

from stockscan.web.routes import hedge as h


def test_sanitize_symbol_normalizes_and_validates():
    assert h._sanitize_symbol("  mu ") == "MU"
    assert h._sanitize_symbol("brk.b") == "BRK.B"
    for bad in ("", "1MU", "MU;DROP", "TOOLONGSYMBOL", "M U"):
        with pytest.raises(h._FormError):
            h._sanitize_symbol(bad)


def test_parse_decimal_tolerates_commas_and_dollar_signs():
    assert h._parse_decimal("2,500", field="Premium") == 2500
    assert h._parse_decimal("$1,300.50", field="Strike") == pytest.approx(1300.50)


def test_parse_decimal_rejects_junk_and_bounds():
    for bad in ("", "abc", "nan", "inf", "1,2,3x"):
        with pytest.raises(h._FormError):
            h._parse_decimal(bad, field="X")
    with pytest.raises(h._FormError):  # <= 0 not allowed by default
        h._parse_decimal("0", field="Strike")
    # zero allowed for premium
    assert h._parse_decimal("0", field="Premium", allow_zero=True) == 0
    with pytest.raises(h._FormError):  # negative even when zero allowed
        h._parse_decimal("-5", field="Premium", allow_zero=True)
    with pytest.raises(h._FormError):  # above max
        h._parse_decimal("50", field="Contracts", max_value=10)


def test_parse_float_enforces_range():
    assert h._parse_float("0.05", field="a", lo=1e-6, hi=1e6) == pytest.approx(0.05)
    with pytest.raises(h._FormError):
        h._parse_float("2", field="pct", lo=1e-4, hi=1.0)


def test_next_monthly_opex_is_a_third_friday_at_least_a_week_out():
    for d in (date(2026, 7, 1), date(2026, 7, 16), date(2026, 12, 20)):
        opex = h._next_monthly_opex(d)
        assert opex.weekday() == 4  # Friday
        assert 15 <= opex.day <= 21  # third Friday lands here
        assert (opex - d).days >= 7


def test_round_strike_snaps_to_sensible_increments():
    assert h._round_strike(1302.7) == 1305.0  # $5 increments for >= 100
    assert h._round_strike(41.3) == 41.0  # $1 for 25–100
    assert h._round_strike(12.4) == 12.5  # $0.50 under 25


def test_parse_signed_float_allows_negative_drift():
    assert h._parse_signed_float("-25", field="Drift", lo=-1000, hi=1000) == -25.0
    assert h._parse_signed_float("", field="Drift", lo=-1000, hi=1000) == 0.0
    with pytest.raises(h._FormError):
        h._parse_signed_float("abc", field="Drift", lo=-1000, hi=1000)
    with pytest.raises(h._FormError):
        h._parse_signed_float("5000", field="Drift", lo=-1000, hi=1000)


def test_opt_float_blank_is_none_zero_allowed():
    assert h._opt_float("", field="X") is None
    assert h._opt_float("0", field="X") == 0.0
    assert h._opt_float("1300", field="X") == 1300.0
