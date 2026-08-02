from __future__ import annotations

import asyncio
import json

import pytest

from app.services import rag_service
from app.services.rag_service import GeneratedOutput


CONTEXT = {
    "source": "catalog-2026.pdf",
    "content": "2026-07-15 Amazon美国市场轻量旅行背包 SKU-A100 的价格为USD 79.90。",
    "page_number": 10,
    "content_type": "text",
    "provenance_id": "p10",
    "distance": 0.1,
}
QUESTION = "2026-07-15 Amazon美国市场SKU-A100轻量旅行背包价格是多少？"


def _structured(citation="C1", value="79.90"):
    return json.dumps({
        "answer_text": f"SKU-A100价格为USD {value} [{citation}]。",
        "facts": [{
            "fact_type": "price", "value_text": value, "currency": "USD",
            "sku": "SKU-A100", "product": "轻量旅行背包", "platform": "Amazon",
            "market": "美国", "date": "2026-07-15", "citation_ids": ["C1"],
        }],
    }, ensure_ascii=False)


def test_execute_answer_from_contexts_never_retrieves(monkeypatch):
    monkeypatch.setattr(rag_service.settings, "RAG_ANSWER_PROFILE", "verified_v3")

    async def fail_retrieve(*args, **kwargs):
        raise AssertionError("frozen-context generation must not retrieve")

    async def generate(*args, **kwargs):
        return GeneratedOutput(content=_structured())

    monkeypatch.setattr(rag_service, "retrieve_context", fail_retrieve)
    monkeypatch.setattr(rag_service, "generate_output", generate)
    result = asyncio.run(rag_service.execute_answer_from_contexts(QUESTION, [CONTEXT], answer_profile="verified_v3"))
    assert result.answer_status == "verified"
    assert result.retrieval_ms is None


def test_execute_answer_verified_success(monkeypatch):
    monkeypatch.setattr(rag_service.settings, "RAG_ANSWER_PROFILE", "verified_v3")

    async def retrieve(*args, **kwargs):
        return [CONTEXT]

    async def generate(*args, **kwargs):
        return GeneratedOutput(content=_structured(), usage={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30})

    monkeypatch.setattr(rag_service, "retrieve_context", retrieve)
    monkeypatch.setattr(rag_service, "generate_output", generate)
    result = asyncio.run(rag_service.execute_answer(QUESTION, 1, active_index_targets=[(1, "v1")]))
    assert result.answer_status == "verified"
    assert result.verification.passed
    assert result.structured_answer.facts[0].fact_type == "price"
    assert result.sources[0]["citation_id"] == "C1"


@pytest.mark.parametrize("output", ["not-json", _structured(citation="C99"), _structured(value="89.90")])
def test_execute_answer_fail_closed_without_candidate_leak(monkeypatch, output):
    monkeypatch.setattr(rag_service.settings, "RAG_ANSWER_PROFILE", "verified_v3")

    async def retrieve(*args, **kwargs):
        return [CONTEXT]

    async def generate(*args, **kwargs):
        return GeneratedOutput(content=output)

    monkeypatch.setattr(rag_service, "retrieve_context", retrieve)
    monkeypatch.setattr(rag_service, "generate_output", generate)
    result = asyncio.run(rag_service.execute_answer(QUESTION, 1))
    assert result.answer_status == "refused"
    assert result.answer == rag_service.VERIFIED_REFUSAL
    assert output not in result.answer
    assert result.sources == []
    assert not result.verification.passed


def test_unsupported_fact_refuses_before_retrieval(monkeypatch):
    async def fail_retrieve(*args, **kwargs):
        raise AssertionError("unsupported fact must not retrieve")

    monkeypatch.setattr(rag_service, "retrieve_context", fail_retrieve)
    result = asyncio.run(rag_service.execute_answer("SKU-A100重量是多少？", 1))
    assert result.answer_status == "refused"
    assert result.sources == []
    assert result.refusal_code == "policy_refusal"


def test_materialized_evidence_is_shared_by_prompt_ledger_and_sources(monkeypatch):
    monkeypatch.setattr(rag_service.settings, "RAG_CONTEXT_ITEM_MAX_CHARS", 20)
    monkeypatch.setattr(rag_service.settings, "RAG_CONTEXT_MAX_CHARS", 200)
    contexts = [{**CONTEXT, "content": "x" * 20 + "USD 79.90"}]
    materialized = rag_service.materialize_contexts(contexts)
    prompt_context = rag_service.format_context(materialized)
    ledger = rag_service.build_citation_ledger(materialized)
    sources = rag_service.build_sources(materialized)
    assert ledger["C1"].content == materialized[0]["content"]
    assert ledger["C1"].content in prompt_context
    assert sources[0]["snippet"] == materialized[0]["content"]
    assert "79.90" not in ledger["C1"].content
