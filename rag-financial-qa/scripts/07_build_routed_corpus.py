from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.utils.paddle_artifact_adapter import (  # noqa: E402
    ARTIFACT_SCHEMA,
    PaddleArtifactAdapter,
    PaddleArtifactValidationError,
)
from app.utils.pdf_parse_router import (  # noqa: E402
    PDF_ROUTING_POLICY_VERSION,
    POLICY_FINGERPRINT,
)
from app.utils.table_pdf_parser import ParsedBlock, build_index_chunks  # noqa: E402
from app.utils.text_splitter import RecursiveTextSplitter  # noqa: E402
from scripts.atomic_json import write_json_atomic  # noqa: E402
from scripts.evidence_guard import ensure_evidence_output_writable  # noqa: E402
from scripts.audit_paddleocr_candidate_coverage import (  # noqa: E402
    file_sha256,
    load_json,
    validate_candidate_manifest,
)

DEFAULT_BASE = PROJECT_ROOT / "evals" / "task2_paddleocr"
DEFAULT_INVENTORY = DEFAULT_BASE / "manifest" / "page_inventory.json"
DEFAULT_CANDIDATES = DEFAULT_BASE / "manifest" / "candidate_pages.json"
DEFAULT_RAW_DIR = DEFAULT_BASE / "raw"
DEFAULT_OCR_SUMMARY = DEFAULT_BASE / "reports" / "ocr_batch_summary.json"
DEFAULT_OLD_CACHE_DIR = PROJECT_ROOT / "evals" / "task2_parse_cache"
DEFAULT_OUTPUT = DEFAULT_BASE / "chunks" / "router_v1_routed_corpus.json"
ROUTED_SCHEMA = "router-v1-routed-corpus-v1"
BUILDER_VERSION = "router-v1-routed-corpus-builder-v2"
OLD_CACHE_PROFILE = "legacy-pdfplumber-v1"
EXPECTED_OLD_CHUNKS = 4125
CHUNK_SIZE = 400
CHUNK_OVERLAP = 80
TABLE_ROW_OVERLAP = 1


class RoutedCorpusError(ValueError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="离线构建 router_v1 L1+L3 routed corpus；不加载GT/API/Paddle。",
    )
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--candidate-manifest", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--ocr-summary", type=Path, default=DEFAULT_OCR_SUMMARY)
    parser.add_argument("--old-cache-dir", type=Path, default=DEFAULT_OLD_CACHE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args()


def _canonical_sha256(value: Any) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise RoutedCorpusError(f"{label} SHA-256无效")
    try:
        int(value, 16)
    except ValueError as exc:
        raise RoutedCorpusError(f"{label} SHA-256无效") from exc
    return value.lower()


def _load_inventory(path: Path) -> dict[str, Any]:
    payload = load_json(path)
    if not isinstance(payload, dict):
        raise RoutedCorpusError("inventory不是对象")
    if payload.get("schema_version") != "page-inventory-v1":
        raise RoutedCorpusError("inventory schema无效")
    if payload.get("scan_method") != "pdfplumber-embedded-text-only":
        raise RoutedCorpusError("inventory不是内嵌文本扫描结果")
    reports = payload.get("reports")
    if not isinstance(reports, list) or not reports:
        raise RoutedCorpusError("inventory reports无效")
    if payload.get("report_count") != len(reports):
        raise RoutedCorpusError("inventory report_count不一致")
    return payload


def _load_candidate(path: Path) -> dict[str, Any]:
    payload = load_json(path)
    try:
        validate_candidate_manifest(payload)
    except ValueError as exc:
        raise RoutedCorpusError(str(exc)) from exc
    assert isinstance(payload, dict)
    return payload


def _load_ocr_summary(path: Path, candidate_sha256: str) -> tuple[dict[str, Any], str]:
    payload = load_json(path)
    if not isinstance(payload, dict):
        raise RoutedCorpusError("OCR summary不是对象")
    if payload.get("schema_version") != "paddleocr-batch-audit-v1":
        raise RoutedCorpusError("OCR summary schema无效")
    inputs = payload.get("inputs") or {}
    if inputs.get("candidate_manifest_sha256") != candidate_sha256:
        raise RoutedCorpusError("OCR summary与candidate manifest SHA不一致")
    fingerprint = _require_sha(
        inputs.get("engine_configuration_fingerprint"), "OCR engine fingerprint"
    )
    return payload, fingerprint


def _load_old_cache(
    cache_dir: Path,
    *,
    source: str,
    doc_id: int,
    pdf_sha256: str,
    page_count: int,
) -> tuple[list[dict[str, Any]], str]:
    path = cache_dir / f"{pdf_sha256}.old.chunk-400-overlap-80.json"
    payload = load_json(path)
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("pdf_sha256") != pdf_sha256
        or payload.get("arm") != "old"
    ):
        raise RoutedCorpusError(f"冻结L1 cache身份无效: {path}")
    items = payload.get("chunks")
    if not isinstance(items, list) or not items:
        raise RoutedCorpusError(f"冻结L1 cache chunks无效: {path}")
    normalized = []
    covered_pages: set[int] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise RoutedCorpusError(f"冻结L1 cache chunk不是对象: {source}/{index}")
        content = item.get("content")
        metadata = item.get("metadata")
        if not isinstance(content, str) or not content or not isinstance(metadata, dict):
            raise RoutedCorpusError(f"冻结L1 cache chunk内容无效: {source}/{index}")
        page_number = metadata.get("page_number")
        if (
            Path(str(metadata.get("source", ""))).name != source
            or metadata.get("content_type") != "text"
            or metadata.get("parser") != "pdfplumber_page_text"
            or isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or not 1 <= page_number <= page_count
        ):
            raise RoutedCorpusError(f"冻结L1 cache metadata无效: {source}/{index}")
        copied = dict(metadata)
        covered_pages.add(page_number)
        copied.update({
            "source": source,
            "doc_id": doc_id,
            "pdf_sha256": pdf_sha256,
            "parser_layer": "L1",
            "selected_layer": "L1",
            "route_path": "L1",
            "policy_version": PDF_ROUTING_POLICY_VERSION,
            "policy_fingerprint": POLICY_FINGERPRINT,
            "legacy_cache_chunk_index": index,
        })
        normalized.append({"content": content, "metadata": copied})
    if covered_pages != set(range(1, page_count + 1)):
        missing = sorted(set(range(1, page_count + 1)) - covered_pages)
        raise RoutedCorpusError(
            f"冻结L1 cache未覆盖全部物理页: {source} missing={missing[:20]}"
        )
    return normalized, file_sha256(path)


def _candidate_jobs(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = []
    for doc_id, report in enumerate(candidate["reports"], 1):
        for page in report["selected_pages"]:
            jobs.append({
                "doc_id": doc_id,
                "source": report["source"],
                "pdf_sha256": report["pdf_sha256"],
                "page_number": int(page["page_number"]),
                "candidate_reasons": tuple(page.get("reasons") or ()),
            })
    return jobs


def build_routed_corpus(
    inventory_path: Path,
    candidate_path: Path,
    raw_dir: Path,
    ocr_summary_path: Path,
    old_cache_dir: Path,
) -> dict[str, Any]:
    inventory = _load_inventory(inventory_path)
    candidate = _load_candidate(candidate_path)
    candidate_sha = file_sha256(candidate_path)
    ocr_summary, engine_fingerprint = _load_ocr_summary(
        ocr_summary_path, candidate_sha
    )
    inventory_by_source = {item["source"]: item for item in inventory["reports"]}
    candidate_by_source = {item["source"]: item for item in candidate["reports"]}
    if set(inventory_by_source) != set(candidate_by_source):
        raise RoutedCorpusError("inventory与candidate报告集合不一致")

    policy = candidate.get("selection_policy") or {}
    candidate_policy_version = policy.get("router_policy_version")
    candidate_policy_fingerprint = policy.get("router_policy_fingerprint")
    degraded_reasons: list[dict[str, Any]] = []
    if candidate_policy_version is None or candidate_policy_fingerprint is None:
        degraded_reasons.append({
            "reason": "candidate_router_policy_identity_missing",
            "detail": "legacy candidate manifest; shared router identity recorded as fallback",
        })
    elif (
        candidate_policy_version != PDF_ROUTING_POLICY_VERSION
        or candidate_policy_fingerprint != POLICY_FINGERPRINT
    ):
        raise RoutedCorpusError("candidate router policy identity与共享policy不一致")

    splitter = RecursiveTextSplitter(CHUNK_SIZE, CHUNK_OVERLAP)
    l1_chunks: list[dict[str, Any]] = []
    table_blocks: list[ParsedBlock] = []
    old_cache_shas: dict[str, str] = {}
    pdf_shas: dict[str, str] = {}
    l1_page_count = sum(
        int(report["page_count"]) for report in inventory["reports"]
    )
    for doc_id, source in enumerate(candidate_by_source, 1):
        inventory_report = inventory_by_source[source]
        candidate_report = candidate_by_source[source]
        pdf_sha = _require_sha(inventory_report.get("sha256"), source)
        if candidate_report.get("pdf_sha256") != pdf_sha:
            raise RoutedCorpusError(f"candidate PDF SHA不一致: {source}")
        pdf_shas[source] = pdf_sha
        cached, cache_sha = _load_old_cache(
            old_cache_dir,
            source=source,
            doc_id=doc_id,
            pdf_sha256=pdf_sha,
            page_count=int(inventory_report["page_count"]),
        )
        l1_chunks.extend(cached)
        old_cache_shas[source] = cache_sha
    if len(l1_chunks) != EXPECTED_OLD_CHUNKS:
        raise RoutedCorpusError(
            f"冻结L1 chunks必须为{EXPECTED_OLD_CHUNKS}，实际{len(l1_chunks)}"
        )

    adapter = PaddleArtifactAdapter(
        raw_dir, expected_engine_fingerprint=engine_fingerprint
    )
    missing_reasons: list[dict[str, Any]] = []
    dropped_reasons: list[dict[str, Any]] = []
    l3_pages_with_tables = l3_table_count = 0
    for job in _candidate_jobs(candidate):
        try:
            result = adapter.parse_page(
                "offline-artifact-only",
                job["page_number"],
                doc_id=job["doc_id"],
                source=job["source"],
                pdf_sha256=job["pdf_sha256"],
            )
        except PaddleArtifactValidationError as exc:
            dropped_reasons.append({
                "source": job["source"],
                "page_number": job["page_number"],
                "reason": "l3_artifact_invalid",
                "detail": str(exc)[:300],
            })
            continue
        if result.status == "missing":
            missing_reasons.append({
                "source": job["source"],
                "page_number": job["page_number"],
                "reason": "l3_artifact_missing",
            })
            continue
        if not result.blocks:
            degraded_reasons.append({
                "source": job["source"],
                "page_number": job["page_number"],
                "reason": "l3_completed_without_tables",
            })
            continue
        l3_pages_with_tables += 1
        l3_table_count += len(result.blocks)
        for block in result.blocks:
            metadata = dict(block.metadata)
            expected_artifact = adapter.artifact_path(
                job["pdf_sha256"], job["page_number"]
            ).resolve()
            recorded_artifact = str(metadata.get("artifact_path", ""))
            if recorded_artifact and Path(recorded_artifact).resolve() != expected_artifact:
                raise RoutedCorpusError(
                    f"L3 artifact locator身份不一致: {job['source']} p{job['page_number']}"
                )
            try:
                artifact_locator = expected_artifact.relative_to(raw_dir.resolve()).as_posix()
            except ValueError as exc:
                raise RoutedCorpusError(
                    f"L3 artifact不在raw目录内: {job['source']} p{job['page_number']}"
                ) from exc
            if artifact_locator.startswith("../") or Path(artifact_locator).is_absolute():
                raise RoutedCorpusError("L3 artifact locator必须是raw目录内相对路径")
            metadata.pop("artifact_path", None)
            metadata.update({
                "artifact_locator": artifact_locator,
                "parser_layer": "L3",
                "selected_layer": "L3",
                "route_path": "L1->L3",
                "candidate_reasons": job["candidate_reasons"],
                "policy_version": PDF_ROUTING_POLICY_VERSION,
                "policy_fingerprint": POLICY_FINGERPRINT,
            })
            table_blocks.append(ParsedBlock(block.content, metadata))

    l3_chunks = build_index_chunks(
        table_blocks, splitter, table_row_overlap=TABLE_ROW_OVERLAP
    )
    serialized = list(l1_chunks)
    for chunk in l3_chunks:
        serialized.append({"content": chunk.content, "metadata": dict(chunk.metadata)})
    layer_counts: Counter[str] = Counter()
    for index, item in enumerate(serialized):
        item["metadata"]["chunk_index"] = index
        layer_counts[str(item["metadata"].get("parser_layer", ""))] += 1

    candidate_jobs = _candidate_jobs(candidate)
    counts = {
        "report_count": len(candidate_by_source),
        "inventory_page_count": sum(
            int(report["page_count"]) for report in inventory["reports"]
        ),
        "l1_page_count": l1_page_count,
        "l1_nonempty_page_count": l1_page_count,
        "candidate_page_count": len(candidate_jobs),
        "l3_pages_with_tables": l3_pages_with_tables,
        "l3_table_count": l3_table_count,
        "l1_chunk_count": layer_counts["L1"],
        "l3_chunk_count": layer_counts["L3"],
        "chunk_count": len(serialized),
        "degraded_reason_count": len(degraded_reasons),
        "missing_reason_count": len(missing_reasons),
        "dropped_reason_count": len(dropped_reasons),
    }
    if counts["l1_page_count"] != counts["inventory_page_count"]:
        raise RoutedCorpusError("L1页面数与inventory不一致")
    l1_identity = [
        {
            "content": item["content"],
            "metadata": {
                key: item["metadata"].get(key)
                for key in (
                    "source", "doc_id", "page_number", "content_type", "parser"
                )
            },
        }
        for item in serialized
        if item["metadata"].get("parser_layer") == "L1"
    ]
    l3_identity = [
        {"content": item["content"], "metadata": item["metadata"]}
        for item in serialized
        if item["metadata"].get("parser_layer") == "L3"
    ]
    return {
        "schema_version": ROUTED_SCHEMA,
        "builder_version": BUILDER_VERSION,
        "l1_corpus_sha256": _canonical_sha256(l1_identity),
        "l3_corpus_sha256": _canonical_sha256(l3_identity),
        "status": "degraded" if any((degraded_reasons, missing_reasons, dropped_reasons)) else "completed",
        "ground_truth_loaded": False,
        "api_called": False,
        "paddle_imported": False,
        "routing": {
            "policy_version": PDF_ROUTING_POLICY_VERSION,
            "policy_fingerprint": POLICY_FINGERPRINT,
            "candidate_policy_version": candidate_policy_version,
            "candidate_policy_fingerprint": candidate_policy_fingerprint,
            "layers": {"L1": OLD_CACHE_PROFILE, "L3": ARTIFACT_SCHEMA},
            "table_row_overlap": TABLE_ROW_OVERLAP,
            "chunk_size": CHUNK_SIZE,
            "chunk_overlap": CHUNK_OVERLAP,
        },
        "inputs": {
            "inventory_schema": inventory["schema_version"],
            "candidate_schema": candidate["schema_version"],
            "ocr_summary_schema": ocr_summary["schema_version"],
            "inventory_sha256": file_sha256(inventory_path),
            "candidate_manifest_sha256": candidate_sha,
            "ocr_summary_sha256": file_sha256(ocr_summary_path),
            "engine_configuration_fingerprint": engine_fingerprint,
            "pdf_sha256_by_source": pdf_shas,
            "old_cache_sha256_by_source": old_cache_shas,
        },
        "counts": counts,
        "degraded_reasons": degraded_reasons,
        "missing_reasons": missing_reasons,
        "dropped_reasons": dropped_reasons,
        "chunks": serialized,
    }


def main() -> int:
    args = parse_args()
    try:
        payload = build_routed_corpus(
            args.inventory.resolve(),
            args.candidate_manifest.resolve(),
            args.raw_dir.resolve(),
            args.ocr_summary.resolve(),
            args.old_cache_dir.resolve(),
        )
        if not args.validate_only:
            ensure_evidence_output_writable(
                args.output.resolve(), project_root=PROJECT_ROOT, force=False,
            )
            write_json_atomic(args.output.resolve(), payload, overwrite=False)
    except (RoutedCorpusError, ValueError, json.JSONDecodeError) as exc:
        print(f"[FAILED] {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "status": "VALIDATED" if args.validate_only else payload["status"].upper(),
        "output_written": not args.validate_only,
        "ground_truth_loaded": False,
        "api_called": False,
        "counts": payload["counts"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
