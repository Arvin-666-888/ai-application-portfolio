from __future__ import annotations

import argparse
import hashlib
import json
import re
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
    PaddleArtifactValidationError,
    load_paddle_artifact,
)
from app.utils.table_pdf_parser import (  # noqa: E402
    MAX_METADATA_BYTES,
    IndexChunk,
    html_table_to_markdown,
    scalarize_metadata,
)
from scripts.atomic_json import write_json_atomic  # noqa: E402
from scripts.audit_paddleocr_candidate_coverage import (  # noqa: E402
    file_sha256,
    load_json,
    validate_candidate_manifest,
    validate_ground_truth,
)
from scripts.compare_table_retrieval import (  # noqa: E402
    normalize_match_text,
    strict_context_hit,
    strict_value_match,
)

DEFAULT_BASE = PROJECT_ROOT / "evals" / "task2_paddleocr"
DEFAULT_CANDIDATES = DEFAULT_BASE / "manifest" / "candidate_pages.json"
DEFAULT_RAW_DIR = DEFAULT_BASE / "raw"
DEFAULT_OCR_SUMMARY = DEFAULT_BASE / "reports" / "ocr_batch_summary.json"
DEFAULT_GROUND_TRUTH = PROJECT_ROOT / "evals" / "table_ground_truth.json"
DEFAULT_OUTPUT = DEFAULT_BASE / "chunks" / "paddle_table_chunks.json"
DEFAULT_SUMMARY = DEFAULT_BASE / "reports" / "table_chunk_summary.json"
DEFAULT_COVERAGE = DEFAULT_BASE / "reports" / "strict_table_chunk_coverage.json"

PAGE_SCHEMA = ARTIFACT_SCHEMA
CHUNK_SCHEMA = "paddleocr-table-chunks-v1"
SUMMARY_SCHEMA = "paddleocr-table-chunk-summary-v1"
COVERAGE_SCHEMA = "strict-table-chunk-coverage-v1"
PARSER_PROFILE = "paddleocr-ppstructurev3-table-v1"
EXPECTED_REPORTS = 5
EXPECTED_PAGES = 400
EXPECTED_TABLES = 601
MIN_STRICT_COVERAGE_CASES = 24
DIAGNOSTIC_LIMIT = 20


class ChunkBuildError(ValueError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="离线构建 PaddleOCR 表格 chunks；不加载 PaddleOCR、不调用模型 API。",
    )
    parser.add_argument("--candidate-manifest", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--ocr-summary", type=Path, default=DEFAULT_OCR_SUMMARY)
    parser.add_argument("--ground-truth", type=Path, default=DEFAULT_GROUND_TRUTH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--coverage-output", type=Path, default=DEFAULT_COVERAGE)
    parser.add_argument("--chunk-size", type=int, default=400)
    parser.add_argument("--table-row-overlap", type=int, default=1)
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args()


def table_content_digest(pred_html: str, ocr_text: str) -> str:
    """Compatibility helper; validation itself is delegated to the adapter."""
    identity = json.dumps(
        {"pred_html": pred_html, "ocr_text": ocr_text},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _manifest_jobs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        validate_candidate_manifest(payload)
    except ValueError as exc:
        raise ChunkBuildError(str(exc)) from exc
    reports = payload["reports"]
    if len(reports) != EXPECTED_REPORTS:
        raise ChunkBuildError(
            f"候选报告数必须为 {EXPECTED_REPORTS}，实际 {len(reports)}"
        )
    jobs = []
    for doc_id, report in enumerate(reports, 1):
        for page in sorted(
            report["selected_pages"],
            key=lambda item: item["page_number"],
        ):
            jobs.append({
                "doc_id": doc_id,
                "source": report["source"],
                "pdf_sha256": report["pdf_sha256"],
                "page_number": int(page["page_number"]),
                "candidate_reasons": sorted(set(page.get("reasons") or [])),
            })
    if len(jobs) != EXPECTED_PAGES:
        raise ChunkBuildError(
            f"候选页必须为 {EXPECTED_PAGES}，实际 {len(jobs)}"
        )
    return jobs


def artifact_path(raw_dir: Path, job: dict[str, Any]) -> Path:
    return (
        raw_dir
        / job["pdf_sha256"][:12]
        / f"p{job['page_number']:04d}.json"
    )


def validate_ocr_summary(
    payload: Any,
    candidate_sha256: str,
) -> str:
    if not isinstance(payload, dict):
        raise ChunkBuildError("OCR summary 不是对象")
    if payload.get("schema_version") != "paddleocr-batch-audit-v1":
        raise ChunkBuildError("OCR summary schema 不受支持")
    if payload.get("status") != "passed":
        raise ChunkBuildError("OCR summary 未通过")
    inputs = payload.get("inputs") or {}
    counts = payload.get("counts") or {}
    if inputs.get("candidate_manifest_sha256") != candidate_sha256:
        raise ChunkBuildError("OCR summary 与候选清单 SHA 不一致")
    required_zero = (
        "failed_pages",
        "missing_pages",
        "stale_pages",
        "unexpected_pages",
        "page_mapping_errors",
    )
    if (
        counts.get("expected_pages") != EXPECTED_PAGES
        or counts.get("completed_pages") != EXPECTED_PAGES
        or any(counts.get(key) != 0 for key in required_zero)
    ):
        raise ChunkBuildError("OCR summary 完整性计数未通过")
    if counts.get("total_tables") != EXPECTED_TABLES:
        raise ChunkBuildError(
            f"OCR 表格数应为 {EXPECTED_TABLES}，实际 {counts.get('total_tables')}"
        )
    fingerprint = inputs.get("engine_configuration_fingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise ChunkBuildError("OCR engine fingerprint 无效")
    return fingerprint


def validate_artifact(
    payload: Any,
    job: dict[str, Any],
    engine_fingerprint: str,
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ChunkBuildError("OCR artifact 不是对象")
    engine = payload.get("engine") or {}
    mapping = payload.get("single_page_result") or {}
    tables = payload.get("tables")
    if (
        payload.get("schema_version") != PAGE_SCHEMA
        or payload.get("status") != "completed"
        or payload.get("source") != job["source"]
        or payload.get("pdf_sha256") != job["pdf_sha256"]
        or payload.get("physical_page_number") != job["page_number"]
        or engine.get("configuration_fingerprint") != engine_fingerprint
        or mapping.get("page_index") != 0
        or mapping.get("page_count") != 1
        or mapping.get("page_mapping_ok") is not True
        or payload.get("error") is not None
        or not isinstance(tables, list)
        or payload.get("table_count") != len(tables)
    ):
        raise ChunkBuildError(
            f"OCR artifact 身份或完整性无效: {job['source']} p{job['page_number']}"
        )
    for index, table in enumerate(tables):
        if not isinstance(table, dict):
            raise ChunkBuildError("OCR table 不是对象")
        html = table.get("pred_html")
        text = table.get("ocr_text")
        if (
            table.get("table_index") != index
            or not isinstance(html, str)
            or not isinstance(text, str)
            or table.get("table_content_sha256")
            != table_content_digest(html, text)
        ):
            raise ChunkBuildError(
                f"OCR table digest/索引无效: {job['source']} "
                f"p{job['page_number']} t{index}"
            )
    return tables


def load_inputs(
    candidate_path: Path,
    raw_dir: Path,
    summary_path: Path,
) -> tuple[list[tuple[dict[str, Any], list[dict[str, Any]]]], dict[str, Any]]:
    candidate = load_json(candidate_path)
    if not isinstance(candidate, dict):
        raise ChunkBuildError("候选清单不是对象")
    jobs = _manifest_jobs(candidate)
    candidate_sha = file_sha256(candidate_path)
    engine_fingerprint = validate_ocr_summary(
        load_json(summary_path),
        candidate_sha,
    )
    expected_paths = {artifact_path(raw_dir, job).resolve() for job in jobs}
    actual_paths = (
        {path.resolve() for path in raw_dir.rglob("*.json")}
        if raw_dir.is_dir()
        else set()
    )
    unexpected = sorted(actual_paths - expected_paths)
    if unexpected:
        raise ChunkBuildError(
            "存在候选清单之外的 OCR artifacts: "
            + ", ".join(str(path) for path in unexpected[:DIAGNOSTIC_LIMIT])
        )

    loaded = []
    table_count = 0
    pages_with_tables = 0
    for job in jobs:
        path = artifact_path(raw_dir, job)
        if not path.is_file():
            raise ChunkBuildError(f"OCR artifact 缺失: {path}")
        try:
            adapted = load_paddle_artifact(
                path,
                doc_id=job["doc_id"],
                source=job["source"],
                pdf_sha256=job["pdf_sha256"],
                physical_page_number=job["page_number"],
                engine_fingerprint=engine_fingerprint,
            )
        except PaddleArtifactValidationError as exc:
            raise ChunkBuildError(
                f"OCR adapter校验失败: {job['source']} p{job['page_number']}: {exc}"
            ) from exc
        payload = load_json(path)
        tables = validate_artifact(payload, job, engine_fingerprint)
        if len(adapted.blocks) != len(tables):
            raise ChunkBuildError(
                f"OCR adapter表格计数不一致: {job['source']} p{job['page_number']}"
            )
        loaded.append((job, tables))
        table_count += len(tables)
        pages_with_tables += int(bool(tables))
    if table_count != EXPECTED_TABLES:
        raise ChunkBuildError(
            f"OCR artifact 表格合计应为 {EXPECTED_TABLES}，实际 {table_count}"
        )
    return loaded, {
        "candidate_manifest_sha256": candidate_sha,
        "engine_configuration_fingerprint": engine_fingerprint,
        "page_count": len(jobs),
        "pages_with_tables": pages_with_tables,
        "pages_without_tables": len(jobs) - pages_with_tables,
        "table_count": table_count,
    }


def _fixed_windows(content: str, hard_limit: int) -> list[str]:
    return [
        content[start:start + hard_limit]
        for start in range(0, len(content), hard_limit)
        if content[start:start + hard_limit]
    ]


def split_table_markdown(
    prefix: str,
    markdown: str,
    *,
    chunk_size: int,
    row_overlap: int,
) -> list[str]:
    soft_limit = max(chunk_size, 600)
    hard_limit = max(chunk_size * 2, 1200)
    lines = [line.rstrip() for line in markdown.splitlines() if line.strip()]
    if len(lines) < 2 or not lines[0].lstrip().startswith("|"):
        return _fixed_windows(f"{prefix}\n\n{markdown}".strip(), hard_limit)
    header = lines[:2]
    rows = lines[2:]
    base = f"{prefix}\n\n" + "\n".join(header)
    if len(base) >= hard_limit:
        return _fixed_windows(f"{prefix}\n\n{markdown}".strip(), hard_limit)
    if not rows:
        return [base]

    chunks: list[str] = []
    current: list[str] = []
    previous_rows: list[str] = []
    for row in rows:
        if len(base) + 1 + len(row) > hard_limit:
            if current:
                chunks.append(base + "\n" + "\n".join(current))
                previous_rows = current[-row_overlap:] if row_overlap else []
                current = []
            available = hard_limit - len(base) - 1
            for start in range(0, len(row), available):
                chunks.append(base + "\n" + row[start:start + available])
            previous_rows = []
            continue

        candidate_rows = current + [row]
        candidate = base + "\n" + "\n".join(candidate_rows)
        if current and len(candidate) > soft_limit:
            chunks.append(base + "\n" + "\n".join(current))
            previous_rows = current[-row_overlap:] if row_overlap else []
            current = list(previous_rows)
            while current and len(
                base + "\n" + "\n".join(current + [row])
            ) > hard_limit:
                current.pop(0)
        current.append(row)
    if current:
        chunks.append(base + "\n" + "\n".join(current))
    if not chunks or not all(0 < len(chunk) <= hard_limit for chunk in chunks):
        raise ChunkBuildError("表格 chunk 超过 hard limit 或为空")
    return chunks


def build_chunks(
    loaded: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    *,
    chunk_size: int,
    row_overlap: int,
    input_fingerprint: str,
) -> tuple[list[IndexChunk], list[dict[str, Any]]]:
    if chunk_size < 100:
        raise ChunkBuildError("chunk_size 必须至少为 100")
    if row_overlap < 0 or row_overlap > 3:
        raise ChunkBuildError("table_row_overlap 必须在 0 到 3 之间")

    chunks: list[IndexChunk] = []
    raw_tables: list[dict[str, Any]] = []
    table_ids = set()
    for job, tables in loaded:
        for raw_index, table in enumerate(tables):
            table_index = raw_index + 1
            table_id = (
                f"doc_{job['doc_id']}:page_{job['page_number']}:table_{table_index}"
            )
            if table_id in table_ids:
                raise ChunkBuildError(f"table_id 重复: {table_id}")
            table_ids.add(table_id)
            markdown = html_table_to_markdown(
                table["pred_html"],
                table["ocr_text"],
            ).strip()
            if not markdown:
                markdown = "[Table: no extractable cells]"
            prefix = f"[Table | source={job['source']} | page={job['page_number']}]"
            contents = split_table_markdown(
                prefix,
                markdown,
                chunk_size=chunk_size,
                row_overlap=row_overlap,
            )
            raw_tables.append({
                "source": job["source"],
                "page_number": job["page_number"],
                "table_id": table_id,
                "content": f"{prefix}\n\n{markdown}",
            })
            for local_index, content in enumerate(contents):
                metadata = scalarize_metadata({
                    "source": job["source"],
                    "doc_id": job["doc_id"],
                    "content_type": "table",
                    "page_number": job["page_number"],
                    "element_type": "PPStructureV3Table",
                    "parser": PARSER_PROFILE,
                    "pdf_sha256": job["pdf_sha256"],
                    "table_id": table_id,
                    "provenance_id": table_id,
                    "table_index": table_index,
                    "raw_table_index": raw_index,
                    "table_chunk_index": local_index,
                    "table_chunk_count": len(contents),
                    "table_content_sha256": table["table_content_sha256"],
                    "engine_configuration_fingerprint": input_fingerprint,
                    "candidate_reasons": job["candidate_reasons"],
                    "table_markdown": markdown,
                    "table_html": table["pred_html"],
                })
                chunks.append(IndexChunk(content.strip(), metadata))
    normalized = []
    for chunk_index, chunk in enumerate(chunks):
        metadata = dict(chunk.metadata)
        metadata["chunk_index"] = chunk_index
        normalized.append(IndexChunk(chunk.content, scalarize_metadata(metadata)))
    return normalized, raw_tables


def _classify_coverage(
    contexts: list[dict[str, Any]],
    case: dict[str, Any],
) -> tuple[str, list[str]]:
    target = [
        context
        for context in contexts
        if Path(str(context.get("source", ""))).name == str(case["pdf"])
        and int(context.get("page_number", 0)) == int(case["expected_page"])
    ]
    strict = [context for context in target if strict_context_hit(context, case)]
    if strict:
        return "strict_target_present", [
            str(context.get("table_id", "")) for context in strict[:5]
        ]
    if not target:
        return "no_target_page_table_chunk", []
    metric_hits = [
        context
        for context in target
        if normalize_match_text(case["metric"])
        in normalize_match_text(context.get("content", ""))
    ]
    value_hits = [
        context
        for context in target
        if strict_value_match(case["expected_value"], context.get("content", ""))
    ]
    if metric_hits and value_hits:
        return "metric_value_split_across_chunks", []
    if metric_hits:
        return "metric_only", []
    if value_hits:
        return "value_only", []
    return "metric_and_value_missing", []


def build_coverage(
    raw_tables: list[dict[str, Any]],
    chunks: list[IndexChunk],
    cases: list[dict[str, Any]],
    *,
    chunk_file_sha256: str,
    ground_truth_sha256: str,
) -> dict[str, Any]:
    chunk_contexts = [
        {"content": chunk.content, **chunk.metadata} for chunk in chunks
    ]
    results = []
    raw_hits = post_hits = 0
    categories = Counter()
    for index, case in enumerate(cases):
        raw_hit = any(strict_context_hit(context, case) for context in raw_tables)
        category, table_ids = _classify_coverage(chunk_contexts, case)
        post_hit = category == "strict_target_present"
        raw_hits += int(raw_hit)
        post_hits += int(post_hit)
        categories[category] += 1
        results.append({
            "case_index": index,
            "source": case["pdf"],
            "page_number": case["expected_page"],
            "raw_same_table_match": raw_hit,
            "post_chunk_classification": category,
            "matching_table_ids": table_ids,
        })
    passed = (
        post_hits >= MIN_STRICT_COVERAGE_CASES
        and post_hits >= raw_hits
    )
    return {
        "schema_version": COVERAGE_SCHEMA,
        "status": "passed" if passed else "failed",
        "inputs": {
            "chunk_file_sha256": chunk_file_sha256,
            "ground_truth_sha256": ground_truth_sha256,
        },
        "definition": (
            "同一表格或chunk同时匹配报告basename、PDF物理页、指标和数值边界"
        ),
        "counts": {
            "ground_truth_cases": len(cases),
            "raw_same_table_covered": raw_hits,
            "post_chunk_strict_covered": post_hits,
            "strict_missing": len(cases) - post_hits,
            "minimum_required": MIN_STRICT_COVERAGE_CASES,
        },
        "classifications": dict(sorted(categories.items())),
        "cases": results,
        "next_step": (
            "表格chunk覆盖门禁通过；可进入Recall@5评测实现。"
            if passed
            else "表格chunk覆盖门禁未通过；停止，不调用Embedding API。"
        ),
    }


def serialize_chunks(
    chunks: list[IndexChunk],
    inputs: dict[str, Any],
    *,
    chunk_size: int,
    row_overlap: int,
) -> dict[str, Any]:
    report_counts: dict[str, Counter[str]] = {}
    for chunk in chunks:
        source = str(chunk.metadata["source"])
        report_counts.setdefault(source, Counter())["chunk_count"] += 1
        report_counts[source]["table_ids"] += 0
    unique_tables_by_report: dict[str, set[str]] = {}
    for chunk in chunks:
        source = str(chunk.metadata["source"])
        unique_tables_by_report.setdefault(source, set()).add(
            str(chunk.metadata["table_id"])
        )
    return {
        "schema_version": CHUNK_SCHEMA,
        "status": "completed",
        "inputs": {
            **inputs,
            "chunk_size": chunk_size,
            "table_row_overlap": row_overlap,
            "parser_profile": PARSER_PROFILE,
        },
        "counts": {
            "report_count": EXPECTED_REPORTS,
            "page_count": inputs["page_count"],
            "pages_with_tables": inputs["pages_with_tables"],
            "pages_without_tables": inputs["pages_without_tables"],
            "table_count": inputs["table_count"],
            "chunk_count": len(chunks),
        },
        "per_report": [
            {
                "source": source,
                "table_count": len(unique_tables_by_report[source]),
                "chunk_count": report_counts[source]["chunk_count"],
            }
            for source in sorted(report_counts)
        ],
        "chunks": [
            {"content": chunk.content, "metadata": chunk.metadata}
            for chunk in chunks
        ],
    }


def validate_built_chunks(
    chunks: list[IndexChunk],
    table_count: int,
    chunk_size: int,
) -> None:
    hard_limit = max(chunk_size * 2, 1200)
    if not chunks:
        raise ChunkBuildError("没有生成任何表格 chunk")
    table_ids = {str(chunk.metadata.get("table_id", "")) for chunk in chunks}
    if len(table_ids) != table_count or "" in table_ids:
        raise ChunkBuildError("源表格与 chunk table_id 数量不一致")
    for index, chunk in enumerate(chunks):
        if not chunk.content or len(chunk.content) > hard_limit:
            raise ChunkBuildError(f"chunk {index} 内容为空或超过 hard limit")
        if chunk.metadata.get("chunk_index") != index:
            raise ChunkBuildError(f"chunk {index} 全局索引不连续")
        if any(isinstance(value, (dict, list, tuple, set)) for value in chunk.metadata.values()):
            raise ChunkBuildError(f"chunk {index} metadata 包含非标量值")
        size = len(
            json.dumps(
                chunk.metadata,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if size > MAX_METADATA_BYTES:
            raise ChunkBuildError(f"chunk {index} metadata 超过16KiB")


def main() -> int:
    args = parse_args()
    try:
        loaded, inputs = load_inputs(
            args.candidate_manifest.resolve(),
            args.raw_dir.resolve(),
            args.ocr_summary.resolve(),
        )
        if args.validate_only:
            print(
                f"[PASSED] validate-only pages={inputs['page_count']} "
                f"tables={inputs['table_count']} "
                f"with_tables={inputs['pages_with_tables']}"
            )
            return 0

        chunks, raw_tables = build_chunks(
            loaded,
            chunk_size=args.chunk_size,
            row_overlap=args.table_row_overlap,
            input_fingerprint=inputs["engine_configuration_fingerprint"],
        )
        validate_built_chunks(chunks, inputs["table_count"], args.chunk_size)
        chunk_payload = serialize_chunks(
            chunks,
            inputs,
            chunk_size=args.chunk_size,
            row_overlap=args.table_row_overlap,
        )
        output = args.output.resolve()
        write_json_atomic(output, chunk_payload)

        cases = validate_ground_truth(load_json(args.ground_truth.resolve()))
        coverage = build_coverage(
            raw_tables,
            chunks,
            cases,
            chunk_file_sha256=file_sha256(output),
            ground_truth_sha256=file_sha256(args.ground_truth.resolve()),
        )
        write_json_atomic(args.coverage_output.resolve(), coverage)
        summary = {
            "schema_version": SUMMARY_SCHEMA,
            "status": coverage["status"],
            "inputs": chunk_payload["inputs"],
            "counts": chunk_payload["counts"],
            "per_report": chunk_payload["per_report"],
            "coverage": coverage["counts"],
            "chunk_file": str(output),
            "coverage_file": str(args.coverage_output.resolve()),
            "next_step": coverage["next_step"],
        }
        write_json_atomic(args.summary.resolve(), summary)
    except (ChunkBuildError, ValueError, json.JSONDecodeError) as exc:
        print(f"[FAILED] {exc}", file=sys.stderr)
        return 1

    print(
        f"[{coverage['status'].upper()}] tables={inputs['table_count']} "
        f"chunks={len(chunks)} raw_coverage={coverage['counts']['raw_same_table_covered']}/30 "
        f"post_chunk={coverage['counts']['post_chunk_strict_covered']}/30"
    )
    return 0 if coverage["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
