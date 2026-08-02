from __future__ import annotations

import hashlib
import json

import pytest

from app.utils.paddle_artifact_adapter import (
    PaddleArtifactAdapter,
    PaddleArtifactValidationError,
    load_paddle_artifact,
)


PDF_SHA = "a" * 64
ENGINE = "f" * 64


def _digest(html: str, text: str) -> str:
    identity = json.dumps(
        {"pred_html": html, "ocr_text": text},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _artifact(*, tables=None, page=12):
    if tables is None:
        html = (
            "<table><tr><th>指标</th><th>金额</th></tr>"
            "<tr><td>营业收入</td><td>100</td></tr></table>"
        )
        text = "营业收入 100"
        tables = [{
            "table_index": 0,
            "pred_html": html,
            "ocr_text": text,
            "table_content_sha256": _digest(html, text),
        }]
    return {
        "schema_version": "paddleocr-table-page-v1",
        "status": "completed",
        "source": "report.pdf",
        "pdf_sha256": PDF_SHA,
        "physical_page_number": page,
        "single_page_result": {
            "page_index": 0,
            "page_count": 1,
            "page_mapping_ok": True,
        },
        "engine": {"configuration_fingerprint": ENGINE},
        "table_count": len(tables),
        "tables": tables,
        "error": None,
    }


def _load(path, **overrides):
    kwargs = {
        "doc_id": 7,
        "source": "report.pdf",
        "pdf_sha256": PDF_SHA,
        "physical_page_number": 12,
        "engine_fingerprint": ENGINE,
    }
    kwargs.update(overrides)
    return load_paddle_artifact(path, **kwargs)


def test_adapter_has_no_paddle_import():
    import app.utils.paddle_artifact_adapter as adapter

    source = open(adapter.__file__, encoding="utf-8").read()
    assert "import paddle" not in source.lower()
    assert "from paddle" not in source.lower()


def test_valid_artifact_becomes_stable_parsed_block(tmp_path):
    path = tmp_path / "private" / "absolute" / "p0012.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_artifact(), ensure_ascii=False), encoding="utf-8")

    result = _load(path)

    assert result.status == "completed"
    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.metadata["table_id"] == "doc_7:page_12:table_1"
    assert block.metadata["page_number"] == 12
    assert block.metadata["artifact_table_index"] == 0
    assert block.metadata["engine_configuration_fingerprint"] == ENGINE
    assert block.metadata["table_quality_status"] == "good"
    assert block.metadata["artifact_locator"] == f"{PDF_SHA[:12]}/p0012.json"
    assert len(block.metadata["artifact_file_sha256"]) == 64
    assert block.metadata["artifact_id"] == f"paddleocr-table-page-v1:{PDF_SHA}:p0012"
    assert "artifact_path" not in block.metadata
    assert str(tmp_path) not in json.dumps(block.metadata)
    assert "| 营业收入 | 100 |" in block.content
    assert PDF_SHA[:12] in block.metadata["provenance_id"]


def test_missing_artifact_and_completed_no_table_are_auditable(tmp_path):
    missing = _load(tmp_path / "missing.json")
    assert missing.status == "missing"
    assert missing.blocks == []
    assert missing.audit["reason"] == "artifact_missing"
    assert missing.audit["artifact_locator"] == f"{PDF_SHA[:12]}/p0012.json"
    assert missing.audit["artifact_file_sha256"] == ""
    assert "artifact_path" not in missing.audit
    assert str(tmp_path) not in json.dumps(missing.audit)

    path = tmp_path / "empty.json"
    path.write_text(json.dumps(_artifact(tables=[])), encoding="utf-8")
    empty = _load(path)
    assert empty.status == "no_tables"
    assert empty.audit["table_count"] == 0
    assert empty.audit["reason"] == "completed_artifact_without_tables"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda item: item.update(status="failed"), "status"),
        (lambda item: item.update(source="other.pdf"), "source"),
        (lambda item: item.update(pdf_sha256="b" * 64), "pdf_sha256"),
        (lambda item: item.update(physical_page_number=13), "physical_page_number"),
        (
            lambda item: item["single_page_result"].update(page_count=2),
            "single_page_result",
        ),
        (
            lambda item: item["engine"].update(configuration_fingerprint="e" * 64),
            "engine_fingerprint",
        ),
        (lambda item: item["tables"][0].update(table_index=1), "index"),
        (
            lambda item: item["tables"][0].update(table_content_sha256="bad"),
            "digest",
        ),
    ],
)
def test_adapter_rejects_identity_mapping_index_and_digest_errors(
    tmp_path, mutate, message
):
    artifact = _artifact()
    mutate(artifact)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")

    with pytest.raises(PaddleArtifactValidationError, match=message):
        _load(path)


def test_adapter_uses_dynamic_table_count_without_dataset_constants(tmp_path):
    tables = []
    for index in range(3):
        html = f"<table><tr><td>{index}</td></tr></table>"
        text = str(index)
        tables.append({
            "table_index": index,
            "pred_html": html,
            "ocr_text": text,
            "table_content_sha256": _digest(html, text),
        })
    path = tmp_path / "three.json"
    path.write_text(json.dumps(_artifact(tables=tables)), encoding="utf-8")

    result = _load(path)

    assert len(result.blocks) == 3
    assert result.audit["table_count"] == 3
    assert [block.metadata["table_index"] for block in result.blocks] == [1, 2, 3]


def test_router_adapter_derives_artifact_path_with_required_engine_pin(tmp_path):
    adapter = PaddleArtifactAdapter(tmp_path, expected_engine_fingerprint=ENGINE)
    path = tmp_path / PDF_SHA[:12] / "p0012.json"
    path.parent.mkdir()
    path.write_text(json.dumps(_artifact()), encoding="utf-8")

    result = adapter.parse_page(
        "ignored-single-page.pdf",
        12,
        doc_id=7,
        source="report.pdf",
        pdf_sha256=PDF_SHA,
    )

    assert adapter.artifact_path(PDF_SHA, 12) == path
    assert result.status == "completed"
    assert result.blocks[0].metadata["engine_configuration_fingerprint"] == ENGINE


def test_legacy_artifact_is_explicitly_unbound(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(_artifact(), ensure_ascii=False), encoding="utf-8")

    result = _load(path)

    assert result.blocks[0].metadata["artifact_schema_version"] == "paddleocr-table-page-v1"
    assert result.blocks[0].metadata["binding_method"] == "legacy_unbound"
    assert result.blocks[0].metadata["binding_confidence"] == "none"
    assert "Statement:" not in result.blocks[0].content


def test_router_adapter_configured_fingerprint_mismatch_is_rejected(tmp_path):
    adapter = PaddleArtifactAdapter(tmp_path, expected_engine_fingerprint="e" * 64)
    path = adapter.artifact_path(PDF_SHA, 12)
    path.parent.mkdir()
    path.write_text(json.dumps(_artifact()), encoding="utf-8")

    with pytest.raises(PaddleArtifactValidationError, match="engine_fingerprint"):
        adapter.parse_page(
            "page.pdf",
            12,
            doc_id=7,
            source="report.pdf",
            pdf_sha256=PDF_SHA,
        )


def test_router_adapter_requires_valid_configured_engine_pin(tmp_path):
    with pytest.raises(TypeError):
        PaddleArtifactAdapter(tmp_path)
    with pytest.raises(PaddleArtifactValidationError, match="64-character hexadecimal"):
        PaddleArtifactAdapter(tmp_path, expected_engine_fingerprint="not-a-sha256")


def test_router_adapter_rejects_malformed_payload_fingerprint(tmp_path):
    adapter = PaddleArtifactAdapter(tmp_path, expected_engine_fingerprint=ENGINE)
    artifact = _artifact()
    artifact["engine"]["configuration_fingerprint"] = "not-a-sha256"
    path = adapter.artifact_path(PDF_SHA, 12)
    path.parent.mkdir()
    path.write_text(json.dumps(artifact), encoding="utf-8")

    with pytest.raises(PaddleArtifactValidationError, match="engine_fingerprint"):
        adapter.parse_page(
            "page.pdf",
            12,
            doc_id=7,
            source="report.pdf",
            pdf_sha256=PDF_SHA,
        )
