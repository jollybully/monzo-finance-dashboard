from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://finance:finance@localhost:5432/finance"

    google_credentials_file: str = "/credentials/service-account.json"
    google_sheet_id: str = ""
    google_sheet_range: str = "Monzo Transactions!A:O"

    app_tz: str = "Europe/London"
    reserved_buffer_default: float = 0.0

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    email_to: str = ""
    smtp_use_tls: bool = True

    report_daily_enabled: bool = True
    report_weekly_enabled: bool = True
    report_monthly_enabled: bool = True
    report_daily_hour: int = 7
    report_daily_minute: int = 0
    report_weekly_hour: int = 7
    report_weekly_minute: int = 30
    report_monthly_hour: int = 8
    report_monthly_minute: int = 0

    sync_interval_minutes: int = 15


@lru_cache
def get_settings() -> Settings:
    return Settings()
