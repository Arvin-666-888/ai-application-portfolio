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


def test_query_only_rejects_label_fields(tmp_path):
    _write_valid_fixture(tmp_path)
    (tmp_path / "query_only.jsonl").write_text(
        '{"case_id":"holdout_00","question":"问题？","expected_value":"100"}\n',
        encoding="utf-8",
    )

    with pytest.raises(validator.HoldoutValidationError, match="标签字段"):
        validator.validate(tmp_path)


def test_freeze_validation_rejects_ground_truth_before_unseal(tmp_path):
    _write_valid_fixture(tmp_path)
    (tmp_path / "private" / "ground_truth.json").write_text("[]", encoding="utf-8")

    with pytest.raises(validator.HoldoutValidationError, match="已存在"):
        validator.validate(tmp_path)


def test_require_pdfs_rejects_missing_download(tmp_path):
    _write_valid_fixture(tmp_path)

    with pytest.raises(validator.HoldoutValidationError, match="PDF 不存在"):
        validator.validate(tmp_path, require_pdfs=True)
