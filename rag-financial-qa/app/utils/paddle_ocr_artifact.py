from __future__ import annotations

import hashlib
import importlib.metadata as package_metadata
import json
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from packaging.utils import canonicalize_name
from pypdf import PdfReader, PdfWriter

from app.utils.paddle_artifact_adapter import ARTIFACT_SCHEMA
from app.utils.table_semantic_context import bind_page_tables, page_blocks


ENGINE_CONFIGURATION = {
    "name": "PP-StructureV3",
    "lang": "ch",
    "use_table_recognition": True,
    "use_doc_orientation_classify": False,
    "use_doc_unwarping": False,
    "use_textline_orientation": False,
    "use_formula_recognition": False,
    "use_seal_recognition": False,
    "use_chart_recognition": False,
}


class PaddleOCRArtifactError(RuntimeError):
    pass


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def artifact_path(root: str | Path, pdf_sha256: str, page_number: int) -> Path:
    if len(pdf_sha256) != 64 or any(char not in "0123456789abcdef" for char in pdf_sha256.lower()):
        raise ValueError("pdf_sha256 must be a 64-character hexadecimal digest")
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    return Path(root) / pdf_sha256[:12] / f"p{page_number:04d}.json"


def portable_artifact_locator(
    target: str | Path,
    *,
    artifact_root: str | Path,
    shared_root: str | Path,
) -> str:
    """Return a portable POSIX object key relative to the shared runtime root."""
    shared = Path(shared_root).resolve()
    artifacts = Path(artifact_root).resolve()
    resolved_target = Path(target).resolve()
    try:
        artifacts.relative_to(shared)
        resolved_target.relative_to(artifacts)
        locator = resolved_target.relative_to(shared)
    except ValueError as exc:
        raise PaddleOCRArtifactError(
            "Paddle artifact paths must remain inside PADDLE_WORKER_SHARED_ROOT"
        ) from exc
    if not locator.parts or ".." in locator.parts:
        raise PaddleOCRArtifactError("Paddle artifact locator is not portable")
    return locator.as_posix()


def write_json_atomic(path: str | Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def _parse_locked_versions(path: str | Path) -> dict[str, str]:
    lock_path = Path(path)
    if not lock_path.is_file():
        raise PaddleOCRArtifactError(f"PaddleOCR lock file not found: {lock_path}")
    versions: dict[str, str] = {}
    for raw_line in lock_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, version = line.split("==", 1)
        normalized = canonicalize_name(name.strip())
        if not normalized or not version.strip() or normalized in versions:
            raise PaddleOCRArtifactError(f"Invalid PaddleOCR lock line: {line}")
        versions[normalized] = version.strip()
    required = {"paddleocr", "paddlex", "paddlepaddle-gpu", "pymupdf"}
    missing = sorted(required - set(versions))
    if missing:
        raise PaddleOCRArtifactError("PaddleOCR lock missing: " + ", ".join(missing))
    return versions


def build_engine_profile(device: str, lock_file: str | Path) -> dict[str, Any]:
    if not device.strip():
        raise ValueError("device must not be empty")
    lock_path = Path(lock_file)
    profile = {
        "configuration": {**ENGINE_CONFIGURATION, "device": device},
        "locked_versions": _parse_locked_versions(lock_path),
        "lock_file_sha256": file_sha256(lock_path),
    }
    canonical = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    profile["configuration_fingerprint"] = hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    return profile


def installed_runtime_versions(distributions: Iterable[str]) -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for distribution in distributions:
        try:
            versions[distribution] = package_metadata.version(distribution)
        except package_metadata.PackageNotFoundError:
            versions[distribution] = None
    return versions


def validate_runtime(profile: dict[str, Any]) -> dict[str, Any]:
    runtime = installed_runtime_versions(profile["locked_versions"])
    mismatches = {
        name: {"locked": version, "runtime": runtime.get(name)}
        for name, version in profile["locked_versions"].items()
        if runtime.get(name) != version
    }
    if mismatches:
        raise PaddleOCRArtifactError(
            "PaddleOCR runtime does not match lock: "
            + json.dumps(mismatches, ensure_ascii=False)
        )
    return {**profile, "runtime_versions": runtime}


def create_engine(profile: dict[str, Any]) -> Any:
    validated = validate_runtime(profile)
    configuration = validated["configuration"]
    try:
        from paddleocr import PPStructureV3

        return PPStructureV3(
            device=configuration["device"],
            lang=configuration["lang"],
            use_table_recognition=True,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            use_formula_recognition=False,
            use_seal_recognition=False,
            use_chart_recognition=False,
        )
    except Exception as exc:
        raise PaddleOCRArtifactError(
            f"PP-StructureV3 import or initialization failed: {exc}"
        ) from exc


def extract_page_as_pdf(source_path: str | Path, page_number: int, output_path: str | Path) -> None:
    reader = PdfReader(str(source_path))
    if page_number < 1 or page_number > len(reader.pages):
        raise PaddleOCRArtifactError(f"physical page out of range: {page_number}")
    writer = PdfWriter()
    writer.add_page(reader.pages[page_number - 1])
    with Path(output_path).open("wb") as output:
        writer.write(output)


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _iter_strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_strings(child)


def table_content_digest(
    pred_html: str,
    ocr_text: str,
    *,
    table_bbox: list[float] | None = None,
    table_order: int | None = None,
    semantic_context: dict[str, Any] | None = None,
) -> str:
    identity: dict[str, Any] = {"pred_html": pred_html, "ocr_text": ocr_text}
    if table_bbox is not None or table_order is not None or semantic_context is not None:
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


def project_tables(payload: dict[str, Any], page_number: int) -> list[dict[str, Any]]:
    tables = payload.get("table_res_list") or []
    if not isinstance(tables, list):
        raise PaddleOCRArtifactError("PP-StructureV3 table_res_list must be an array")
    projected: list[dict[str, Any]] = []
    table_page_blocks = [block for block in page_blocks(payload) if block.label == "table"]
    for index, table in enumerate(tables):
        if not isinstance(table, dict):
            raise PaddleOCRArtifactError("PP-StructureV3 table result must be an object")
        html = table.get("pred_html") if isinstance(table.get("pred_html"), str) else ""
        ocr_text = "\n".join(_iter_strings(table.get("table_ocr_pred") or {}))
        page_block = table_page_blocks[index] if index < len(table_page_blocks) else None
        projected.append({
            "table_index": index,
            "pred_html": html,
            "ocr_text": ocr_text,
            "table_bbox": list(page_block.bbox) if page_block and page_block.bbox else None,
            "table_order": page_block.order if page_block else None,
        })
    semantics = bind_page_tables(payload, projected, page_number)
    for projected_table, semantic in zip(projected, semantics):
        projected_table["semantic_context"] = semantic
        projected_table["table_content_sha256"] = table_content_digest(
            projected_table["pred_html"],
            projected_table["ocr_text"],
            table_bbox=projected_table["table_bbox"],
            table_order=projected_table["table_order"],
            semantic_context=semantic,
        )
    return projected


def result_json_payload(result: Any) -> dict[str, Any]:
    payload = getattr(result, "json", result)
    if callable(payload):
        payload = payload()
    if not isinstance(payload, dict):
        raise PaddleOCRArtifactError("PP-StructureV3 result must expose a JSON object")
    return payload.get("res") if isinstance(payload.get("res"), dict) else payload


def build_completed_artifact(
    *,
    source: str,
    pdf_sha256: str,
    page_number: int,
    reasons: list[str],
    profile: dict[str, Any],
    result_payload: dict[str, Any],
    elapsed_seconds: float,
) -> dict[str, Any]:
    page_index = result_payload.get("page_index")
    page_count = result_payload.get("page_count")
    if page_index != 0 or page_count != 1:
        raise PaddleOCRArtifactError(
            f"single-page mapping mismatch: page_index={page_index}, page_count={page_count}"
        )
    tables = project_tables(result_payload, page_number)
    projected_blocks = [
        {
            "label": block.label,
            "content": block.content,
            "bbox": list(block.bbox) if block.bbox else None,
            "order": block.order,
        }
        for block in page_blocks(result_payload)
    ]
    return {
        "schema_version": ARTIFACT_SCHEMA,
        "status": "completed",
        "source": source,
        "pdf_sha256": pdf_sha256,
        "physical_page_number": page_number,
        "candidate_reasons": sorted(set(reasons)),
        "single_page_result": {
            "page_index": 0,
            "page_count": 1,
            "page_mapping_ok": True,
        },
        "engine": profile,
        "elapsed_seconds": round(elapsed_seconds, 4),
        "table_count": len(tables),
        "page_blocks": projected_blocks,
        "tables": tables,
        "error": None,
    }


def build_failed_artifact(
    *,
    source: str,
    pdf_sha256: str,
    page_number: int,
    reasons: list[str],
    profile: dict[str, Any],
    error: Exception,
    elapsed_seconds: float,
) -> dict[str, Any]:
    return {
        "schema_version": ARTIFACT_SCHEMA,
        "status": "failed",
        "source": source,
        "pdf_sha256": pdf_sha256,
        "physical_page_number": page_number,
        "candidate_reasons": sorted(set(reasons)),
        "single_page_result": {
            "page_index": None,
            "page_count": None,
            "page_mapping_ok": False,
        },
        "engine": profile,
        "elapsed_seconds": round(elapsed_seconds, 4),
        "table_count": 0,
        "tables": [],
        "error": {"type": type(error).__name__, "message": str(error)[:500]},
    }


def run_page_ocr(
    *,
    engine: Any,
    source_path: str | Path,
    source: str,
    pdf_sha256: str,
    page_number: int,
    reasons: list[str],
    profile: dict[str, Any],
    artifact_root: str | Path,
) -> tuple[Path, dict[str, Any]]:
    actual_sha = file_sha256(source_path)
    if actual_sha != pdf_sha256:
        raise PaddleOCRArtifactError("source PDF SHA-256 changed after enqueue")
    target = artifact_path(artifact_root, pdf_sha256, page_number)
    started = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix="paddleocr_page_") as directory:
            page_pdf = Path(directory) / "page.pdf"
            extract_page_as_pdf(source_path, page_number, page_pdf)
            results = list(engine.predict(str(page_pdf)))
        if len(results) != 1:
            raise PaddleOCRArtifactError(
                f"single-page PDF returned {len(results)} page results"
            )
        payload = build_completed_artifact(
            source=source,
            pdf_sha256=pdf_sha256,
            page_number=page_number,
            reasons=reasons,
            profile=profile,
            result_payload=result_json_payload(results[0]),
            elapsed_seconds=time.perf_counter() - started,
        )
    except Exception as exc:
        payload = build_failed_artifact(
            source=source,
            pdf_sha256=pdf_sha256,
            page_number=page_number,
            reasons=reasons,
            profile=profile,
            error=exc,
            elapsed_seconds=time.perf_counter() - started,
        )
    write_json_atomic(target, payload)
    return target, payload
