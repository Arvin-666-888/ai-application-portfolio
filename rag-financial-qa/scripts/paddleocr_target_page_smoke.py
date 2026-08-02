from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = PROJECT_ROOT / "evals" / "task2_chinese_financial_reports"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "evals" / "task2_paddleocr"
PASS_THRESHOLD = 4


@dataclass(frozen=True)
class SmokeCase:
    company: str
    filename: str
    page_number: int
    metric: str
    expected_value: str

    @property
    def page_index(self) -> int:
        return self.page_number - 1

    @property
    def output_stem(self) -> str:
        return f"{self.company}_p{self.page_number:04d}"


CASES = (
    SmokeCase(
        "格力电器",
        "格力电器_2024年年度报告.pdf",
        113,
        "其中：营业收入",
        "189,163,654,064.64",
    ),
    SmokeCase(
        "美的集团",
        "美的集团_2024年年度报告.pdf",
        158,
        "其中：营业收入",
        "407,149,600",
    ),
    SmokeCase(
        "贵州茅台",
        "贵州茅台_2024年年度报告.pdf",
        63,
        "其中：营业收入",
        "170,899,152,276.34",
    ),
    SmokeCase(
        "比亚迪",
        "比亚迪_2024年年度报告.pdf",
        145,
        "营业收入",
        "777,102,455",
    ),
    SmokeCase(
        "招商银行",
        "招商银行_2024年年度报告_A股.pdf",
        138,
        "营业收入合计",
        "337,488",
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="用 PP-StructureV3 验证 5 个中文财务表格目标页；不会运行全页扫描。",
    )
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--device", default="gpu")
    return parser.parse_args()


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"[\s,，]", "", text)
    return text.replace("％", "%")


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _iter_strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_strings(child)


def unwrap_result_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise TypeError(f"PP-StructureV3 JSON 结果不是对象: {type(payload).__name__}")
    if isinstance(payload.get("res"), dict):
        payload = payload["res"]
    if not isinstance(payload, dict):
        raise TypeError("PP-StructureV3 JSON res 字段不是对象")
    return payload


def extract_true_table_texts(payload: dict[str, Any]) -> list[str]:
    tables = payload.get("table_res_list", [])
    if not isinstance(tables, list):
        raise TypeError("table_res_list 不是数组")
    return ["\n".join(_iter_strings(table)) for table in tables if isinstance(table, dict)]


def evaluate_table_payload(payload: dict[str, Any], case: SmokeCase) -> dict[str, Any]:
    tables = extract_true_table_texts(payload)
    metric = normalize_text(case.metric)
    expected_value = normalize_text(case.expected_value)
    table_checks = []

    for table_index, table_text in enumerate(tables):
        normalized = normalize_text(table_text)
        metric_found = metric in normalized
        value_found = expected_value in normalized
        table_checks.append({
            "table_index": table_index,
            "metric_found": metric_found,
            "value_found": value_found,
            "same_table_match": metric_found and value_found,
        })

    metric_found = any(item["metric_found"] for item in table_checks)
    value_found = any(item["value_found"] for item in table_checks)
    same_table_match = any(item["same_table_match"] for item in table_checks)
    page_mapping_ok = payload.get("page_index") == 0 and payload.get("page_count") == 1

    if not tables:
        failure_reason = "table_not_detected"
    elif not page_mapping_ok:
        failure_reason = "page_mapping_error"
    elif not metric_found:
        failure_reason = "metric_missing"
    elif not value_found:
        failure_reason = "value_missing"
    elif not same_table_match:
        failure_reason = "metric_value_split"
    else:
        failure_reason = None

    return {
        "source": case.filename,
        "page_number": case.page_number,
        "table_count": len(tables),
        "metric_found": metric_found,
        "value_found": value_found,
        "same_table_match": same_table_match,
        "page_mapping_ok": page_mapping_ok,
        "status": "passed" if failure_reason is None else "failed",
        "failure_reason": failure_reason,
        "table_checks": table_checks,
    }


def result_json_payload(result: Any) -> dict[str, Any]:
    payload = getattr(result, "json", None)
    if callable(payload):
        payload = payload()
    if payload is None:
        raise TypeError("PP-StructureV3 结果没有 json 属性")
    return unwrap_result_payload(payload)


def result_markdown_text(result: Any) -> str:
    markdown = getattr(result, "markdown", "")
    if callable(markdown):
        markdown = markdown()
    if isinstance(markdown, str):
        return markdown
    if isinstance(markdown, dict):
        for key in ("markdown_texts", "content", "text"):
            value = markdown.get(key)
            if isinstance(value, str):
                return value
    return ""


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def extract_page_as_pdf(source: Path, page_index: int, destination: Path) -> None:
    import fitz

    with fitz.open(source) as document:
        if not 0 <= page_index < len(document):
            raise ValueError(f"页索引 {page_index} 超出范围，PDF 共 {len(document)} 页")
        output = fitz.open()
        try:
            output.insert_pdf(document, from_page=page_index, to_page=page_index)
            output.save(destination)
        finally:
            output.close()


def parse_case(engine: Any, case: SmokeCase, input_dir: Path, output_dir: Path) -> dict[str, Any]:
    source = input_dir / case.filename
    if not source.is_file():
        return {
            "source": case.filename,
            "page_number": case.page_number,
            "table_count": 0,
            "metric_found": False,
            "value_found": False,
            "same_table_match": False,
            "page_mapping_ok": False,
            "status": "failed",
            "failure_reason": "pdf_not_found",
            "table_checks": [],
        }

    pages_dir = output_dir / "smoke_pages"
    raw_dir = output_dir / "smoke_raw_v2"
    pages_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    page_path = pages_dir / f"{case.output_stem}.pdf"
    json_path = raw_dir / f"{case.output_stem}.json"
    markdown_path = raw_dir / f"{case.output_stem}.md"

    try:
        extract_page_as_pdf(source, case.page_index, page_path)
        results = list(engine.predict(str(page_path)))
        if len(results) != 1:
            raise RuntimeError(f"单页 PDF 返回 {len(results)} 个页面结果")
        payload = result_json_payload(results[0])
        write_json(json_path, payload)
        markdown_path.write_text(result_markdown_text(results[0]), encoding="utf-8")
        return evaluate_table_payload(payload, case)
    except Exception as exc:
        return {
            "source": case.filename,
            "page_number": case.page_number,
            "table_count": 0,
            "metric_found": False,
            "value_found": False,
            "same_table_match": False,
            "page_mapping_ok": False,
            "status": "failed",
            "failure_reason": "parser_error",
            "error": str(exc)[:500],
            "table_checks": [],
        }


def build_summary(cases: list[dict[str, Any]], device: str, elapsed_seconds: float) -> dict[str, Any]:
    passed_pages = sum(item["status"] == "passed" for item in cases)
    return {
        "status": "passed" if passed_pages >= PASS_THRESHOLD else "failed",
        "parser": "PP-StructureV3",
        "device": device,
        "gate": {
            "required_passed_pages": PASS_THRESHOLD,
            "total_pages": len(cases),
            "criterion": "目标指标和值位于同一个 table_res_list 表格对象，且页码映射正确",
        },
        "passed_pages": passed_pages,
        "failed_pages": len(cases) - passed_pages,
        "elapsed_seconds": round(elapsed_seconds, 1),
        "cases": cases,
        "next_step": (
            "门禁通过；下一步由用户手动执行全页轻量文本扫描。"
            if passed_pages >= PASS_THRESHOLD
            else "门禁未通过；停止，不执行全页扫描或批量 OCR。"
        ),
    }


def main() -> int:
    args = parse_args()
    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    report_path = output_dir / "reports" / "smoke_summary.json"
    started = time.perf_counter()

    try:
        from paddleocr import PPStructureV3

        print(f"[INFO] 初始化 PP-StructureV3，device={args.device}")
        engine = PPStructureV3(
            device=args.device,
            lang="ch",
            use_table_recognition=True,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            use_formula_recognition=False,
            use_seal_recognition=False,
            use_chart_recognition=False,
        )
    except Exception as exc:
        summary = build_summary([], args.device, time.perf_counter() - started)
        summary["status"] = "failed"
        summary["failure_reason"] = "initialization_error"
        summary["error"] = str(exc)[:500]
        write_json(report_path, summary)
        print(f"[FAILED] PP-StructureV3 初始化失败: {exc}", file=sys.stderr)
        return 1

    results = []
    for index, case in enumerate(CASES, 1):
        result = parse_case(engine, case, input_dir, output_dir)
        results.append(result)
        print(
            f"[{index}/{len(CASES)}] {case.company} p{case.page_number} "
            f"tables={result['table_count']} metric={result['metric_found']} "
            f"value={result['value_found']} status={result['status']}"
        )

    summary = build_summary(results, args.device, time.perf_counter() - started)
    write_json(report_path, summary)
    print(
        f"[{summary['status'].upper()}] {summary['passed_pages']}/{len(CASES)} 页通过；"
        f"报告: {report_path}"
    )
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
