from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings  # noqa: E402
from app.utils.table_pdf_parser import IndexChunk, scalarize_metadata  # noqa: E402
from scripts.atomic_json import write_json_atomic  # noqa: E402
from scripts.evidence_guard import ensure_evidence_output_writable  # noqa: E402
from scripts.audit_paddleocr_candidate_coverage import file_sha256, load_json  # noqa: E402
from scripts.compare_table_retrieval import (  # noqa: E402
    EvaluationBlocked,
    _load_chunk_cache,
    calculate_improvement,
    load_ground_truth,
    score_case,
    serialize_context,
)

DEFAULT_OLD_CACHE_DIR = PROJECT_ROOT / "evals" / "task2_parse_cache"
DEFAULT_PADDLE_CHUNKS = (
    PROJECT_ROOT
    / "evals"
    / "task2_paddleocr"
    / "chunks"
    / "paddle_table_chunks.json"
)
DEFAULT_ROUTED_CORPUS = None
DEFAULT_CHUNK_SUMMARY = (
    PROJECT_ROOT
    / "evals"
    / "task2_paddleocr"
    / "reports"
    / "table_chunk_summary.json"
)
DEFAULT_STRICT_COVERAGE = (
    PROJECT_ROOT
    / "evals"
    / "task2_paddleocr"
    / "reports"
    / "strict_table_chunk_coverage.json"
)
DEFAULT_GROUND_TRUTH = PROJECT_ROOT / "evals" / "table_ground_truth.json"
DEFAULT_EMBEDDING_CACHE = (
    PROJECT_ROOT / "evals" / "task2_paddleocr" / "embedding_cache"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "evals"
    / "task2_paddleocr"
    / "reports"
    / "paddle_retrieval_evaluation.json"
)
DEFAULT_QUESTIONS = (
    PROJECT_ROOT / "evals" / "task2_paddleocr" / "development_questions.jsonl"
)
DEFAULT_CANDIDATES_OUTPUT = (
    PROJECT_ROOT
    / "evals"
    / "task2_paddleocr"
    / "reports"
    / "retrieval_v1_candidates.json"
)

PADDLE_CHUNK_SCHEMA = "paddleocr-table-chunks-v1"
ROUTED_CORPUS_SCHEMA = "router-v1-routed-corpus-v1"
CACHE_MANIFEST_SCHEMA = "paddleocr-embedding-cache-manifest-v1"
CACHE_ITEM_SCHEMA = "paddleocr-embedding-cache-item-v1"
RESULT_SCHEMA = "paddleocr-retrieval-evaluation-v1"
CANDIDATE_SCHEMA = "paddleocr-retrieval-candidates-v1"
OLD_PROFILE = "legacy-pdfplumber-v1"
FORBIDDEN_QUERY_FIELDS = frozenset({"pdf", "metric", "expected_value", "expected_page", "table_id"})
EXPECTED_REPORTS = 5
EXPECTED_CASES = 30
EXPECTED_OLD_CHUNKS = 4125
EXPECTED_PADDLE_CHUNKS = 1167
EXPECTED_TABLES = 601


class RetrievalInputError(EvaluationBlocked):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="公平比较全量文本基线与文本+PaddleOCR表格增强的 Recall@5。",
    )
    parser.add_argument("--ground-truth", type=Path, default=DEFAULT_GROUND_TRUTH)
    parser.add_argument("--old-cache-dir", type=Path, default=DEFAULT_OLD_CACHE_DIR)
    parser.add_argument("--paddle-chunks", type=Path, default=DEFAULT_PADDLE_CHUNKS)
    parser.add_argument(
        "--routed-corpus",
        type=Path,
        default=DEFAULT_ROUTED_CORPUS,
        help="可选router_v1 routed corpus；提供后替代旧文本+Paddle chunks增强语料。",
    )
    parser.add_argument("--chunk-summary", type=Path, default=DEFAULT_CHUNK_SUMMARY)
    parser.add_argument("--strict-coverage", type=Path, default=DEFAULT_STRICT_COVERAGE)
    parser.add_argument("--embedding-cache-dir", type=Path, default=DEFAULT_EMBEDDING_CACHE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--candidate-k", type=int, default=20)
    parser.add_argument("--numeric-weight", type=float, default=0.15)
    parser.add_argument("--embedding-batch-size", type=int, default=20)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--cache-only", action="store_true")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--candidates-output", type=Path, default=DEFAULT_CANDIDATES_OUTPUT)
    parser.add_argument(
        "--force",
        action="store_true",
        help="允许覆盖显式指定的非canonical输出；默认canonical路径永不允许覆盖。",
    )
    parser.add_argument("--dense-k", type=int, default=100)
    parser.add_argument("--lexical-k", type=int, default=100)
    parser.add_argument(
        "--retrieval-profile",
        choices=("legacy", "financial_v2"),
        default="legacy",
    )
    return parser.parse_args()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _same_path(left: Path, right: Path) -> bool:
    return left.resolve() == right.resolve()


def ensure_output_writable(
    path: Path,
    *,
    force: bool,
    canonical_paths: tuple[Path, ...],
) -> None:
    try:
        ensure_evidence_output_writable(path, project_root=PROJECT_ROOT, force=force)
    except FileExistsError as exc:
        raise RetrievalInputError(str(exc)) from exc
    target = path.resolve()
    if not target.exists():
        return
    if any(_same_path(target, canonical) for canonical in canonical_paths):
        raise RetrievalInputError(
            f"canonical输出已存在，禁止覆盖（即使使用--force）: {target}"
        )
    if not force:
        raise RetrievalInputError(f"输出已存在；如确认覆盖非canonical路径请使用--force: {target}")


def write_output_artifact(
    path: Path,
    payload: dict[str, Any],
    *,
    force: bool,
    canonical_paths: tuple[Path, ...],
) -> str:
    target = path.resolve()
    ensure_output_writable(target, force=force, canonical_paths=canonical_paths)
    is_canonical = any(_same_path(target, canonical) for canonical in canonical_paths)
    try:
        write_json_atomic(target, payload, overwrite=force and not is_canonical)
    except FileExistsError as exc:
        raise RetrievalInputError(f"输出在写入期间已出现，拒绝覆盖: {target}") from exc
    return file_sha256(target)


def candidate_ranking_identity(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    identity = []
    for case in cases:
        item = {
            "case_id": case.get("case_id"),
            "baseline": [candidate.get("candidate_id") for candidate in case["baseline"]["fusion"]],
            "paddle": [candidate.get("candidate_id") for candidate in case["paddle"]["fusion"]],
        }
        for arm in ("baseline_v2", "paddle_v2"):
            if arm in case:
                item[arm] = [
                    candidate.get("candidate_id") for candidate in case[arm]["ranking"]
                ]
        identity.append(item)
    return identity


def _is_absolute_filesystem_path(value: str) -> bool:
    return Path(value).is_absolute() or (
        len(value) >= 3 and value[1] == ":" and value[2] in {"/", "\\"}
    )


def _stable_candidate_value(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {
            item_key: _stable_candidate_value(item_value, item_key)
            for item_key, item_value in value.items()
            if item_key not in {
                "runtime",
                "runtime_seconds",
                "timestamp",
                "created_at",
                "updated_at",
            }
            and not item_key.endswith("_path")
        }
    if isinstance(value, list):
        return [_stable_candidate_value(item) for item in value]
    if isinstance(value, str) and _is_absolute_filesystem_path(value):
        if key == "source":
            return Path(value.replace("\\", "/")).name
        return None
    return value


def candidate_canonical_identity(payload: dict[str, Any]) -> dict[str, Any]:
    """Return logical content without runtime, timestamps, or absolute paths."""
    inputs = payload.get("inputs") or {}
    stable_input_fields = (
        "questions_sha256",
        "paddle_chunks_sha256",
        "baseline_corpus_sha256",
        "paddle_corpus_sha256",
        "routed_corpus_sha256",
        "config_sha256",
        "candidate_cache_identity",
    )
    return {
        "schema_version": payload.get("schema_version"),
        "status": payload.get("status"),
        "ground_truth_loaded": payload.get("ground_truth_loaded"),
        "api_called": payload.get("api_called"),
        "inputs": {key: inputs.get(key) for key in stable_input_fields},
        "configuration": _stable_candidate_value(payload.get("configuration")),
        "embedding_cache": _stable_candidate_value(payload.get("embedding_cache")),
        "ranking_sha256": payload.get("ranking_sha256"),
        "cases": _stable_candidate_value(payload.get("cases")),
    }


def candidate_canonical_sha256(payload: dict[str, Any]) -> str:
    return canonical_sha256(candidate_canonical_identity(payload))


def candidate_cache_identity(payload: dict[str, Any]) -> str:
    inputs = payload.get("inputs") or {}
    augmented_corpus_sha = (
        inputs.get("routed_corpus_sha256")
        if inputs.get("routed_corpus_sha256") is not None
        else inputs.get("paddle_chunks_sha256")
    )
    return canonical_sha256({
        "questions_sha256": inputs.get("questions_sha256"),
        "configuration_sha256": inputs.get("config_sha256"),
        "baseline_corpus_sha256": inputs.get("baseline_corpus_sha256"),
        "augmented_corpus_sha256": augmented_corpus_sha,
    })


def attach_candidate_identity(payload: dict[str, Any]) -> dict[str, Any]:
    payload["candidate_canonical_sha256"] = candidate_canonical_sha256(payload)
    return payload


def load_query_only_cases(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise RetrievalInputError(f"query-only questions不存在: {path}")
    cases = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RetrievalInputError(f"questions第{line_number}行不是有效JSON") from exc
        if not isinstance(item, dict):
            raise RetrievalInputError(f"questions第{line_number}行不是对象")
        forbidden = FORBIDDEN_QUERY_FIELDS & set(item)
        if forbidden:
            raise RetrievalInputError(
                f"query-only输入包含标签字段: {', '.join(sorted(forbidden))}"
            )
        if set(item) != {"case_id", "question"}:
            raise RetrievalInputError("query-only schema只允许case_id和question")
        case_id = str(item["case_id"]).strip()
        question = str(item["question"]).strip()
        if not case_id or not question:
            raise RetrievalInputError("case_id和question不能为空")
        cases.append({"case_id": case_id, "question": question})
    if len(cases) != EXPECTED_CASES:
        raise RetrievalInputError(f"questions必须为{EXPECTED_CASES}条，实际{len(cases)}")
    if len({item["case_id"] for item in cases}) != len(cases):
        raise RetrievalInputError("questions case_id重复")
    return cases


def validate_ranking_config(
    top_k: int,
    candidate_k: int,
    numeric_weight: float,
    batch_size: int,
) -> None:
    if top_k != 5:
        raise RetrievalInputError("正式验收固定 top_k=5")
    if candidate_k < top_k:
        raise RetrievalInputError("candidate_k 必须大于等于 top_k")
    if not 0 <= numeric_weight <= 1 - settings.LEXICAL_WEIGHT:
        raise RetrievalInputError("numeric_weight 与 lexical weight 之和不能超过1")
    if batch_size < 1:
        raise RetrievalInputError("embedding_batch_size 必须大于0")


def _validate_ground_truth(path: Path) -> list[dict[str, Any]]:
    cases = load_ground_truth(path)
    if len(cases) != EXPECTED_CASES:
        raise RetrievalInputError(
            f"ground truth 必须为 {EXPECTED_CASES} 条，实际 {len(cases)}"
        )
    reports = Counter(str(case["pdf"]) for case in cases)
    if len(reports) != EXPECTED_REPORTS or any(count != 6 for count in reports.values()):
        raise RetrievalInputError("ground truth 必须覆盖5份报告且每份6条")
    return cases


def validate_paddle_artifacts(
    chunk_path: Path,
    summary_path: Path,
    coverage_path: Path | None,
    ground_truth_path: Path | None,
) -> tuple[list[IndexChunk], dict[str, Any]]:
    payload = load_json(chunk_path)
    summary = load_json(summary_path)
    coverage = load_json(coverage_path) if coverage_path is not None else None
    if not isinstance(payload, dict) or payload.get("schema_version") != PADDLE_CHUNK_SCHEMA:
        raise RetrievalInputError("Paddle chunk artifact schema 无效")
    if payload.get("status") != "completed":
        raise RetrievalInputError("Paddle chunk artifact 未完成")
    chunks_raw = payload.get("chunks")
    if not isinstance(chunks_raw, list) or len(chunks_raw) != EXPECTED_PADDLE_CHUNKS:
        raise RetrievalInputError(
            f"Paddle chunk 数必须为 {EXPECTED_PADDLE_CHUNKS}"
        )
    counts = payload.get("counts") or {}
    if (
        counts.get("report_count") != EXPECTED_REPORTS
        or counts.get("table_count") != EXPECTED_TABLES
        or counts.get("chunk_count") != len(chunks_raw)
    ):
        raise RetrievalInputError("Paddle chunk counts 与内容不一致")

    chunks = []
    table_ids = set()
    source_doc_ids: dict[str, int] = {}
    source_hashes: dict[str, str] = {}
    for index, item in enumerate(chunks_raw):
        if not isinstance(item, dict):
            raise RetrievalInputError(f"Paddle chunk {index} 不是对象")
        content = item.get("content")
        metadata = item.get("metadata")
        if not isinstance(content, str) or not content or not isinstance(metadata, dict):
            raise RetrievalInputError(f"Paddle chunk {index} 内容或metadata无效")
        if (
            metadata.get("chunk_index") != index
            or metadata.get("content_type") != "table"
            or metadata.get("parser") != "paddleocr-ppstructurev3-table-v1"
            or any(isinstance(value, (dict, list, tuple, set)) for value in metadata.values())
        ):
            raise RetrievalInputError(f"Paddle chunk {index} metadata无效")
        source = str(metadata.get("source", ""))
        doc_id = metadata.get("doc_id")
        page = metadata.get("page_number")
        digest = str(metadata.get("pdf_sha256", ""))
        table_id = str(metadata.get("table_id", ""))
        if (
            not source
            or isinstance(doc_id, bool)
            or not isinstance(doc_id, int)
            or isinstance(page, bool)
            or not isinstance(page, int)
            or page < 1
            or len(digest) != 64
            or not table_id
        ):
            raise RetrievalInputError(f"Paddle chunk {index} identity无效")
        if source in source_doc_ids and source_doc_ids[source] != doc_id:
            raise RetrievalInputError(f"Paddle source doc_id不一致: {source}")
        if source in source_hashes and source_hashes[source] != digest:
            raise RetrievalInputError(f"Paddle source SHA不一致: {source}")
        source_doc_ids[source] = doc_id
        source_hashes[source] = digest
        table_ids.add(table_id)
        normalized = dict(metadata)
        normalized["artifact_chunk_index"] = index
        chunks.append(IndexChunk(content, scalarize_metadata(normalized)))
    if (
        len(source_doc_ids) != EXPECTED_REPORTS
        or set(source_doc_ids.values()) != set(range(1, EXPECTED_REPORTS + 1))
        or len(table_ids) != EXPECTED_TABLES
    ):
        raise RetrievalInputError("Paddle报告/doc_id/table_id集合无效")

    chunk_sha = file_sha256(chunk_path)
    if not isinstance(summary, dict) or summary.get("status") != "passed":
        raise RetrievalInputError("table chunk summary 未通过")
    if summary.get("counts") != counts or summary.get("per_report") != payload.get("per_report"):
        raise RetrievalInputError("table chunk summary 与artifact不一致")

    strict_coverage_sha256 = None
    if coverage_path is not None or ground_truth_path is not None:
        if coverage_path is None or ground_truth_path is None:
            raise RetrievalInputError("coverage与ground truth必须同时提供")
        ground_sha = file_sha256(ground_truth_path)
        if not isinstance(coverage, dict) or coverage.get("status") != "passed":
            raise RetrievalInputError("strict coverage 未通过")
        coverage_inputs = coverage.get("inputs") or {}
        coverage_counts = coverage.get("counts") or {}
        if (
            coverage_inputs.get("chunk_file_sha256") != chunk_sha
            or coverage_inputs.get("ground_truth_sha256") != ground_sha
            or coverage_counts.get("ground_truth_cases") != EXPECTED_CASES
            or coverage_counts.get("post_chunk_strict_covered", 0)
            < coverage_counts.get("minimum_required", 10**9)
            or coverage_counts.get("post_chunk_strict_covered", 0)
            < coverage_counts.get("raw_same_table_covered", 10**9)
            or coverage_counts.get("strict_missing")
            != EXPECTED_CASES - coverage_counts.get("post_chunk_strict_covered", 0)
        ):
            raise RetrievalInputError("strict coverage hash或计数链无效")
        strict_coverage_sha256 = file_sha256(coverage_path)
    return chunks, {
        "source_doc_ids": source_doc_ids,
        "source_hashes": source_hashes,
        "chunk_sha256": chunk_sha,
        "chunk_summary_sha256": file_sha256(summary_path),
        "strict_coverage_sha256": strict_coverage_sha256,
        "table_count": len(table_ids),
    }


def load_old_caches(
    cache_dir: Path,
    source_doc_ids: dict[str, int],
    source_hashes: dict[str, str],
) -> dict[str, list[IndexChunk]]:
    reports: dict[str, list[IndexChunk]] = {}
    for source, doc_id in sorted(source_doc_ids.items(), key=lambda item: item[1]):
        digest = source_hashes[source]
        path = cache_dir / f"{digest}.old.chunk-400-overlap-80.json"
        chunks = _load_chunk_cache(path, digest, "old", OLD_PROFILE)
        if chunks is None:
            raise RetrievalInputError(f"旧文本cache缺失或无效，禁止重解析PDF: {path}")
        normalized = []
        for index, chunk in enumerate(chunks):
            if not isinstance(chunk.content, str) or not chunk.content.strip():
                raise RetrievalInputError(f"旧cache存在空chunk: {source}/{index}")
            metadata = dict(chunk.metadata)
            if (
                Path(str(metadata.get("source", ""))).name != source
                or metadata.get("content_type") != "text"
                or metadata.get("parser") != "pdfplumber_page_text"
                or isinstance(metadata.get("page_number"), bool)
                or not isinstance(metadata.get("page_number"), int)
                or metadata.get("page_number") < 1
            ):
                raise RetrievalInputError(f"旧cache metadata无效: {source}/{index}")
            metadata["doc_id"] = doc_id
            metadata["legacy_cache_chunk_index"] = index
            normalized.append(IndexChunk(chunk.content, scalarize_metadata(metadata)))
        reports[source] = normalized
    total = sum(len(chunks) for chunks in reports.values())
    if len(reports) != EXPECTED_REPORTS or total != EXPECTED_OLD_CHUNKS:
        raise RetrievalInputError(
            f"旧文本chunks必须为 {EXPECTED_OLD_CHUNKS}，实际 {total}"
        )
    return reports


def group_paddle_chunks(
    chunks: list[IndexChunk],
    sources: set[str],
) -> dict[str, list[IndexChunk]]:
    grouped = {source: [] for source in sources}
    for chunk in chunks:
        source = str(chunk.metadata.get("source", ""))
        if source not in grouped:
            raise RetrievalInputError(f"Paddle chunk出现未知报告: {source}")
        grouped[source].append(chunk)
    return grouped


def _l1_identity(records: list[IndexChunk]) -> list[dict[str, Any]]:
    return [
        {
            "content": record.content,
            "metadata": {
                key: record.metadata.get(key)
                for key in (
                    "source", "doc_id", "page_number", "content_type", "parser"
                )
            },
        }
        for record in records
    ]


def _layer_identity(records: list[IndexChunk]) -> list[dict[str, Any]]:
    return [
        {
            "content": record.content,
            "metadata": {
                key: value for key, value in record.metadata.items()
                if key != "artifact_chunk_index"
            },
        }
        for record in records
    ]


def load_routed_corpus(
    path: Path,
    source_doc_ids: dict[str, int],
    source_hashes: dict[str, str],
    baseline_by_source: dict[str, list[IndexChunk]],
) -> tuple[dict[str, list[IndexChunk]], dict[str, Any]]:
    payload = load_json(path)
    if not isinstance(payload, dict) or payload.get("schema_version") != ROUTED_CORPUS_SCHEMA:
        raise RetrievalInputError("routed corpus schema无效")
    if payload.get("builder_version") != "router-v1-routed-corpus-builder-v2":
        raise RetrievalInputError("routed corpus builder_version无效，拒绝旧artifact")
    declared_l1_sha = payload.get("l1_corpus_sha256")
    declared_l3_sha = payload.get("l3_corpus_sha256")
    if not all(isinstance(value, str) and len(value) == 64 for value in (declared_l1_sha, declared_l3_sha)):
        raise RetrievalInputError("routed corpus layer SHA无效")
    if payload.get("status") not in {"completed", "degraded"}:
        raise RetrievalInputError("routed corpus状态无效")
    if payload.get("ground_truth_loaded") is not False or payload.get("api_called") is not False:
        raise RetrievalInputError("routed corpus必须声明未加载GT且未调用API")
    routing = payload.get("routing") or {}
    policy_version = routing.get("policy_version")
    policy_fingerprint = routing.get("policy_fingerprint")
    if not isinstance(policy_version, str) or not policy_version:
        raise RetrievalInputError("routed policy version无效")
    if not isinstance(policy_fingerprint, str) or len(policy_fingerprint) != 64:
        raise RetrievalInputError("routed policy fingerprint无效")
    chunks_raw = payload.get("chunks")
    if not isinstance(chunks_raw, list) or not chunks_raw:
        raise RetrievalInputError("routed corpus chunks无效")
    grouped = {source: [] for source in source_doc_ids}
    for index, item in enumerate(chunks_raw):
        if not isinstance(item, dict):
            raise RetrievalInputError(f"routed chunk {index}不是对象")
        content = item.get("content")
        metadata = item.get("metadata")
        if not isinstance(content, str) or not content.strip() or not isinstance(metadata, dict):
            raise RetrievalInputError(f"routed chunk {index}内容或metadata无效")
        source = str(metadata.get("source", ""))
        if source not in grouped:
            raise RetrievalInputError(f"routed chunk出现未知报告: {source}")
        if (
            metadata.get("doc_id") != source_doc_ids[source]
            or metadata.get("pdf_sha256") != source_hashes[source]
            or metadata.get("chunk_index") != index
            or metadata.get("parser_layer") not in {"L1", "L3"}
        ):
            raise RetrievalInputError(f"routed chunk {index}身份无效")
        normalized = dict(metadata)
        normalized["artifact_chunk_index"] = index
        grouped[source].append(IndexChunk(content.strip(), scalarize_metadata(normalized)))
    counts = payload.get("counts") or {}
    if (
        counts.get("chunk_count") != len(chunks_raw)
        or counts.get("l1_chunk_count") != EXPECTED_OLD_CHUNKS
        or counts.get("l3_chunk_count") != EXPECTED_PADDLE_CHUNKS
        or len(chunks_raw) != EXPECTED_OLD_CHUNKS + EXPECTED_PADDLE_CHUNKS
    ):
        raise RetrievalInputError("routed corpus layer/chunk计数无效，拒绝旧artifact")
    ordered_sources = [
        source for source, _ in sorted(source_doc_ids.items(), key=lambda item: item[1])
    ]
    actual_l1 = [
        chunk
        for source in ordered_sources
        for chunk in grouped[source]
        if chunk.metadata.get("parser_layer") == "L1"
    ]
    actual_l3 = [
        chunk
        for source in ordered_sources
        for chunk in grouped[source]
        if chunk.metadata.get("parser_layer") == "L3"
    ]
    baseline = [chunk for source in ordered_sources for chunk in baseline_by_source[source]]
    actual_l1_sha = canonical_sha256(_l1_identity(actual_l1))
    baseline_l1_sha = canonical_sha256(_l1_identity(baseline))
    actual_l3_sha = canonical_sha256(_layer_identity(actual_l3))
    if (
        len(actual_l1) != EXPECTED_OLD_CHUNKS
        or len(actual_l3) != EXPECTED_PADDLE_CHUNKS
        or actual_l1_sha != baseline_l1_sha
        or declared_l1_sha != actual_l1_sha
        or declared_l3_sha != actual_l3_sha
    ):
        raise RetrievalInputError("routed L1不等于baseline或layer SHA不一致")
    corpus_sha = file_sha256(path)
    return grouped, {
        "schema_version": ROUTED_CORPUS_SCHEMA,
        "policy_version": policy_version,
        "policy_fingerprint": policy_fingerprint,
        "corpus_sha256": corpus_sha,
        "chunk_count": len(chunks_raw),
        "l1_corpus_sha256": actual_l1_sha,
        "l3_corpus_sha256": actual_l3_sha,
        "configuration_fingerprint": canonical_sha256({
            "schema_version": ROUTED_CORPUS_SCHEMA,
            "policy_version": policy_version,
            "policy_fingerprint": policy_fingerprint,
            "corpus_sha256": corpus_sha,
        }),
    }


def apply_routed_corpus(
    corpora: dict[str, Any],
    routed_by_source: dict[str, list[IndexChunk]],
) -> dict[str, Any]:
    sources = corpora["ordered_sources"]
    if set(routed_by_source) != set(sources):
        raise RetrievalInputError("routed corpus报告集合不一致")
    routed_l1 = {
        source: [
            chunk for chunk in routed_by_source[source]
            if chunk.metadata.get("parser_layer") == "L1"
        ]
        for source in sources
    }
    routed_l3 = {
        source: [
            chunk for chunk in routed_by_source[source]
            if chunk.metadata.get("parser_layer") == "L3"
        ]
        for source in sources
    }
    actual_prefix_sha = canonical_sha256(_l1_identity(
        [chunk for source in sources for chunk in routed_l1[source]]
    ))
    baseline_identity_sha = canonical_sha256(_l1_identity(
        [chunk for source in sources for chunk in corpora["baseline_by_source"][source]]
    ))
    if actual_prefix_sha != baseline_identity_sha:
        raise RetrievalInputError("routed实际L1前缀与baseline不一致")
    routed_count = sum(len(routed_by_source[source]) for source in sources)
    return {
        **corpora,
        "augmented_by_source": {
            source: list(routed_by_source[source]) for source in sources
        },
        "paddle_by_source": {
            source: list(routed_by_source[source]) for source in sources
        },
        "routed_l3_by_source": routed_l3,
        "paddle_old_prefix_sha256": actual_prefix_sha,
        "baseline_old_corpus_sha256": baseline_identity_sha,
        "paddle_chunk_count": routed_count,
        "routed_corpus_enabled": True,
    }


def corpus_fingerprint(records: list[IndexChunk]) -> str:
    return canonical_sha256([
        {"content": record.content, "metadata": record.metadata}
        for record in records
    ])


def build_fair_corpora(
    old_by_source: dict[str, list[IndexChunk]],
    paddle_chunks: list[IndexChunk],
    source_doc_ids: dict[str, int],
) -> dict[str, Any]:
    paddle_by_source = group_paddle_chunks(paddle_chunks, set(old_by_source))
    ordered_sources = [
        source for source, _ in sorted(source_doc_ids.items(), key=lambda item: item[1])
    ]
    baseline_by_source = {source: list(old_by_source[source]) for source in ordered_sources}
    augmented_by_source = {
        source: list(old_by_source[source]) + list(paddle_by_source[source])
        for source in ordered_sources
    }
    baseline_old = [chunk for source in ordered_sources for chunk in baseline_by_source[source]]
    augmented_old_prefix = [
        chunk
        for source in ordered_sources
        for chunk in augmented_by_source[source][:len(baseline_by_source[source])]
    ]
    baseline_sha = corpus_fingerprint(baseline_old)
    augmented_prefix_sha = corpus_fingerprint(augmented_old_prefix)
    if baseline_sha != augmented_prefix_sha:
        raise RetrievalInputError("两臂旧文本前缀不一致")
    return {
        "ordered_sources": ordered_sources,
        "baseline_by_source": baseline_by_source,
        "augmented_by_source": augmented_by_source,
        "paddle_by_source": paddle_by_source,
        "baseline_old_corpus_sha256": baseline_sha,
        "paddle_old_prefix_sha256": augmented_prefix_sha,
        "baseline_chunk_count": len(baseline_old),
        "paddle_chunk_count": sum(len(v) for v in augmented_by_source.values()),
    }


def embedding_identity() -> dict[str, str]:
    return {
        "contract": "openai-compatible-embeddings-v1",
        "base_url": settings.BASE_URL.rstrip("/"),
        "model": settings.EMBEDDING_MODEL,
    }


def embedding_namespace_fingerprint(identity: dict[str, str]) -> str:
    return canonical_sha256(identity)


def validate_embedding_vector(value: Any, expected_dimension: int | None = None) -> list[float] | None:
    if not isinstance(value, list) or not value:
        return None
    vector = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
            return None
        vector.append(float(item))
    if expected_dimension is not None and len(vector) != expected_dimension:
        return None
    return vector


def cache_item_path(cache_dir: Path, namespace: str, digest: str) -> Path:
    return cache_dir / namespace / "items" / f"{digest}.json"


def load_cached_embedding(
    path: Path,
    namespace: str,
    text: str,
    expected_dimension: int | None = None,
) -> list[float] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    digest = text_sha256(text)
    if (
        payload.get("schema_version") != CACHE_ITEM_SCHEMA
        or payload.get("namespace_fingerprint") != namespace
        or payload.get("text_sha256") != digest
        or payload.get("text_utf8_bytes") != len(text.encode("utf-8"))
    ):
        return None
    vector = validate_embedding_vector(payload.get("embedding"), expected_dimension)
    if vector is None or payload.get("embedding_dimension") != len(vector):
        return None
    return vector


def write_cached_embedding(
    path: Path,
    namespace: str,
    text: str,
    embedding: list[float],
) -> None:
    vector = validate_embedding_vector(embedding)
    if vector is None:
        raise RetrievalInputError("API返回无效embedding向量")
    write_json_atomic(path, {
        "schema_version": CACHE_ITEM_SCHEMA,
        "namespace_fingerprint": namespace,
        "text_sha256": text_sha256(text),
        "text_utf8_bytes": len(text.encode("utf-8")),
        "embedding_dimension": len(vector),
        "embedding": vector,
    })


def inspect_embedding_cache(
    texts: list[str],
    cache_dir: Path,
    namespace: str,
) -> dict[str, Any]:
    unique = list(dict.fromkeys(texts))
    hits = invalid = 0
    dimension = None
    for text in unique:
        path = cache_item_path(cache_dir, namespace, text_sha256(text))
        if not path.exists():
            continue
        vector = load_cached_embedding(path, namespace, text, dimension)
        if vector is None:
            invalid += 1
        else:
            dimension = dimension or len(vector)
            hits += 1
    return {
        "unique_texts": len(unique),
        "valid_hits": hits,
        "missing": len(unique) - hits - invalid,
        "invalid": invalid,
        "embedding_dimension": dimension,
    }


async def get_embeddings_cached(
    texts: list[str],
    cache_dir: Path,
    identity: dict[str, str],
    batch_size: int,
) -> tuple[list[list[float]], dict[str, Any]]:
    from app.services.document_service import _batch_embed

    namespace = embedding_namespace_fingerprint(identity)
    unique = list(dict.fromkeys(texts))
    vectors: dict[str, list[float]] = {}
    missing = []
    dimension = None
    initial_hits = invalid_items = 0
    for text in unique:
        path = cache_item_path(cache_dir, namespace, text_sha256(text))
        vector = load_cached_embedding(path, namespace, text, dimension)
        if vector is not None:
            dimension = dimension or len(vector)
            vectors[text] = vector
            initial_hits += 1
        else:
            invalid_items += int(path.exists())
            missing.append(text)

    api_embedded = 0
    for start in range(0, len(missing), batch_size):
        batch = missing[start:start + batch_size]
        embedded = await _batch_embed(batch, batch_size=batch_size)
        if len(embedded) != len(batch):
            raise RetrievalInputError("Embedding API返回数量不一致")
        validated = []
        for text, vector_raw in zip(batch, embedded):
            vector = validate_embedding_vector(vector_raw, dimension)
            if vector is None:
                raise RetrievalInputError("Embedding API返回维度或数值无效")
            dimension = dimension or len(vector)
            validated.append((text, vector))
        for text, vector in validated:
            path = cache_item_path(cache_dir, namespace, text_sha256(text))
            write_cached_embedding(path, namespace, text, vector)
            vectors[text] = vector
            api_embedded += 1
        print(
            f"[EMBED] {min(start + len(batch), len(missing))}/{len(missing)} "
            f"cache_hits={initial_hits}"
        )

    manifest_path = cache_dir / namespace / "manifest.json"
    write_json_atomic(manifest_path, {
        "schema_version": CACHE_MANIFEST_SCHEMA,
        "namespace_fingerprint": namespace,
        "identity": identity,
        "embedding_dimension": dimension,
    })
    return [vectors[text] for text in texts], {
        "namespace_fingerprint": namespace,
        "unique_text_count": len(unique),
        "initial_hits": initial_hits,
        "api_embedded": api_embedded,
        "final_hits": len(unique),
        "invalid_items": invalid_items,
        "embedding_dimension": dimension,
    }


def get_embeddings_cache_only(
    texts: list[str],
    cache_dir: Path,
    identity: dict[str, str],
) -> tuple[list[list[float]], dict[str, Any]]:
    namespace = embedding_namespace_fingerprint(identity)
    manifest_path = cache_dir / namespace / "manifest.json"
    if not manifest_path.is_file():
        raise RetrievalInputError(f"embedding cache manifest缺失: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RetrievalInputError("embedding cache manifest无效") from exc
    if (
        manifest.get("schema_version") != CACHE_MANIFEST_SCHEMA
        or manifest.get("namespace_fingerprint") != namespace
        or manifest.get("identity") != identity
    ):
        raise RetrievalInputError("embedding cache namespace或identity不匹配")
    dimension = manifest.get("embedding_dimension")
    if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension < 1:
        raise RetrievalInputError("embedding cache dimension无效")

    unique = list(dict.fromkeys(texts))
    vectors: dict[str, list[float]] = {}
    missing = invalid = 0
    for text in unique:
        path = cache_item_path(cache_dir, namespace, text_sha256(text))
        if not path.is_file():
            missing += 1
            continue
        vector = load_cached_embedding(path, namespace, text, dimension)
        if vector is None:
            invalid += 1
            continue
        vectors[text] = vector
    if missing or invalid:
        raise RetrievalInputError(
            f"cache-only不完整: total={len(unique)} valid={len(vectors)} "
            f"missing={missing} invalid={invalid}"
        )
    return [vectors[text] for text in texts], {
        "namespace_fingerprint": namespace,
        "unique_text_count": len(unique),
        "initial_hits": len(unique),
        "api_embedded": 0,
        "final_hits": len(unique),
        "missing": 0,
        "invalid_items": 0,
        "embedding_dimension": dimension,
        "cache_only": True,
    }


def load_evaluation_inputs(args: argparse.Namespace) -> dict[str, Any]:
    validate_ranking_config(
        args.top_k,
        args.candidate_k,
        args.numeric_weight,
        args.embedding_batch_size,
    )
    ground_path = args.ground_truth.resolve()
    cases = _validate_ground_truth(ground_path)
    paddle_chunks, paddle_info = validate_paddle_artifacts(
        args.paddle_chunks.resolve(),
        args.chunk_summary.resolve(),
        args.strict_coverage.resolve(),
        ground_path,
    )
    old_by_source = load_old_caches(
        args.old_cache_dir.resolve(),
        paddle_info["source_doc_ids"],
        paddle_info["source_hashes"],
    )
    corpora = build_fair_corpora(
        old_by_source,
        paddle_chunks,
        paddle_info["source_doc_ids"],
    )
    routed_info = None
    routed_path = getattr(args, "routed_corpus", None)
    if routed_path is not None:
        routed_by_source, routed_info = load_routed_corpus(
            routed_path.resolve(),
            paddle_info["source_doc_ids"],
            paddle_info["source_hashes"],
            corpora["baseline_by_source"],
        )
        corpora = apply_routed_corpus(corpora, routed_by_source)
    expected_augmented = (
        routed_info["chunk_count"]
        if routed_info is not None
        else EXPECTED_OLD_CHUNKS + EXPECTED_PADDLE_CHUNKS
    )
    if (
        corpora["baseline_chunk_count"] != EXPECTED_OLD_CHUNKS
        or corpora["paddle_chunk_count"] != expected_augmented
    ):
        raise RetrievalInputError("双臂corpus计数无效")
    return {
        "cases": cases,
        "ground_truth_sha256": file_sha256(ground_path),
        "paddle_info": paddle_info,
        "routed_info": routed_info,
        "corpora": corpora,
    }


def load_retrieval_inputs(args: argparse.Namespace) -> dict[str, Any]:
    validate_ranking_config(
        args.top_k,
        args.candidate_k,
        args.numeric_weight,
        args.embedding_batch_size,
    )
    if args.dense_k < args.top_k or args.lexical_k < args.top_k:
        raise RetrievalInputError("dense_k和lexical_k必须大于等于top_k")
    questions = load_query_only_cases(args.questions.resolve())
    paddle_chunks, paddle_info = validate_paddle_artifacts(
        args.paddle_chunks.resolve(),
        args.chunk_summary.resolve(),
        None,
        None,
    )
    old_by_source = load_old_caches(
        args.old_cache_dir.resolve(),
        paddle_info["source_doc_ids"],
        paddle_info["source_hashes"],
    )
    corpora = build_fair_corpora(
        old_by_source,
        paddle_chunks,
        paddle_info["source_doc_ids"],
    )
    routed_info = None
    routed_path = getattr(args, "routed_corpus", None)
    if routed_path is not None:
        routed_by_source, routed_info = load_routed_corpus(
            routed_path.resolve(),
            paddle_info["source_doc_ids"],
            paddle_info["source_hashes"],
            corpora["baseline_by_source"],
        )
        corpora = apply_routed_corpus(corpora, routed_by_source)
    expected_augmented = (
        routed_info["chunk_count"]
        if routed_info is not None
        else EXPECTED_OLD_CHUNKS + EXPECTED_PADDLE_CHUNKS
    )
    if (
        corpora["baseline_chunk_count"] != EXPECTED_OLD_CHUNKS
        or corpora["paddle_chunk_count"] != expected_augmented
    ):
        raise RetrievalInputError("双臂corpus计数无效")
    return {
        "questions": questions,
        "questions_sha256": canonical_sha256(questions),
        "paddle_info": paddle_info,
        "routed_info": routed_info,
        "corpora": corpora,
    }


def build_validate_report(args: argparse.Namespace, inputs: dict[str, Any]) -> dict[str, Any]:
    corpora = inputs["corpora"]
    all_texts = []
    for source in corpora["ordered_sources"]:
        all_texts.extend(chunk.content for chunk in corpora["baseline_by_source"][source])
        all_texts.extend(chunk.content for chunk in corpora["paddle_by_source"][source])
    all_texts.extend(str(case["question"]) for case in inputs["cases"])
    identity = embedding_identity()
    namespace = embedding_namespace_fingerprint(identity)
    cache = inspect_embedding_cache(
        all_texts,
        args.embedding_cache_dir.resolve(),
        namespace,
    )
    return {
        "status": "VALIDATED",
        "api_called": False,
        "output_written": False,
        "inputs": {
            "ground_truth_cases": len(inputs["cases"]),
            "old_reports": len(corpora["ordered_sources"]),
            "old_chunks": corpora["baseline_chunk_count"],
            "paddle_tables": inputs["paddle_info"]["table_count"],
            "paddle_table_chunks": EXPECTED_PADDLE_CHUNKS,
        },
        "arms": {
            "baseline_chunks": corpora["baseline_chunk_count"],
            "paddle_chunks": corpora["paddle_chunk_count"],
            "old_prefixes_identical": (
                corpora["baseline_old_corpus_sha256"]
                == corpora["paddle_old_prefix_sha256"]
            ),
        },
        "embedding_cache": {
            "namespace_fingerprint": namespace,
            **cache,
        },
        "retrieval": {
            "top_k": args.top_k,
            "candidate_k": args.candidate_k,
            "numeric_weight": args.numeric_weight,
        },
    }


def _flatten(by_source: dict[str, list[IndexChunk]], sources: list[str]) -> list[IndexChunk]:
    return [chunk for source in sources for chunk in by_source[source]]


def evaluation_records(
    corpora: dict[str, Any],
) -> tuple[list[IndexChunk], list[IndexChunk]]:
    sources = corpora["ordered_sources"]
    old_records = _flatten(corpora["baseline_by_source"], sources)
    augmented_records = _flatten(corpora["paddle_by_source"], sources)
    if len(old_records) != EXPECTED_OLD_CHUNKS:
        raise RetrievalInputError(
            f"实际旧文本records应为{EXPECTED_OLD_CHUNKS}，实际{len(old_records)}"
        )
    expected_augmented = (
        corpora["paddle_chunk_count"]
        if corpora.get("routed_corpus_enabled")
        else EXPECTED_PADDLE_CHUNKS
    )
    if len(augmented_records) != expected_augmented:
        label = "routed" if corpora.get("routed_corpus_enabled") else "Paddle表格"
        raise RetrievalInputError(
            f"实际{label}records应为{expected_augmented}，实际{len(augmented_records)}"
        )
    return old_records, augmented_records


def _serialize_candidate(context: dict[str, Any]) -> dict[str, Any]:
    return serialize_context(context) | {
        field: context[field]
        for field in (
            "candidate_id",
            "dense_rank",
            "lexical_rank",
            "fusion_rank",
            "artifact_chunk_index",
        )
        if field in context
    }


def _serialize_diagnostics(diagnostics: dict[str, list[dict]]) -> dict[str, list[dict]]:
    return {
        channel: [_serialize_candidate(context) for context in contexts]
        for channel, contexts in diagnostics.items()
    }


async def run_candidate_retrieval(args: argparse.Namespace, inputs: dict[str, Any]) -> dict[str, Any]:
    import chromadb

    from app.utils.vector_store import VectorStore

    started = time.perf_counter()
    corpora = inputs["corpora"]
    sources = corpora["ordered_sources"]
    old_records, table_records = evaluation_records(corpora)
    questions = inputs["questions"]
    question_texts = [item["question"] for item in questions]
    identity = embedding_identity()
    all_vectors, cache_stats = get_embeddings_cache_only(
        [chunk.content for chunk in old_records]
        + [chunk.content for chunk in table_records]
        + question_texts,
        args.embedding_cache_dir.resolve(),
        identity,
    )
    expected_unique = len(set(
        [chunk.content for chunk in old_records]
        + [chunk.content for chunk in table_records]
        + question_texts
    ))
    if cache_stats["unique_text_count"] != expected_unique:
        raise RetrievalInputError(
            f"cache-only唯一文本应为{expected_unique}，实际{cache_stats['unique_text_count']}"
        )

    old_count = len(old_records)
    table_count = len(table_records)
    old_vectors = all_vectors[:old_count]
    table_vectors = all_vectors[old_count:old_count + table_count]
    query_vectors = all_vectors[old_count + table_count:]

    client = chromadb.EphemeralClient()
    baseline_store = VectorStore(client=client, collection_prefix="v1_baseline")
    paddle_store = VectorStore(client=client, collection_prefix="v1_paddle")
    offset = table_offset = 0
    for source in sources:
        old = corpora["baseline_by_source"][source]
        tables = corpora["paddle_by_source"][source]
        old_embeddings = old_vectors[offset:offset + len(old)]
        table_embeddings = table_vectors[table_offset:table_offset + len(tables)]
        doc_id = int(old[0].metadata["doc_id"])
        baseline_store.add_documents(
            1,
            [chunk.content for chunk in old],
            old_embeddings,
            doc_id,
            source,
            [chunk.metadata for chunk in old],
        )
        augmented = tables if corpora.get("routed_corpus_enabled") else old + tables
        augmented_embeddings = (
            table_embeddings
            if corpora.get("routed_corpus_enabled")
            else old_embeddings + table_embeddings
        )
        paddle_store.add_documents(
            1,
            [chunk.content for chunk in augmented],
            augmented_embeddings,
            doc_id,
            source,
            [chunk.metadata for chunk in augmented],
        )
        offset += len(old)
        table_offset += len(tables)

    cases = []
    for question, query_vector in zip(questions, query_vectors):
        baseline = baseline_store.query_diagnostics(
            1,
            query_vector,
            question["question"],
            dense_k=args.dense_k,
            lexical_k=args.lexical_k,
            numeric_weight=args.numeric_weight,
        )
        paddle = paddle_store.query_diagnostics(
            1,
            query_vector,
            question["question"],
            dense_k=args.dense_k,
            lexical_k=args.lexical_k,
            numeric_weight=args.numeric_weight,
        )
        case_result = {
            "case_id": question["case_id"],
            "question": question["question"],
            "query_text_sha256": text_sha256(question["question"]),
            "baseline": _serialize_diagnostics(baseline),
            "paddle": _serialize_diagnostics(paddle),
        }
        if args.retrieval_profile == "financial_v2":
            baseline_v2 = baseline_store.query_financial_v2(
                1,
                query_vector,
                question["question"],
                top_k=5,
                diagnostic_k=args.dense_k,
            )
            paddle_v2 = paddle_store.query_financial_v2(
                1,
                query_vector,
                question["question"],
                top_k=5,
                diagnostic_k=args.dense_k,
            )
            case_result["baseline_v2"] = {
                "channels": {
                    channel: [_serialize_candidate(item) for item in contexts]
                    for channel, contexts in baseline_v2["channels"].items()
                },
                "ranking": [_serialize_candidate(item) for item in baseline_v2["ranking"]],
                "top_k": [_serialize_candidate(item) for item in baseline_v2["top_k"]],
            }
            case_result["paddle_v2"] = {
                "channels": {
                    channel: [_serialize_candidate(item) for item in contexts]
                    for channel, contexts in paddle_v2["channels"].items()
                },
                "ranking": [_serialize_candidate(item) for item in paddle_v2["ranking"]],
                "top_k": [_serialize_candidate(item) for item in paddle_v2["top_k"]],
            }
        cases.append(case_result)

    config = {
        "retrieval_profile": args.retrieval_profile,
        "dense_k": args.dense_k,
        "lexical_k": args.lexical_k,
        "lexical_weight": settings.LEXICAL_WEIGHT,
        "numeric_weight": args.numeric_weight,
        "embedding_identity": identity,
        "routed_corpus": (
            {
                "schema_version": inputs["routed_info"]["schema_version"],
                "policy_version": inputs["routed_info"]["policy_version"],
                "policy_fingerprint": inputs["routed_info"]["policy_fingerprint"],
                "corpus_sha256": inputs["routed_info"]["corpus_sha256"],
                "configuration_fingerprint": inputs["routed_info"][
                    "configuration_fingerprint"
                ],
            }
            if inputs.get("routed_info")
            else None
        ),
    }
    config_sha = canonical_sha256(config)
    candidate_cache_identity = canonical_sha256({
        "questions_sha256": inputs["questions_sha256"],
        "configuration_sha256": config_sha,
        "baseline_corpus_sha256": corpora["baseline_old_corpus_sha256"],
        "augmented_corpus_sha256": (
            inputs["routed_info"]["corpus_sha256"]
            if inputs.get("routed_info")
            else inputs["paddle_info"]["chunk_sha256"]
        ),
    })
    result = {
        "schema_version": CANDIDATE_SCHEMA,
        "status": "completed",
        "ground_truth_loaded": False,
        "api_called": False,
        "inputs": {
            "questions_sha256": inputs["questions_sha256"],
            "paddle_chunks_sha256": inputs["paddle_info"]["chunk_sha256"],
            "baseline_corpus_sha256": corpora["baseline_old_corpus_sha256"],
            "paddle_corpus_sha256": (
                inputs["routed_info"]["corpus_sha256"]
                if inputs.get("routed_info")
                else canonical_sha256({
                    "old": corpora["paddle_old_prefix_sha256"],
                    "tables": inputs["paddle_info"]["chunk_sha256"],
                })
            ),
            "routed_corpus_sha256": (
                inputs["routed_info"]["corpus_sha256"]
                if inputs.get("routed_info") else None
            ),
            "config_sha256": config_sha,
            "candidate_cache_identity": candidate_cache_identity,
        },
        "configuration": config,
        "embedding_cache": cache_stats,
        "ranking_sha256": canonical_sha256(candidate_ranking_identity(cases)),
        "cases": cases,
        "runtime_seconds": round(time.perf_counter() - started, 4),
    }
    return attach_candidate_identity(result)


async def run_evaluation(args: argparse.Namespace, inputs: dict[str, Any]) -> dict[str, Any]:
    if not settings.API_KEY:
        raise RetrievalInputError(
            "真实评测要求API_KEY；禁止使用deterministic mock embeddings"
        )
    import chromadb

    from app.utils.vector_store import VectorStore

    started = time.perf_counter()
    corpora = inputs["corpora"]
    sources = corpora["ordered_sources"]
    old_records, table_records = evaluation_records(corpora)
    questions = [str(case["question"]) for case in inputs["cases"]]
    identity = embedding_identity()

    embedding_started = time.perf_counter()
    all_vectors, cache_stats = await get_embeddings_cached(
        [chunk.content for chunk in old_records]
        + [chunk.content for chunk in table_records]
        + questions,
        args.embedding_cache_dir.resolve(),
        identity,
        args.embedding_batch_size,
    )
    old_count = len(old_records)
    table_count = len(table_records)
    old_vectors = all_vectors[:old_count]
    table_vectors = all_vectors[old_count:old_count + table_count]
    query_vectors = all_vectors[old_count + table_count:]
    embedding_seconds = time.perf_counter() - embedding_started

    index_started = time.perf_counter()
    client = chromadb.EphemeralClient()
    baseline_store = VectorStore(client=client, collection_prefix="paddle_baseline")
    paddle_store = VectorStore(client=client, collection_prefix="paddle_augmented")
    offset = 0
    table_offset = 0
    for source in sources:
        old = corpora["baseline_by_source"][source]
        tables = corpora["paddle_by_source"][source]
        old_embeddings = old_vectors[offset:offset + len(old)]
        table_embeddings = table_vectors[table_offset:table_offset + len(tables)]
        doc_id = int(old[0].metadata["doc_id"])
        baseline_store.add_documents(
            1,
            [chunk.content for chunk in old],
            old_embeddings,
            doc_id,
            source,
            [chunk.metadata for chunk in old],
        )
        augmented = tables if corpora.get("routed_corpus_enabled") else old + tables
        augmented_embeddings = (
            table_embeddings
            if corpora.get("routed_corpus_enabled")
            else old_embeddings + table_embeddings
        )
        paddle_store.add_documents(
            1,
            [chunk.content for chunk in augmented],
            augmented_embeddings,
            doc_id,
            source,
            [chunk.metadata for chunk in augmented],
        )
        offset += len(old)
        table_offset += len(tables)
    baseline_index_count = baseline_store.get_collection_count(1)
    paddle_index_count = paddle_store.get_collection_count(1)
    expected_paddle_index_count = (
        len(table_records)
        if corpora.get("routed_corpus_enabled")
        else len(old_records) + len(table_records)
    )
    if (
        offset != len(old_records)
        or table_offset != len(table_records)
        or baseline_index_count != len(old_records)
        or paddle_index_count != expected_paddle_index_count
    ):
        raise RetrievalInputError(
            "Chroma双臂索引计数无效: "
            f"baseline={baseline_index_count}/{len(old_records)}, "
            f"paddle={paddle_index_count}/{len(old_records) + len(table_records)}"
        )
    index_seconds = time.perf_counter() - index_started

    query_started = time.perf_counter()
    results = []
    baseline_hits = paddle_hits = 0
    for case_index, (case, query_vector) in enumerate(zip(inputs["cases"], query_vectors)):
        question = str(case["question"])
        baseline_contexts = baseline_store.query(
            1,
            query_vector,
            args.top_k,
            question,
            candidate_k=args.candidate_k,
            numeric_weight=args.numeric_weight,
        )
        paddle_contexts = paddle_store.query(
            1,
            query_vector,
            args.top_k,
            question,
            candidate_k=args.candidate_k,
            numeric_weight=args.numeric_weight,
        )
        baseline_score = score_case(baseline_contexts[:args.top_k], case)
        paddle_score = score_case(paddle_contexts[:args.top_k], case)
        baseline_hits += int(baseline_score["hit"])
        paddle_hits += int(paddle_score["hit"])
        results.append({
            "case_index": case_index,
            "ground_truth": case,
            "query_text_sha256": text_sha256(question),
            "baseline": {
                **baseline_score,
                "top_k": [serialize_context(ctx) for ctx in baseline_contexts[:args.top_k]],
            },
            "paddle": {
                **paddle_score,
                "top_k": [serialize_context(ctx) for ctx in paddle_contexts[:args.top_k]],
            },
        })
    query_seconds = time.perf_counter() - query_started

    total = len(inputs["cases"])
    baseline_recall = baseline_hits / total
    paddle_recall = paddle_hits / total
    improvement = calculate_improvement(baseline_recall, paddle_recall)
    return {
        "schema_version": RESULT_SCHEMA,
        "status": "completed",
        "inputs": {
            "ground_truth_sha256": inputs["ground_truth_sha256"],
            "paddle_chunks_sha256": inputs["paddle_info"]["chunk_sha256"],
            "chunk_summary_sha256": inputs["paddle_info"]["chunk_summary_sha256"],
            "strict_coverage_sha256": inputs["paddle_info"]["strict_coverage_sha256"],
            "old_cache_dir": str(args.old_cache_dir.resolve()),
        },
        "configuration": {
            "top_k": args.top_k,
            "candidate_k": args.candidate_k,
            "lexical_weight": settings.LEXICAL_WEIGHT,
            "numeric_weight": args.numeric_weight,
            "min_relevance_score": settings.MIN_RELEVANCE_SCORE,
            "embedding_mode": "api",
            "embedding_model": identity["model"],
            "embedding_base_url": identity["base_url"],
            "embedding_dimension": cache_stats["embedding_dimension"],
            "shared_query_embedding": True,
            "shared_old_document_embeddings": True,
            "vector_store": "chromadb.EphemeralClient",
            "strict_hit_definition": (
                "same final Top-5 context must match report basename, physical page, "
                "metric, and numeric value boundaries"
            ),
            "ground_truth_used_during_retrieval": False,
        },
        "corpus": {
            "report_count": len(sources),
            "baseline_chunk_count": corpora["baseline_chunk_count"],
            "paddle_chunk_count": corpora["paddle_chunk_count"],
            "old_chunk_count": len(old_records),
            "paddle_table_chunk_count": len(table_records),
            "baseline_index_count": baseline_index_count,
            "paddle_index_count": paddle_index_count,
            "paddle_table_count": inputs["paddle_info"]["table_count"],
            "baseline_old_corpus_sha256": corpora["baseline_old_corpus_sha256"],
            "paddle_old_prefix_sha256": corpora["paddle_old_prefix_sha256"],
            "old_prefixes_identical": True,
        },
        "embedding_cache": cache_stats,
        "cases": results,
        "metrics": {
            "ground_truth_count": total,
            "baseline_hits": baseline_hits,
            "paddle_hits": paddle_hits,
            "baseline_recall_at_5": round(baseline_recall, 6),
            "paddle_recall_at_5": round(paddle_recall, 6),
            **improvement,
            "acceptance_absolute_improvement_at_least_20_points": (
                improvement["absolute_percentage_points"] >= 20
            ),
        },
        "failures": {
            "baseline": [case["ground_truth"] for case in results if not case["baseline"]["hit"]],
            "paddle": [case["ground_truth"] for case in results if not case["paddle"]["hit"]],
        },
        "runtime": {
            "embedding_seconds": round(embedding_seconds, 4),
            "index_seconds": round(index_seconds, 4),
            "query_seconds": round(query_seconds, 4),
            "total_seconds": round(time.perf_counter() - started, 4),
        },
    }


async def async_main(args: argparse.Namespace) -> int:
    output = args.output.resolve()
    force = bool(getattr(args, "force", False))
    try:
        if args.cache_only:
            candidates_output = args.candidates_output.resolve()
            ensure_output_writable(
                candidates_output,
                force=force,
                canonical_paths=(DEFAULT_CANDIDATES_OUTPUT,),
            )
            inputs = load_retrieval_inputs(args)
            result = await run_candidate_retrieval(args, inputs)
            file_sha = write_output_artifact(
                candidates_output,
                result,
                force=force,
                canonical_paths=(DEFAULT_CANDIDATES_OUTPUT,),
            )
            print(json.dumps({
                "status": "COMPLETED",
                "output": str(candidates_output),
                "api_called": False,
                "ground_truth_loaded": False,
                "embedding_cache": result["embedding_cache"],
                "candidate_file_sha256": file_sha,
                "candidate_canonical_sha256": result["candidate_canonical_sha256"],
                "ranking_sha256": result["ranking_sha256"],
            }, ensure_ascii=False, indent=2))
            return 0
        if not args.validate_only:
            ensure_output_writable(
                output,
                force=force,
                canonical_paths=(DEFAULT_OUTPUT,),
            )
        inputs = load_evaluation_inputs(args)
        if args.validate_only:
            report = build_validate_report(args, inputs)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        result = await run_evaluation(args, inputs)
        write_output_artifact(
            output,
            result,
            force=force,
            canonical_paths=(DEFAULT_OUTPUT,),
        )
        print(json.dumps({
            "status": "COMPLETED",
            "output": str(output),
            "metrics": result["metrics"],
        }, ensure_ascii=False, indent=2))
        return 0
    except EvaluationBlocked as exc:
        target = (
            args.candidates_output.resolve()
            if getattr(args, "cache_only", False)
            else output
        )
        print(json.dumps({
            "status": "BLOCKED",
            "reason": str(exc),
            "output_written": False,
            "existing_output_left_unchanged": target.exists(),
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except Exception as exc:
        target = (
            args.candidates_output.resolve()
            if getattr(args, "cache_only", False)
            else output
        )
        print(json.dumps({
            "status": "BLOCKED",
            "reason": f"unexpected_error: {exc}",
            "output_written": False,
            "existing_output_left_unchanged": target.exists(),
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


def main() -> int:
    return asyncio.run(async_main(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
