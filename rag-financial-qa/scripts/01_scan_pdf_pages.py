from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.utils.pdf_parse_router import page_features as router_page_features  # noqa: E402
from scripts.atomic_json import write_json_atomic  # noqa: E402

DEFAULT_PDF_DIR = PROJECT_ROOT / "evals" / "task2_chinese_financial_reports"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "evals"
    / "task2_paddleocr"
    / "manifest"
    / "page_inventory.json"
)
EXPECTED_REPORTS = (
    "格力电器_2024年年度报告.pdf",
    "美的集团_2024年年度报告.pdf",
    "贵州茅台_2024年年度报告.pdf",
    "比亚迪_2024年年度报告.pdf",
    "招商银行_2024年年度报告_A股.pdf",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="扫描 5 份中文年报的内嵌文本；不加载 PaddleOCR、不渲染页面。",
    )
    parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def page_features(text: str, source: str, page_number: int) -> dict[str, Any]:
    """Serialize the shared router probe without retaining its raw page text."""
    probe = asdict(router_page_features(text, source, page_number))
    probe.pop("text", None)
    # Keep the historical inventory schema stable while using router semantics.
    probe.pop("low_text", None)
    probe.pop("extraction_error", None)
    for name in (
        "table_title_hits",
        "metric_hits",
        "year_hits",
        "period_hits",
        "unit_hits",
    ):
        probe[name] = list(probe[name])
    return probe


def validate_reports(pdf_dir: Path) -> list[Path]:
    if not pdf_dir.is_dir():
        raise FileNotFoundError(f"PDF 目录不存在: {pdf_dir}")
    missing = [name for name in EXPECTED_REPORTS if not (pdf_dir / name).is_file()]
    if missing:
        raise FileNotFoundError("缺少规范命名的中文年报: " + ", ".join(missing))
    return [pdf_dir / name for name in EXPECTED_REPORTS]


def scan_report(path: Path) -> dict[str, Any]:
    import pdfplumber

    pages = []
    errors = []
    started = time.perf_counter()
    with pdfplumber.open(path) as document:
        page_count = len(document.pages)
        for page_number, page in enumerate(document.pages, 1):
            try:
                text = page.extract_text() or ""
                pages.append(page_features(text, path.name, page_number))
            except Exception as exc:
                errors.append({
                    "page_number": page_number,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:300],
                })
                pages.append(page_features("", path.name, page_number))
            if page_number == page_count or page_number % 50 == 0:
                print(f"[SCAN] {path.name}: {page_number}/{page_count} pages")

    return {
        "source": path.name,
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "page_count": len(pages),
        "scanned_pages": len(pages),
        "empty_text_pages": sum(page["empty_text"] for page in pages),
        "error_count": len(errors),
        "errors": errors,
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "pages": pages,
    }


def build_inventory(paths: list[Path]) -> dict[str, Any]:
    started = time.perf_counter()
    reports = [scan_report(path) for path in paths]
    total_pages = sum(report["page_count"] for report in reports)
    error_count = sum(report["error_count"] for report in reports)
    return {
        "schema_version": "page-inventory-v1",
        "scan_method": "pdfplumber-embedded-text-only",
        "ocr_enabled": False,
        "render_pages": False,
        "report_count": len(reports),
        "total_pages": total_pages,
        "error_count": error_count,
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "reports": reports,
    }


def write_summary(inventory: dict[str, Any], output: Path) -> Path:
    summary_path = output.with_name("page_inventory_summary.json")
    summary = {
        "status": "passed" if inventory["error_count"] == 0 else "partial",
        "schema_version": inventory["schema_version"],
        "scan_method": inventory["scan_method"],
        "ocr_enabled": inventory["ocr_enabled"],
        "report_count": inventory["report_count"],
        "total_pages": inventory["total_pages"],
        "error_count": inventory["error_count"],
        "elapsed_seconds": inventory["elapsed_seconds"],
        "reports": [
            {
                key: report[key]
                for key in (
                    "source",
                    "sha256",
                    "size_bytes",
                    "page_count",
                    "empty_text_pages",
                    "error_count",
                    "elapsed_seconds",
                )
            }
            for report in inventory["reports"]
        ],
        "inventory_file": str(output.resolve()),
        "next_step": "运行 02_select_table_pages.py；不要直接启动批量 OCR。",
    }
    write_json_atomic(summary_path, summary)
    return summary_path


def main() -> int:
    args = parse_args()
    try:
        paths = validate_reports(args.pdf_dir.resolve())
        inventory = build_inventory(paths)
        output = args.output.resolve()
        write_json_atomic(output, inventory)
        summary_path = write_summary(inventory, output)
    except Exception as exc:
        print(f"[FAILED] {exc}", file=sys.stderr)
        return 1

    print(
        f"[DONE] reports={inventory['report_count']} pages={inventory['total_pages']} "
        f"errors={inventory['error_count']} summary={summary_path}"
    )
    return 0 if inventory["error_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
