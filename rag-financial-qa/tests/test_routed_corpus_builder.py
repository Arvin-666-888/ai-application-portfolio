from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "07_build_routed_corpus.py"
SPEC = importlib.util.spec_from_file_location("routed_corpus_builder", SCRIPT)
assert SPEC and SPEC.loader
builder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = builder
SPEC.loader.exec_module(builder)


def _digest(html: str, text: str) -> str:
    identity = json.dumps(
        {"pred_html": html, "ocr_text": text},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _write_inputs(tmp_path):
    source = "report.pdf"
    pdf_sha = "a" * 64
    inventory = {
        "schema_version": "page-inventory-v1",
        "scan_method": "pdfplumber-embedded-text-only",
        "report_count": 1,
        "reports": [{
            "source": source,
            "sha256": pdf_sha,
            "page_count": 2,
            "pages": [{"page_number": 1}, {"page_number": 2}],
        }],
    }
    candidate = {
        "schema_version": "candidate-pages-v1",
        "selection_policy": {
            "uses_ground_truth": False,
            "router_policy_version": builder.PDF_ROUTING_POLICY_VERSION,
            "router_policy_fingerprint": builder.POLICY_FINGERPRINT,
        },
        "report_count": 1,
        "selected_page_count": 1,
        "dropped_page_count": 0,
        "reports": [{
            "source": source,
            "pdf_sha256": pdf_sha,
            "page_count": 2,
            "selected_count": 1,
            "dropped_count": 0,
            "selected_pages": [{
                "source": source,
                "page_number": 2,
                "reasons": ["financial_table_title"],
            }],
            "dropped_pages": [],
        }],
    }
    inventory_path = tmp_path / "inventory.json"
    candidate_path = tmp_path / "candidate.json"
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    old_cache_dir = tmp_path / "old_cache"
    old_cache_dir.mkdir()
    old_contents = ["冻结chunk-1", "冻结chunk-2"]
    old_cache_path = old_cache_dir / f"{pdf_sha}.old.chunk-400-overlap-80.json"
    old_cache_path.write_text(json.dumps({
        "schema_version": 1,
        "pdf_sha256": pdf_sha,
        "arm": "old",
        "chunks": [
            {
                "content": content,
                "metadata": {
                    "source": source,
                    "doc_id": 9,
                    "content_type": "text",
                    "page_number": index + 1,
                    "parser": "pdfplumber_page_text",
                },
            }
            for index, content in enumerate(old_contents)
        ],
    }, ensure_ascii=False), encoding="utf-8")

    engine = "f" * 64
    summary = {
        "schema_version": "paddleocr-batch-audit-v1",
        "status": "passed",
        "inputs": {
            "candidate_manifest_sha256": builder.file_sha256(candidate_path),
            "engine_configuration_fingerprint": engine,
        },
        "counts": {"expected_pages": 1},
    }
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    html = "<table><tr><th>指标</th><th>金额</th></tr><tr><td>收入</td><td>100</td></tr></table>"
    text = "收入 100"
    raw_dir = tmp_path / "raw"
    artifact_path = raw_dir / pdf_sha[:12] / "p0002.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(json.dumps({
        "schema_version": "paddleocr-table-page-v1",
        "status": "completed",
        "source": source,
        "pdf_sha256": pdf_sha,
        "physical_page_number": 2,
        "single_page_result": {"page_index": 0, "page_count": 1, "page_mapping_ok": True},
        "engine": {"configuration_fingerprint": engine},
        "table_count": 1,
        "tables": [{
            "table_index": 0,
            "pred_html": html,
            "ocr_text": text,
            "table_content_sha256": _digest(html, text),
        }],
        "error": None,
    }), encoding="utf-8")
    return inventory_path, candidate_path, raw_dir, summary_path, old_cache_dir


def test_builder_is_offline_and_has_no_gt_api_or_paddle_import():
    source = SCRIPT.read_text(encoding="utf-8").lower()
    assert "table_ground_truth" not in source
    assert "load_ground_truth" not in source
    assert "api_key" not in source
    assert "import paddle" not in source
    assert "from paddle" not in source


def test_builds_frozen_l1_chunks_and_legal_l3_table_with_overlap_one(tmp_path, monkeypatch):
    paths = _write_inputs(tmp_path)
    monkeypatch.setattr(builder, "EXPECTED_OLD_CHUNKS", 2)

    payload = builder.build_routed_corpus(*paths)

    assert payload["schema_version"] == builder.ROUTED_SCHEMA
    assert payload["builder_version"] == builder.BUILDER_VERSION
    assert len(payload["l1_corpus_sha256"]) == 64
    assert len(payload["l3_corpus_sha256"]) == 64
    assert payload["ground_truth_loaded"] is False
    assert payload["api_called"] is False
    assert payload["routing"]["table_row_overlap"] == 1
    assert payload["routing"]["layers"]["L1"] == builder.OLD_CACHE_PROFILE
    assert payload["counts"]["l1_page_count"] == 2
    assert payload["counts"]["l1_chunk_count"] == 2
    assert payload["counts"]["l3_table_count"] == 1
    assert [chunk["content"] for chunk in payload["chunks"][:2]] == [
        "冻结chunk-1", "冻结chunk-2"
    ]
    assert payload["chunks"][0]["metadata"]["parser"] == "pdfplumber_page_text"
    assert payload["chunks"][0]["metadata"]["doc_id"] == 1
    layers = {chunk["metadata"]["parser_layer"] for chunk in payload["chunks"]}
    assert layers == {"L1", "L3"}
    l3_metadata = next(
        chunk["metadata"] for chunk in payload["chunks"]
        if chunk["metadata"]["parser_layer"] == "L3"
    )
    assert l3_metadata["artifact_locator"] == "aaaaaaaaaaaa/p0002.json"
    assert "artifact_path" not in l3_metadata
    assert not Path(l3_metadata["artifact_locator"]).is_absolute()
    assert payload["inputs"]["pdf_sha256_by_source"]["report.pdf"] == "a" * 64
    assert len(payload["inputs"]["old_cache_sha256_by_source"]["report.pdf"]) == 64


def test_atomic_write_uses_new_output_without_temp_residue(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "EXPECTED_OLD_CHUNKS", 2)
    payload = builder.build_routed_corpus(*_write_inputs(tmp_path))
    output = tmp_path / "router_v1_routed_corpus.json"

    builder.write_json_atomic(output, payload)

    assert json.loads(output.read_text(encoding="utf-8"))["schema_version"] == builder.ROUTED_SCHEMA
    assert not output.with_suffix(".json.tmp").exists()
