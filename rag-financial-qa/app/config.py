import os
import secrets
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "跨境电商商品事实问答系统"
    DEBUG: bool = True

    DATABASE_URL: str = "sqlite:///./kb_qa.db"
    SQLITE_BUSY_TIMEOUT_MS: int = 5000
    DOCUMENT_JOB_LEASE_SECONDS: int = 300
    DOCUMENT_JOB_STALE_AFTER_SECONDS: int = 600
    DOCUMENT_JOB_MAX_ATTEMPTS: int = 3
    DOCUMENT_JOB_RETRY_BASE_SECONDS: int = 5
    DOCUMENT_JOB_RETRY_MAX_SECONDS: int = 300
    DOCUMENT_WORKER_POLL_SECONDS: float = 1.0
    DOCUMENT_PARSE_SNAPSHOT_DIR: str = "./parse_snapshots"
    PADDLE_WORKER_DEVICE: str = "gpu"
    PADDLE_WORKER_LOCK_FILE: str = "./requirements-paddleocr-windows-py312.lock.txt"
    PADDLE_WORKER_DEPLOYMENT_MODE: str = "disabled"
    PADDLE_WORKER_SHARED_ROOT: str = "."
    SECRET_KEY: str = "change-this-secret-key-in-production"
    ALLOW_INSECURE_DEMO_MODE: bool = True
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173"

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

    PDF_PARSE_PROFILE: str = "three_layer_v1"
    PDF_HI_RES_ENABLED: bool = True
    PDF_HI_RES_MAX_PAGES_PER_DOCUMENT: int = 80
    PDF_PADDLE_ARTIFACT_ENABLED: bool = False
    PDF_PADDLE_ARTIFACT_DIR: str = "./ocr_artifacts"
    PDF_PADDLE_EXPECTED_ENGINE_FINGERPRINT: str = ""
    PDF_NATIVE_TEXT_MIN_CHARS: int = 20
    PDF_TABLE_NUMERIC_RATIO_MIN: float = 0.18
    PDF_TABLE_LINE_COUNT_MIN: int = 15
    PDF_TABLE_TITLE_NEIGHBOR_BEFORE: int = 1
    PDF_TABLE_TITLE_NEIGHBOR_AFTER: int = 3
    PDF_TABLE_ROW_OVERLAP: int = 1

    RETRIEVAL_CANDIDATE_MULTIPLIER: int = 4
    RETRIEVAL_PROFILE: str = "legacy"
    RAG_ANSWER_PROFILE: str = "legacy"
    RAG_CONTEXT_MAX_CHARS: int = 24000
    RAG_CONTEXT_ITEM_MAX_CHARS: int = 6000
    LLM_INPUT_COST_PER_1M: str = ""
    LLM_OUTPUT_COST_PER_1M: str = ""
    EMBEDDING_COST_PER_1M: str = ""
    COST_CURRENCY: str = "USD"
    LEXICAL_WEIGHT: float = 0.35
    NUMERIC_WEIGHT: float = 0.15
    MIN_RELEVANCE_SCORE: float = 0.05

    class Config:
        env_file = ".env"
        extra = "ignore"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if self.SQLITE_BUSY_TIMEOUT_MS < 0:
            raise ValueError("SQLITE_BUSY_TIMEOUT_MS 不能小于 0")
        if self.DOCUMENT_JOB_LEASE_SECONDS < 1:
            raise ValueError("DOCUMENT_JOB_LEASE_SECONDS 必须大于 0")
        if self.DOCUMENT_JOB_STALE_AFTER_SECONDS < 1:
            raise ValueError("DOCUMENT_JOB_STALE_AFTER_SECONDS 必须大于 0")
        if self.DOCUMENT_JOB_MAX_ATTEMPTS < 1:
            raise ValueError("DOCUMENT_JOB_MAX_ATTEMPTS 必须大于 0")
        if self.DOCUMENT_JOB_RETRY_BASE_SECONDS < 0:
            raise ValueError("DOCUMENT_JOB_RETRY_BASE_SECONDS 不能小于 0")
        if self.DOCUMENT_JOB_RETRY_MAX_SECONDS < self.DOCUMENT_JOB_RETRY_BASE_SECONDS:
            raise ValueError("DOCUMENT_JOB_RETRY_MAX_SECONDS 不能小于基础退避秒数")
        if self.DOCUMENT_WORKER_POLL_SECONDS <= 0:
            raise ValueError("DOCUMENT_WORKER_POLL_SECONDS 必须大于 0")
        if self.PADDLE_WORKER_DEPLOYMENT_MODE not in {"disabled", "windows_same_root", "docker_only"}:
            raise ValueError("PADDLE_WORKER_DEPLOYMENT_MODE 配置无效")
        if not self.PADDLE_WORKER_SHARED_ROOT.strip():
            raise ValueError("PADDLE_WORKER_SHARED_ROOT 不能为空")
        if self.RETRIEVAL_PROFILE not in {"legacy", "ecommerce_v2"}:
            raise ValueError("RETRIEVAL_PROFILE 配置无效")
        if self.RAG_ANSWER_PROFILE not in {"legacy", "verified_v3"}:
            raise ValueError("RAG_ANSWER_PROFILE 配置无效")
        if self.RAG_CONTEXT_MAX_CHARS < 1 or self.RAG_CONTEXT_ITEM_MAX_CHARS < 1:
            raise ValueError("RAG context 字符预算必须大于 0")
        insecure_secret = (
            len(self.SECRET_KEY) < 32
            or self.SECRET_KEY in {
                "change-this-secret-key-in-production",
                "replace-with-a-long-random-secret",
            }
        )
        if insecure_secret:
            if self.DEBUG and self.ALLOW_INSECURE_DEMO_MODE:
                self.SECRET_KEY = secrets.token_urlsafe(48)
            else:
                raise ValueError(
                    "SECRET_KEY 必须是至少 32 字符的随机值；仅 DEBUG + "
                    "ALLOW_INSECURE_DEMO_MODE 可自动生成临时演示密钥"
                )
        if self.TOP_K < 1 or self.RETRIEVAL_CANDIDATE_MULTIPLIER < 1:
            raise ValueError("TOP_K 和 RETRIEVAL_CANDIDATE_MULTIPLIER 必须大于 0")
        if not 0 <= self.LEXICAL_WEIGHT <= 1 or not 0 <= self.NUMERIC_WEIGHT <= 1:
            raise ValueError("检索权重必须在 0 到 1 之间")
        if not 0 <= self.MIN_RELEVANCE_SCORE <= 1:
            raise ValueError("MIN_RELEVANCE_SCORE 必须在 0 到 1 之间")
        if self.PDF_PARSE_PROFILE not in {"three_layer_v1", "unstructured_fast", "unstructured_hi_res"}:
            raise ValueError("PDF_PARSE_PROFILE 配置无效")
        if self.PDF_HI_RES_MAX_PAGES_PER_DOCUMENT < 1:
            raise ValueError("PDF_HI_RES_MAX_PAGES_PER_DOCUMENT 必须大于 0")
        if self.PDF_PADDLE_ARTIFACT_ENABLED:
            fingerprint = self.PDF_PADDLE_EXPECTED_ENGINE_FINGERPRINT.lower()
            if len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
                raise ValueError(
                    "启用 PDF_PADDLE_ARTIFACT_ENABLED 时必须提供有效的 "
                    "PDF_PADDLE_EXPECTED_ENGINE_FINGERPRINT"
                )
        if self.PDF_NATIVE_TEXT_MIN_CHARS < 0:
            raise ValueError("PDF_NATIVE_TEXT_MIN_CHARS 不能小于 0")
        if not 0 <= self.PDF_TABLE_NUMERIC_RATIO_MIN <= 1:
            raise ValueError("PDF_TABLE_NUMERIC_RATIO_MIN 必须在 0 到 1 之间")
        if self.PDF_TABLE_LINE_COUNT_MIN < 1:
            raise ValueError("PDF_TABLE_LINE_COUNT_MIN 必须大于 0")
        if self.PDF_TABLE_TITLE_NEIGHBOR_BEFORE < 0 or self.PDF_TABLE_TITLE_NEIGHBOR_AFTER < 0:
            raise ValueError("PDF 表格标题邻页范围不能小于 0")
        if self.PDF_TABLE_ROW_OVERLAP not in {0, 1}:
            raise ValueError("PDF_TABLE_ROW_OVERLAP 必须为 0 或 1")
        for rate_name in (
            "LLM_INPUT_COST_PER_1M",
            "LLM_OUTPUT_COST_PER_1M",
            "EMBEDDING_COST_PER_1M",
        ):
            rate = getattr(self, rate_name)
            if not rate:
                continue
            try:
                if float(rate) < 0:
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{rate_name} 必须是非负数值") from exc
        os.makedirs(self.UPLOAD_DIR, exist_ok=True)
        os.makedirs(self.CHROMA_DIR, exist_ok=True)
        os.makedirs(self.DOCUMENT_PARSE_SNAPSHOT_DIR, exist_ok=True)
        os.makedirs(self.PDF_PADDLE_ARTIFACT_DIR, exist_ok=True)


settings = Settings()
