from __future__ import annotations

import asyncio
import json

import pytest

from app.services import rag_service
from app.services.rag_service import GeneratedOutput


CONTEXT = {
    "source": "贵州茅台_2024年报.pdf",
    "content": "贵州茅台 2024年 合并 营业收入为人民币1,234.5万元。",
    "page_number": 10,
    "content_type": "text",
    "provenance_id": "p10",
    "distance": 0.1,
}


def _structured(citation="C1", value="1,234.5"):
    return json.dumps(
        {
            "answer_text": f"贵州茅台2024年合并营业收入为{value}万元 [{citation}]。",
            "facts": [
                {
                    "metric": "营业收入",
                    "value_text": value,
                    "unit": "万元",
                    "currency": "CNY",
                    "year": "2024",
                    "company": "贵州茅台",
                    "scope": "合并",
                    "citation_ids": ["C1"],
                }
            ],
        },
        ensure_ascii=False,
    )


def test_execute_answer_from_contexts_never_retrieves(monkeypatch):
    monkeypatch.setattr(rag_service.settings, "RAG_ANSWER_PROFILE", "verified_v3")

    async def fail_retrieve(*args, **kwargs):
        raise AssertionError("frozen-context generation must not retrieve")

    async def generate(*args, **kwargs):
        return GeneratedOutput(content=_structured())

    monkeypatch.setattr(rag_service, "retrieve_context", fail_retrieve)
    monkeypatch.setattr(rag_service, "generate_output", generate)

    result = asyncio.run(rag_service.execute_answer_from_contexts(
        "贵州茅台2024年合并营业收入是多少？",
        [CONTEXT],
        answer_profile="verified_v3",
    ))

    assert result.answer_status == "verified"
    assert result.retrieval_ms is None


def test_execute_answer_verified_success(monkeypatch):
    monkeypatch.setattr(rag_service.settings, "RAG_ANSWER_PROFILE", "verified_v3")

    async def retrieve(*args, **kwargs):
        return [CONTEXT]

    async def generate(*args, **kwargs):
        return GeneratedOutput(
            content=_structured(),
            usage={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
        )

    monkeypatch.setattr(rag_service, "retrieve_context", retrieve)
    monkeypatch.setattr(rag_service, "generate_output", generate)

    result = asyncio.run(rag_service.execute_answer(
        "贵州茅台2024年合并营业收入是多少？", 1, active_index_targets=[(1, "v1")]
    ))

    assert result.answer_status == "verified"
    assert result.verification.passed is True
    assert result.structured_answer.facts[0].citation_ids == ["C1"]
    assert result.sources[0]["citation_id"] == "C1"


@pytest.mark.parametrize(
    "output",
    ["not-json", _structured(citation="C99"), _structured(value="9,999")],
)
def test_execute_answer_fail_closed_without_candidate_leak(monkeypatch, output):
    monkeypatch.setattr(rag_service.settings, "RAG_ANSWER_PROFILE", "verified_v3")

    async def retrieve(*args, **kwargs):
        return [CONTEXT]

    async def generate(*args, **kwargs):
        return GeneratedOutput(content=output)

    monkeypatch.setattr(rag_service, "retrieve_context", retrieve)
    monkeypatch.setattr(rag_service, "generate_output", generate)

    result = asyncio.run(rag_service.execute_answer(
        "贵州茅台2024年合并营业收入是多少？", 1
    ))

    assert result.answer_status == "refused"
    assert result.answer == rag_service.VERIFIED_REFUSAL
    assert output not in result.answer
    assert result.sources == []
    assert result.verification.passed is False


def test_execute_answer_non_numeric_question_is_not_misclassified_as_refusal(monkeypatch):
    monkeypatch.setattr(rag_service.settings, "RAG_ANSWER_PROFILE", "verified_v3")

    async def retrieve(*args, **kwargs):
        return [{**CONTEXT, "content": "公司面临原材料价格和汇率波动风险。"}]

    async def generate(_messages, *, structured=None):
        assert structured is False
        return GeneratedOutput(content="公司面临原材料价格和汇率波动风险。[C1]")

    monkeypatch.setattr(rag_service, "retrieve_context", retrieve)
    monkeypatch.setattr(rag_service, "generate_output", generate)

    result = asyncio.run(rag_service.execute_answer("公司有哪些风险？", 1))

    assert result.answer_status == "unverified"
    assert result.refusal_code is None
    assert result.verification.status == "not_applicable"
    assert result.sources[0]["citation_id"] == "C1"


def test_materialized_evidence_is_shared_by_prompt_ledger_and_sources(monkeypatch):
    monkeypatch.setattr(rag_service.settings, "RAG_CONTEXT_ITEM_MAX_CHARS", 20)
    monkeypatch.setattr(rag_service.settings, "RAG_CONTEXT_MAX_CHARS", 200)
    contexts = [{**CONTEXT, "content": "x" * 20 + "人民币1,234.5万元"}]

    materialized = rag_service.materialize_contexts(contexts)
    prompt_context = rag_service.format_context(materialized)
    ledger = rag_service.build_citation_ledger(materialized)
    sources = rag_service.build_sources(materialized)

    assert ledger["C1"].content == materialized[0]["content"]
    assert ledger["C1"].content in prompt_context
    assert sources[0]["snippet"] == materialized[0]["content"]
    assert "1,234.5" not in ledger["C1"].content
