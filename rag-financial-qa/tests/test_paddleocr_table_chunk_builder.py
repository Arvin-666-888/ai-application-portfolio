from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "04_build_paddleocr_table_chunks.py"
)
SPEC = importlib.util.spec_from_file_location("paddle_chunk_builder", SCRIPT)
assert SPEC and SPEC.loader
builder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = builder
SPEC.loader.exec_module(builder)


def _job(page=12):
    return {
        "doc_id": 1,
        "source": "report.pdf",
        "pdf_sha256": "a" * 64,
        "page_number": page,
        "candidate_reasons": ["financial_table_title"],
    }


def _table(index=0, html=None, text="营业收入 100"):
    html = html or (
        "<table><tr><th>指标</th><th>金额</th></tr>"
        "<tr><td>营业收入</td><td>100</td></tr></table>"
    )
    return {
        "table_index": index,
        "pred_html": html,
        "ocr_text": text,
        "table_content_sha256": builder.table_content_digest(html, text),
    }


def _artifact(job, tables):
    return {
        "schema_version": builder.PAGE_SCHEMA,
        "status": "completed",
        "source": job["source"],
        "pdf_sha256": job["pdf_sha256"],
        "physical_page_number": job["page_number"],
        "single_page_result": {
            "page_index": 0,
            "page_count": 1,
            "page_mapping_ok": True,
        },
        "engine": {"configuration_fingerprint": "f" * 64},
        "table_count": len(tables),
        "tables": tables,
        "error": None,
    }


def test_script_does_not_import_paddleocr():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "from paddleocr" not in source
    assert "import paddleocr" not in source


def test_script_aligns_schema_and_validation_with_common_adapter():
    assert builder.PAGE_SCHEMA == builder.ARTIFACT_SCHEMA
    assert callable(builder.load_paddle_artifact)


def test_validate_artifact_accepts_no_table_and_rejects_bad_digest():
    job = _job()
    assert builder.validate_artifact(_artifact(job, []), job, "f" * 64) == []

    artifact = _artifact(job, [_table()])
    artifact["tables"][0]["table_content_sha256"] = "bad"
    with pytest.raises(builder.ChunkBuildError, match="digest"):
        builder.validate_artifact(artifact, job, "f" * 64)


def test_split_repeats_header_and_overlaps_previous_row():
    rows = "\n".join(f"| 指标{i} | {i * 100} |" for i in range(60))
    markdown = "| 指标 | 金额 |\n| --- | --- |\n" + rows

    chunks = builder.split_table_markdown(
        "[Table | source=report.pdf | page=12]",
        markdown,
        chunk_size=400,
        row_overlap=1,
    )

    assert len(chunks) > 1
    assert all("| 指标 | 金额 |" in chunk for chunk in chunks)
    previous_rows = chunks[0].splitlines()[3:]
    assert previous_rows[-1] in chunks[1]
    assert all(len(chunk) <= 1200 for chunk in chunks)


def test_build_chunks_preserves_table_identity_and_scalar_metadata():
    loaded = [(_job(), [_table()]), (_job(page=13), [])]

    chunks, raw_tables = builder.build_chunks(
        loaded,
        chunk_size=400,
        row_overlap=1,
        input_fingerprint="f" * 64,
    )

    assert len(raw_tables) == 1
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.metadata["source"] == "report.pdf"
    assert chunk.metadata["page_number"] == 12
    assert chunk.metadata["table_id"] == "doc_1:page_12:table_1"
    assert chunk.metadata["chunk_index"] == 0
    assert all(
        not isinstance(value, (dict, list, tuple, set))
        for value in chunk.metadata.values()
    )
    assert "营业收入" in chunk.content
    assert "100" in chunk.content


def test_build_coverage_counts_raw_and_post_chunk_strict_hits():
    case = {
        "pdf": "report.pdf",
        "question": "营业收入是多少？",
        "metric": "营业收入",
        "expected_value": "100",
        "expected_page": 12,
    }
    chunks, raw_tables = builder.build_chunks(
        [(_job(), [_table()])],
        chunk_size=400,
        row_overlap=1,
        input_fingerprint="f" * 64,
    )

    coverage = builder.build_coverage(
        raw_tables,
        chunks,
        [case],
        chunk_file_sha256="c" * 64,
        ground_truth_sha256="g" * 64,
    )

    assert coverage["counts"]["raw_same_table_covered"] == 1
    assert coverage["counts"]["post_chunk_strict_covered"] == 1
    assert coverage["cases"][0]["post_chunk_classification"] == "strict_target_present"


def test_atomic_write_does_not_leave_temp_file(tmp_path):
    path = tmp_path / "result.json"
    builder.write_json_atomic(path, {"ok": True})

    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
    assert not path.with_suffix(".json.tmp").exists()
