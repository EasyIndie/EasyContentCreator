from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded exclusively from environment variables."""

    model_config = SettingsConfigDict(env_prefix="ECC_", env_file=".env", extra="ignore")

    environment: str = "development"
    log_level: str = "INFO"
    database_url: str = Field(
        default="postgresql://easycontent:change-me@localhost:5432/easycontent",
        repr=False,
    )
    worker_poll_interval_seconds: float = Field(default=5.0, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
