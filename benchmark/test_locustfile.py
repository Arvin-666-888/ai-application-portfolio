from __future__ import annotations

import importlib

import pytest


ENV_NAMES = (
    "AGENT_BASE_URL",
    "RAG_BASE_URL",
    "AGENT_PATH",
    "RAG_PATH",
    "BENCHMARK_USERNAME",
    "BENCHMARK_PASSWORD",
    "AGENT_DS_ID",
    "AGENT_DS_CONNECTION_STRING",
    "RAG_ACCESS_TOKEN",
    "RAG_CONVERSATION_ID",
    "REQUEST_TIMEOUT",
    "BENCHMARK_USE_MOCK",
    "BENCHMARK_SCENARIOS",
)


@pytest.fixture()
def benchmark_config(monkeypatch: pytest.MonkeyPatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    module = importlib.import_module("benchmark.config")
    return importlib.reload(module)


def test_default_config_matches_real_repository_routes(benchmark_config) -> None:
    config = benchmark_config.BenchmarkConfig.from_env()

    assert config.agent_base_url == "http://127.0.0.1:8001"
    assert config.rag_base_url == "http://127.0.0.1:8000"
    assert config.agent_path == "/api/analysis/ask"
    assert config.rag_path == "/api/chat/{conversation_id}"
    assert config.scenarios == {"agent", "rag"}
    assert config.rag_conversation_id is None
    assert config.agent_ds_connection_string == "sqlite:////app/data/sample.db"


def test_config_accepts_documented_overrides(benchmark_config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_BASE_URL", "https://agent.example/")
    monkeypatch.setenv("RAG_BASE_URL", "https://rag.example/")
    monkeypatch.setenv("AGENT_DS_ID", "12")
    monkeypatch.setenv("RAG_CONVERSATION_ID", "34")
    monkeypatch.setenv("REQUEST_TIMEOUT", "12.5")
    monkeypatch.setenv("BENCHMARK_USE_MOCK", "false")
    monkeypatch.setenv("BENCHMARK_SCENARIOS", "rag")

    config = benchmark_config.BenchmarkConfig.from_env()

    assert config.agent_base_url == "https://agent.example"
    assert config.rag_base_url == "https://rag.example"
    assert config.agent_ds_id == 12
    assert config.rag_conversation_id == 34
    assert config.request_timeout == 12.5
    assert config.use_mock is False
    assert config.scenarios == {"rag"}


def test_invalid_scenario_is_rejected(benchmark_config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BENCHMARK_SCENARIOS", "agent,unknown")

    with pytest.raises(ValueError, match="BENCHMARK_SCENARIOS"):
        benchmark_config.BenchmarkConfig.from_env()


def test_business_response_rejects_model_failures_and_mock_answers() -> None:
    from benchmark.config import business_response_error

    for answer in ("大模型调用失败: timeout", "[模拟回答] demo"):
        assert business_response_error({"answer": answer}, "Agent", "answer")

    assert business_response_error({"answer": "真实业务回答"}, "Agent", "answer") is None
    assert business_response_error({}, "Agent", "answer")


def test_rag_path_formats_conversation_id(benchmark_config) -> None:
    config = benchmark_config.BenchmarkConfig.from_env()
    assert config.rag_path.format(conversation_id=7) == "/api/chat/7"
