"""Smoke test: settings load and contain sane defaults."""

from decimal import Decimal


def test_settings_load_defaults() -> None:
    from stockscan.config import Settings

    s = Settings(_env_file=None)
    assert s.timezone == "America/New_York"
    assert s.default_risk_pct == Decimal("0.01")
    assert s.max_positions == 15
    assert s.max_position_pct == Decimal("0.08")
    assert s.max_sector_pct == Decimal("0.25")
    assert s.drawdown_circuit_breaker == Decimal("0.15")


def test_settings_env_property() -> None:
    from stockscan.config import Settings

    assert Settings(env="dev", _env_file=None).is_dev is True
    assert Settings(env="prod", _env_file=None).is_prod is True
    assert Settings(env="test", _env_file=None).is_test is True
