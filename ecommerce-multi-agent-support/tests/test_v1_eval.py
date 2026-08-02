from pathlib import Path

from evals.run_eval import load_cases, run


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = PROJECT_ROOT / "evals" / "cases.jsonl"


def test_v1_eval_dataset_has_distinct_migrated_cases():
    cases = load_cases(CASES_PATH)
    assert len(cases) >= 30
    assert len({case["id"] for case in cases}) == len(cases)
    assert {case["expected_route"] for case in cases} == {
        "product_inquiry",
        "order_query",
        "logistics_tracking",
        "aftersales_handling",
        "unsupported",
    }
    assert sum(bool(case.get("security_case")) for case in cases) >= 4
    assert {case.get("expected_currency") for case in cases} >= {"USD", "EUR", "GBP"}
    assert sum(bool(case.get("proposal_only")) for case in cases) >= 2
    assert {
        text
        for case in cases
        for text in case.get("answer_contains_all", [])
        if text in {"America/Los_Angeles", "Europe/Berlin", "Europe/London"}
    } == {"America/Los_Angeles", "Europe/Berlin", "Europe/London"}


def test_v1_eval_suite_passes_all_cases(client):
    report = run(CASES_PATH)
    assert report["total"] >= 30
    assert report["passed"] == report["total"]
    assert report["route_accuracy"] == 1.0
    assert report["tool_accuracy"] == 1.0
    assert report["security_passed"] == report["security_total"]
