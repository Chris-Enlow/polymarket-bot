from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
import structlog

log = structlog.get_logger()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Polymarket API endpoints
    gamma_api_base: str = "https://gamma-api.polymarket.com"
    clob_api_base: str = "https://clob.polymarket.com"
    data_api_base: str = "https://data-api.polymarket.com"
    # Paper trading parameters
    trade_size_usd: float = 10.0
    leader_refresh_interval_hours: int = 6
    pnl_check_interval_minutes: int = 15
    monitor_poll_interval_seconds: int = 60

    # Leader qualification thresholds
    min_closed_positions: int = 50
    min_win_rate: float = 0.55          # 55%

    # PostgreSQL (asyncpg driver)
    database_url: str = "postgresql+asyncpg://poly:poly@db:5432/polymarket"

    # Logging
    log_level: str = "INFO"

    # Concurrency — number of parallel CLOB price fetches in the trade queue
    trade_worker_count: int = 8

    # HTTP client timeouts (seconds)
    http_timeout: int = 10

    @field_validator("log_level")
    @classmethod
    def normalise_log_level(cls, v: str) -> str:
        return v.upper()


settings = Settings()
