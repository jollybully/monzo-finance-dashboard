from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class LidlSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://finance:finance@localhost:5432/finance"
    lidl_refresh_token: str = ""
    lidl_country: str = "GB"
    lidl_language: str = "en"
    lidl_sync_interval_hours: int = 24
    lidl_device_id: str = "a1b2c3d4e5f67890"
    lidl_app_version: str = "16.45.5"
    app_tz: str = "Europe/London"


@lru_cache
def get_settings() -> LidlSettings:
    return LidlSettings()
