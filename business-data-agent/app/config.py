import os
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_NAME: str = "跨境电商经营数据分析 Agent"
    DEBUG: bool = True

    DATABASE_URL: str = "sqlite:///./business_data_agent.db"
    SECRET_KEY: str = "change-this-secret-key-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    CHART_DIR: str = "./charts"
    SAMPLE_DB_PATH: str = "./sample_data/sample.db"

    API_KEY: str = ""
    BASE_URL: str = "https://dash.ovload.com/v1"
    MODEL: str = "gpt-5.5"

    MAX_AGENT_STEPS: int = 8
    MAX_QUERY_ROWS: int = 1000
    MAX_TOOL_RESULT_CHARS: int = 2500
    MAX_PREVIEW_ROWS: int = 3
    MAX_CHART_ITEMS: int = 30

    RAG_ENABLED: bool = False
    RAG_API_BASE_URL: str = "http://localhost:8018"
    RAG_ACCESS_TOKEN: str = ""
    RAG_CONVERSATION_ID: int = 0
    RAG_TIMEOUT: float = 30.0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("DEBUG", mode="before")
    @classmethod
    def parse_debug_env(cls, value: Any) -> Any:
        if isinstance(value, str) and value.lower() in {"release", "prod", "production"}:
            return False
        return value

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        os.makedirs(self.CHART_DIR, exist_ok=True)
        os.makedirs(os.path.dirname(self.SAMPLE_DB_PATH), exist_ok=True)


settings = Settings()
