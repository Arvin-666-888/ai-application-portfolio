from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_NAME: str = "VoltCore Multi-Agent Support"
    DEBUG: bool = False
    DATABASE_URL: str = f"sqlite:///{(PROJECT_ROOT / 'data' / 'ecommerce.db').as_posix()}"
    SECRET_KEY: str = Field(
        default="development-secret-key-change-before-production",
        min_length=32,
    )
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=60, ge=5, le=1440)
    COMMERCE_BACKEND: Literal["sqlite", "woocommerce"] = "sqlite"
    SEED_DEMO_DATA: bool = True
    DEMO_DATA_SEED: int = 20260722
    API_KEY: str = ""
    BASE_URL: str = "https://api.openai.com/v1"
    MODEL: str = "gpt-4.1-mini"


settings = Settings()
