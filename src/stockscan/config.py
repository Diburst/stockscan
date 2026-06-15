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

    # ---- Logging (see stockscan.logging_setup) ----
    # Empty log_dir → <repo_root>/logs (resolved_log_dir property below).
    log_dir: str = Field("", alias="STOCKSCAN_LOG_DIR")
    log_to_file: bool = Field(True, alias="STOCKSCAN_LOG_TO_FILE")
    # Requests slower than this log at WARNING in the web timing middleware.
    slow_request_ms: int = Field(750, alias="STOCKSCAN_SLOW_REQUEST_MS")

    # ---- Database ----
    database_url: str = Field(
        "postgresql+psycopg://stockscan:CHANGE_ME@127.0.0.1:5432/stockscan",
        alias="DATABASE_URL",
    )

    # ---- EODHD data provider ----
    eodhd_api_key: SecretStr = Field(SecretStr(""), alias="EODHD_API_KEY")
    eodhd_base_url: str = Field("https://eodhd.com/api", alias="EODHD_BASE_URL")

    # ---- FRED macro data (HY OAS, yield-curve spreads, etc.) ----
    fred_api_key: SecretStr = Field(SecretStr(""), alias="FRED_API_KEY")
    fred_base_url: str = Field(
        "https://api.stlouisfed.org/fred", alias="FRED_BASE_URL"
    )

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

    # ---- Options analysis ----
    # Annualised risk-free rate used by the Black-Scholes strike solver on the
    # analysis page. The effect on a 30-day, 20-delta strike is small; ~4%
    # tracks short-term Treasury yields closely enough for strike framing.
    # We have no option chain, so realized HV stands in for implied vol — see
    # stockscan.analysis.black_scholes.
    risk_free_rate: float = Field(0.04, alias="STOCKSCAN_RISK_FREE_RATE")

    @property
    def resolved_log_dir(self) -> Path:
        """Log directory as a Path — explicit setting or <repo_root>/logs."""
        return Path(self.log_dir) if self.log_dir else PROJECT_ROOT / "logs"

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


def config_warnings() -> list[str]:
    """Degraded-capability warnings for startup announcement.

    A misconfigured deploy should say so on boot rather than fail quietly
    at 8pm when the nightly job runs. Called by the web app factory and the
    CLI callback; each line is logged at WARNING.
    """
    out: list[str] = []
    if not settings.eodhd_api_key.get_secret_value():
        out.append(
            "EODHD_API_KEY not set — data refresh falls back to StubProvider "
            "(synthetic bars; fine for dev, useless in prod)"
        )
    if "CHANGE_ME" in settings.database_url:
        out.append("DATABASE_URL still contains the placeholder password")
    if not settings.fred_api_key.get_secret_value():
        out.append(
            "FRED_API_KEY not set — regime composite runs without the "
            "credit-stress component (weights renormalize)"
        )
    has_email = bool(settings.notify_email_to and settings.notify_email_from)
    has_discord = bool(settings.discord_webhook_url.get_secret_value())
    if not has_email and not has_discord:
        out.append(
            "no notification channel configured (email/Discord) — nightly "
            "summaries and alerts have nowhere to go"
        )
    return out
