import argparse
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from scripts.compare_table_retrieval import (
    EvaluationBlocked,
    async_main,
    build_inventory,
    calculate_improvement,
    filter_cases_for_paths,
    load_ground_truth,
    row_strict_context_hit,
    score_case,
    strict_context_hit,
    _chunk_cache_path,
    _load_chunk_cache,
    _write_chunk_cache,
)


def test_missing_ground_truth_blocks_without_creating_output():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        output = root / "compare_result.json"
        args = argparse.Namespace(
            pdf_dir=[], pdf=[], ground_truth=str(root / "missing.json"),
            output=str(output), top_k=5, min_reports=5,
        )

        exit_code = asyncio.run(async_main(args))

        assert exit_code == 2
        assert not output.exists()


def test_single_pdf_filters_fixed_ground_truth_in_memory():
    cases = [
        {"pdf": "a.pdf", "question": "A", "metric": "收入", "expected_value": "1", "expected_page": 1},
        {"pdf": "b.pdf", "question": "B", "metric": "利润", "expected_value": "2", "expected_page": 2},
    ]

    selected = filter_cases_for_paths(cases, [Path("a.pdf")])

    assert selected == [cases[0]]
    assert len(cases) == 2


def test_single_pdf_without_fixed_cases_is_blocked():
    cases = [
        {"pdf": "a.pdf", "question": "A", "metric": "收入", "expected_value": "1", "expected_page": 1},
    ]

    with pytest.raises(EvaluationBlocked, match="没有验收用例"):
        filter_cases_for_paths(cases, [Path("missing.pdf")])


def test_missing_pdf_blocks_inventory():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        cases = [{
            "pdf": "missing.pdf",
            "question": "营业收入？",
            "metric": "营业收入",
            "expected_value": "100",
            "expected_page": 1,
        }]

        with pytest.raises(EvaluationBlocked, match="PDF 不存在"):
            build_inventory([root / "missing.pdf"], cases, min_reports=1)


def test_fewer_than_five_unique_hashes_blocks():
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter

    with TemporaryDirectory() as directory:
        root = Path(directory)
        first = root / "a.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        with first.open("wb") as handle:
            writer.write(handle)
        paths = []
        cases = []
        for index in range(5):
            path = root / f"report-{index}.pdf"
            path.write_bytes(first.read_bytes())
            paths.append(path)
            cases.append({
                "pdf": path.name, "question": "收入", "metric": "收入",
                "expected_value": "100", "expected_page": 1,
            })

        with pytest.raises(EvaluationBlocked, match="唯一"):
            build_inventory(paths, cases, min_reports=5)


def test_blocked_run_does_not_overwrite_existing_output():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        output = root / "compare_result.json"
        output.write_text("sentinel", encoding="utf-8")
        args = argparse.Namespace(
            pdf_dir=[], pdf=[], ground_truth=str(root / "missing.json"),
            output=str(output), top_k=5, min_reports=5,
        )

        exit_code = asyncio.run(async_main(args))

        assert exit_code == 2
        assert output.read_text(encoding="utf-8") == "sentinel"


def test_load_ground_truth_requires_explicit_page_value_and_metric():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "ground.json"
        path.write_text(json.dumps([{"pdf": "a.pdf", "question": "收入"}]), encoding="utf-8")

        with pytest.raises(EvaluationBlocked, match="缺少字段"):
            load_ground_truth(path)


def test_strict_hit_requires_metric_value_report_and_page_in_same_context():
    case = {
        "pdf": "annual.pdf", "question": "收入？", "metric": "营业收入",
        "expected_value": "8,600", "expected_page": 12,
    }
    matching = {"source": "annual.pdf", "page_number": 12, "content": "营业收入 8600"}
    wrong_page = {**matching, "page_number": 13}
    split_conditions = [
        {"source": "annual.pdf", "page_number": 12, "content": "营业收入"},
        {"source": "annual.pdf", "page_number": 12, "content": "8600"},
    ]

    assert strict_context_hit(matching, case)
    assert not strict_context_hit(wrong_page, case)
    assert score_case(split_conditions, case)["hit"] is False


def test_strict_hit_rejects_numeric_substring_false_positives():
    case = {
        "pdf": "annual.pdf", "question": "收入？", "metric": "营业收入",
        "expected_value": "100", "expected_page": 12,
    }

    assert not strict_context_hit(
        {"source": "annual.pdf", "page_number": 12, "content": "营业收入 1000 万元"},
        case,
    )
    decimal_case = {**case, "expected_value": "8.6"}
    assert not strict_context_hit(
        {"source": "annual.pdf", "page_number": 12, "content": "营业收入 18.6 万元"},
        decimal_case,
    )
    assert strict_context_hit(
        {"source": "annual.pdf", "page_number": 12, "content": "营业收入 8.6 万元"},
        decimal_case,
    )


def test_strict_hit_accepts_whitespace_between_adjacent_table_values():
    case = {
        "pdf": "annual.pdf", "question": "Total net sales?", "metric": "Total net sales",
        "expected_value": "416,161", "expected_page": 32,
    }

    assert strict_context_hit(
        {
            "source": "annual.pdf",
            "page_number": 32,
            "content": "Total net sales 416,161 391,035 383,285",
        },
        case,
    )
    assert not strict_context_hit(
        {
            "source": "annual.pdf",
            "page_number": 32,
            "content": "Total net sales 4 16161 391,035 383,285",
        },
        case,
    )


def test_row_strict_rejects_case_27_cross_row_false_positive():
    case = {
        "pdf": "招商银行_2024年年度报告_A股.pdf",
        "question": "2024年度招商银行集团经营活动产生的现金流量净额是多少？",
        "metric": "经营活动产生的现金流量净额",
        "expected_value": "447,023",
        "expected_page": 142,
    }
    context = {
        "source": case["pdf"],
        "page_number": 142,
        "content": (
            "| Column 1 | Column 2 | Column 3 |\n"
            "| --- | --- | --- |\n"
            "| 经营活动产生的现金流量净额 59(a) | (1,139,938) | |\n"
            "| 二、投资活动产生的现金流量 | 447,023 | 357,753 |"
        ),
    }

    assert strict_context_hit(context, case)
    assert not row_strict_context_hit(context, case)
    assert score_case([context], case, scorer="row_strict")["miss_reason"] == [
        "row_false_positive"
    ]


def test_row_strict_accepts_metric_and_value_in_same_markdown_row():
    case = {
        "pdf": "annual.pdf",
        "question": "2024年营业收入是多少？",
        "metric": "营业收入",
        "expected_value": "416,161",
        "expected_page": 32,
    }
    context = {
        "source": "annual.pdf",
        "page_number": 32,
        "content": (
            "| 指标 | 2024年 | 2023年 |\n"
            "| --- | --- | --- |\n"
            "| 营业收入 | 416,161 | 391,035 |"
        ),
    }

    assert row_strict_context_hit(context, case)


def test_row_strict_rejects_value_from_wrong_year_column():
    case = {
        "pdf": "annual.pdf",
        "question": "2024年营业收入是多少？",
        "metric": "营业收入",
        "expected_value": "391,035",
        "expected_page": 32,
    }
    context = {
        "source": "annual.pdf",
        "page_number": 32,
        "content": (
            "| 指标 | 2024年 | 2023年 |\n"
            "| --- | --- | --- |\n"
            "| 营业收入 | 416,161 | 391,035 |"
        ),
    }

    assert strict_context_hit(context, case)
    assert not row_strict_context_hit(context, case)


def test_row_strict_plain_text_requires_same_line():
    case = {
        "pdf": "annual.pdf",
        "question": "2024年营业收入是多少？",
        "metric": "营业收入",
        "expected_value": "416,161",
        "expected_page": 32,
    }

    assert row_strict_context_hit(
        {
            "source": "annual.pdf",
            "page_number": 32,
            "content": "营业收入 416,161\n净利润 20,000",
        },
        case,
    )
    assert not row_strict_context_hit(
        {
            "source": "annual.pdf",
            "page_number": 32,
            "content": "营业收入\n净利润 416,161",
        },
        case,
    )


def test_chunk_cache_round_trip_and_hash_validation():
    from app.utils.table_pdf_parser import IndexChunk

    with TemporaryDirectory() as directory:
        path = _chunk_cache_path(directory, "abc123", "new", 400, 80, "unstructured-fast-v1")
        chunks = [IndexChunk("表格内容", {"source": "年报.pdf", "page_number": 12})]

        _write_chunk_cache(path, "abc123", "new", "unstructured-fast-v1", chunks)
        loaded = _load_chunk_cache(path, "abc123", "new", "unstructured-fast-v1")

        assert loaded == chunks
        assert _load_chunk_cache(path, "different", "new", "unstructured-fast-v1") is None
        assert _load_chunk_cache(path, "abc123", "old", "unstructured-fast-v1") is None
        assert _load_chunk_cache(path, "abc123", "new", "unstructured-hi-res-v2") is None


def test_old_cache_keeps_legacy_schema_and_filename():
    from app.utils.table_pdf_parser import IndexChunk

    with TemporaryDirectory() as directory:
        path = _chunk_cache_path(directory, "abc123", "old", 400, 80, "legacy-pdfplumber-v1")
        chunks = [IndexChunk("旧版内容", {"source": "年报.pdf", "page_number": 12})]

        _write_chunk_cache(path, "abc123", "old", "legacy-pdfplumber-v1", chunks)

        assert path.name == "abc123.old.chunk-400-overlap-80.json"
        assert _load_chunk_cache(path, "abc123", "old", "legacy-pdfplumber-v1") == chunks


def test_chunk_cache_rejects_legacy_new_cache_schema():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "legacy-new-cache.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "pdf_sha256": "abc123",
            "arm": "new",
            "chunks": [{"content": "旧缓存", "metadata": {}}],
        }), encoding="utf-8")

        assert _load_chunk_cache(path, "abc123", "new", "unstructured-fast-v1") is None


def test_chunk_cache_rejects_malformed_payload():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "bad.json"
        path.write_text("not-json", encoding="utf-8")

        assert _load_chunk_cache(path, "abc123", "new", "unstructured-fast-v1") is None


def test_relative_improvement_is_null_for_zero_baseline():
    result = calculate_improvement(0.0, 0.4)

    assert result["absolute_percentage_points"] == 40.0
    assert result["relative_percent"] is None
    assert result["relative_change_reason"] == "undefined_zero_baseline"
