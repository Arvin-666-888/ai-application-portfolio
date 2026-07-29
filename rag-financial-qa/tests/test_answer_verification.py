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
from app.utils.financial_normalization import normalize_financial_value, normalize_text


def _fact(**overrides):
    payload = {
        "metric": "营业收入",
        "value_text": "1,234.5",
        "unit": "万元",
        "currency": "CNY",
        "year": "2024",
        "company": "贵州茅台",
        "scope": "合并",
        "citation_ids": ["C1"],
    }
    payload.update(overrides)
    return payload


def _contexts(content="贵州茅台 2024年 合并 营业收入为人民币1,234.5万元。"):
    return [
        {
            "source": "贵州茅台_2024年报.pdf",
            "content": content,
            "page_number": 10,
            "content_type": "text",
            "provenance_id": "page-10-chunk-1",
        }
    ]


def test_normalization_uses_decimal_nfkc_scales_and_parentheses():
    normalized = normalize_financial_value("（１，２３４．５０）万元", currency="人民币")

    assert normalize_text("（１，２３４．５０）") == "(1,234.50)"
    assert normalized is not None
    assert normalized.value == Decimal("-1234.50")
    assert normalized.canonical_value == Decimal("-12345000.00")
    assert normalized.unit == "元"
    assert normalized.currency == "CNY"


@pytest.mark.parametrize(
    ("text", "kind", "canonical"),
    [
        ("12%", "percent", Decimal("12")),
        ("2.5百分点", "percentage_point", Decimal("2.5")),
        ("250bp", "percentage_point", Decimal("2.5")),
        ("250bps", "percentage_point", Decimal("2.5")),
    ],
)
def test_normalization_handles_percentage_units(text, kind, canonical):
    normalized = normalize_financial_value(text)

    assert normalized is not None
    assert normalized.kind == kind
    assert normalized.canonical_value == canonical


@pytest.mark.parametrize(
    ("text", "canonical", "currency"),
    [
        ("1元", Decimal("1"), None),
        ("1千元", Decimal("1000"), None),
        ("1万元", Decimal("10000"), None),
        ("1百万元", Decimal("1000000"), None),
        ("1亿元", Decimal("100000000"), None),
        ("USD 1百万元", Decimal("1000000"), "USD"),
        ("1亿港币", Decimal("1"), "HKD"),
    ],
)
def test_normalization_handles_scale_and_currency(text, canonical, currency):
    normalized = normalize_financial_value(text)

    assert normalized is not None
    assert normalized.canonical_value == canonical
    assert normalized.currency == currency


def test_normalization_does_not_guess_missing_unit():
    normalized = normalize_financial_value("1234.5")

    assert normalized is not None
    assert normalized.kind == "number"
    assert normalized.unit is None


def test_citation_ledger_keeps_private_full_content_identity_and_sha():
    contexts = _contexts("x" * 500)

    ledger = build_citation_ledger(contexts)

    assert ledger["C1"].content == "x" * 500
    assert ledger["C1"].identity == ("provenance", "page-10-chunk-1")
    assert len(ledger["C1"].content_sha256) == 64


def test_preflight_rejects_complex_formula_and_skips_non_numeric_question():
    assert numeric_question_preflight("请计算三年复合增长率").errors == ["unsupported_complex_formula"]
    assert numeric_question_preflight("公司有哪些风险？").status == "not_applicable"


def test_evidence_preflight_requires_bound_metric_value_year_and_company():
    question = "贵州茅台2024年合并营业收入是多少？"

    assert evidence_preflight(question, build_citation_ledger(_contexts())).passed is True
    assert evidence_preflight(
        question,
        build_citation_ledger(_contexts("贵州茅台2024年合并营业收入表现良好。")),
    ).errors == ["no_fact_binding"]
    assert evidence_preflight(
        question,
        build_citation_ledger(_contexts("贵州茅台2024年合并营业收入。\n人民币1,234.5万元。")),
    ).errors == ["no_fact_binding"]


def test_real_table_evidence_prefix_binds_header_scope_unit_and_value():
    content = (
        "[TableEvidence | source=海尔智家_2024年报.pdf | page=121 | table=t1]\n"
        "Statement: 2024年度合并利润表\n"
        "Scope: 合并\n"
        "Unit: 单位：元 币种：人民币\n"
        "Columns: c1=2024/合并/元; c2=2023/合并/元\n\n"
        "| 项目 | 2024年度 | 2023年度 |\n"
        "| --- | ---: | ---: |\n"
        "| 其中：营业收入 | 285,981,225,203.93 | 274,204,520,847.97 |"
    )
    contexts = [{
        "source": "海尔智家_2024年报.pdf",
        "content": content,
        "page_number": 121,
        "content_type": "table",
        "table_id": "t1",
    }]

    result = evidence_preflight(
        "海尔智家2024年合并利润表营业收入是多少？",
        build_citation_ledger(contexts),
    )

    assert result.passed is True


def test_table_preamble_without_bound_scope_or_unit_still_fails_closed():
    content = (
        "[Table | source=海尔智家_2024年报.pdf | page=121]\n\n"
        "| 项目 | 2024年度 | 2023年度 |\n"
        "| --- | ---: | ---: |\n"
        "| 营业收入 | 285,981,225,203.93 | 274,204,520,847.97 |"
    )
    contexts = [{
        "source": "海尔智家_2024年报.pdf",
        "content": content,
        "page_number": 121,
        "content_type": "table",
        "table_id": "t1",
    }]

    result = evidence_preflight(
        "海尔智家2024年合并利润表营业收入是多少？",
        build_citation_ledger(contexts),
    )

    assert result.errors == ["no_fact_binding"]


def test_multi_metric_question_is_explicitly_unsupported():
    question = "贵州茅台2024年合并营业收入和净利润分别是多少？"
    ledger = build_citation_ledger(_contexts())

    assert evidence_preflight(question, ledger).errors == ["unsupported_multi_metric"]
    result = AnswerVerificationService().verify(
        question,
        {"answer_text": "营业收入为1,234.5万元 [C1]。", "facts": [_fact()]},
        citation_ledger=ledger,
    )
    assert result.errors == ["unsupported_multi_metric"]


def test_structured_output_parser_accepts_json_fence_and_value_text_stays_string():
    parsed = parse_structured_output(
        '```json\n{"answer_text":"收入为1万元。","facts":[{"metric":"营业收入",'
        '"value_text":"1","unit":"万元","citation_ids":["C1"]}]}\n```'
    )

    assert isinstance(parsed, StructuredAnswer)
    assert parsed.facts[0].value_text == "1"


def test_verifier_passes_same_sentence_evidence():
    service = AnswerVerificationService()
    answer = {
        "answer_text": "贵州茅台2024年合并营业收入为1,234.5万元 [C1]。",
        "facts": [_fact()],
    }

    result = service.verify("贵州茅台2024年合并营业收入是多少？", answer, contexts=_contexts())

    assert result.passed is True
    assert result.verified_citation_ids == ["C1"]


def test_verifier_passes_same_table_row_evidence():
    content = "| 公司 | 年度 | 口径 | 指标 | 金额 |\n| 贵州茅台 | 2024 | 合并 | 营业收入 | CNY 1,234.5万元 |"
    result = AnswerVerificationService().verify(
        "贵州茅台2024年合并营业收入是多少？",
        {"answer_text": "1,234.5万元 [C1]", "facts": [_fact()]},
        contexts=_contexts(content),
    )

    assert result.passed is True


@pytest.mark.parametrize(
    ("fact_overrides", "error_fragment"),
    [
        ({"citation_ids": []}, "missing_citation"),
        ({"citation_ids": ["C99"]}, "unknown_citation"),
        ({"value_text": "9,999"}, "evidence_mismatch"),
        ({"unit": "亿元"}, "evidence_mismatch"),
        ({"currency": "USD"}, "evidence_mismatch"),
        ({"year": "2023"}, "year_mismatch"),
        ({"company": "美的集团"}, "company_mismatch"),
        ({"metric": "净利润"}, "metric_mismatch"),
        ({"scope": "母公司"}, "scope_mismatch"),
    ],
)
def test_verifier_fails_closed_on_fact_mismatch(fact_overrides, error_fragment):
    result = AnswerVerificationService().verify(
        "贵州茅台2024年合并营业收入是多少？",
        {"answer_text": "结果见引用 [C1]。", "facts": [_fact(**fact_overrides)]},
        contexts=_contexts(),
    )

    assert result.passed is False
    assert any(error_fragment in error for error in result.errors)


def test_verifier_rejects_known_but_unsupported_extra_citation():
    contexts = _contexts() + [
        {
            "source": "贵州茅台_2024年报.pdf",
            "content": "贵州茅台 2024年 合并 净利润为人民币500万元。",
            "page_number": 11,
            "content_type": "text",
            "provenance_id": "page-11-chunk-1",
        }
    ]
    fact = _fact(citation_ids=["C1", "C2"])
    result = AnswerVerificationService().verify(
        "贵州茅台2024年合并营业收入是多少？",
        {
            "answer_text": "营业收入为1,234.5万元 [C1][C2]。",
            "facts": [fact],
        },
        contexts=contexts,
    )

    assert result.passed is False
    assert "fact_1:unsupported_citation:C2" in result.errors


def test_verifier_rejects_answer_text_citation_mismatch():
    service = AnswerVerificationService()
    base = _fact()

    unknown = service.verify(
        "贵州茅台2024年合并营业收入是多少？",
        {"answer_text": "营业收入为1,234.5万元 [C99]。", "facts": [base]},
        contexts=_contexts(),
    )
    missing = service.verify(
        "贵州茅台2024年合并营业收入是多少？",
        {"answer_text": "营业收入为1,234.5万元。", "facts": [base]},
        contexts=_contexts(),
    )

    assert "answer_unknown_citation:C99" in unknown.errors
    assert "answer_fact_citation_mismatch" in unknown.errors
    assert "answer_missing_citation" in missing.errors


def test_verifier_rejects_cross_fragment_support():
    content = "贵州茅台2024年合并营业收入。\n人民币1,234.5万元。"

    result = AnswerVerificationService().verify(
        "贵州茅台2024年合并营业收入是多少？",
        {"answer_text": "结果见引用 [C1]。", "facts": [_fact()]},
        contexts=_contexts(content),
    )

    assert result.passed is False
    assert any("cross_fragment" in error for error in result.errors)


def test_verifier_rejects_ambiguous_same_segment_values():
    result = AnswerVerificationService().verify(
        "贵州茅台2024年合并营业收入是多少？",
        {"answer_text": "结果见引用 [C1]。", "facts": [_fact()]},
        contexts=_contexts(
            "贵州茅台 2024年 合并 营业收入为人民币1,234.5万元，调整前为人民币1,111万元。"
        ),
    )

    assert result.passed is False
    assert any("evidence_mismatch" in error for error in result.errors)


def test_verifier_binds_common_markdown_year_columns():
    content = (
        "| 指标 | 2024年 | 2023年 |\n"
        "|---|---:|---:|\n"
        "| 营业收入 | CNY 1,234.5万元 | CNY 1,111万元 |"
    )
    answer = {
        "answer_text": "2024年营业收入为1,234.5万元 [C1]。",
        "facts": [
            _fact(company=None, scope=None),
        ],
    }
    context = [{**_contexts()[0], "content": content, "content_type": "table"}]

    result = AnswerVerificationService().verify(
        "2024年营业收入是多少？", answer, contexts=context
    )

    assert result.passed is True


def test_verifier_rejects_missing_evidence_currency_when_fact_declares_currency():
    result = AnswerVerificationService().verify(
        "贵州茅台2024年合并营业收入是多少？",
        {"answer_text": "结果见引用 [C1]。", "facts": [_fact()]},
        contexts=_contexts("贵州茅台 2024年 合并 营业收入为1,234.5万元。"),
    )

    assert result.passed is False


def test_verifier_rejects_extra_answer_number():
    result = AnswerVerificationService().verify(
        "贵州茅台2024年合并营业收入是多少？",
        {
            "answer_text": "2024年营业收入1,234.5万元，同比增长12% [C1]。",
            "facts": [_fact()],
        },
        contexts=_contexts(),
    )

    assert result.passed is False
    assert "answer_contains_uncited_numeric_value" in result.errors


def test_verifier_rejects_amount_without_explicit_unit():
    result = AnswerVerificationService().verify(
        "贵州茅台2024年合并营业收入是多少？",
        {
            "answer_text": "结果见引用 [C1]。",
            "facts": [_fact(value_text="1234.5", unit=None)],
        },
        contexts=_contexts("贵州茅台 2024年 合并 营业收入为1234.5。"),
    )

    assert result.passed is False
