from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load_script(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scan = _load_script("scan_pdf_pages", "01_scan_pdf_pages.py")
select = _load_script("select_table_pages", "02_select_table_pages.py")


def test_scan_uses_canonical_report_names_and_no_ocr_import():
    source = (SCRIPTS_DIR / "01_scan_pdf_pages.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert "招商银行_2024年年度报告_A股.pdf" in scan.EXPECTED_REPORTS
    assert not any(name.startswith(("paddle", "paddleocr")) for name in imported_modules)


def test_page_features_capture_only_small_selection_features():
    text = """合并利润表\n单位：元\n项目 2024年 2023年\n其中：营业收入 1,000 900\n净利润 200 180"""

    page = scan.page_features(text, "report.pdf", 12)

    assert page["source"] == "report.pdf"
    assert page["page_number"] == 12
    assert "合并利润表" in page["table_title_hits"]
    assert "营业收入" in page["metric_hits"]
    assert page["year_hits"] == ["2023", "2024"]
    assert "单位：元" in page["unit_hits"]
    assert "text" not in page
    assert isinstance(page["table_title_hits"], list)


def test_scan_delegates_features_to_shared_router_without_raw_text():
    probe = scan.router_page_features("合并利润表 2024年", "report.pdf", 1)
    serialized = scan.page_features("合并利润表 2024年", "report.pdf", 1)

    assert serialized["numeric_ratio"] == probe.numeric_ratio
    assert serialized["table_title_hits"] == list(probe.table_title_hits)
    assert "text" not in serialized


def _page(page_number: int, *, title=False, numeric_ratio=0.0, metric=False):
    return {
        "source": "report.pdf",
        "page_number": page_number,
        "text_chars": 100,
        "chinese_chars": 50,
        "digit_chars": round(numeric_ratio * 100),
        "numeric_ratio": numeric_ratio,
        "line_count": 20,
        "table_title_hits": ["合并利润表"] if title else [],
        "metric_hits": ["营业收入"] if metric else [],
        "year_hits": ["2023", "2024"] if metric else [],
        "period_hits": [],
        "unit_hits": [],
        "empty_text": False,
    }


def test_selector_uses_fixed_rules_and_title_neighbors():
    report = {
        "source": "report.pdf",
        "sha256": "abc",
        "page_count": 6,
        "pages": [
            _page(1),
            _page(2),
            _page(3, title=True),
            _page(4),
            _page(5),
            _page(6, numeric_ratio=0.25, metric=True),
        ],
    }

    result = select.select_report_pages(report, max_pages=80)
    selected = {item["page_number"]: item for item in result["selected_pages"]}

    assert set(selected) == {2, 3, 4, 5, 6}
    assert "financial_table_title" in selected[3]["reasons"]
    assert "title_neighbor" in selected[2]["reasons"]
    assert "numeric_financial_page" in selected[6]["reasons"]


def test_candidate_manifest_records_shared_router_policy_identity(monkeypatch):
    monkeypatch.setattr(select, "EXPECTED_REPORTS", ("report.pdf",))
    inventory = {
        "schema_version": "page-inventory-v1",
        "reports": [{
            "source": "report.pdf",
            "sha256": "a" * 64,
            "page_count": 1,
            "pages": [_page(1, title=True)],
        }],
    }

    manifest = select.build_candidate_manifest(inventory, 80)

    assert manifest["selection_policy"]["router_policy_version"] == select.PDF_ROUTING_POLICY_VERSION
    assert manifest["selection_policy"]["router_policy_fingerprint"] == select.POLICY_FINGERPRINT


def test_selector_cap_prioritizes_title_then_numeric_ratio():
    report = {
        "source": "report.pdf",
        "sha256": "abc",
        "page_count": 4,
        "pages": [
            _page(1, numeric_ratio=0.30, metric=True),
            _page(2, title=True, numeric_ratio=0.10),
            _page(3, numeric_ratio=0.40, metric=True),
            _page(4, numeric_ratio=0.20, metric=True),
        ],
    }

    result = select.select_report_pages(report, max_pages=2)

    assert [item["page_number"] for item in result["selected_pages"]] == [2, 3]
    assert result["dropped_count"] == 2


def test_candidate_selector_source_does_not_reference_ground_truth():
    source = (SCRIPTS_DIR / "02_select_table_pages.py").read_text(encoding="utf-8")

    assert "table_ground_truth" not in source
    assert "expected_page" not in source
    assert "expected_value" not in source
