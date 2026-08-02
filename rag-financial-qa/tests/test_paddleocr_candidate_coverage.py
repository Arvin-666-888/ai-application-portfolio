from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "audit_paddleocr_candidate_coverage.py"
)
SPEC = importlib.util.spec_from_file_location("candidate_coverage_audit", SCRIPT)
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


def _page(source: str, number: int):
    return {"source": source, "page_number": number, "reasons": ["rule"]}


def _manifest(selected=(1,), dropped=(2,)):
    source = "a.pdf"
    return {
        "schema_version": "candidate-pages-v1",
        "selection_policy": {"uses_ground_truth": False},
        "report_count": 1,
        "selected_page_count": len(selected),
        "dropped_page_count": len(dropped),
        "reports": [{
            "source": source,
            "page_count": 3,
            "selected_count": len(selected),
            "dropped_count": len(dropped),
            "selected_pages": [_page(source, page) for page in selected],
            "dropped_pages": [_page(source, page) for page in dropped],
        }],
    }


def _case(page: int, question: str):
    return {
        "pdf": "a.pdf",
        "question": question,
        "metric": "营业收入",
        "expected_value": "100",
        "expected_page": page,
    }


def test_audit_aggregates_repeated_cases_and_three_buckets(monkeypatch):
    monkeypatch.setattr(audit, "EXPECTED_CASES", 4)
    monkeypatch.setattr(audit, "EXPECTED_UNIQUE_TARGETS", 3)
    cases = [_case(1, "q1"), _case(1, "q2"), _case(2, "q3"), _case(3, "q4")]

    result = audit.build_audit(
        cases,
        _manifest(),
        candidate_sha256="candidate",
        ground_truth_sha256="ground",
    )

    assert result["counts"] == {
        "ground_truth_cases": 4,
        "unique_target_pages": 3,
        "selected_target_pages": 1,
        "dropped_target_pages": 1,
        "missing_target_pages": 1,
        "selected_cases": 2,
        "dropped_cases": 1,
        "missing_cases": 1,
    }
    targets = {(item["page_number"], item["status"]): item for item in result["targets"]}
    assert targets[(1, "selected")]["case_count"] == 2
    assert result["status"] == "failed"


def test_selected_and_dropped_overlap_is_rejected():
    manifest = _manifest(selected=(1,), dropped=(1,))

    with pytest.raises(audit.AuditInputError, match="交叉"):
        audit.validate_candidate_manifest(manifest)


def test_manifest_rejects_declared_count_mismatch():
    manifest = _manifest()
    manifest["selected_page_count"] = 999

    with pytest.raises(audit.AuditInputError, match="selected_page_count"):
        audit.validate_candidate_manifest(manifest)


def test_manifest_rejects_invalid_page_and_unknown_ground_truth_pdf():
    manifest = _manifest(selected=(4,), dropped=())
    with pytest.raises(audit.AuditInputError, match="超出范围"):
        audit.validate_candidate_manifest(manifest)

    valid = _manifest()
    case = _case(1, "q")
    case["pdf"] = "unknown.pdf"
    with pytest.raises(audit.AuditInputError, match="不存在的报告"):
        audit.build_audit(
            [case],
            valid,
            candidate_sha256="candidate",
            ground_truth_sha256="ground",
        )


def test_ground_truth_rejects_bool_page():
    case = _case(1, "q")
    case["expected_page"] = True

    with pytest.raises(audit.AuditInputError, match="正整数"):
        audit.validate_ground_truth([case])
