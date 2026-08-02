from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.utils.paddle_artifact_adapter import ARTIFACT_SCHEMA, load_paddle_artifact
from app.utils.paddle_ocr_artifact import (
    artifact_path,
    build_completed_artifact,
    build_engine_profile,
    run_page_ocr,
    table_content_digest,
)


def test_engine_profile_is_deterministic(tmp_path):
    lock = tmp_path / "paddle.lock"
    lock.write_text(
        "paddleocr==3.7.0\npaddlex==3.7.2\npaddlepaddle-gpu==3.3.0\npymupdf==1.26.7\n",
        encoding="utf-8",
    )

    first = build_engine_profile("gpu", lock)
    second = build_engine_profile("gpu", lock)

    assert first == second
    assert len(first["configuration_fingerprint"]) == 64


def test_completed_artifact_is_adapter_compatible(tmp_path):
    pdf_sha = "a" * 64
    profile = {"configuration_fingerprint": "b" * 64}
    artifact = build_completed_artifact(
        source="report.pdf",
        pdf_sha256=pdf_sha,
        page_number=3,
        reasons=["financial_table_title"],
        profile=profile,
        result_payload={
            "page_index": 0,
            "page_count": 1,
            "table_res_list": [{
                "pred_html": "<table><tr><td>营业收入</td><td>100</td></tr></table>",
                "table_ocr_pred": {"rec_texts": ["营业收入", "100"]},
            }],
        },
        elapsed_seconds=1.0,
    )
    target = artifact_path(tmp_path, pdf_sha, 3)
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")

    result = load_paddle_artifact(
        target,
        doc_id=1,
        source="report.pdf",
        pdf_sha256=pdf_sha,
        physical_page_number=3,
        engine_fingerprint="b" * 64,
    )

    assert result.status == "completed"
    assert len(result.blocks) == 1
    assert result.audit["artifact_schema_version"] == ARTIFACT_SCHEMA


def test_completed_artifact_binds_ecommerce_table_context(tmp_path):
    pdf_sha = "a" * 64
    artifact = build_completed_artifact(
        source="catalog.pdf",
        pdf_sha256=pdf_sha,
        page_number=158,
        reasons=["ecommerce_table_title"],
        profile={"configuration_fingerprint": "b" * 64},
        result_payload={
            "page_index": 0,
            "page_count": 1,
            "parsing_res_list": [
                {"block_label": "paragraph_title", "block_content": "Amazon 美国商品价格表 2026-07-15 USD", "block_bbox": [1, 10, 20, 20], "block_order": 1},
                {"block_label": "table", "block_content": "", "block_bbox": [1, 31, 20, 90], "block_order": 2},
            ],
            "table_res_list": [{
                "pred_html": (
                    "<table><tr><th>SKU</th><th>商品</th><th>价格 USD</th></tr>"
                    "<tr><td>SKU-A100</td><td>背包</td><td>79.90</td></tr></table>"
                ),
                "table_ocr_pred": {"rec_texts": ["SKU-A100", "背包", "79.90"]},
            }],
        },
        elapsed_seconds=1.0,
    )

    semantic = artifact["tables"][0]["semantic_context"]
    assert artifact["schema_version"] == ARTIFACT_SCHEMA
    assert semantic["table_type"] == "price"
    assert semantic["platform"] == "Amazon"
    assert semantic["market"] == "美国"
    assert semantic["currency"] == "USD"
    assert semantic["effective_date"] == "2026-07-15"
    assert len(semantic["canonical_sha256"]) == 64


def test_run_page_ocr_writes_failed_artifact_atomically(tmp_path, monkeypatch):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"pdf")
    pdf_sha = hashlib.sha256(b"pdf").hexdigest()

    class Engine:
        def predict(self, path):
            raise RuntimeError("worker failure")

    monkeypatch.setattr(
        "app.utils.paddle_ocr_artifact.extract_page_as_pdf",
        lambda *args, **kwargs: Path(args[2]).write_bytes(b"page"),
    )

    target, payload = run_page_ocr(
        engine=Engine(),
        source_path=source,
        source="report.pdf",
        pdf_sha256=pdf_sha,
        page_number=1,
        reasons=[],
        profile={"configuration_fingerprint": "c" * 64},
        artifact_root=tmp_path / "artifacts",
    )

    assert payload["status"] == "failed"
    assert json.loads(target.read_text(encoding="utf-8"))["error"]["type"] == "RuntimeError"
    assert not target.with_suffix(".json.tmp").exists()


def test_table_digest_matches_adapter_contract():
    digest = table_content_digest("<table></table>", "ocr")
    expected = hashlib.sha256(
        json.dumps(
            {"pred_html": "<table></table>", "ocr_text": "ocr"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert digest == expected
