from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urljoin


@dataclass(frozen=True)
class BenchmarkConfig:
    agent_base_url: str
    rag_base_url: str
    agent_path: str
    rag_path: str
    username: str
    password: str
    agent_ds_id: int | None
    agent_ds_connection_string: str
    rag_access_token: str
    rag_conversation_id: int | None
    request_timeout: float
    use_mock: bool
    scenarios: frozenset[str]

    @classmethod
    def from_env(cls) -> "BenchmarkConfig":
        scenarios = frozenset(
            item.strip().lower()
            for item in os.getenv("BENCHMARK_SCENARIOS", "agent,rag").split(",")
            if item.strip()
        )
        invalid = scenarios - {"agent", "rag"}
        if not scenarios or invalid:
            raise ValueError(
                "BENCHMARK_SCENARIOS must contain agent, rag, or both; "
                f"invalid values: {sorted(invalid)}"
            )

        return cls(
            agent_base_url=_base_url("AGENT_BASE_URL", "http://127.0.0.1:8001"),
            rag_base_url=_base_url("RAG_BASE_URL", "http://127.0.0.1:8000"),
            agent_path=_path("AGENT_PATH", "/api/analysis/ask"),
            rag_path=_path("RAG_PATH", "/api/chat/{conversation_id}"),
            username=os.getenv("BENCHMARK_USERNAME", "benchmark_user"),
            password=os.getenv("BENCHMARK_PASSWORD", "benchmark-password-123"),
            agent_ds_id=_optional_positive_int("AGENT_DS_ID"),
            agent_ds_connection_string=os.getenv(
                "AGENT_DS_CONNECTION_STRING", "sqlite:////app/data/sample.db"
            ),
            rag_access_token=os.getenv("RAG_ACCESS_TOKEN", "").strip(),
            rag_conversation_id=_optional_positive_int("RAG_CONVERSATION_ID"),
            request_timeout=_positive_float("REQUEST_TIMEOUT", 60.0),
            use_mock=_boolean("BENCHMARK_USE_MOCK", True),
            scenarios=scenarios,
        )


def _base_url(name: str, default: str) -> str:
    value = os.getenv(name, default).strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise ValueError(f"{name} must be an absolute HTTP(S) URL")
    return value


def _path(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    if not value.startswith("/"):
        raise ValueError(f"{name} must start with /")
    return value


def _optional_positive_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_float(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def business_response_error(body: object, service: str, required_key: str) -> str | None:
    if not isinstance(body, dict) or not body.get(required_key):
        return f"{service} response missing non-empty {required_key}"

    answer = str(body[required_key])
    failure_markers = (
        "大模型调用失败",
        "[模拟回答]",
        "请配置 API_KEY",
    )
    if any(marker in answer for marker in failure_markers):
        return f"{service} returned a model failure or mock answer"
    return None


def build_url(base_url: str, path: str) -> str:
    return urljoin(f"{base_url}/", path.lstrip("/"))
