from dataclasses import dataclass

from app.utils.table_pdf_parser import (
    MAX_METADATA_BYTES,
    ParsedBlock,
    TablePDFParser,
    assess_table_quality,
    build_index_chunks,
    html_table_to_markdown,
    scalarize_metadata,
)
from app.utils.text_splitter import RecursiveTextSplitter


def test_html_table_to_markdown_preserves_header_rows_and_empty_cells():
    html = """
    <table><thead><tr><th>指标</th><th>2024</th><th>2023</th></tr></thead>
    <tbody><tr><td>营业收入</td><td>8.6亿元</td><td></td></tr></tbody></table>
    """

    markdown = html_table_to_markdown(html)

    assert "| 指标 | 2024 | 2023 |" in markdown
    assert "| 营业收入 | 8.6亿元 |  |" in markdown


def test_html_table_to_markdown_escapes_pipes_backslashes_and_line_breaks():
    html = r"<table><tr><th>字段</th><th>值</th></tr><tr><td>A|B</td><td>C\D<br>下一行</td></tr></table>"

    markdown = html_table_to_markdown(html)

    assert r"A\|B" in markdown
    assert r"C\\D<br>下一行" in markdown


def test_html_table_without_th_uses_synthetic_header_without_losing_first_row():
    html = "<table><tr><td>营业收入</td><td>100</td></tr><tr><td>净利润</td><td>20</td></tr></table>"

    markdown = html_table_to_markdown(html)

    assert "| Column 1 | Column 2 |" in markdown
    assert "| 营业收入 | 100 |" in markdown
    assert "| 净利润 | 20 |" in markdown


def test_merged_cells_degrade_to_rectangular_markdown():
    html = """
    <table><tr><th rowspan="2">指标</th><th colspan="2">年度</th></tr>
    <tr><th>2024</th><th>2023</th></tr><tr><td>收入</td><td>100</td><td>90</td></tr></table>
    """

    markdown = html_table_to_markdown(html)

    assert "| 指标 | 年度 / 2024 | 2023 |" in markdown
    assert "| 收入 | 100 | 90 |" in markdown


def test_invalid_merged_cell_falls_back_to_element_text():
    html = '<table><tr><td rowspan="bad">收入</td><td>100</td></tr></table>'

    assert html_table_to_markdown(html, "收入 100") == "收入 100"


def test_scalarize_metadata_serializes_nested_values_and_omits_non_finite():
    metadata = scalarize_metadata({
        "source": "report.pdf",
        "doc_id": 1,
        "nested": {"b": 2, "a": 1},
        "items": ["a", "b"],
        "invalid": float("nan"),
    })

    assert metadata["nested"] == '{"a":1,"b":2}'
    assert metadata["items"] == '["a","b"]'
    assert "invalid" not in metadata
    assert all(isinstance(value, (str, int, float, bool)) for value in metadata.values())


def test_scalarize_metadata_omits_oversized_html_but_keeps_markdown_and_provenance():
    metadata = scalarize_metadata({
        "source": "report.pdf",
        "doc_id": 1,
        "content_type": "table",
        "page_number": 12,
        "provenance_id": "doc_1:page_12:element_1",
        "table_html": "汉" * 5_000,
        "table_markdown": "| 指标 | 值 |\n| --- | --- |\n| 收入 | 100 |",
    })

    assert metadata["table_html_omitted"] is True
    assert "table_html" not in metadata
    assert metadata["table_markdown"].startswith("| 指标")
    assert metadata["page_number"] == 12
    import json
    assert len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) <= MAX_METADATA_BYTES


@dataclass
class _Metadata:
    page_number: int
    text_as_html: str = ""


class _Element:
    def __init__(self, text, category, page, element_id, html=""):
        self.text = text
        self.category = category
        self.id = element_id
        self.metadata = _Metadata(page, html)


def _fake_partitioner(filename=None, strategy="auto", infer_table_structure=False, languages=None):
    assert strategy == "fast"
    assert infer_table_structure is False
    return [
        _Element("主要财务指标（单位：万元）", "Title", 12, "title-1"),
        _Element(
            "营业收入 100 净利润 20",
            "Table",
            12,
            "table-1",
            "<table><tr><th>指标</th><th>金额</th></tr><tr><td>营业收入</td><td>100</td></tr></table>",
        ),
        _Element("本期收入增长。", "NarrativeText", 12, "text-2"),
    ]


def test_table_pdf_parser_hi_res_is_explicit_opt_in():
    received = {}

    def partitioner(filename=None, strategy="auto", infer_table_structure=False, languages=None):
        received.update({
            "strategy": strategy,
            "infer_table_structure": infer_table_structure,
            "languages": languages,
        })
        return [_Element("表格说明", "NarrativeText", 1, "text-1")]

    blocks = TablePDFParser(partitioner, use_hi_res=True).parse(
        "report.pdf",
        doc_id=1,
        source="report.pdf",
    )

    assert received == {
        "strategy": "hi_res",
        "infer_table_structure": True,
        "languages": ["chi_sim", "eng"],
    }
    assert blocks[0].metadata["parser"] == "unstructured-hi-res-v2"


def test_table_pdf_parser_default_profile_is_fast():
    blocks = TablePDFParser(_fake_partitioner).parse(
        "report.pdf",
        doc_id=7,
        source="report.pdf",
    )

    assert all(block.metadata["parser"] == "unstructured-fast-v1" for block in blocks)


def test_table_pdf_parser_preserves_page_source_type_and_context():
    blocks = TablePDFParser(_fake_partitioner).parse("report.pdf", doc_id=7, source="report.pdf")

    assert [block.metadata["content_type"] for block in blocks] == ["text", "table", "text"]
    table = blocks[1]
    assert table.metadata["page_number"] == 12
    assert table.metadata["source"] == "report.pdf"
    assert table.metadata["table_id"] == "doc_7:page_12:table_1"
    assert table.metadata["provenance_id"] == "doc_7:page_12:element_1"
    assert "Context before: 主要财务指标" in table.content
    assert "Context after: 本期收入增长" in table.content
    assert "| 营业收入 | 100 |" in table.content


def test_parse_page_remaps_local_page_to_physical_page():
    def partitioner(filename=None, strategy="auto", infer_table_structure=False, languages=None):
        return [_Element("单页正文", "NarrativeText", 1, "text-1")]

    block = TablePDFParser(partitioner).parse_page(
        "page.pdf", doc_id=3, source="report.pdf", physical_page_number=113
    )[0]

    assert block.metadata["page_number"] == 113
    assert block.metadata["physical_page_number"] == 113
    assert block.metadata["parser_page_number"] == 1
    assert block.metadata["single_page_mapping"] is True
    assert block.metadata["provenance_id"] == "doc_3:page_113:element_0"


def test_assess_table_quality_exposes_structural_signal():
    quality = assess_table_quality(
        "<table><tr><th>指标</th><th>值</th></tr><tr><td>收入</td><td>100</td></tr></table>"
    )

    assert quality["status"] == "good"
    assert quality["row_count"] == 1
    assert quality["column_count"] == 2
    assert quality["score"] >= 0.75


def test_table_pdf_parser_empty_table_uses_placeholder_without_fabrication():
    def partitioner(filename=None, strategy="auto", infer_table_structure=False, languages=None):
        return [_Element("", "Table", 3, "empty", "")]

    blocks = TablePDFParser(partitioner).parse("empty.pdf", doc_id=2, source="empty.pdf")

    assert len(blocks) == 1
    assert blocks[0].metadata["content_type"] == "table"
    assert blocks[0].metadata["table_conversion"] == "placeholder"
    assert "no extractable cells" in blocks[0].content


def test_table_pdf_parser_does_not_duplicate_table_as_text():
    blocks = TablePDFParser(_fake_partitioner).parse("report.pdf", doc_id=7, source="report.pdf")

    assert sum(block.metadata["content_type"] == "table" for block in blocks) == 1
    assert not any(
        block.metadata["content_type"] == "text" and block.content == "营业收入 100 净利润 20"
        for block in blocks
    )


def test_build_index_chunks_repeats_table_header_and_preserves_numeric_rows():
    rows = "\n".join(f"| 指标{i} | {i * 100} |" for i in range(50))
    markdown = "| 指标 | 金额 |\n| --- | --- |\n" + rows
    block = ParsedBlock(
        "[Table | source=report.pdf | page=8]\n\n" + markdown,
        {
            "source": "report.pdf",
            "doc_id": 1,
            "page_number": 8,
            "content_type": "table",
            "provenance_id": "table-1",
            "table_id": "table-1",
            "table_markdown": markdown,
        },
    )

    chunks = build_index_chunks([block], RecursiveTextSplitter(chunk_size=200, chunk_overlap=0))

    assert len(chunks) > 1
    assert all("| 指标 | 金额 |" in chunk.content for chunk in chunks)
    assert any("| 指标49 | 4900 |" in chunk.content for chunk in chunks)
    assert all(chunk.metadata["content_type"] == "table" for chunk in chunks)
    assert [chunk.metadata["table_chunk_index"] for chunk in chunks] == list(range(len(chunks)))


def test_build_index_chunks_optionally_overlaps_previous_table_row():
    rows = "\n".join(f"| 指标{i} | {i * 100} |" for i in range(50))
    markdown = "| 指标 | 金额 |\n| --- | --- |\n" + rows
    block = ParsedBlock(
        "[Table]\n\n" + markdown,
        {"content_type": "table", "table_markdown": markdown},
    )

    chunks = build_index_chunks(
        [block],
        RecursiveTextSplitter(chunk_size=200, chunk_overlap=0),
        table_row_overlap=1,
    )

    previous_last_row = chunks[0].content.splitlines()[-1]
    assert previous_last_row in chunks[1].content.splitlines()[3:]
    assert all(len(chunk.content) <= 1_200 for chunk in chunks)


def test_scalarize_metadata_enforces_total_budget_for_mandatory_strings():
    import json

    keys = (
        "source", "doc_id", "chunk_index", "content_type", "page_number", "provenance_id",
        "element_type", "table_id", "table_index", "table_chunk_index", "table_chunk_count",
    )
    metadata = scalarize_metadata({key: "汉" * 4_000 for key in keys})

    assert set(metadata) == set(keys)
    assert len(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= MAX_METADATA_BYTES


def test_table_header_over_hard_limit_uses_bounded_lossless_windows():
    markdown = "| " + "H" * 2_000 + " |\n| --- |\n| row |"
    block = ParsedBlock(
        "[Table]\n\n" + markdown,
        {"content_type": "table", "table_markdown": markdown},
    )

    chunks = build_index_chunks([block], RecursiveTextSplitter(chunk_size=100, chunk_overlap=0))

    assert all(0 < len(chunk.content) <= 1_200 for chunk in chunks)
    assert "".join(chunk.content for chunk in chunks) == block.content


def test_single_line_table_markdown_respects_hard_limit():
    content = "H" * 2_000 + "\n"
    block = ParsedBlock(content, {"content_type": "table", "table_markdown": content})

    chunks = build_index_chunks([block], RecursiveTextSplitter(chunk_size=100, chunk_overlap=0))

    assert all(0 < len(chunk.content) <= 1_200 for chunk in chunks)
    assert "".join(chunk.content for chunk in chunks) == content.strip()


def test_oversized_table_row_never_exceeds_hard_limit():
    markdown = "| 指标 | 值 |\n| --- | --- |\n| 收入 | " + "9" * 2_000 + " |"
    block = ParsedBlock(
        "[Table]\n\n" + markdown,
        {"content_type": "table", "table_markdown": markdown},
    )

    chunks = build_index_chunks([block], RecursiveTextSplitter(chunk_size=100, chunk_overlap=0))

    assert len(chunks) > 1
    assert all(0 < len(chunk.content) <= 1_200 for chunk in chunks)


def test_build_index_chunks_distinguishes_text_and_table_blocks():
    blocks = [
        ParsedBlock("普通文本内容", {"content_type": "text", "page_number": 1}),
        ParsedBlock(
            "[Table]\n\n| 指标 | 值 |\n| --- | --- |\n| 收入 | 100 |",
            {
                "content_type": "table",
                "page_number": 2,
                "table_markdown": "| 指标 | 值 |\n| --- | --- |\n| 收入 | 100 |",
            },
        ),
    ]

    chunks = build_index_chunks(blocks, RecursiveTextSplitter(chunk_size=400, chunk_overlap=0))

    assert [chunk.metadata["content_type"] for chunk in chunks] == ["text", "table"]
    assert [chunk.metadata["page_number"] for chunk in chunks] == [1, 2]
