from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.utils.table_pdf_parser import (
    ParsedBlock,
    assess_table_quality,
    html_table_to_markdown,
)
from app.utils.table_semantic_context import (
    SEMANTIC_SCHEMA,
    semantic_digest_valid,
    semantic_prefix,
)


ARTIFACT_SCHEMA = "paddleocr-table-page-v2"
LEGACY_ARTIFACT_SCHEMA = "paddleocr-table-page-v1"
SUPPORTED_ARTIFACT_SCHEMAS = frozenset({ARTIFACT_SCHEMA, LEGACY_ARTIFACT_SCHEMA})
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class PaddleArtifactValidationError(ValueError):
    """Raised when a page artifact cannot be trusted as parser input."""


@dataclass(frozen=True)
class PaddleArtifactResult:
    status: str
    blocks: list[ParsedBlock] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)


def _valid_fingerprint(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None


def _table_content_digest(
    pred_html: str,
    ocr_text: str,
    *,
    table_bbox: Any = None,
    table_order: Any = None,
    semantic_context: Any = None,
    schema_version: str = ARTIFACT_SCHEMA,
) -> str:
    identity: dict[str, Any] = {"pred_html": pred_html, "ocr_text": ocr_text}
    if schema_version == ARTIFACT_SCHEMA:
        identity.update({
            "table_bbox": table_bbox,
            "table_order": table_order,
            "semantic_context": semantic_context,
        })
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _artifact_locator(pdf_sha256: str, physical_page_number: int) -> str:
    return f"{pdf_sha256[:12]}/p{physical_page_number:04d}.json"


def _artifact_id(pdf_sha256: str, physical_page_number: int) -> str:
    return f"{ARTIFACT_SCHEMA}:{pdf_sha256}:p{physical_page_number:04d}"


def _audit(
    *,
    status: str,
    source: str,
    pdf_sha256: str,
    physical_page_number: int,
    artifact_schema: str = ARTIFACT_SCHEMA,
    artifact_file_sha256: str = "",
    table_count: int = 0,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "artifact_locator": _artifact_locator(pdf_sha256, physical_page_number),
        "artifact_file_sha256": artifact_file_sha256,
        "artifact_id": f"{artifact_schema}:{pdf_sha256}:p{physical_page_number:04d}",
        "artifact_schema_version": artifact_schema,
        "artifact_status": status,
        "source": source,
        "pdf_sha256": pdf_sha256,
        "physical_page_number": physical_page_number,
        "table_count": table_count,
        "reason": reason,
    }


def load_paddle_artifact(
    artifact_path: str | Path,
    *,
    doc_id: int,
    source: str,
    pdf_sha256: str,
    physical_page_number: int,
    engine_fingerprint: str,
) -> PaddleArtifactResult:
    """Validate and adapt one single-page OCR artifact without loading its engine."""
    path = Path(artifact_path)
    if (
        isinstance(physical_page_number, bool)
        or not isinstance(physical_page_number, int)
        or physical_page_number < 1
    ):
        raise ValueError("physical_page_number must be a positive integer")
    if not _valid_fingerprint(engine_fingerprint):
        raise PaddleArtifactValidationError(
            "engine_fingerprint must be a 64-character hexadecimal SHA-256"
        )
    if not path.is_file():
        return PaddleArtifactResult(
            status="missing",
            audit=_audit(
                status="missing",
                source=source,
                pdf_sha256=pdf_sha256,
                physical_page_number=physical_page_number,
                reason="artifact_missing",
            ),
        )

    try:
        artifact_bytes = path.read_bytes()
        artifact_file_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
        payload = json.loads(artifact_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PaddleArtifactValidationError(f"artifact JSON unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise PaddleArtifactValidationError("artifact root must be an object")

    engine = payload.get("engine")
    mapping = payload.get("single_page_result")
    tables = payload.get("tables")
    table_count = payload.get("table_count")
    errors: list[str] = []
    artifact_schema = str(payload.get("schema_version", ""))
    if artifact_schema not in SUPPORTED_ARTIFACT_SCHEMAS:
        errors.append("schema_version")
    if payload.get("status") != "completed":
        errors.append("status")
    if payload.get("source") != source:
        errors.append("source")
    if payload.get("pdf_sha256") != pdf_sha256:
        errors.append("pdf_sha256")
    if payload.get("physical_page_number") != physical_page_number:
        errors.append("physical_page_number")
    if not isinstance(mapping, dict) or not (
        mapping.get("page_index") == 0
        and mapping.get("page_count") == 1
        and mapping.get("page_mapping_ok") is True
    ):
        errors.append("single_page_result")
    if not isinstance(engine, dict) or engine.get("configuration_fingerprint") != engine_fingerprint:
        errors.append("engine_fingerprint")
    if not isinstance(tables, list):
        errors.append("tables")
    if (
        isinstance(table_count, bool)
        or not isinstance(table_count, int)
        or not isinstance(tables, list)
        or table_count != len(tables)
    ):
        errors.append("table_count")
    if payload.get("error") is not None:
        errors.append("error")
    if errors:
        raise PaddleArtifactValidationError(
            "artifact identity/integrity validation failed: " + ", ".join(errors)
        )

    blocks: list[ParsedBlock] = []
    assert isinstance(tables, list)
    for index, table in enumerate(tables):
        if not isinstance(table, dict):
            raise PaddleArtifactValidationError(f"table[{index}] must be an object")
        html = table.get("pred_html")
        ocr_text = table.get("ocr_text")
        digest = table.get("table_content_sha256")
        if table.get("table_index") != index:
            raise PaddleArtifactValidationError(f"table[{index}] index is not contiguous")
        if not isinstance(html, str) or not isinstance(ocr_text, str):
            raise PaddleArtifactValidationError(f"table[{index}] HTML/OCR must be strings")
        semantic = table.get("semantic_context")
        table_bbox = table.get("table_bbox")
        table_order = table.get("table_order")
        expected_digest = _table_content_digest(
            html,
            ocr_text,
            table_bbox=table_bbox,
            table_order=table_order,
            semantic_context=semantic,
            schema_version=artifact_schema,
        )
        if not isinstance(digest, str) or digest != expected_digest:
            raise PaddleArtifactValidationError(f"table[{index}] content digest mismatch")
        if artifact_schema == ARTIFACT_SCHEMA:
            if not isinstance(semantic, dict) or semantic.get("schema_version") != SEMANTIC_SCHEMA:
                raise PaddleArtifactValidationError(f"table[{index}] semantic context invalid")
            if not semantic_digest_valid(semantic):
                raise PaddleArtifactValidationError(f"table[{index}] semantic digest mismatch")
        else:
            semantic = {
                "schema_version": SEMANTIC_SCHEMA,
                "statement_title": "",
                "statement_type": "",
                "table_scope": "",
                "unit_text": "",
                "unit": "",
                "currency": "",
                "statement_period": "",
                "column_bindings": [],
                "binding_source_page": None,
                "binding_method": "legacy_unbound",
                "binding_confidence": "none",
                "continuation_from_page": None,
                "canonical_sha256": "",
            }

        markdown = html_table_to_markdown(html, ocr_text)
        conversion = "html" if markdown and html.strip() else "fallback"
        if not markdown:
            markdown = f"[Table on page {physical_page_number}: no extractable cells]"
            conversion = "placeholder"
        quality = assess_table_quality(html, ocr_text)
        table_number = index + 1
        table_id = f"doc_{doc_id}:page_{physical_page_number}:table_{table_number}"
        provenance_id = (
            f"{table_id}:artifact_{pdf_sha256[:12]}:{engine_fingerprint[:12]}"
        )
        metadata = {
            "source": source,
            "doc_id": doc_id,
            "page_number": physical_page_number,
            "physical_page_number": physical_page_number,
            "content_type": "table",
            "element_type": "Table",
            "parser": f"paddleocr-table-page-{'v2' if artifact_schema == ARTIFACT_SCHEMA else 'v1'}",
            "provenance_id": provenance_id,
            "table_id": table_id,
            "table_index": table_number,
            "artifact_table_index": index,
            "artifact_schema_version": artifact_schema,
            "artifact_status": "completed",
            "artifact_locator": _artifact_locator(pdf_sha256, physical_page_number),
            "artifact_file_sha256": artifact_file_sha256,
            "artifact_id": f"{artifact_schema}:{pdf_sha256}:p{physical_page_number:04d}",
            "pdf_sha256": pdf_sha256,
            "engine_configuration_fingerprint": engine_fingerprint,
            "table_content_sha256": digest,
            "table_html": html,
            "table_markdown": markdown,
            "table_conversion": conversion,
            "table_quality_status": quality["status"],
            "table_quality_score": quality["score"],
            "table_row_count": quality["row_count"],
            "table_column_count": quality["column_count"],
            "table_nonempty_cell_ratio": quality["nonempty_cell_ratio"],
            "single_page_mapping": True,
            "table_bbox": table_bbox or "",
            "table_order": table_order if isinstance(table_order, int) else -1,
            "table_semantic_schema_version": semantic["schema_version"],
            "table_semantic_canonical_sha256": semantic.get("canonical_sha256", ""),
            "statement_title": semantic.get("statement_title", ""),
            "statement_type": semantic.get("statement_type", ""),
            "table_scope": semantic.get("table_scope", ""),
            "unit_text": semantic.get("unit_text", ""),
            "unit": semantic.get("unit", ""),
            "currency": semantic.get("currency", ""),
            "statement_period": semantic.get("statement_period", ""),
            "column_bindings": semantic.get("column_bindings", []),
            "binding_source_page": semantic.get("binding_source_page") or 0,
            "binding_method": semantic.get("binding_method", "unbound"),
            "binding_confidence": semantic.get("binding_confidence", "none"),
            "continuation_from_page": semantic.get("continuation_from_page") or 0,
            "statement_anchor_bbox": semantic.get("statement_anchor_bbox") or "",
            "unit_anchor_bbox": semantic.get("unit_anchor_bbox") or "",
        }
        content = semantic_prefix(source, physical_page_number, table_id, semantic) + f"\n\n{markdown}"
        blocks.append(ParsedBlock(content, metadata))

    status = "completed" if blocks else "no_tables"
    return PaddleArtifactResult(
        status=status,
        blocks=blocks,
        audit=_audit(
            status=status,
            source=source,
            pdf_sha256=pdf_sha256,
            physical_page_number=physical_page_number,
            artifact_schema=artifact_schema,
            artifact_file_sha256=artifact_file_sha256,
            table_count=len(blocks),
            reason="" if blocks else "completed_artifact_without_tables",
        ),
    )


class PaddleArtifactAdapter:
    """Router-compatible reader for precomputed single-page PaddleOCR artifacts."""

    def __init__(
        self,
        artifact_root: str | Path,
        expected_engine_fingerprint: str,
    ) -> None:
        if not _valid_fingerprint(expected_engine_fingerprint):
            raise PaddleArtifactValidationError(
                "expected_engine_fingerprint must be a 64-character hexadecimal SHA-256"
            )
        self.artifact_root = Path(artifact_root)
        self.expected_engine_fingerprint = expected_engine_fingerprint

    def artifact_path(self, pdf_sha256: str, page_number: int) -> Path:
        if not _valid_fingerprint(pdf_sha256):
            raise PaddleArtifactValidationError(
                "pdf_sha256 must be a 64-character hexadecimal SHA-256"
            )
        if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
            raise ValueError("page_number must be a positive integer")
        return self.artifact_root / pdf_sha256[:12] / f"p{page_number:04d}.json"

    def parse_page(
        self,
        file_path: str | Path,
        page_number: int,
        *,
        doc_id: int,
        source: str,
        pdf_sha256: str,
    ) -> PaddleArtifactResult:
        """Load the physical page artifact; ``file_path`` is router compatibility input."""
        del file_path
        path = self.artifact_path(pdf_sha256, page_number)
        return load_paddle_artifact(
            path,
            doc_id=doc_id,
            source=source,
            pdf_sha256=pdf_sha256,
            physical_page_number=page_number,
            engine_fingerprint=self.expected_engine_fingerprint,
        )
