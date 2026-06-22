import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "企业知识库问答系统"
    DEBUG: bool = True

    DATABASE_URL: str = "sqlite:///./kb_qa.db"
    SECRET_KEY: str = "change-this-secret-key-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    UPLOAD_DIR: str = "./uploads"
    CHROMA_DIR: str = "./chroma_data"
    MAX_FILE_SIZE: int = 10 * 1024 * 1024
    ALLOWED_EXTENSIONS: set = {".txt", ".md", ".pdf"}

    API_KEY: str = ""
    BASE_URL: str = "https://api.openai.com/v1"
    MODEL: str = "gpt-3.5-turbo"
    EMBEDDING_MODEL: str = "text-embedding-3-small"

    CHUNK_SIZE: int = 400
    CHUNK_OVERLAP: int = 80
    TOP_K: int = 3
    MAX_HISTORY_ROUNDS: int = 5
    RETRIEVAL_CANDIDATE_MULTIPLIER: int = 4
    LEXICAL_WEIGHT: float = 0.35
    MIN_RELEVANCE_SCORE: float = 0.05

    class Config:
        env_file = ".env"
        extra = "ignore"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        os.makedirs(self.UPLOAD_DIR, exist_ok=True)
        os.makedirs(self.CHROMA_DIR, exist_ok=True)


settings = Settings()
