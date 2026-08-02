from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.evidence_guard import ensure_evidence_output_writable  # noqa: E402

REQUIRED_GROUND_TRUTH_FIELDS = ("pdf", "question", "metric", "expected_value", "expected_page")


class EvaluationBlocked(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare legacy and table-aware financial PDF retrieval.")
    parser.add_argument("--pdf-dir", action="append", default=[], help="Directory containing financial PDFs.")
    parser.add_argument("--pdf", action="append", default=[], help="Explicit financial PDF path; may be repeated.")
    parser.add_argument("--ground-truth", default="evals/table_ground_truth.json", help="JSON or JSONL ground truth path.")
    parser.add_argument("--output", default="compare_result.json", help="Successful comparison output path.")
    parser.add_argument("--top-k", type=int, default=5, help="Final Recall@K cutoff after reranking.")
    parser.add_argument(
        "--candidate-k",
        type=int,
        default=None,
        help="Exact vector and lexical candidate count before reranking; defaults to top-k times configured multiplier.",
    )
    parser.add_argument(
        "--numeric-weight",
        type=float,
        default=None,
        help="Numeric query/content overlap weight; defaults to NUMERIC_WEIGHT.",
    )
    parser.add_argument("--min-reports", type=int, default=1)
    parser.add_argument(
        "--use-hi-res",
        action="store_true",
        help="Opt in to CPU-intensive hi_res table extraction; the default uses fast parsing.",
    )
    parser.add_argument(
        "--parse-cache-dir",
        default="",
        help="Optional directory for SHA-256 keyed old/new parsed chunk caches.",
    )
    parser.add_argument(
        "--require-parse-cache",
        action="store_true",
        help="Block on any parsed chunk cache miss instead of reading or parsing PDF content.",
    )
    return parser.parse_args()


def load_ground_truth(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.is_file():
        raise EvaluationBlocked(f"ground truth 不存在: {source}")
    text = source.read_text(encoding="utf-8-sig").strip()
    if not text:
        raise EvaluationBlocked(f"ground truth 为空: {source}")
    try:
        payload = json.loads(text)
        cases = payload if isinstance(payload, list) else payload.get("cases") if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        cases = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise EvaluationBlocked(f"ground truth JSONL 第 {line_number} 行无效: {exc}") from exc
    if not isinstance(cases, list) or not cases:
        raise EvaluationBlocked("ground truth 必须是非空 JSON 数组、含 cases 的对象或 JSONL")

    normalized = []
    seen = set()
    for index, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            raise EvaluationBlocked(f"ground truth 第 {index} 项不是对象")
        missing = [field for field in REQUIRED_GROUND_TRUTH_FIELDS if case.get(field) in (None, "")]
        if missing:
            raise EvaluationBlocked(f"ground truth 第 {index} 项缺少字段: {', '.join(missing)}")
        try:
            expected_page = int(case["expected_page"])
        except (TypeError, ValueError) as exc:
            raise EvaluationBlocked(f"ground truth 第 {index} 项 expected_page 必须为正整数") from exc
        if expected_page <= 0:
            raise EvaluationBlocked(f"ground truth 第 {index} 项 expected_page 必须为正整数")
        item = {field: case[field] for field in REQUIRED_GROUND_TRUTH_FIELDS}
        item["expected_page"] = expected_page
        key = tuple(str(item[field]) for field in REQUIRED_GROUND_TRUTH_FIELDS)
        if key in seen:
            raise EvaluationBlocked(f"ground truth 存在重复用例: {item['pdf']} / {item['question']}")
        seen.add(key)
        normalized.append(item)
    return normalized


def collect_pdf_paths(pdf_dirs: list[str], pdf_paths: list[str]) -> list[Path]:
    collected: list[Path] = []
    for raw in pdf_paths:
        path = Path(raw).resolve()
        if path not in collected:
            collected.append(path)
    for raw in pdf_dirs:
        directory = Path(raw).resolve()
        if directory.is_dir():
            for path in sorted(directory.glob("*.pdf")):
                resolved = path.resolve()
                if resolved not in collected:
                    collected.append(resolved)
    return collected


def filter_cases_for_paths(cases: list[dict[str, Any]], paths: list[Path]) -> list[dict[str, Any]]:
    selected_names = {path.name for path in paths}
    if not selected_names:
        return cases
    selected = [case for case in cases if str(case["pdf"]) in selected_names]
    if not selected:
        raise EvaluationBlocked("所选 PDF 在固定 ground truth 中没有验收用例")
    return selected


def build_inventory(paths: list[Path], cases: list[dict[str, Any]], min_reports: int = 1) -> list[dict[str, Any]]:
    if not paths:
        raise EvaluationBlocked("未提供任何 PDF")
    case_counts: dict[str, int] = {}
    for case in cases:
        case_counts[str(case["pdf"])] = case_counts.get(str(case["pdf"]), 0) + 1

    inventory = []
    seen_names = set()
    hashes = set()
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise EvaluationBlocked("缺少 pypdf，无法验证 PDF 页数") from exc

    for path in paths:
        if not path.is_file():
            raise EvaluationBlocked(f"PDF 不存在: {path}")
        if path.suffix.lower() != ".pdf":
            raise EvaluationBlocked(f"输入不是 PDF: {path}")
        if path.name in seen_names:
            raise EvaluationBlocked(f"PDF basename 重复且 ground truth 无法区分: {path.name}")
        seen_names.add(path.name)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        hashes.add(digest)
        try:
            page_count = len(PdfReader(str(path)).pages)
        except Exception as exc:
            raise EvaluationBlocked(f"无法读取 PDF 页数 {path}: {exc}") from exc
        inventory.append({
            "path": str(path),
            "filename": path.name,
            "size_bytes": path.stat().st_size,
            "page_count": page_count,
            "sha256": digest,
            "ground_truth_cases": case_counts.get(path.name, 0),
        })

    if len(inventory) < min_reports:
        raise EvaluationBlocked(f"需要至少 {min_reports} 份 PDF，实际 {len(inventory)} 份")
    if len(hashes) < min_reports:
        raise EvaluationBlocked(f"需要至少 {min_reports} 份内容唯一的 PDF，实际唯一哈希 {len(hashes)} 份")

    inventory_names = {item["filename"] for item in inventory}
    missing_files = sorted({str(case["pdf"]) for case in cases} - inventory_names)
    if missing_files:
        raise EvaluationBlocked(f"ground truth 引用未提供的 PDF: {', '.join(missing_files)}")
    without_cases = sorted(inventory_names - set(case_counts))
    if without_cases:
        raise EvaluationBlocked(f"以下 PDF 没有 ground truth: {', '.join(without_cases)}")
    return inventory


def normalize_match_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    text = text.replace("％", "%").replace("，", ",")
    text = "".join(text.split())
    return text.replace(",", "")


def strict_value_match(expected: Any, content: Any) -> bool:
    expected_text = unicodedata.normalize("NFKC", str(expected)).casefold()
    content_text = unicodedata.normalize("NFKC", str(content)).casefold()
    expected_text = expected_text.replace("％", "%").replace("，", ",")
    content_text = content_text.replace("％", "%").replace("，", ",")
    if not expected_text.strip():
        return False
    if any(character.isdigit() for character in expected_text):
        expected_numeric = re.sub(r"[\s,]", "", expected_text)
        content_numeric = content_text.replace(",", "")
        content_numeric = re.sub(r"\s+", " ", content_numeric)
        pattern = rf"(?<![\d.]){re.escape(expected_numeric)}(?![\d.])"
        return re.search(pattern, content_numeric) is not None
    return normalize_match_text(expected_text) in normalize_match_text(content_text)


def strict_context_hit(context: dict[str, Any], case: dict[str, Any]) -> bool:
    source_match = Path(str(context.get("source", ""))).name == str(case["pdf"])
    try:
        page_match = int(context.get("page_number", 0)) == int(case["expected_page"])
    except (TypeError, ValueError):
        page_match = False
    content = normalize_match_text(context.get("content", ""))
    metric_match = normalize_match_text(case["metric"]) in content
    value_match = strict_value_match(case["expected_value"], context.get("content", ""))
    return source_match and page_match and metric_match and value_match


def _markdown_cells(line: str) -> list[str] | None:
    stripped = line.strip()
    if "|" not in stripped:
        return None
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    if len(cells) < 2:
        return None
    if all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells):
        return None
    return cells


def _query_years(question: Any) -> list[str]:
    return list(dict.fromkeys(re.findall(r"(?<!\d)(20\d{2})(?!\d)", str(question))))


def _year_columns(rows: list[list[str]], years: list[str]) -> dict[str, int]:
    if not years:
        return {}
    for cells in rows:
        mapping: dict[str, int] = {}
        for index, cell in enumerate(cells):
            compact = normalize_match_text(cell)
            for year in years:
                if compact in {year, f"{year}年", f"截至{year}年"}:
                    mapping[year] = index
        if mapping:
            return mapping
    return {}


def row_strict_context_hit(context: dict[str, Any], case: dict[str, Any]) -> bool:
    if Path(str(context.get("source", ""))).name != str(case["pdf"]):
        return False
    try:
        if int(context.get("page_number", 0)) != int(case["expected_page"]):
            return False
    except (TypeError, ValueError):
        return False

    content = unicodedata.normalize("NFKC", str(context.get("content", "")))
    markdown_rows = [cells for line in content.splitlines() if (cells := _markdown_cells(line))]
    years = _query_years(case.get("question", ""))
    year_columns = _year_columns(markdown_rows, years)
    target_year = next((year for year in years if year in year_columns), None)
    metric = normalize_match_text(case["metric"])

    if markdown_rows:
        for cells in markdown_rows:
            row_text = " | ".join(cells)
            if metric not in normalize_match_text(row_text):
                continue
            if target_year is not None:
                target_index = year_columns[target_year]
                if target_index >= len(cells):
                    continue
                if strict_value_match(case["expected_value"], cells[target_index]):
                    return True
                continue
            if strict_value_match(case["expected_value"], row_text):
                return True
        return False

    for line in content.splitlines():
        if metric not in normalize_match_text(line):
            continue
        if strict_value_match(case["expected_value"], line):
            return True
    return False


def score_case(
    contexts: list[dict[str, Any]],
    case: dict[str, Any],
    scorer: str = "historical_chunk_strict",
) -> dict[str, Any]:
    if scorer == "historical_chunk_strict":
        hit_fn = strict_context_hit
    elif scorer == "row_strict":
        hit_fn = row_strict_context_hit
    else:
        raise ValueError(f"未知 scorer: {scorer}")

    for rank, context in enumerate(contexts, 1):
        if hit_fn(context, case):
            return {"hit": True, "hit_rank": rank, "miss_reason": None}

    reasons = []
    if not any(Path(str(ctx.get("source", ""))).name == str(case["pdf"]) for ctx in contexts):
        reasons.append("report_mismatch")
    if not any(str(ctx.get("page_number", "")) == str(case["expected_page"]) for ctx in contexts):
        reasons.append("page_mismatch")
    if not any(normalize_match_text(case["metric"]) in normalize_match_text(ctx.get("content", "")) for ctx in contexts):
        reasons.append("metric_missing")
    if not any(strict_value_match(case["expected_value"], ctx.get("content", "")) for ctx in contexts):
        reasons.append("value_missing")
    if scorer == "row_strict" and any(strict_context_hit(ctx, case) for ctx in contexts):
        reasons.append("row_false_positive")
    if not reasons:
        reasons.append("conditions_not_in_same_context")
    return {"hit": False, "hit_rank": None, "miss_reason": reasons}


def calculate_improvement(old_recall: float, new_recall: float) -> dict[str, Any]:
    absolute_points = round((new_recall - old_recall) * 100, 4)
    if old_recall == 0:
        return {
            "absolute_percentage_points": absolute_points,
            "relative_percent": None,
            "relative_change_reason": "undefined_zero_baseline",
        }
    return {
        "absolute_percentage_points": absolute_points,
        "relative_percent": round((new_recall - old_recall) / old_recall * 100, 4),
        "relative_change_reason": None,
    }


def legacy_blocks(path: Path, doc_id: int) -> list[Any]:
    import pdfplumber
    from app.utils.table_pdf_parser import ParsedBlock

    blocks = []
    with pdfplumber.open(path) as pdf:
        for page_number, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            if text.strip():
                blocks.append(ParsedBlock(text, {
                    "source": path.name,
                    "doc_id": doc_id,
                    "content_type": "text",
                    "page_number": page_number,
                    "element_type": "LegacyPageText",
                    "provenance_id": f"legacy_doc_{doc_id}:page_{page_number}",
                    "parser": "pdfplumber_page_text",
                }))
    if not blocks:
        raise ValueError("legacy pdfplumber 未提取到文本")
    return blocks


def serialize_context(context: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "content", "source", "doc_id", "chunk_index", "page_number", "content_type",
        "provenance_id", "table_id", "distance", "vector_relevance", "lexical_score", "numeric_score",
        "relevance",
    )
    result = {field: context[field] for field in fields if field in context}
    result["content"] = str(result.get("content", ""))[:2_000]
    return result


async def run_comparison(
    paths: list[Path],
    inventory: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    top_k: int,
    parse_cache_dir: str | Path | None = None,
    use_hi_res: bool = False,
    candidate_k: int | None = None,
    numeric_weight: float | None = None,
    require_parse_cache: bool = False,
) -> dict[str, Any]:
    import chromadb
    import importlib.metadata as package_metadata

    from app.config import settings
    from app.services.document_service import _batch_embed
    from app.utils.table_pdf_parser import TablePDFParser, build_index_chunks
    from app.utils.text_splitter import RecursiveTextSplitter
    from app.utils.vector_store import VectorStore

    client = chromadb.EphemeralClient()
    old_store = VectorStore(client=client, collection_prefix="task2_old")
    new_store = VectorStore(client=client, collection_prefix="task2_new")
    splitter = RecursiveTextSplitter(settings.CHUNK_SIZE, settings.CHUNK_OVERLAP)
    parser = TablePDFParser(use_hi_res=use_hi_res)
    if require_parse_cache and not parse_cache_dir:
        raise EvaluationBlocked("--require-parse-cache 必须同时提供 --parse-cache-dir")
    parse_failures = []
    timings = {"old_parse_index_seconds": 0.0, "new_parse_index_seconds": 0.0, "query_seconds": 0.0}
    cache_stats = {"enabled": bool(parse_cache_dir), "old_hits": 0, "old_misses": 0, "new_hits": 0, "new_misses": 0}
    inventory_by_name = {item["filename"]: item for item in inventory}

    for doc_id, path in enumerate(paths, 1):
        digest = str(inventory_by_name[path.name]["sha256"])
        old_started = time.perf_counter()
        try:
            old_cache = (
                _chunk_cache_path(
                    parse_cache_dir,
                    digest,
                    "old",
                    settings.CHUNK_SIZE,
                    settings.CHUNK_OVERLAP,
                    "legacy-pdfplumber-v1",
                )
                if parse_cache_dir else None
            )
            old_chunks = (
                _load_chunk_cache(old_cache, digest, "old", "legacy-pdfplumber-v1")
                if old_cache else None
            )
            if old_chunks is None:
                cache_stats["old_misses"] += int(bool(parse_cache_dir))
                if require_parse_cache:
                    raise EvaluationBlocked(f"旧链路解析缓存缺失，禁止重新解析 PDF: {old_cache}")
                old_chunks = build_index_chunks(legacy_blocks(path, doc_id), splitter)
                if old_cache:
                    _write_chunk_cache(old_cache, digest, "old", "legacy-pdfplumber-v1", old_chunks)
            else:
                cache_stats["old_hits"] += 1
            old_texts = [chunk.content for chunk in old_chunks]
            old_embeddings = await _batch_embed(old_texts)
            old_store.add_documents(1, old_texts, old_embeddings, doc_id, path.name, [chunk.metadata for chunk in old_chunks])
        except EvaluationBlocked:
            raise
        except Exception as exc:
            parse_failures.append({"pdf": path.name, "arm": "old", "error": str(exc)[:1_000]})
        timings["old_parse_index_seconds"] += time.perf_counter() - old_started

        new_started = time.perf_counter()
        try:
            new_cache = (
                _chunk_cache_path(
                    parse_cache_dir,
                    digest,
                    "new",
                    settings.CHUNK_SIZE,
                    settings.CHUNK_OVERLAP,
                    parser.profile,
                )
                if parse_cache_dir else None
            )
            new_chunks = (
                _load_chunk_cache(new_cache, digest, "new", parser.profile)
                if new_cache else None
            )
            if new_chunks is None:
                cache_stats["new_misses"] += int(bool(parse_cache_dir))
                if require_parse_cache:
                    raise EvaluationBlocked(f"新链路解析缓存缺失，禁止重新解析 PDF: {new_cache}")
                new_chunks = build_index_chunks(parser.parse(path, doc_id=doc_id, source=path.name), splitter)
                if new_cache:
                    _write_chunk_cache(new_cache, digest, "new", parser.profile, new_chunks)
            else:
                cache_stats["new_hits"] += 1
            new_texts = [chunk.content for chunk in new_chunks]
            new_embeddings = await _batch_embed(new_texts)
            new_store.add_documents(1, new_texts, new_embeddings, doc_id, path.name, [chunk.metadata for chunk in new_chunks])
        except EvaluationBlocked:
            raise
        except Exception as exc:
            parse_failures.append({"pdf": path.name, "arm": "new", "error": str(exc)[:1_000]})
        timings["new_parse_index_seconds"] += time.perf_counter() - new_started

    if parse_failures:
        raise EvaluationBlocked("PDF 解析或索引失败: " + json.dumps(parse_failures, ensure_ascii=False))

    effective_candidate_k = candidate_k or top_k * settings.RETRIEVAL_CANDIDATE_MULTIPLIER
    effective_numeric_weight = settings.NUMERIC_WEIGHT if numeric_weight is None else numeric_weight
    results = []
    old_hits = new_hits = 0
    query_started = time.perf_counter()
    for case in cases:
        query_embedding = await _batch_embed([str(case["question"])])
        old_contexts = old_store.query(
            1,
            query_embedding[0],
            top_k,
            str(case["question"]),
            candidate_k=effective_candidate_k,
            numeric_weight=effective_numeric_weight,
        )
        new_contexts = new_store.query(
            1,
            query_embedding[0],
            top_k,
            str(case["question"]),
            candidate_k=effective_candidate_k,
            numeric_weight=effective_numeric_weight,
        )
        old_score = score_case(old_contexts, case)
        new_score = score_case(new_contexts, case)
        old_hits += int(old_score["hit"])
        new_hits += int(new_score["hit"])
        results.append({
            "ground_truth": case,
            "old": {**old_score, "top_k": [serialize_context(ctx) for ctx in old_contexts]},
            "new": {**new_score, "top_k": [serialize_context(ctx) for ctx in new_contexts]},
        })
    timings["query_seconds"] = time.perf_counter() - query_started

    total = len(cases)
    old_recall = old_hits / total
    new_recall = new_hits / total
    improvement = calculate_improvement(old_recall, new_recall)
    return {
        "status": "completed",
        "configuration": {
            "top_k": top_k,
            "candidate_k": effective_candidate_k,
            "numeric_weight": effective_numeric_weight,
            "embedding_mode": "api" if settings.API_KEY else "deterministic_mock",
            "strict_hit_definition": "same Top-K context must match report basename, physical page, metric, and numeric value boundaries",
            "old_pipeline": "pdfplumber page text -> RecursiveTextSplitter -> Chroma hybrid retrieval",
            "new_pipeline": (
                "partition_pdf hi_res -> structured Markdown tables -> Chroma hybrid retrieval"
                if use_hi_res
                else "partition_pdf fast -> text elements -> Chroma hybrid retrieval"
            ),
            "new_parser_profile": parser.profile,
            "parse_cache": cache_stats,
        },
        "runtime": {
            "python": sys.version,
            "unstructured": package_metadata.version("unstructured"),
            "unstructured_inference": package_metadata.version("unstructured-inference"),
            "paddlepaddle": package_metadata.version("paddlepaddle"),
            **{key: round(value, 4) for key, value in timings.items()},
            "total_seconds": round(sum(timings.values()), 4),
        },
        "pdf_inventory": inventory,
        "ground_truth_count": total,
        "cases": results,
        "metrics": {
            "old_recall": round(old_recall, 6),
            "new_recall": round(new_recall, 6),
            **improvement,
            "acceptance_absolute_improvement_at_least_20_points": improvement["absolute_percentage_points"] >= 20,
        },
        "failures": {
            "old": [item["ground_truth"] for item in results if not item["old"]["hit"]],
            "new": [item["ground_truth"] for item in results if not item["new"]["hit"]],
        },
        "parse_failure_count": 0,
    }


def write_result_atomic(output: str | Path, result: dict[str, Any]) -> None:
    target = Path(output).resolve()
    ensure_evidence_output_writable(target, project_root=PROJECT_ROOT, force=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def _chunk_cache_path(
    cache_dir: str | Path,
    digest: str,
    arm: str,
    chunk_size: int,
    chunk_overlap: int,
    parser_profile: str,
) -> Path:
    profile_part = f".{parser_profile}" if arm == "new" else ""
    return Path(cache_dir) / (
        f"{digest}.{arm}{profile_part}.chunk-{chunk_size}-overlap-{chunk_overlap}.json"
    )


def _load_chunk_cache(
    path: Path,
    digest: str,
    arm: str,
    parser_profile: str,
) -> list[Any] | None:
    if not path.is_file():
        return None
    from app.utils.table_pdf_parser import IndexChunk

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        expected_schema = 1 if arm == "old" else 2
        if (
            payload.get("schema_version") != expected_schema
            or payload.get("pdf_sha256") != digest
            or payload.get("arm") != arm
            or (arm == "new" and payload.get("parser_profile") != parser_profile)
        ):
            return None
        chunks = payload.get("chunks")
        if not isinstance(chunks, list) or not chunks:
            return None
        return [IndexChunk(str(item["content"]), dict(item["metadata"])) for item in chunks]
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _write_chunk_cache(
    path: Path,
    digest: str,
    arm: str,
    parser_profile: str,
    chunks: list[Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1 if arm == "old" else 2,
        "pdf_sha256": digest,
        "arm": arm,
        "chunks": [{"content": chunk.content, "metadata": chunk.metadata} for chunk in chunks],
    }
    if arm == "new":
        payload["parser_profile"] = parser_profile
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


async def async_main(args: argparse.Namespace) -> int:
    output = Path(args.output)
    try:
        cases = load_ground_truth(args.ground_truth)
        paths = collect_pdf_paths(args.pdf_dir, args.pdf)
        cases = filter_cases_for_paths(cases, paths)
        inventory = build_inventory(paths, cases, args.min_reports)
        print(json.dumps({"status": "INVENTORY_OK", "pdf_inventory": inventory}, ensure_ascii=False, indent=2))
        result = await run_comparison(
            paths,
            inventory,
            cases,
            args.top_k,
            getattr(args, "parse_cache_dir", "") or None,
            getattr(args, "use_hi_res", False),
            getattr(args, "candidate_k", None),
            getattr(args, "numeric_weight", None),
            getattr(args, "require_parse_cache", False),
        )
        write_result_atomic(output, result)
        print(json.dumps({"status": "COMPLETED", "output": str(output), "metrics": result["metrics"]}, ensure_ascii=False, indent=2))
        return 0
    except EvaluationBlocked as exc:
        print(json.dumps({
            "status": "BLOCKED",
            "reason": str(exc),
            "output_written": False,
            "existing_output_left_unchanged": output.exists(),
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({
            "status": "BLOCKED",
            "reason": f"unexpected_error: {exc}",
            "output_written": False,
            "existing_output_left_unchanged": output.exists(),
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


def main() -> int:
    return asyncio.run(async_main(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
