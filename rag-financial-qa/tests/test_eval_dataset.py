import json
import os
from collections import Counter
from pathlib import Path

os.environ["DEBUG"] = "false"

from evals.run_eval import load_cases


QUESTIONS_PATH = Path(__file__).resolve().parents[1] / "evals" / "questions.jsonl"


def test_eval_dataset_has_required_metadata_and_unique_ids():
    cases = load_cases(str(QUESTIONS_PATH))
    ids = [case["id"] for case in cases]

    assert len(cases) >= 24
    assert len(ids) == len(set(ids))
    for case in cases:
        assert case.get("category")
        assert case.get("difficulty") in {"easy", "medium", "hard"}
        assert case.get("answer_type")
        assert isinstance(case.get("should_refuse"), bool)


def test_eval_dataset_balances_answerable_and_refusal_cases():
    cases = load_cases(str(QUESTIONS_PATH))
    answerable = [case for case in cases if not case["should_refuse"]]
    refusal = [case for case in cases if case["should_refuse"]]
    categories = Counter(case["category"] for case in cases)

    assert len(answerable) >= 16
    assert len(refusal) >= 8
    assert categories["financial_guardrail"] >= 3
    assert categories["out_of_corpus"] >= 5
    assert categories["cross_document_reasoning"] >= 1


def test_answerable_cases_have_sources_and_keywords():
    cases = load_cases(str(QUESTIONS_PATH))

    for case in cases:
        if case["should_refuse"]:
            continue
        assert case["expected_sources"]
        assert case["expected_keywords"]
        assert case["expected_context_keywords"]


def test_jsonl_is_plain_json_per_line():
    for line in QUESTIONS_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            assert isinstance(json.loads(line), dict)
