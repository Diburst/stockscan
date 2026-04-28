"""Application configuration loaded from environment variables / .env.

Settings are accessed via the module-level `settings` singleton:

    from stockscan.config import settings
    print(settings.database_url)

A single instance is created on import. For tests, override fields by
constructing a fresh `Settings(...)` (see tests/conftest.py).
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """All app config in one place. Validated by Pydantic at startup."""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE) if ENV_FILE.exists() else None,
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,  # accept either field name or alias as kwarg
    )

    # ---- App ----
    env: Literal["dev", "test", "prod"] = Field("dev", alias="STOCKSCAN_ENV")
    log_level: str = Field("INFO", alias="STOCKSCAN_LOG_LEVEL")
    timezone: str = Field("America/New_York", alias="STOCKSCAN_TIMEZONE")

    # ---- Database ----
    database_url: str = Field(
        "postgresql+psycopg://stockscan:CHANGE_ME@127.0.0.1:5432/stockscan",
        alias="DATABASE_URL",
    )

    # ---- EODHD data provider ----
    eodhd_api_key: SecretStr = Field(SecretStr(""), alias="EODHD_API_KEY")
    eodhd_base_url: str = Field("https://eodhd.com/api", alias="EODHD_BASE_URL")

    # ---- E*TRADE (Phase 4) ----
    etrade_consumer_key: SecretStr = Field(SecretStr(""), alias="ETRADE_CONSUMER_KEY")
    etrade_consumer_secret: SecretStr = Field(SecretStr(""), alias="ETRADE_CONSUMER_SECRET")
    etrade_use_sandbox: bool = Field(True, alias="ETRADE_USE_SANDBOX")

    # ---- Notifications ----
    notify_email_from: str = Field("", alias="NOTIFY_EMAIL_FROM")
    notify_email_to: str = Field("", alias="NOTIFY_EMAIL_TO")
    postmark_token: SecretStr = Field(SecretStr(""), alias="POSTMARK_TOKEN")
    discord_webhook_url: SecretStr = Field(SecretStr(""), alias="DISCORD_WEBHOOK_URL")

    # ---- Risk caps (DESIGN §4.7) ----
    default_risk_pct: Decimal = Field(Decimal("0.01"), alias="STOCKSCAN_DEFAULT_RISK_PCT")
    max_positions: int = Field(15, alias="STOCKSCAN_MAX_POSITIONS")
    max_sector_pct: Decimal = Field(Decimal("0.25"), alias="STOCKSCAN_MAX_SECTOR_PCT")
    max_position_pct: Decimal = Field(Decimal("0.08"), alias="STOCKSCAN_MAX_POSITION_PCT")
    max_adv_pct: Decimal = Field(Decimal("0.05"), alias="STOCKSCAN_MAX_ADV_PCT")
    drawdown_circuit_breaker: Decimal = Field(
        Decimal("0.15"), alias="STOCKSCAN_DRAWDOWN_CIRCUIT_BREAKER"
    )

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"

    @property
    def is_test(self) -> bool:
        return self.env == "test"

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


@lru_cache(maxsize=1)
def _get_settings() -> Settings:
    return Settings()


# Convenience singleton — most code should import this.
settings: Settings = _get_settings()
