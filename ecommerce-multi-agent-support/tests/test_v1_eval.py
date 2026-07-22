from pathlib import Path

from evals.run_eval import load_cases, run


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = PROJECT_ROOT / "evals" / "cases.jsonl"


def test_v1_eval_dataset_has_30_distinct_cases():
    cases = load_cases(CASES_PATH)
    assert len(cases) == 30
    assert len({case["id"] for case in cases}) == 30
    assert {case["expected_route"] for case in cases} == {
        "catalog",
        "order",
        "aftersales",
        "unsupported",
    }
    assert sum(bool(case.get("security_case")) for case in cases) >= 4


def test_v1_eval_suite_passes_all_cases(client):
    report = run(CASES_PATH)
    assert report["total"] == 30
    assert report["passed"] == 30
    assert report["route_accuracy"] == 1.0
    assert report["tool_accuracy"] == 1.0
    assert report["security_passed"] == report["security_total"]
