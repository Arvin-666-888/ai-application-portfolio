from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

MAX_PAGES = 5
EXPECTED_REPORTS = 5
MAX_SAMPLE_CHUNKS = 12
CHINESE_PATTERN = re.compile(r"[㐀-䶿一-鿿]")


class SmokeTestFailed(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        pdf: str = "",
        phase: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.pdf = pdf
        self.phase = phase
        self.details = details or {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="对 5 份中文 A 股年报的前 5 页执行无 OCR 的 fast PDF 解析冒烟测试。",
    )
    parser.add_argument(
        "--pdf-dir",
        default="evals/task2_chinese_financial_reports",
        help="包含 5 份中文年报 PDF 的目录。",
    )
    parser.add_argument(
        "--output-dir",
        default="chinese_sample_output",
        help="成功样本 chunk 与运行报告的输出目录。",
    )
    return parser.parse_args()


def collect_reports(pdf_dir: str | Path) -> list[Path]:
    directory = Path(pdf_dir).resolve()
    if not directory.is_dir():
        raise SmokeTestFailed(f"PDF 目录不存在: {directory}", phase="collect")
    reports = sorted(directory.glob("*.pdf"), key=lambda path: path.name)
    if len(reports) != EXPECTED_REPORTS:
        raise SmokeTestFailed(
            f"需要恰好 {EXPECTED_REPORTS} 份 PDF，实际发现 {len(reports)} 份: {directory}",
            phase="collect",
        )
    return reports


def write_first_pages(source: Path, destination: Path) -> int:
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise SmokeTestFailed("缺少 pypdf，无法裁剪前 5 页", pdf=source.name, phase="slice") from exc

    try:
        reader = PdfReader(str(source))
        page_count = min(MAX_PAGES, len(reader.pages))
        if page_count == 0:
            raise ValueError("PDF 没有可读取页面")
        writer = PdfWriter()
        for page in reader.pages[:page_count]:
            writer.add_page(page)
        with destination.open("wb") as output:
            writer.write(output)
        return page_count
    except SmokeTestFailed:
        raise
    except Exception as exc:
        raise SmokeTestFailed(
            f"裁剪前 {MAX_PAGES} 页失败: {exc}",
            pdf=source.name,
            phase="slice",
        ) from exc


def has_chinese(text: str) -> bool:
    return CHINESE_PATTERN.search(text) is not None


def select_sample_chunks(chunks: list[Any]) -> list[Any]:
    chinese_tables = [
        chunk
        for chunk in chunks
        if chunk.metadata.get("content_type") == "table" and has_chinese(chunk.content)
    ]
    other_chinese = [
        chunk
        for chunk in chunks
        if chunk.metadata.get("content_type") != "table" and has_chinese(chunk.content)
    ]
    samples = chinese_tables + other_chinese
    return samples[:MAX_SAMPLE_CHUNKS]


def serialize_chunk(chunk: Any, chunk_index: int) -> dict[str, Any]:
    return {
        "chunk_index": chunk_index,
        "content": chunk.content,
        "metadata": dict(chunk.metadata),
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_generated_outputs(output_dir: Path) -> None:
    if not output_dir.is_dir():
        return
    for path in output_dir.glob("*.sample_chunks.json"):
        path.unlink()
    for filename in ("smoke_test_summary.json", "smoke_test_error.json"):
        path = output_dir / filename
        if path.is_file():
            path.unlink()


def run_smoke_test(
    reports: list[Path],
    output_dir: Path,
    *,
    parser_factory: Callable[[], Any] | None = None,
    splitter_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    from app.utils.table_pdf_parser import TablePDFParser, build_index_chunks
    from app.utils.text_splitter import RecursiveTextSplitter

    parser_factory = parser_factory or (lambda: TablePDFParser(use_hi_res=False))
    splitter_factory = splitter_factory or (lambda: RecursiveTextSplitter(chunk_size=400, chunk_overlap=80))

    parser = parser_factory()
    if parser.profile != "unstructured-fast-v1":
        raise SmokeTestFailed(
            f"拒绝运行非 fast 解析器: {parser.profile}",
            phase="configuration",
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    pending_samples: list[tuple[Path, dict[str, Any]]] = []
    results: list[dict[str, Any]] = []
    total_chinese_table_chunks = 0
    started = time.perf_counter()

    with tempfile.TemporaryDirectory(prefix="chinese_pdf_smoke_") as temporary_dir:
        temporary_root = Path(temporary_dir)
        for doc_id, source in enumerate(reports, 1):
            sliced_pdf = temporary_root / f"{doc_id}.pdf"
            page_count = write_first_pages(source, sliced_pdf)
            try:
                blocks = parser.parse(sliced_pdf, doc_id=doc_id, source=source.name)
                chunks = build_index_chunks(blocks, splitter_factory())
            except Exception as exc:
                raise SmokeTestFailed(
                    f"fast 解析前 {page_count} 页失败: {exc}",
                    pdf=source.name,
                    phase="parse",
                ) from exc

            chinese_chunks = [chunk for chunk in chunks if has_chinese(chunk.content)]
            chinese_table_chunks = [
                chunk
                for chunk in chinese_chunks
                if chunk.metadata.get("content_type") == "table"
            ]
            if not chinese_chunks:
                raise SmokeTestFailed(
                    "解析完成，但没有生成含中文的 chunk",
                    pdf=source.name,
                    phase="validate",
                )

            samples = select_sample_chunks(chunks)
            sample_payload = {
                "source": source.name,
                "pages_parsed": page_count,
                "parser": parser.profile,
                "ocr_enabled": False,
                "chunk_count": len(chunks),
                "chinese_chunk_count": len(chinese_chunks),
                "table_chunk_count": sum(
                    chunk.metadata.get("content_type") == "table" for chunk in chunks
                ),
                "chinese_table_chunk_count": len(chinese_table_chunks),
                "sample_chunks": [serialize_chunk(chunk, index) for index, chunk in enumerate(samples)],
            }
            sample_path = output_dir / f"{source.stem}.sample_chunks.json"
            pending_samples.append((sample_path, sample_payload))

            result = {key: value for key, value in sample_payload.items() if key != "sample_chunks"}
            result["sample_file"] = str(sample_path.resolve())
            results.append(result)
            total_chinese_table_chunks += len(chinese_table_chunks)
            print(
                f"[OK] {source.name}: pages={page_count}, chunks={len(chunks)}, "
                f"chinese={len(chinese_chunks)}, chinese_tables={len(chinese_table_chunks)}"
            )

    if total_chinese_table_chunks == 0:
        raise SmokeTestFailed(
            "5 份报告均生成了中文 chunk，但前 5 页没有提取出中文 table chunk；不能据此证明中文表格提取已跑通",
            phase="validate",
            details={
                "report_count": len(reports),
                "pages_per_report_at_most": MAX_PAGES,
                "supports_chinese_embedded_text_pipeline": True,
                "supports_chinese_table_chunks_in_sample": False,
                "total_chinese_table_chunks": 0,
                "reports": [
                    {key: value for key, value in result.items() if key != "sample_file"}
                    for result in results
                ],
            },
        )

    for sample_path, sample_payload in pending_samples:
        write_json(sample_path, sample_payload)

    summary = {
        "status": "passed",
        "scope": {
            "report_count": len(reports),
            "pages_per_report_at_most": MAX_PAGES,
            "full_document_parse": False,
        },
        "configuration": {
            "strategy": "fast",
            "parser": parser.profile,
            "ocr_enabled": False,
            "infer_table_structure": False,
        },
        "evidence": {
            "all_reports_have_chinese_chunks": True,
            "total_chinese_table_chunks": total_chinese_table_chunks,
            "supports_chinese_embedded_text_pipeline": True,
            "supports_chinese_table_chunks_in_sample": True,
        },
        "reports": results,
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "next_step": "仅完成前 5 页冒烟验证；是否运行全量解析需手动决定。",
    }
    write_json(output_dir / "smoke_test_summary.json", summary)
    return summary


def write_error_report(output_dir: Path, exc: Exception) -> None:
    payload = {
        "status": "failed",
        "strategy": "fast",
        "ocr_enabled": False,
        "stopped_without_fallback": True,
        "pdf": getattr(exc, "pdf", ""),
        "phase": getattr(exc, "phase", "unknown"),
        "error_type": type(exc).__name__,
        "error": str(exc),
        "details": getattr(exc, "details", {}),
        "next_step": "已按约定停止；未尝试 OCR、hi_res 或自动修复。",
    }
    write_json(output_dir / "smoke_test_error.json", payload)


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir).resolve()
    clear_generated_outputs(output_dir)
    try:
        reports = collect_reports(args.pdf_dir)
        summary = run_smoke_test(reports, output_dir)
    except Exception as exc:
        write_error_report(output_dir, exc)
        print(f"[FAILED] {exc}", file=sys.stderr)
        print(f"错误报告: {output_dir / 'smoke_test_error.json'}", file=sys.stderr)
        return 1

    print(
        f"[PASSED] {len(summary['reports'])} 份报告的前 {MAX_PAGES} 页已通过 fast 冒烟测试。"
    )
    print(f"样本输出: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
