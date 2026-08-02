from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "paddleocr_target_page_smoke.py"
)
SPEC = importlib.util.spec_from_file_location(
    "paddleocr_target_page_smoke",
    SCRIPT_PATH,
)
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke
SPEC.loader.exec_module(smoke)


def _case(metric="其中：营业收入", value="189,163,654,064.64"):
    return smoke.SmokeCase(
        "格力电器",
        "格力电器_2024年年度报告.pdf",
        113,
        metric,
        value,
    )


def test_case_uses_pdf_physical_page_and_explicit_filename():
    case = smoke.CASES[-1]

    assert case.filename == "招商银行_2024年年度报告_A股.pdf"
    assert case.page_number == 138
    assert case.page_index == 137
    assert case.output_stem == "招商银行_p0138"


def test_evaluate_requires_true_table_res_list_same_table_match():
    payload = {
        "page_index": 0,
        "page_count": 1,
        "parsing_res_list": [
            {"block_label": "text", "block_content": "其中：营业收入 189,163,654,064.64"}
        ],
        "table_res_list": [
            {
                "pred_html": (
                    "<table><tr><td>其中：营业收入</td>"
                    "<td>189 163 654 064.64</td></tr></table>"
                )
            }
        ],
    }

    result = smoke.evaluate_table_payload(payload, _case())

    assert result["status"] == "passed"
    assert result["table_count"] == 1
    assert result["same_table_match"] is True
    assert result["page_mapping_ok"] is True


def test_page_text_is_not_treated_as_table_fallback():
    payload = {
        "page_index": 0,
        "page_count": 1,
        "parsing_res_list": [
            {"block_label": "text", "block_content": "其中：营业收入 189,163,654,064.64"}
        ],
        "table_res_list": [],
    }

    result = smoke.evaluate_table_payload(payload, _case())

    assert result["status"] == "failed"
    assert result["failure_reason"] == "table_not_detected"
    assert result["table_count"] == 0


def test_metric_and_value_in_different_tables_do_not_pass():
    payload = {
        "page_index": 0,
        "page_count": 1,
        "table_res_list": [
            {"pred_html": "<table><tr><td>其中：营业收入</td></tr></table>"},
            {"pred_html": "<table><tr><td>189,163,654,064.64</td></tr></table>"},
        ],
    }

    result = smoke.evaluate_table_payload(payload, _case())

    assert result["metric_found"] is True
    assert result["value_found"] is True
    assert result["same_table_match"] is False
    assert result["failure_reason"] == "metric_value_split"


def test_gate_passes_with_four_of_five_pages():
    cases = [{"status": "passed"} for _ in range(4)] + [{"status": "failed"}]

    summary = smoke.build_summary(cases, "gpu", 36.9)

    assert summary["status"] == "passed"
    assert summary["passed_pages"] == 4
    assert summary["failed_pages"] == 1
    assert summary["gate"]["required_passed_pages"] == 4
