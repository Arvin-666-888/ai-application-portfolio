from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from app.utils.table_pdf_parser import ParsedBlock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "chinese_pdf_smoke_test.py"
SPEC = importlib.util.spec_from_file_location("chinese_pdf_smoke_test", SCRIPT_PATH)
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def _write_pdf(path: Path, page_count: int) -> None:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=100, height=100)
    with path.open("wb") as output:
        writer.write(output)


def test_write_first_pages_limits_output_to_five_pages(tmp_path):
    source = tmp_path / "source.pdf"
    destination = tmp_path / "first-five.pdf"
    _write_pdf(source, 7)

    written = smoke.write_first_pages(source, destination)

    assert written == 5
    assert len(PdfReader(str(destination)).pages) == 5


def test_collect_reports_requires_exactly_five_pdfs(tmp_path):
    for index in range(4):
        (tmp_path / f"report-{index}.pdf").touch()

    with pytest.raises(smoke.SmokeTestFailed, match="恰好 5 份"):
        smoke.collect_reports(tmp_path)


class _FastParser:
    profile = "unstructured-fast-v1"

    def __init__(self, calls):
        self.calls = calls

    def parse(self, file_path, *, doc_id, source):
        self.calls.append(source)
        return [
            ParsedBlock(
                "主要财务指标",
                {
                    "source": source,
                    "doc_id": doc_id,
                    "content_type": "text",
                    "page_number": 1,
                    "element_type": "Title",
                    "provenance_id": f"doc_{doc_id}:page_1:title",
                    "parser": self.profile,
                },
            ),
            ParsedBlock(
                f"[Table | source={source} | page=3]\n\n"
                "| 指标 | 金额 |\n| --- | --- |\n| 营业收入 | 100 |",
                {
                    "source": source,
                    "doc_id": doc_id,
                    "content_type": "table",
                    "page_number": 3,
                    "element_type": "Table",
                    "provenance_id": f"doc_{doc_id}:page_3:table",
                    "parser": self.profile,
                    "table_id": f"doc_{doc_id}:page_3:table_1",
                    "table_markdown": "| 指标 | 金额 |\n| --- | --- |\n| 营业收入 | 100 |",
                },
            ),
        ]


def test_run_smoke_test_writes_chinese_samples_with_fast_profile(tmp_path, monkeypatch):
    reports = [tmp_path / f"中文年报_{index}.pdf" for index in range(5)]
    for report in reports:
        report.touch()
    output_dir = tmp_path / "output"
    calls = []

    def fake_write_first_pages(source, destination):
        destination.touch()
        return 5

    monkeypatch.setattr(smoke, "write_first_pages", fake_write_first_pages)
    summary = smoke.run_smoke_test(
        reports,
        output_dir,
        parser_factory=lambda: _FastParser(calls),
    )

    assert calls == [report.name for report in reports]
    assert summary["status"] == "passed"
    assert summary["configuration"] == {
        "strategy": "fast",
        "parser": "unstructured-fast-v1",
        "ocr_enabled": False,
        "infer_table_structure": False,
    }
    assert summary["evidence"]["total_chinese_table_chunks"] == 5
    assert (output_dir / "smoke_test_summary.json").is_file()
    assert len(list(output_dir.glob("*.sample_chunks.json"))) == 5


def test_run_smoke_test_does_not_write_samples_when_no_table_is_extracted(tmp_path, monkeypatch):
    reports = [tmp_path / f"中文年报_{index}.pdf" for index in range(5)]
    for report in reports:
        report.touch()

    class TextOnlyFastParser:
        profile = "unstructured-fast-v1"

        def parse(self, file_path, *, doc_id, source):
            return [
                ParsedBlock(
                    "中文内嵌文本",
                    {
                        "source": source,
                        "doc_id": doc_id,
                        "content_type": "text",
                        "page_number": 1,
                        "element_type": "Text",
                        "provenance_id": f"doc_{doc_id}:page_1:text",
                        "parser": self.profile,
                    },
                )
            ]

    def fake_write_first_pages(source, destination):
        destination.touch()
        return 5

    output_dir = tmp_path / "output"
    monkeypatch.setattr(smoke, "write_first_pages", fake_write_first_pages)
    with pytest.raises(smoke.SmokeTestFailed, match="没有提取出中文 table chunk") as exc_info:
        smoke.run_smoke_test(reports, output_dir, parser_factory=TextOnlyFastParser)

    assert exc_info.value.details["report_count"] == 5
    assert exc_info.value.details["supports_chinese_embedded_text_pipeline"] is True
    assert exc_info.value.details["supports_chinese_table_chunks_in_sample"] is False
    assert list(output_dir.glob("*.sample_chunks.json")) == []


def test_run_smoke_test_stops_after_first_parse_failure(tmp_path, monkeypatch):
    reports = [tmp_path / f"中文年报_{index}.pdf" for index in range(5)]
    for report in reports:
        report.touch()
    calls = []

    class FailingFastParser:
        profile = "unstructured-fast-v1"

        def parse(self, file_path, *, doc_id, source):
            calls.append(source)
            raise RuntimeError("embedded text parse failed")

    def fake_write_first_pages(source, destination):
        destination.touch()
        return 5

    monkeypatch.setattr(smoke, "write_first_pages", fake_write_first_pages)
    with pytest.raises(smoke.SmokeTestFailed, match="fast 解析前 5 页失败"):
        smoke.run_smoke_test(
            reports,
            tmp_path / "output",
            parser_factory=FailingFastParser,
        )

    assert calls == [reports[0].name]


def test_run_smoke_test_rejects_hi_res_parser_before_reading_pdfs(tmp_path):
    class HiResParser:
        profile = "unstructured-hi-res-v2"

    with pytest.raises(smoke.SmokeTestFailed, match="拒绝运行非 fast"):
        smoke.run_smoke_test(
            [tmp_path / "report.pdf"],
            tmp_path / "output",
            parser_factory=HiResParser,
        )
