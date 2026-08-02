from decimal import Decimal

import pytest

from app.services.answer_verification_service import (
    AnswerVerificationService,
    build_citation_ledger,
    evidence_preflight,
    numeric_question_preflight,
)
from app.utils.financial_normalization import normalize_ecommerce_value


@pytest.mark.parametrize(
    ("fact_type", "text", "unit", "currency", "kind", "value"),
    [
        ("price", "USD 79.90", None, "USD", "price", Decimal("79.90")),
        ("inventory_quantity", "42", None, None, "inventory_quantity", Decimal("42")),
        ("delivery_duration", "3 business days", "business_day", None, "delivery_duration", Decimal("3")),
        ("delivery_duration", "48小时", "hour", None, "delivery_duration", Decimal("48")),
        ("customs_duty_rate", "12%", "percent", None, "customs_duty_rate", Decimal("12")),
    ],
)
def test_supported_fact_normalization(fact_type, text, unit, currency, kind, value):
    parsed = normalize_ecommerce_value(text, fact_type=fact_type)

    assert parsed is not None
    assert parsed.kind == kind
    assert parsed.value == value
    assert parsed.unit == unit
    assert parsed.currency == currency


@pytest.mark.parametrize(
    ("fact_type", "text"),
    [
        ("price", "79.90"),
        ("inventory_quantity", "4.5"),
        ("delivery_duration", "3"),
        ("customs_duty_rate", "12"),
    ],
)
def test_normalization_never_guesses_unit_or_currency(fact_type, text):
    assert normalize_ecommerce_value(text, fact_type=fact_type) is None


def _context(content):
    return [{
        "source": "catalog-2026.txt",
        "content": content,
        "content_type": "text",
        "provenance_id": "product-1",
    }]


def _fact(fact_type="price", **overrides):
    payload = {
        "fact_type": fact_type,
        "value_text": "79.90",
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


def test_price_passes_same_sentence_binding_and_private_ledger():
    contexts = _context(
        "2026-07-15 Amazon美国市场轻量旅行背包 SKU-A100 的价格为 USD 79.90。"
    )
    ledger = build_citation_ledger(contexts)
    answer = {"answer_text": "SKU-A100价格为USD 79.90 [C1]。", "facts": [_fact()]}

    result = AnswerVerificationService().verify(
        "2026-07-15 Amazon美国市场SKU-A100轻量旅行背包价格是多少？",
        answer,
        citation_ledger=ledger,
    )

    assert result.passed is True
    assert result.verified_citation_ids == ["C1"]
    assert ledger["C1"].content == contexts[0]["content"]
    assert len(ledger["C1"].content_sha256) == 64


@pytest.mark.parametrize(
    ("question", "content", "fact"),
    [
        (
            "SKU-A100库存数量有多少？",
            "SKU-A100 库存数量为42。",
            _fact("inventory_quantity", value_text="42", currency=None, product=None, platform=None, market=None, date=None),
        ),
        (
            "SKU-A100配送时长多久？",
            "SKU-A100 配送时长为3 business days。",
            _fact("delivery_duration", value_text="3", unit="business_day", currency=None, product=None, platform=None, market=None, date=None),
        ),
        (
            "SKU-A100关税税率是多少？",
            "SKU-A100 关税税率为12%。",
            _fact("customs_duty_rate", value_text="12", unit="percent", currency=None, product=None, platform=None, market=None, date=None),
        ),
    ],
)
def test_all_supported_facts_verify(question, content, fact):
    answer = {"answer_text": f"结果为{fact['value_text']} [C1]。", "facts": [fact]}
    result = AnswerVerificationService().verify(question, answer, contexts=_context(content))
    assert result.passed is True


def test_evidence_preflight_requires_same_local_fact_binding():
    question = "SKU-A100价格是多少？"
    split = _context("SKU-A100的价格如下。\nUSD 79.90。")
    bound = _context("SKU-A100的价格为USD 79.90。")

    assert evidence_preflight(question, build_citation_ledger(split)).errors == ["no_fact_binding"]
    assert evidence_preflight(question, build_citation_ledger(bound)).passed is True


@pytest.mark.parametrize("question", ["SKU-A100重量是多少？", "SKU-A100尺寸是多少？", "SKU-A100功率是多少？"])
def test_unsupported_specifications_fail_closed(question):
    assert numeric_question_preflight(question).errors == ["unsupported_fact_type"]


def test_multi_fact_and_formula_fail_closed():
    assert numeric_question_preflight("SKU-A100价格和库存分别是多少？").errors == ["unsupported_multi_fact"]
    assert numeric_question_preflight("请把SKU-A100美元价格换算成人民币").errors == ["unsupported_complex_formula"]


def test_verifier_rejects_missing_currency_ambiguous_values_and_cross_fragment():
    service = AnswerVerificationService()
    question = "SKU-A100价格是多少？"

    missing_currency = service.verify(
        question,
        {"answer_text": "价格为79.90 [C1]。", "facts": [_fact(currency=None)]},
        contexts=_context("SKU-A100价格为79.90。"),
    )
    ambiguous = service.verify(
        question,
        {"answer_text": "价格为USD 79.90 [C1]。", "facts": [_fact(product=None, platform=None, market=None, date=None)]},
        contexts=_context("SKU-A100价格为USD 79.90，促销前价格为USD 89.90。"),
    )
    cross_fragment = service.verify(
        question,
        {"answer_text": "价格为USD 79.90 [C1]。", "facts": [_fact(product=None, platform=None, market=None, date=None)]},
        contexts=_context("SKU-A100价格如下。\nUSD 79.90。"),
    )

    assert missing_currency.passed is False
    assert ambiguous.passed is False
    assert cross_fragment.passed is False


def test_numeric_sku_and_date_are_not_extra_answer_numbers():
    answer = {
        "answer_text": "2026-07-15 的 SKU-A100 价格为 USD 79.90 [C1]。",
        "facts": [_fact()],
    }
    result = AnswerVerificationService().verify(
        "2026-07-15 SKU-A100价格是多少？",
        answer,
        contexts=_context("2026-07-15 SKU-A100价格为USD 79.90。"),
    )
    assert "answer_contains_uncited_numeric_value" not in result.errors
