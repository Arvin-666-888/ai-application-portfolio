import json
import os
from collections import Counter
from pathlib import Path

os.environ["DEBUG"] = "false"

from app.services.answer_verification_service import (
    build_citation_ledger,
    evidence_preflight,
    verify_structured_answer,
)
from evals.run_eval import has_retrieval_hit, has_source_support, load_cases, validate_dataset


QUESTIONS_PATH = Path(__file__).resolve().parents[1] / "evals" / "questions.jsonl"
FIXTURE_DIR = QUESTIONS_PATH.parent / "fixtures"


FACT_OUTPUTS = {
    "price": {"value_text": "79.90", "currency": "USD", "unit": None},
    "inventory_quantity": {"value_text": "42", "currency": None, "unit": None},
    "delivery_duration": {"value_text": "48", "currency": None, "unit": "hour"},
    "customs_duty_rate": {"value_text": "6", "currency": None, "unit": "percent"},
}


def test_eval_dataset_has_required_metadata_and_unique_ids():
    cases = load_cases(str(QUESTIONS_PATH))
    ids = [case["id"] for case in cases]

    assert len(cases) >= 10
    assert len(ids) == len(set(ids))
    for case in cases:
        assert case.get("category")
        assert case.get("difficulty") in {"easy", "medium", "hard"}
        assert case.get("answer_type")
        assert isinstance(case.get("should_refuse"), bool)


def test_eval_dataset_covers_four_facts_and_refusal_boundaries():
    cases = load_cases(str(QUESTIONS_PATH))
    answerable = [case for case in cases if not case["should_refuse"]]
    refusal = [case for case in cases if case["should_refuse"]]
    categories = Counter(case["category"] for case in cases)

    assert len(answerable) == 4
    assert len(refusal) >= 6
    assert {case["expected_fact_type"] for case in answerable} == {
        "price", "inventory_quantity", "delivery_duration", "customs_duty_rate",
    }
    assert {
        "out_of_corpus_sku", "unsupported_fact", "multi_fact_guardrail",
        "complex_formula_guardrail", "insufficient_evidence",
    } <= set(categories)
    assert {"medium", "hard"} <= {case["difficulty"] for case in cases}
    assert validate_dataset(cases) == []


def test_answerable_cases_have_sources_and_keywords():
    cases = load_cases(str(QUESTIONS_PATH))

    for case in cases:
        if case["should_refuse"]:
            continue
        assert case["expected_sources"]
        assert case["expected_keywords"]
        assert case["expected_context_keywords"]


def test_authoritative_fixtures_pass_evidence_and_structured_contracts():
    cases = [case for case in load_cases(str(QUESTIONS_PATH)) if not case["should_refuse"]]
    for case in cases:
        source = case["expected_sources"][0]
        content = (FIXTURE_DIR / source).read_text(encoding="utf-8")
        contexts = [{
            "source": source,
            "content": content,
            "content_type": "text",
            "provenance_id": case["id"],
        }]
        ledger = build_citation_ledger(contexts)
        assert evidence_preflight(case["question"], ledger).passed, case["id"]

        contract = FACT_OUTPUTS[case["expected_fact_type"]]
        fact = {
            "fact_type": case["expected_fact_type"],
            "value_text": contract["value_text"],
            "unit": contract["unit"],
            "currency": contract["currency"],
            "sku": case["expected_sku"],
            "product": {
                "SKU-A100": "轻量旅行背包",
                "SKU-B200": "折叠收纳箱",
                "SKU-C300": "桌面收纳架",
            }[case["expected_sku"]],
            "platform": {"SKU-A100": "Amazon", "SKU-B200": "京东", "SKU-C300": "Shopee"}[case["expected_sku"]],
            "market": {"SKU-A100": "美国", "SKU-B200": "中国", "SKU-C300": "新加坡"}[case["expected_sku"]],
            "date": {"SKU-A100": "2026-07-15", "SKU-B200": "2026-07-16", "SKU-C300": "2026-07-17"}[case["expected_sku"]],
            "citation_ids": ["C1"],
        }
        output = {"answer_text": f"{case['expected_sku']} {case['expected_value']} [C1]", "facts": [fact]}
        verification = verify_structured_answer(case["question"], output, ledger)
        assert verification.passed, (case["id"], verification.errors)
        assert verification.verified_citation_ids == ["C1"]


def test_retrieval_and_source_support_require_source_and_all_context_terms():
    case = next(case for case in load_cases(str(QUESTIONS_PATH)) if case["id"] == "sku_a100_price")
    partial = [{"source": "ecommerce_product_manual.txt", "content": "SKU-A100 价格 USD 79.90"}]
    wrong_source = [{"source": "other.txt", "content": "SKU-A100 轻量旅行背包 Amazon 美国 2026-07-15 价格 USD 79.90"}]
    complete = [{"source": "ecommerce_product_manual.txt", "content": "SKU-A100 轻量旅行背包 Amazon 美国 2026-07-15 价格 USD 79.90"}]

    assert not has_retrieval_hit(partial, case)
    assert not has_retrieval_hit(wrong_source, case)
    assert has_retrieval_hit(complete, case)
    assert not has_source_support(complete, [{"document": "other.txt"}], case)
    assert has_source_support(complete, [{"document": "ecommerce_product_manual.txt"}], case)


def test_jsonl_is_plain_json_per_line():
    for line in QUESTIONS_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            assert isinstance(json.loads(line), dict)
