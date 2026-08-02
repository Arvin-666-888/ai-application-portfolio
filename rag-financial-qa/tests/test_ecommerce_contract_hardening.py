from pathlib import Path

import pytest

from app.services.answer_verification_service import (
    AnswerVerificationService,
    build_citation_ledger,
    evidence_preflight,
    numeric_question_preflight,
)
from app.utils.financial_normalization import normalize_ecommerce_value
from app.utils.retrieval import enumerate_citation_contexts, parse_query_intent


def _fact(fact_type="price", **overrides):
    payload = {
        "fact_type": fact_type,
        "value_text": "79.90",
        "currency": "USD",
        "sku": "SKU-A100",
        "citation_ids": ["C1"],
    }
    payload.update(overrides)
    return payload


def _verify(question, content, fact, *, source="facts.txt"):
    return AnswerVerificationService().verify(
        question,
        {"answer_text": f"结果为 {fact['value_text']} [C1]。", "facts": [fact]},
        contexts=[{
            "source": source,
            "content": content,
            "content_type": "text",
            "provenance_id": "p1",
        }],
    )


@pytest.mark.parametrize(
    ("question", "content", "fact"),
    [
        (
            "SKU-A100库存数量有多少？",
            "SKU-A100库存数量为-42。",
            _fact("inventory_quantity", value_text="42", currency=None),
        ),
        (
            "SKU-A100关税税率是多少？",
            "SKU-A100关税税率为-12%。",
            _fact("customs_duty_rate", value_text="12", unit="percent", currency=None),
        ),
        (
            "SKU-A100价格是多少？",
            "SKU-A100价格为USD 79.90k。",
            _fact(),
        ),
    ],
)
def test_signed_or_scaled_evidence_never_supports_positive_fact(question, content, fact):
    assert not _verify(question, content, fact).passed


def test_hk_currency_suffix_currency_and_chinese_amount_aliases():
    hkd = _fact(value_text="79.90", currency="HKD")
    assert _verify("SKU-A100价格是多少？", "SKU-A100价格为HK$79.90。", hkd).passed
    assert not _verify("SKU-A100价格是多少？", "SKU-A100价格为HK$79.90。", _fact()).passed
    assert _verify("SKU-A100价格是多少？", "SKU-A100价格为79.90 USD。", _fact()).passed
    assert normalize_ecommerce_value("79.90元", fact_type="price").currency == "CNY"


def test_declared_metadata_cannot_override_value_text():
    contradictory_currency = _fact(value_text="USD 79.90", currency="CNY")
    assert not _verify(
        "SKU-A100价格是多少？", "SKU-A100价格为CNY 79.90。", contradictory_currency
    ).passed
    contradictory_duration = _fact(
        "delivery_duration", value_text="3 hours", unit="day", currency=None
    )
    assert not _verify(
        "SKU-A100配送时长多久？", "SKU-A100配送时长为3天。", contradictory_duration
    ).passed


def test_business_day_and_natural_day_equivalence():
    business = _fact(
        "delivery_duration", value_text="3", unit="business_day", currency=None
    )
    assert _verify(
        "SKU-A100物流时效多久？", "SKU-A100物流时效为3个工作日。", business
    ).passed
    natural = _fact("delivery_duration", value_text="3", unit="day", currency=None)
    assert _verify(
        "SKU-A100配送时长多久？", "SKU-A100配送时长为72小时。", natural
    ).passed


def test_date_formats_are_canonicalized_and_sku_prefixes_do_not_collide():
    dated = _fact(date="2026-07-15")
    result = _verify(
        "2026-07-15 SKU-A100价格是多少？",
        "2026年7月15日 SKU-A100价格为USD 79.90。",
        dated,
    )
    assert result.passed
    assert not _verify(
        "SKU-A10价格是多少？",
        "SKU-A100价格为USD 79.90。",
        _fact(sku="SKU-A100"),
    ).passed


def test_inventory_compound_terms_are_not_quantity_queries():
    assert numeric_question_preflight("SKU-A100库存周转率是多少？").errors == [
        "unsupported_fact_type"
    ]
    assert numeric_question_preflight("SKU-A100库存费是多少？").errors == [
        "unsupported_fact_type"
    ]
    assert parse_query_intent("SKU-A100库存数量有多少？").fact_types == (
        "inventory_quantity",
    )


def test_source_filename_cannot_override_conflicting_body_identity():
    assert not _verify(
        "SKU-A100价格是多少？",
        "SKU-B200价格为USD 79.90。",
        _fact(),
        source="SKU-A100.txt",
    ).passed


def test_conflicting_facts_for_same_identity_fail_closed():
    contexts = [
        {"source": "catalog.txt", "content": "SKU-A100价格为USD 79.90。", "content_type": "text", "provenance_id": "p1"},
        {"source": "catalog.txt", "content": "SKU-A100价格为USD 89.90。", "content_type": "text", "provenance_id": "p2"},
    ]
    answer = {
        "answer_text": "价格为USD 79.90 [C1]，也记录为USD 89.90 [C2]。",
        "facts": [
            _fact(citation_ids=["C1"]),
            _fact(value_text="89.90", citation_ids=["C2"]),
        ],
    }
    result = AnswerVerificationService().verify("SKU-A100价格是多少？", answer, contexts=contexts)
    assert "conflicting_ecommerce_facts" in result.errors


def test_citation_identity_keeps_distinct_chunks_and_missing_metadata_pages():
    contexts = [
        {"source": "facts.txt", "content": "intro", "provenance_id": "doc_1:text", "chunk_index": 0},
        {"source": "facts.txt", "content": "SKU-A100价格为USD 79.90。", "provenance_id": "doc_1:text", "chunk_index": 1},
        {"source": "facts.txt", "page_number": 2, "content": "second page"},
    ]
    citations = enumerate_citation_contexts(contexts)
    assert [item[0] for item in citations] == ["C1", "C2", "C3"]
    assert evidence_preflight("SKU-A100价格是多少？", build_citation_ledger(contexts)).passed
