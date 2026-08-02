from decimal import Decimal

import pytest

from app.schemas.schemas import StructuredAnswer
from app.services.answer_verification_service import (
    AnswerVerificationService,
    build_citation_ledger,
    evidence_preflight,
    numeric_question_preflight,
    parse_structured_output,
)
from app.utils.financial_normalization import normalize_ecommerce_value, normalize_text


def _fact(fact_type="price", **overrides):
    payload = {
        "fact_type": fact_type,
        "value_text": "79.90",
        "unit": None,
        "currency": "USD",
        "sku": "SKU-A100",
        "product": "轻量旅行背包",
        "platform": "Amazon",
        "market": "美国",
        "date": "2026-07-15",
        "citation_ids": ["C1"],
    }
    payload.update(overrides)
    return payload


def _contexts(content="2026-07-15 Amazon美国市场轻量旅行背包 SKU-A100 的价格为USD 79.90。"):
    return [{
        "source": "catalog-2026.pdf",
        "content": content,
        "page_number": 10,
        "content_type": "text",
        "provenance_id": "page-10-chunk-1",
    }]


def test_normalization_uses_decimal_nfkc_and_explicit_currency():
    normalized = normalize_ecommerce_value("ＵＳＤ ７９．９０", fact_type="price")
    assert normalize_text("ＵＳＤ ７９．９０") == "USD 79.90"
    assert normalized is not None
    assert normalized.value == Decimal("79.90")
    assert normalized.currency == "USD"


@pytest.mark.parametrize(
    ("fact_type", "text", "kind", "unit"),
    [
        ("price", "CNY 129", "price", None),
        ("inventory_quantity", "42", "inventory_quantity", None),
        ("delivery_duration", "48 hours", "delivery_duration", "hour"),
        ("delivery_duration", "3 business days", "delivery_duration", "business_day"),
        ("customs_duty_rate", "12%", "customs_duty_rate", "percent"),
    ],
)
def test_normalization_handles_supported_fact_units(fact_type, text, kind, unit):
    normalized = normalize_ecommerce_value(text, fact_type=fact_type)
    assert normalized is not None
    assert normalized.kind == kind
    assert normalized.unit == unit


@pytest.mark.parametrize(
    ("fact_type", "text"),
    [("price", "79.90"), ("inventory_quantity", "4.5"), ("delivery_duration", "3"), ("customs_duty_rate", "12")],
)
def test_normalization_does_not_guess_missing_unit(fact_type, text):
    assert normalize_ecommerce_value(text, fact_type=fact_type) is None


def test_citation_ledger_keeps_private_full_content_identity_and_sha():
    contexts = _contexts("x" * 500)
    ledger = build_citation_ledger(contexts)
    assert ledger["C1"].content == "x" * 500
    assert ledger["C1"].identity == ("provenance", "page-10-chunk-1")
    assert len(ledger["C1"].content_sha256) == 64


def test_preflight_rejects_formula_unsupported_spec_and_multi_fact():
    assert numeric_question_preflight("请把SKU-A100美元价格换算成人民币").errors == ["unsupported_complex_formula"]
    assert numeric_question_preflight("SKU-A100重量是多少？").errors == ["unsupported_fact_type"]
    assert numeric_question_preflight("SKU-A100价格和库存分别是多少？").errors == ["unsupported_multi_fact"]


def test_evidence_preflight_requires_same_local_binding():
    question = "SKU-A100价格是多少？"
    assert evidence_preflight(question, build_citation_ledger(_contexts("SKU-A100价格为USD 79.90。"))).passed
    assert evidence_preflight(question, build_citation_ledger(_contexts("SKU-A100价格如下。\nUSD 79.90。"))).errors == ["no_fact_binding"]


def test_table_row_binding_passes_and_ambiguous_row_fails():
    valid = _contexts("| SKU | 商品 | 价格 |\n| SKU-A100 | 轻量旅行背包 | USD 79.90 |")
    valid[0]["content_type"] = "table"
    valid[0]["table_id"] = "t1"
    ambiguous = _contexts("| SKU | 价格 | 原价 |\n| SKU-A100 | USD 79.90 | USD 89.90 |")
    ambiguous[0]["content_type"] = "table"
    ambiguous[0]["table_id"] = "t1"
    answer = {"answer_text": "SKU-A100价格为USD 79.90 [C1]。", "facts": [_fact(product=None, platform=None, market=None, date=None)]}

    assert AnswerVerificationService().verify("SKU-A100价格是多少？", answer, contexts=valid).passed
    assert not AnswerVerificationService().verify("SKU-A100价格是多少？", answer, contexts=ambiguous).passed


def test_structured_output_parser_accepts_json_fence_and_value_text_stays_string():
    parsed = parse_structured_output(
        '```json\n{"answer_text":"价格为USD 79.90 [C1]。","facts":[{"fact_type":"price",'
        '"value_text":"79.90","currency":"USD","sku":"SKU-A100","citation_ids":["C1"]}]}\n```'
    )
    assert isinstance(parsed, StructuredAnswer)
    assert parsed.facts[0].value_text == "79.90"


def test_verifier_passes_same_sentence_evidence():
    answer = {"answer_text": "SKU-A100价格为USD 79.90 [C1]。", "facts": [_fact()]}
    result = AnswerVerificationService().verify(
        "2026-07-15 Amazon美国市场SKU-A100轻量旅行背包价格是多少？",
        answer,
        contexts=_contexts(),
    )
    assert result.passed
    assert result.verified_citation_ids == ["C1"]


@pytest.mark.parametrize(
    ("fact_overrides", "error_fragment"),
    [
        ({"citation_ids": []}, "missing_citation"),
        ({"citation_ids": ["C99"]}, "unknown_citation"),
        ({"value_text": "89.90"}, "evidence_mismatch"),
        ({"currency": "CNY"}, "evidence_mismatch"),
        ({"sku": "SKU-B200"}, "sku_mismatch"),
        ({"product": "其他商品"}, "product_mismatch"),
        ({"platform": "京东"}, "platform_mismatch"),
        ({"market": "中国"}, "market_mismatch"),
        ({"date": "2026-07-16"}, "date_mismatch"),
    ],
)
def test_verifier_fails_closed_on_fact_mismatch(fact_overrides, error_fragment):
    result = AnswerVerificationService().verify(
        "2026-07-15 Amazon美国市场SKU-A100轻量旅行背包价格是多少？",
        {"answer_text": "结果见引用 [C1]。", "facts": [_fact(**fact_overrides)]},
        contexts=_contexts(),
    )
    assert not result.passed
    assert any(error_fragment in error for error in result.errors)


def test_verifier_rejects_known_but_unsupported_extra_citation():
    contexts = _contexts() + [{
        "source": "catalog-2026.pdf", "content": "SKU-A100库存数量为42。",
        "page_number": 11, "content_type": "text", "provenance_id": "p11",
    }]
    result = AnswerVerificationService().verify(
        "SKU-A100价格是多少？",
        {"answer_text": "价格为USD 79.90 [C1][C2]。", "facts": [_fact(product=None, platform=None, market=None, date=None, citation_ids=["C1", "C2"])]},
        contexts=contexts,
    )
    assert "fact_1:unsupported_citation:C2" in result.errors


def test_verifier_rejects_answer_text_citation_mismatch_and_extra_number():
    service = AnswerVerificationService()
    unknown = service.verify(
        "SKU-A100价格是多少？",
        {"answer_text": "价格为USD 79.90 [C99]。", "facts": [_fact(product=None, platform=None, market=None, date=None)]},
        contexts=_contexts("SKU-A100价格为USD 79.90。"),
    )
    extra = service.verify(
        "SKU-A100价格是多少？",
        {"answer_text": "价格为USD 79.90，折扣10% [C1]。", "facts": [_fact(product=None, platform=None, market=None, date=None)]},
        contexts=_contexts("SKU-A100价格为USD 79.90。"),
    )
    assert "answer_unknown_citation:C99" in unknown.errors
    assert "answer_fact_citation_mismatch" in unknown.errors
    assert "answer_contains_uncited_numeric_value" in extra.errors


def test_inventory_delivery_and_duty_verify_with_strict_units():
    cases = [
        ("SKU-A100库存数量有多少？", "SKU-A100库存数量为42。", _fact("inventory_quantity", value_text="42", currency=None, product=None, platform=None, market=None, date=None)),
        ("SKU-A100配送时长多久？", "SKU-A100配送时长为3 business days。", _fact("delivery_duration", value_text="3", unit="business_day", currency=None, product=None, platform=None, market=None, date=None)),
        ("SKU-A100关税税率是多少？", "SKU-A100关税税率为12%。", _fact("customs_duty_rate", value_text="12", unit="percent", currency=None, product=None, platform=None, market=None, date=None)),
    ]
    for question, content, fact in cases:
        result = AnswerVerificationService().verify(
            question,
            {"answer_text": f"结果为{fact['value_text']} [C1]。", "facts": [fact]},
            contexts=_contexts(content),
        )
        assert result.passed
