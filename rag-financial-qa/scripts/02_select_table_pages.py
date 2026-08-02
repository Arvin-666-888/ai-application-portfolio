from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.utils.pdf_parse_router import (  # noqa: E402
    PDF_ROUTING_POLICY_VERSION,
    POLICY_FINGERPRINT,
    ROUTING_POLICY,
    classify,
    page_features as router_page_features,
    select,
)
from scripts.atomic_json import write_json_atomic  # noqa: E402

DEFAULT_INVENTORY = (
    PROJECT_ROOT
    / "evals"
    / "task2_paddleocr"
    / "manifest"
    / "page_inventory.json"
)
DEFAULT_OUTPUT = DEFAULT_INVENTORY.with_name("candidate_pages.json")
DEFAULT_MAX_PAGES = 80
EXPECTED_REPORTS = (
    "格力电器_2024年年度报告.pdf",
    "美的集团_2024年年度报告.pdf",
    "贵州茅台_2024年年度报告.pdf",
    "比亚迪_2024年年度报告.pdf",
    "招商银行_2024年年度报告_A股.pdf",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="用固定通用规则从内嵌文本扫描结果选择表格候选页。",
    )
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-pages-per-report", type=int, default=DEFAULT_MAX_PAGES)
    return parser.parse_args()


def load_inventory(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"页面扫描清单不存在: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "page-inventory-v1":
        raise ValueError("页面扫描清单 schema_version 不受支持")
    if payload.get("scan_method") != "pdfplumber-embedded-text-only":
        raise ValueError("候选页只能来自内嵌文本轻扫描清单")
    if payload.get("ocr_enabled") is not False:
        raise ValueError("候选页清单的上游不应启用 OCR")
    reports = payload.get("reports")
    if not isinstance(reports, list):
        raise ValueError("页面扫描清单 reports 不是数组")
    names = [report.get("source") for report in reports if isinstance(report, dict)]
    missing = [name for name in EXPECTED_REPORTS if name not in names]
    if missing:
        raise ValueError("页面扫描清单缺少报告: " + ", ".join(missing))
    return payload


def classify_page(page: dict[str, Any]) -> list[str]:
    return list(classify(page))


def _inventory_probe(page: dict[str, Any]):
    """Rehydrate a text-free inventory row as a shared router probe."""
    probe = router_page_features("", str(page["source"]), int(page["page_number"]))
    values = {
        field: page.get(field, getattr(probe, field))
        for field in probe.__dataclass_fields__
        if field != "text"
    }
    values["low_text"] = int(values["text_chars"]) < int(
        ROUTING_POLICY["low_text_min_chars"]
    )
    return type(probe)(text="", **values)


def select_report_pages(report: dict[str, Any], max_pages: int) -> dict[str, Any]:
    pages = report.get("pages")
    if not isinstance(pages, list) or not pages:
        raise ValueError(f"报告没有页面特征: {report.get('source', '')}")

    page_rows = {
        int(page["page_number"]): page
        for page in pages
        if isinstance(page, dict) and page.get("page_number") is not None
    }
    probes = [_inventory_probe(page_rows[number]) for number in sorted(page_rows)]
    routes = select(
        probes,
        max_pages=max_pages,
        policy=ROUTING_POLICY,
        policy_fingerprint=POLICY_FINGERPRINT,
        pdf_sha256=str(report["sha256"]),
    )

    def serialize(route) -> dict[str, Any]:
        page = page_rows[route.page_number]
        return {
            "source": report["source"],
            "pdf_sha256": report["sha256"],
            "page_number": route.page_number,
            "reasons": list(route.reasons),
            "numeric_ratio": page.get("numeric_ratio", 0.0),
            "line_count": page.get("line_count", 0),
            "table_title_hits": page.get("table_title_hits", []),
            "metric_hits": page.get("metric_hits", []),
            "year_hits": page.get("year_hits", []),
            "period_hits": page.get("period_hits", []),
            "unit_hits": page.get("unit_hits", []),
        }

    selected = [serialize(route) for route in routes if route.selected]
    dropped = [serialize(route) for route in routes if route.dropped_by_cap]
    return {
        "source": report["source"],
        "pdf_sha256": report["sha256"],
        "page_count": report["page_count"],
        "candidate_count_before_cap": len(selected) + len(dropped),
        "selected_count": len(selected),
        "dropped_count": len(dropped),
        "selected_pages": selected,
        "dropped_pages": dropped,
    }


def build_candidate_manifest(inventory: dict[str, Any], max_pages: int) -> dict[str, Any]:
    if max_pages < 1:
        raise ValueError("max_pages_per_report 必须大于 0")
    reports_by_name = {report["source"]: report for report in inventory["reports"]}
    reports = [select_report_pages(reports_by_name[name], max_pages) for name in EXPECTED_REPORTS]
    selected_count = sum(report["selected_count"] for report in reports)
    dropped_count = sum(report["dropped_count"] for report in reports)
    return {
        "schema_version": "candidate-pages-v1",
        "source_inventory_schema": inventory["schema_version"],
        "selection_policy": {
            "uses_ground_truth": False,
            "router_policy_version": PDF_ROUTING_POLICY_VERSION,
            "router_policy_fingerprint": POLICY_FINGERPRINT,
            "title_neighbor_range": dict(ROUTING_POLICY["title_neighbor_range"]),
            "numeric_ratio_min": ROUTING_POLICY["numeric_ratio_min"],
            "line_count_min": ROUTING_POLICY["line_count_min"],
            "requires_metric_term": ROUTING_POLICY["requires_metric_term"],
            "requires_explicit_date_or_context": ROUTING_POLICY[
                "requires_explicit_date_or_context"
            ],
            "max_pages_per_report": max_pages,
            "cap_priority": "title_hit, numeric_ratio_desc, page_number_asc",
        },
        "report_count": len(reports),
        "selected_page_count": selected_count,
        "dropped_page_count": dropped_count,
        "reports": reports,
    }


def write_summary(manifest: dict[str, Any], output: Path) -> Path:
    summary_path = output.with_name("candidate_pages_summary.json")
    summary = {
        "status": "passed" if manifest["selected_page_count"] > 0 else "failed",
        "schema_version": manifest["schema_version"],
        "selection_policy": manifest["selection_policy"],
        "report_count": manifest["report_count"],
        "selected_page_count": manifest["selected_page_count"],
        "dropped_page_count": manifest["dropped_page_count"],
        "reports": [
            {
                key: report[key]
                for key in (
                    "source",
                    "page_count",
                    "candidate_count_before_cap",
                    "selected_count",
                    "dropped_count",
                )
            }
            for report in manifest["reports"]
        ],
        "candidate_manifest": str(output.resolve()),
        "next_step": "先审查候选页数量和预计耗时；不要自动启动批量 OCR。",
    }
    write_json_atomic(summary_path, summary)
    return summary_path


def main() -> int:
    args = parse_args()
    try:
        inventory = load_inventory(args.inventory.resolve())
        manifest = build_candidate_manifest(inventory, args.max_pages_per_report)
        output = args.output.resolve()
        write_json_atomic(output, manifest)
        summary_path = write_summary(manifest, output)
    except Exception as exc:
        print(f"[FAILED] {exc}", file=sys.stderr)
        return 1

    print(
        f"[DONE] candidates={manifest['selected_page_count']} "
        f"dropped={manifest['dropped_page_count']} summary={summary_path}"
    )
    return 0 if manifest["selected_page_count"] > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
