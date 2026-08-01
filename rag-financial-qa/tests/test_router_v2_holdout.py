from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "evals"
    / "router_v2_holdout"
    / "validate_freeze.py"
)
SPEC = importlib.util.spec_from_file_location("router_v2_holdout_validate", SCRIPT)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validator
SPEC.loader.exec_module(validator)


def _write_valid_fixture(root: Path) -> None:
    (root / "private").mkdir(parents=True)
    (root / "pdfs").mkdir()
    (root / "query_only.jsonl").write_text(
        '{"case_id":"holdout_00","question":"问题？"}\n', encoding="utf-8"
    )
    (root / "preregistration.json").write_text(json.dumps({
        "status": "frozen_before_ground_truth",
        "case_count": 1,
        "report_count": 1,
        "subsets": {"new_company": ["holdout_00"]},
    }), encoding="utf-8")
    (root / "source_manifest.json").write_text(json.dumps([{
        "subset": "new_company",
        "company": "测试公司",
        "stock_code": "000001",
        "report_year": 2024,
        "filename": "report.pdf",
        "pdf_url": "https://static.cninfo.com.cn/finalpage/report.PDF",
        "identity_status": "verified",
        "page_count": 1,
        "size_bytes": 3,
        "sha256": "a" * 64,
    }]), encoding="utf-8")


def test_freeze_validation_passes_without_loading_ground_truth(tmp_path):
    _write_valid_fixture(tmp_path)

    summary = validator.validate(tmp_path)

    assert summary["status"] == "passed"
    assert summary["ground_truth_loaded"] is False
    assert summary["case_count"] == 1
    assert summary["verified_report_count"] == 0


def test_phase4_freeze_accepts_new_prefix_without_subsets(tmp_path):
    _write_valid_fixture(tmp_path)
    (tmp_path / "query_only.jsonl").write_text(
        '{"case_id":"phase4_00","question":"问题？"}\n', encoding="utf-8"
    )
    (tmp_path / "preregistration.json").write_text(json.dumps({
        "status": "frozen_before_candidate_generation_and_ground_truth",
        "case_id_prefix": "phase4",
        "case_count": 1,
        "report_count": 1,
    }), encoding="utf-8")

    summary = validator.validate(tmp_path)

    assert summary["status"] == "passed"
    assert summary["case_count"] == 1


@pytest.mark.parametrize("field", [
    "expected_value",
    "expected_page",
    "expected_source",
    "expected_unit",
    "expected_year",
    "expected_company",
    "expected_scope",
    "should_refuse",
    "evidence_excerpt",
    "review_notes",
    "refusal_reason",
])
def test_query_only_rejects_label_fields(tmp_path, field):
    _write_valid_fixture(tmp_path)
    (tmp_path / "query_only.jsonl").write_text(
        json.dumps({"case_id": "holdout_00", "question": "问题？", field: "leak"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(validator.HoldoutValidationError, match="标签字段"):
        validator.validate(tmp_path)


@pytest.mark.parametrize(
    ("company", "report_year", "filename", "error"),
    [
        ("旧公司股份有限公司", 2025, "新公司_2025年年度报告.pdf", "excluded company"),
        ("新公司股份有限公司", 2024, "新公司_2024年年度报告.pdf", "excluded report year"),
    ],
)
def test_phase4_freeze_enforces_company_and_year_isolation(
    tmp_path, company, report_year, filename, error
):
    _write_valid_fixture(tmp_path)
    (tmp_path / "query_only.jsonl").write_text(
        json.dumps({"case_id": "phase4_00", "question": f"新公司{report_year}年营业收入是多少？"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "preregistration.json").write_text(json.dumps({
        "status": "frozen_before_candidate_generation_and_ground_truth",
        "case_id_prefix": "phase4",
        "case_count": 1,
        "report_count": 1,
        "data_isolation": {
            "excluded_company_names": ["旧公司"],
            "excluded_report_years": [2024],
            "selected_report_year": 2025,
            "selected_reports_use_only_new_companies_and_new_year": True,
        },
    }), encoding="utf-8")
    (tmp_path / "source_manifest.json").write_text(json.dumps([{
        "subset": "new_company_new_year",
        "company": company,
        "stock_code": "000001",
        "report_year": report_year,
        "filename": filename,
        "pdf_url": "https://static.cninfo.com.cn/finalpage/report.PDF",
        "page_count": 1,
        "identity_status": "pending_download_verification",
    }]), encoding="utf-8")

    with pytest.raises(validator.HoldoutValidationError, match=error):
        validator.validate(tmp_path)


@pytest.mark.parametrize(
    "relative_path",
    [
        "private/ground_truth.json",
        "private/ground_truth_attestation.json",
        "ground_truth_unsealed.json",
    ],
)
def test_freeze_validation_rejects_any_unseal_artifact(tmp_path, relative_path):
    _write_valid_fixture(tmp_path)
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(validator.HoldoutValidationError, match="pre-GT artifact"):
        validator.validate(tmp_path)


def test_require_pdfs_rejects_missing_download(tmp_path):
    _write_valid_fixture(tmp_path)

    with pytest.raises(validator.HoldoutValidationError, match="PDF 不存在"):
        validator.validate(tmp_path, require_pdfs=True)
