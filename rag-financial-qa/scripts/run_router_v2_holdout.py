from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import re
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=False)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(PROJECT_ROOT))
from evals.common.ground_truth_contract import (
    GROUND_TRUTH_SCHEMA,
    GroundTruthContractError,
    validate_ground_truth,
    validate_official_bundle,
)
from app.services.answer_verification_service import build_citation_ledger, evidence_preflight

DEFAULT_ROOT = PROJECT_ROOT / "evals" / "router_v2_holdout"
DEFAULT_LOCK_FILE = (
    PROJECT_ROOT / "requirements" / "locks" / "paddleocr-gpu-windows-py312.lock.txt"
)
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FORBIDDEN_GT_FIELDS = {
    "expected_value",
    "expected_page",
    "expected_source",
    "expected_table_id",
    "answer",
    "ground_truth",
}
GROUND_TRUTH_REQUIRED = ("pdf", "question", "metric", "expected_value", "expected_page")
PRE_GT_STAGES = (
    "inventory",
    "select",
    "ocr-preflight",
    "ocr",
    "ocr-audit",
    "build-l1",
    "build-corpus",
    "embed",
    "candidate",
    "freeze-pre-gt",
)
SUCCESSFUL_PARSED_ARTIFACT_STATUSES = frozenset({"completed", "no_tables"})


class HoldoutPipelineError(RuntimeError):
    pass


def _load_script(module_name: str, filename: str):
    path = PROJECT_ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise HoldoutPipelineError(f"无法加载脚本: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    if not path.is_file():
        raise HoldoutPipelineError(f"输入不存在: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HoldoutPipelineError(f"JSON 无效: {path}") from exc


def _ensure_outputs_absent(*paths: Path) -> None:
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise HoldoutPipelineError(
            "输出已存在，拒绝覆盖: " + ", ".join(existing)
        )


def write_new_json(path: Path, payload: Any) -> None:
    _ensure_outputs_absent(path)
    atomic = _load_script("holdout_atomic_json", "atomic_json.py")
    try:
        atomic.write_json_atomic(path, payload, overwrite=False)
    except FileExistsError as exc:
        raise HoldoutPipelineError(f"输出在写入期间出现，拒绝覆盖: {path}") from exc


def require_sha(value: Any, label: str) -> str:
    normalized = str(value).lower()
    if not SHA256_RE.fullmatch(normalized):
        raise HoldoutPipelineError(f"{label} 不是有效 SHA-256")
    return normalized


def run_directory(root: Path, run_id: str) -> Path:
    if not RUN_ID_RE.fullmatch(run_id):
        raise HoldoutPipelineError("run_id 只允许 1-80 位字母、数字、点、下划线或连字符")
    runs = (root / "runs").resolve()
    target = (runs / run_id).resolve()
    if target.parent != runs:
        raise HoldoutPipelineError("run_id 逃逸 runs 目录")
    return target


def artifact_path(run_dir: Path, stage: str) -> Path:
    names = {
        "inventory": "inventory.json",
        "select": "candidate_pages.json",
        "ocr-preflight": "ocr_preflight.json",
        "ocr": "ocr_run.json",
        "ocr-audit": "ocr_audit.json",
        "build-l1": "l1_corpus.json",
        "build-corpus": "routed_corpus.json",
        "embed": "embedding_summary.json",
        "candidate": "paired_candidates.json",
        "freeze-pre-gt": "pre_gt_freeze.json",
        "validate-ground-truth": "ground_truth_validation.json",
        "score": "score.json",
        "score-provisional": "score_provisional.json",
        "finalize": "final_manifest.json",
    }
    return run_dir / names[stage]


def _load_queries(root: Path) -> list[dict[str, str]]:
    evaluator = _load_script("holdout_eval_queries", "05_evaluate_paddleocr_retrieval.py")
    path = root / "query_only.jsonl"
    cases: list[dict[str, str]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HoldoutPipelineError(f"query-only 第 {line_number} 行无效") from exc
        if not isinstance(item, dict):
            raise HoldoutPipelineError(f"query-only 第 {line_number} 行必须是对象")
        leaked = FORBIDDEN_GT_FIELDS & set(item)
        if leaked:
            raise HoldoutPipelineError(f"query-only 含 Ground Truth 字段: {sorted(leaked)}")
        if set(item) != {"case_id", "question"}:
            raise HoldoutPipelineError("query-only 只允许 case_id/question")
        cases.append({"case_id": str(item["case_id"]), "question": str(item["question"])})
    if len({case["case_id"] for case in cases}) != len(cases):
        raise HoldoutPipelineError("query-only case_id 重复")
    # Keep the same logical query identity used by 05, without its fixed case count.
    if evaluator.canonical_sha256(cases) != canonical_sha256(cases):
        raise HoldoutPipelineError("05 canonical query identity 不一致")
    return cases


def _manifest_and_prereg(root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest = load_json(root / "source_manifest.json")
    prereg = load_json(root / "preregistration.json")
    if not isinstance(manifest, list) or not manifest or not isinstance(prereg, dict):
        raise HoldoutPipelineError("holdout manifest/preregistration 格式无效")
    if prereg.get("report_count") != len(manifest):
        raise HoldoutPipelineError("report_count 与 source manifest 不一致")
    queries = _load_queries(root)
    if prereg.get("case_count") != len(queries):
        raise HoldoutPipelineError("case_count 与 query-only 不一致")
    return manifest, prereg


def _assert_no_gt_fields(value: Any, location: str = "candidate") -> None:
    if isinstance(value, dict):
        leaked = FORBIDDEN_GT_FIELDS & set(value)
        if leaked:
            raise HoldoutPipelineError(f"{location} 含禁止的 Ground Truth 字段: {sorted(leaked)}")
        for key, child in value.items():
            _assert_no_gt_fields(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_gt_fields(child, f"{location}[{index}]")


def _require_pre_gt(payload: Any, stage: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("ground_truth_loaded") is not False:
        raise HoldoutPipelineError(f"{stage} 必须声明 ground_truth_loaded=false")
    _assert_no_gt_fields(payload, stage)
    return payload


def _source_paths(root: Path, manifest: list[dict[str, Any]]) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for item in manifest:
        filename = str(item.get("filename", ""))
        if not filename or Path(filename).name != filename or filename in seen:
            raise HoldoutPipelineError("source manifest filename 无效或重复")
        seen.add(filename)
        path = root / "pdfs" / filename
        if not path.is_file():
            raise HoldoutPipelineError(f"冻结 PDF 不存在: {path}")
        if file_sha256(path) != require_sha(item.get("sha256"), filename):
            raise HoldoutPipelineError(f"冻结 PDF SHA 不一致: {filename}")
        if path.stat().st_size != item.get("size_bytes"):
            raise HoldoutPipelineError(f"冻结 PDF size 不一致: {filename}")
        paths.append(path)
    return paths


def inventory_stage(root: Path, run_dir: Path) -> dict[str, Any]:
    output = artifact_path(run_dir, "inventory")
    if output.exists():
        raise HoldoutPipelineError(f"输出已存在，拒绝覆盖: {output}")
    manifest, prereg = _manifest_and_prereg(root)
    paths = _source_paths(root, manifest)
    scanner = _load_script("holdout_scan", "01_scan_pdf_pages.py")
    inventory = scanner.build_inventory(paths)
    expected = {str(item["filename"]): item for item in manifest}
    for report in inventory["reports"]:
        frozen = expected[report["source"]]
        if report["sha256"] != frozen["sha256"] or report["page_count"] != frozen["page_count"]:
            raise HoldoutPipelineError(f"扫描身份与冻结 manifest 不一致: {report['source']}")
    if inventory["report_count"] != len(manifest):
        raise HoldoutPipelineError("动态 inventory 报告数不一致")
    payload = {
        **inventory,
        "schema_version": "router-v2-holdout-inventory-v1",
        "ground_truth_loaded": False,
        "inputs": {
            "source_manifest_file_sha256": file_sha256(root / "source_manifest.json"),
            "preregistration_file_sha256": file_sha256(root / "preregistration.json"),
            "source_pdf_file_sha256": {
                item["filename"]: item["sha256"] for item in manifest
            },
        },
        "contract": {"expected_report_count": prereg["report_count"]},
    }
    write_new_json(output, payload)
    return payload


def select_stage(root: Path, run_dir: Path, max_pages: int) -> dict[str, Any]:
    if max_pages < 1:
        raise HoldoutPipelineError("max_pages_per_report 必须大于 0")
    inventory_path = artifact_path(run_dir, "inventory")
    inventory = _require_pre_gt(load_json(inventory_path), "inventory")
    selector = _load_script("holdout_select", "02_select_table_pages.py")
    reports = [selector.select_report_pages(report, max_pages) for report in inventory["reports"]]
    payload = {
        "schema_version": "router-v2-holdout-candidate-pages-v1",
        "status": "completed" if any(report["selected_count"] for report in reports) else "empty",
        "ground_truth_loaded": False,
        "selection_policy": {
            "uses_ground_truth": False,
            "router_policy_version": selector.PDF_ROUTING_POLICY_VERSION,
            "router_policy_fingerprint": selector.POLICY_FINGERPRINT,
            "policy_canonical_sha256": canonical_sha256(selector.ROUTING_POLICY),
            "max_pages_per_report": max_pages,
        },
        "report_count": len(reports),
        "selected_page_count": sum(report["selected_count"] for report in reports),
        "dropped_page_count": sum(report["dropped_count"] for report in reports),
        "inputs": {"inventory_file_sha256": file_sha256(inventory_path)},
        "reports": reports,
    }
    if payload["status"] == "empty":
        raise HoldoutPipelineError("动态候选页为空")
    write_new_json(artifact_path(run_dir, "select"), payload)
    return payload


def _candidate_jobs(root: Path, candidate: dict[str, Any]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for doc_id, report in enumerate(candidate.get("reports") or [], 1):
        source = str(report.get("source", ""))
        source_path = root / "pdfs" / source
        if not source_path.is_file() or file_sha256(source_path) != report.get("pdf_sha256"):
            raise HoldoutPipelineError(f"candidate PDF identity 无效: {source}")
        for page in sorted(report.get("selected_pages") or [], key=lambda row: row["page_number"]):
            page_number = int(page["page_number"])
            key = (source, page_number)
            if key in seen:
                raise HoldoutPipelineError(f"重复 OCR job: {source}:p{page_number}")
            seen.add(key)
            jobs.append({
                "doc_id": doc_id,
                "source": source,
                "source_path": source_path,
                "pdf_sha256": report["pdf_sha256"],
                "page_number": page_number,
                "reasons": sorted(set(page.get("reasons") or [])),
            })
    if len(jobs) != candidate.get("selected_page_count"):
        raise HoldoutPipelineError("动态 OCR job 数与 candidate 不一致")
    return jobs


def _portable_jobs(jobs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in job.items() if key != "source_path"}
        for job in jobs
    ]


def ocr_preflight_stage(
    root: Path, run_dir: Path, lock_file: Path, device: str
) -> dict[str, Any]:
    candidate_path = artifact_path(run_dir, "select")
    candidate = _require_pre_gt(load_json(candidate_path), "select")
    runner = _load_script("holdout_ocr_preflight", "03_run_paddleocr_tables.py")
    jobs = _candidate_jobs(root, candidate)
    profile = runner.build_engine_profile(device, lock_file)
    raw_dir = run_dir / "ocr" / "raw"
    expected_paths = {runner.artifact_path(raw_dir, job).resolve() for job in jobs}
    actual_paths = set(raw_dir.rglob("*.json")) if raw_dir.is_dir() else set()
    unexpected = sorted(str(path) for path in actual_paths if path.resolve() not in expected_paths)
    states: Counter[str] = Counter()
    for job in jobs:
        state, _ = runner.classify_artifact(runner.artifact_path(raw_dir, job), job, profile)
        states[state] += 1
    if unexpected or states["stale"]:
        raise HoldoutPipelineError("OCR preflight 发现 stale/unexpected artifact，拒绝覆盖")
    payload = {
        "schema_version": "router-v2-holdout-ocr-preflight-v1",
        "status": "passed",
        "ground_truth_loaded": False,
        "inputs": {
            "candidate_manifest_file_sha256": file_sha256(candidate_path),
            "lock_file_sha256": file_sha256(lock_file),
        },
        "engine": profile,
        "counts": {
            "expected_reports": len({job["source"] for job in jobs}),
            "expected_pages": len(jobs),
            "artifact_states": dict(states),
            "unexpected_artifacts": len(unexpected),
        },
        "jobs": _portable_jobs(jobs),
    }
    write_new_json(artifact_path(run_dir, "ocr-preflight"), payload)
    return payload


def _rehydrate_jobs(root: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    jobs = []
    for row in rows:
        source = str(row["source"])
        jobs.append({**row, "source_path": root / "pdfs" / source})
    return jobs


def ocr_stage(root: Path, run_dir: Path, max_errors: int, device: str) -> dict[str, Any]:
    output = artifact_path(run_dir, "ocr")
    if output.exists():
        raise HoldoutPipelineError(f"输出已存在，拒绝覆盖: {output}")
    preflight_path = artifact_path(run_dir, "ocr-preflight")
    preflight = _require_pre_gt(load_json(preflight_path), "ocr-preflight")
    runner = _load_script("holdout_ocr_run", "03_run_paddleocr_tables.py")
    jobs = _rehydrate_jobs(root, preflight["jobs"])
    raw_dir = run_dir / "ocr" / "raw"
    states = Counter()
    for job in jobs:
        state, _ = runner.classify_artifact(
            runner.artifact_path(raw_dir, job), job, preflight["engine"]
        )
        states[state] += 1
    if states["stale"] or states["failed"]:
        raise HoldoutPipelineError("OCR 默认禁止覆盖 stale/failed artifact；请使用新 run_id")
    dynamic_preflight = {
        "engine": preflight["engine"],
        "jobs": jobs,
        "raw_dir": raw_dir,
        "candidate_manifest_sha256": preflight["inputs"]["candidate_manifest_file_sha256"],
    }
    summary = runner.run_batch(dynamic_preflight, run_dir / "ocr", max_errors, device)
    payload = {
        "schema_version": "router-v2-holdout-ocr-run-v1",
        "status": summary["status"],
        "ground_truth_loaded": False,
        "inputs": {"ocr_preflight_file_sha256": file_sha256(preflight_path)},
        "engine_configuration_fingerprint": preflight["engine"]["configuration_fingerprint"],
        "counts": summary["counts"],
        "run": summary.get("run", {}),
    }
    write_new_json(output, payload)
    return payload


def ocr_audit_stage(root: Path, run_dir: Path) -> dict[str, Any]:
    preflight_path = artifact_path(run_dir, "ocr-preflight")
    preflight = _require_pre_gt(load_json(preflight_path), "ocr-preflight")
    runner = _load_script("holdout_ocr_audit", "03_run_paddleocr_tables.py")
    jobs = _rehydrate_jobs(root, preflight["jobs"])
    summary = runner.audit_artifacts(
        jobs,
        run_dir / "ocr" / "raw",
        preflight["engine"],
        preflight["inputs"]["candidate_manifest_file_sha256"],
    )
    payload = {
        **summary,
        "schema_version": "router-v2-holdout-ocr-audit-v1",
        "ground_truth_loaded": False,
        "inputs": {
            **summary["inputs"],
            "ocr_preflight_file_sha256": file_sha256(preflight_path),
            "ocr_run_file_sha256": (
                file_sha256(artifact_path(run_dir, "ocr"))
                if artifact_path(run_dir, "ocr").is_file() else None
            ),
        },
    }
    write_new_json(artifact_path(run_dir, "ocr-audit"), payload)
    return payload


def build_l1_stage(root: Path, run_dir: Path, chunk_size: int, chunk_overlap: int) -> dict[str, Any]:
    if chunk_size < 100 or chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise HoldoutPipelineError("L1 chunk_size/overlap 无效")
    inventory_path = artifact_path(run_dir, "inventory")
    inventory = _require_pre_gt(load_json(inventory_path), "inventory")
    from app.utils.text_splitter import RecursiveTextSplitter

    try:
        import pdfplumber
    except ImportError as exc:
        raise HoldoutPipelineError("缺少 pdfplumber，无法构建 L1") from exc
    splitter = RecursiveTextSplitter(chunk_size, chunk_overlap)
    chunks: list[dict[str, Any]] = []
    page_count = nonempty_pages = 0
    for doc_id, report in enumerate(inventory["reports"], 1):
        source = report["source"]
        pdf_path = root / "pdfs" / source
        with pdfplumber.open(pdf_path) as document:
            if len(document.pages) != report["page_count"]:
                raise HoldoutPipelineError(f"L1 页数与 inventory 不一致: {source}")
            for page_number, page in enumerate(document.pages, 1):
                page_count += 1
                text = page.extract_text() or ""
                page_chunks = splitter.split_text(text)
                nonempty_pages += int(bool(page_chunks))
                for local_index, content in enumerate(page_chunks):
                    chunks.append({
                        "content": content,
                        "metadata": {
                            "source": source,
                            "doc_id": doc_id,
                            "page_number": page_number,
                            "content_type": "text",
                            "parser": "pdfplumber_page_text",
                            "parser_layer": "L1",
                            "pdf_sha256": report["sha256"],
                            "page_chunk_index": local_index,
                        },
                    })
    for index, chunk in enumerate(chunks):
        chunk["metadata"]["chunk_index"] = index
    parser_config = {
        "profile": "pdfplumber-page-text-v1",
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
    }
    payload = {
        "schema_version": "router-v2-holdout-l1-corpus-v1",
        "status": "completed",
        "ground_truth_loaded": False,
        "parser_profile_fingerprint": canonical_sha256(parser_config),
        "l1_corpus_canonical_sha256": canonical_sha256(chunks),
        "inputs": {"inventory_file_sha256": file_sha256(inventory_path)},
        "configuration": parser_config,
        "counts": {
            "report_count": len(inventory["reports"]),
            "page_count": page_count,
            "nonempty_page_count": nonempty_pages,
            "chunk_count": len(chunks),
        },
        "chunks": chunks,
    }
    write_new_json(artifact_path(run_dir, "build-l1"), payload)
    return payload


def build_corpus_stage(run_dir: Path, chunk_size: int, row_overlap: int) -> dict[str, Any]:
    if chunk_size < 100 or row_overlap not in {0, 1, 2, 3}:
        raise HoldoutPipelineError("L3 chunk_size/row_overlap 无效")
    l1_path = artifact_path(run_dir, "build-l1")
    candidate_path = artifact_path(run_dir, "select")
    audit_path = artifact_path(run_dir, "ocr-audit")
    l1 = _require_pre_gt(load_json(l1_path), "build-l1")
    candidate = _require_pre_gt(load_json(candidate_path), "select")
    audit = _require_pre_gt(load_json(audit_path), "ocr-audit")
    if audit.get("status") != "passed":
        raise HoldoutPipelineError("OCR audit 未通过")

    from app.utils.paddle_artifact_adapter import PaddleArtifactAdapter, PaddleArtifactValidationError
    from app.utils.table_pdf_parser import build_index_chunks
    from app.utils.text_splitter import RecursiveTextSplitter

    engine_fingerprint = require_sha(
        audit["inputs"].get("engine_configuration_fingerprint"), "OCR engine fingerprint"
    )
    adapter = PaddleArtifactAdapter(
        run_dir / "ocr" / "raw", expected_engine_fingerprint=engine_fingerprint
    )
    table_blocks = []
    for doc_id, report in enumerate(candidate["reports"], 1):
        for page in report["selected_pages"]:
            try:
                result = adapter.parse_page(
                    "offline-artifact-only",
                    int(page["page_number"]),
                    doc_id=doc_id,
                    source=report["source"],
                    pdf_sha256=report["pdf_sha256"],
                )
            except PaddleArtifactValidationError as exc:
                raise HoldoutPipelineError(
                    f"L3 artifact 无效: {report['source']}:p{page['page_number']}: {exc}"
                ) from exc
            if result.status not in SUCCESSFUL_PARSED_ARTIFACT_STATUSES:
                raise HoldoutPipelineError(
                    f"L3 artifact 未完成: {report['source']}:p{page['page_number']}"
                )
            for block in result.blocks:
                metadata = dict(block.metadata)
                metadata.pop("artifact_path", None)
                metadata.update({
                    "parser_layer": "L3",
                    "selected_layer": "L3",
                    "route_path": "L1->L3",
                    "candidate_reasons": ",".join(sorted(set(page.get("reasons") or []))),
                    "policy_fingerprint": candidate["selection_policy"]["router_policy_fingerprint"],
                })
                table_blocks.append(type(block)(block.content, metadata))
    splitter = RecursiveTextSplitter(chunk_size, max(0, chunk_size // 5))
    l3_records = build_index_chunks(table_blocks, splitter, table_row_overlap=row_overlap)
    l3_chunks = [
        {"content": record.content, "metadata": dict(record.metadata)}
        for record in l3_records
    ]
    all_chunks = [dict(item) for item in l1["chunks"]] + l3_chunks
    for index, item in enumerate(all_chunks):
        item["metadata"] = dict(item["metadata"])
        item["metadata"]["artifact_chunk_index"] = index
    l3_config = {
        "profile": "paddleocr-ppstructurev3-table-v2",
        "chunk_size": chunk_size,
        "table_row_overlap": row_overlap,
        "ocr_engine_fingerprint": engine_fingerprint,
    }
    payload = {
        "schema_version": "router-v2-holdout-routed-corpus-v2",
        "status": "completed",
        "ground_truth_loaded": False,
        "api_called": False,
        "l1_corpus_canonical_sha256": l1["l1_corpus_canonical_sha256"],
        "l3_corpus_canonical_sha256": canonical_sha256(l3_chunks),
        "corpus_canonical_sha256": canonical_sha256(all_chunks),
        "parser_profile_fingerprint": canonical_sha256({
            "l1": l1["parser_profile_fingerprint"], "l3": l3_config
        }),
        "inputs": {
            "l1_corpus_file_sha256": file_sha256(l1_path),
            "candidate_manifest_file_sha256": file_sha256(candidate_path),
            "ocr_audit_file_sha256": file_sha256(audit_path),
            "ocr_engine_fingerprint": engine_fingerprint,
        },
        "configuration": l3_config,
        "counts": {
            "report_count": l1["counts"]["report_count"],
            "l1_chunk_count": len(l1["chunks"]),
            "l3_chunk_count": len(l3_chunks),
            "chunk_count": len(all_chunks),
        },
        "chunks": all_chunks,
    }
    write_new_json(artifact_path(run_dir, "build-corpus"), payload)
    return payload


def _corpus_texts(root: Path, corpus: dict[str, Any]) -> list[str]:
    queries = _load_queries(root)
    return [str(item["content"]) for item in corpus["chunks"]] + [
        case["question"] for case in queries
    ]


async def embed_stage(root: Path, run_dir: Path, batch_size: int) -> dict[str, Any]:
    if batch_size < 1:
        raise HoldoutPipelineError("embedding batch size 必须大于 0")
    corpus_path = artifact_path(run_dir, "build-corpus")
    corpus = _require_pre_gt(load_json(corpus_path), "build-corpus")
    evaluator = _load_script("holdout_embed", "05_evaluate_paddleocr_retrieval.py")
    identity = evaluator.embedding_identity()
    texts = _corpus_texts(root, corpus)
    _, stats = await evaluator.get_embeddings_cached(
        texts, run_dir / "embedding_cache", identity, batch_size
    )
    payload = {
        "schema_version": "router-v2-holdout-embedding-summary-v1",
        "status": "completed",
        "ground_truth_loaded": False,
        "api_called": stats.get("api_embedded", 0) > 0,
        "embedding_identity": identity,
        "embedding_namespace_fingerprint": evaluator.embedding_namespace_fingerprint(identity),
        "inputs": {
            "corpus_file_sha256": file_sha256(corpus_path),
            "corpus_canonical_sha256": corpus["corpus_canonical_sha256"],
            "query_only_file_sha256": file_sha256(root / "query_only.jsonl"),
        },
        "cache": stats,
    }
    write_new_json(artifact_path(run_dir, "embed"), payload)
    return payload


def _serialize_context(context: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "content", "source", "doc_id", "page_number", "content_type", "parser",
        "parser_layer", "table_id", "provenance_id", "chunk_index",
        "artifact_chunk_index", "candidate_id", "distance", "relevance",
        "lexical_score", "dense_rank", "lexical_rank", "fusion_rank",
        "financial_v2_score", "rrf_score", "normalized_rrf", "best_channel_rank",
        "metric_row_score", "statement_scope_score", "year_score",
        "company_source_score", "content_type_score",
        "table_semantic_schema_version", "table_semantic_canonical_sha256",
        "statement_title", "statement_type", "table_scope", "unit_text", "unit",
        "currency", "statement_period", "column_bindings", "binding_source_page",
        "binding_method", "binding_confidence", "continuation_from_page",
        "statement_anchor_bbox", "unit_anchor_bbox", "table_bbox",
    }
    return {key: value for key, value in context.items() if key in allowed}


def _candidate_ranking_identity(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    identity = []
    for case in cases:
        item = {"case_id": case["case_id"]}
        for profile in ("legacy", "financial_v2"):
            item[profile] = [
                candidate.get("candidate_id")
                for candidate in case["profiles"][profile]["ranking"]
            ]
        identity.append(item)
    return identity


def _candidate_canonical_identity(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": payload.get("schema_version"),
        "status": payload.get("status"),
        "ground_truth_loaded": payload.get("ground_truth_loaded"),
        "inputs": payload.get("inputs"),
        "configuration": payload.get("configuration"),
        "ranking_sha256": payload.get("ranking_sha256"),
        "cases": payload.get("cases"),
    }


def candidate_stage(root: Path, run_dir: Path, diagnostic_k: int) -> dict[str, Any]:
    if diagnostic_k < 5:
        raise HoldoutPipelineError("diagnostic_k 必须至少为 5")
    config_path = run_dir / "retrieval_config.json"
    output_path = artifact_path(run_dir, "candidate")
    _ensure_outputs_absent(config_path, output_path)
    corpus_path = artifact_path(run_dir, "build-corpus")
    embedding_path = artifact_path(run_dir, "embed")
    corpus = _require_pre_gt(load_json(corpus_path), "build-corpus")
    embedding = _require_pre_gt(load_json(embedding_path), "embed")
    evaluator = _load_script("holdout_candidate", "05_evaluate_paddleocr_retrieval.py")
    identity = evaluator.embedding_identity()
    if embedding.get("embedding_identity") != identity:
        raise HoldoutPipelineError("当前 embedding identity 与冻结 cache 不一致")
    queries = _load_queries(root)
    texts = _corpus_texts(root, corpus)
    vectors, cache_stats = evaluator.get_embeddings_cache_only(
        texts, run_dir / "embedding_cache", identity
    )
    chunk_count = len(corpus["chunks"])
    chunk_vectors, query_vectors = vectors[:chunk_count], vectors[chunk_count:]

    import chromadb
    from app.utils.vector_store import VectorStore

    store = VectorStore(client=chromadb.EphemeralClient(), collection_prefix="holdout_paired")
    by_source: dict[str, list[tuple[dict[str, Any], list[float]]]] = {}
    for item, vector in zip(corpus["chunks"], chunk_vectors):
        by_source.setdefault(str(item["metadata"]["source"]), []).append((item, vector))
    for source, rows in by_source.items():
        doc_id = int(rows[0][0]["metadata"]["doc_id"])
        store.add_documents(
            1,
            [row[0]["content"] for row in rows],
            [row[1] for row in rows],
            doc_id,
            source,
            [row[0]["metadata"] for row in rows],
        )
    if store.get_collection_count(1) != chunk_count:
        raise HoldoutPipelineError("候选索引计数与 routed corpus 不一致")

    cases = []
    for query, vector in zip(queries, query_vectors):
        legacy = store.query_diagnostics(
            1, vector, query["question"], dense_k=diagnostic_k,
            lexical_k=diagnostic_k, numeric_weight=0.15,
        )
        financial = store.query_financial_v2(
            1, vector, query["question"], top_k=5, diagnostic_k=diagnostic_k
        )
        legacy_ranking = [_serialize_context(item) for item in legacy["fusion"]]
        financial_ranking = [_serialize_context(item) for item in financial["ranking"]]
        cases.append({
            "case_id": query["case_id"],
            "question": query["question"],
            "query_text_sha256": evaluator.text_sha256(query["question"]),
            "profiles": {
                "legacy": {"ranking": legacy_ranking, "top_k": legacy_ranking[:5]},
                "financial_v2": {
                    "ranking": financial_ranking,
                    "top_k": financial_ranking[:5],
                    "channels": {
                        name: [_serialize_context(item) for item in rows]
                        for name, rows in financial["channels"].items()
                    },
                },
            },
        })
    retrieval_config = {
        "schema_version": "router-v2-holdout-retrieval-config-v1",
        "profiles": ["legacy", "financial_v2"],
        "top_k": 5,
        "diagnostic_k": diagnostic_k,
        "legacy": {"dense_k": diagnostic_k, "lexical_k": diagnostic_k, "numeric_weight": 0.15},
        "financial_v2": {"implementation": "VectorStore.query_financial_v2"},
        "embedding_identity": identity,
    }
    write_new_json(config_path, retrieval_config)
    ranking_sha = canonical_sha256(_candidate_ranking_identity(cases))
    candidate_cache_identity = canonical_sha256({
        "questions_sha256": canonical_sha256(queries),
        "configuration_sha256": canonical_sha256(retrieval_config),
        "corpus_sha256": corpus["corpus_canonical_sha256"],
    })
    payload = {
        "schema_version": "router-v2-holdout-paired-candidates-v2",
        "status": "completed",
        "ground_truth_loaded": False,
        "api_called": False,
        "inputs": {
            "query_only_file_sha256": file_sha256(root / "query_only.jsonl"),
            "questions_canonical_sha256": canonical_sha256(queries),
            "corpus_file_sha256": file_sha256(corpus_path),
            "corpus_canonical_sha256": corpus["corpus_canonical_sha256"],
            "embedding_summary_file_sha256": file_sha256(embedding_path),
            "retrieval_config_file_sha256": file_sha256(config_path),
            "retrieval_config_canonical_sha256": canonical_sha256(retrieval_config),
            "candidate_cache_identity": candidate_cache_identity,
        },
        "configuration": retrieval_config,
        "embedding_cache": cache_stats,
        "ranking_sha256": ranking_sha,
        "cases": cases,
    }
    payload["candidate_canonical_sha256"] = canonical_sha256(
        _candidate_canonical_identity(payload)
    )
    _assert_no_gt_fields(payload)
    write_new_json(artifact_path(run_dir, "candidate"), payload)
    return payload


def validate_candidate_identity(payload: dict[str, Any]) -> dict[str, str]:
    _require_pre_gt(payload, "candidate")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise HoldoutPipelineError("candidate cases 无效")
    ranking = canonical_sha256(_candidate_ranking_identity(cases))
    canonical = canonical_sha256(_candidate_canonical_identity(payload))
    if payload.get("ranking_sha256") != ranking:
        raise HoldoutPipelineError("candidate ranking identity 不一致")
    if payload.get("candidate_canonical_sha256") != canonical:
        raise HoldoutPipelineError("candidate canonical identity 不一致")
    return {"ranking_sha256": ranking, "candidate_canonical_sha256": canonical}


def freeze_pre_gt_stage(root: Path, run_dir: Path) -> dict[str, Any]:
    ground_truth_path = root / "private" / "ground_truth.json"
    if ground_truth_path.exists():
        raise HoldoutPipelineError("Ground Truth 已存在，不能执行 pre-GT freeze")
    manifest, prereg = _manifest_and_prereg(root)
    candidate_path = artifact_path(run_dir, "candidate")
    candidate = _require_pre_gt(load_json(candidate_path), "candidate")
    identities = validate_candidate_identity(candidate)
    corpus_path = artifact_path(run_dir, "build-corpus")
    corpus = _require_pre_gt(load_json(corpus_path), "build-corpus")
    embedding = _require_pre_gt(load_json(artifact_path(run_dir, "embed")), "embed")
    select = _require_pre_gt(load_json(artifact_path(run_dir, "select")), "select")
    preflight = _require_pre_gt(
        load_json(artifact_path(run_dir, "ocr-preflight")), "ocr-preflight"
    )
    config_path = run_dir / "retrieval_config.json"
    retrieval_config = load_json(config_path)
    required_stage_files = {
        stage: {
            "path": artifact_path(run_dir, stage).name,
            "file_sha256": file_sha256(artifact_path(run_dir, stage)),
        }
        for stage in PRE_GT_STAGES[:-1]
    }
    payload = {
        "schema_version": "router-v2-holdout-pre-gt-freeze-v1",
        "status": "frozen",
        "ground_truth_loaded": False,
        "run_id": run_dir.name,
        "preregistration_file_sha256": file_sha256(root / "preregistration.json"),
        "identities": {
            "source_pdf_file_sha256": {
                item["filename"]: item["sha256"] for item in manifest
            },
            "query_only_file_sha256": file_sha256(root / "query_only.jsonl"),
            "candidate_policy_fingerprint": select["selection_policy"]["router_policy_fingerprint"],
            "parser_profile_fingerprint": corpus["parser_profile_fingerprint"],
            "ocr_engine_fingerprint": preflight["engine"]["configuration_fingerprint"],
            "corpus_file_sha256": file_sha256(corpus_path),
            "corpus_canonical_sha256": corpus["corpus_canonical_sha256"],
            "retrieval_config_file_sha256": file_sha256(config_path),
            "retrieval_config_canonical_sha256": canonical_sha256(retrieval_config),
            "embedding_identity": embedding["embedding_identity"],
            "embedding_namespace_fingerprint": embedding["embedding_namespace_fingerprint"],
            "candidate_file_sha256": file_sha256(candidate_path),
            **identities,
            "scorer_file_sha256": file_sha256(Path(__file__).resolve()),
        },
        "stages": required_stage_files,
        "contract": {
            "required_freeze_identities": prereg["required_freeze_identities"],
            "case_count": prereg["case_count"],
            "report_count": prereg["report_count"],
        },
    }
    write_new_json(artifact_path(run_dir, "freeze-pre-gt"), payload)
    return payload


def _ground_truth_cases(path: Path, queries: list[dict[str, str]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = load_json(path)
    metadata: dict[str, Any] = {}
    if isinstance(payload, dict):
        raw_cases = payload.get("cases")
        metadata = {key: value for key, value in payload.items() if key != "cases"}
    else:
        raw_cases = payload
    if not isinstance(raw_cases, list) or len(raw_cases) != len(queries):
        raise HoldoutPipelineError("Ground Truth case 数与 query-only 不一致")
    by_id: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_cases):
        if not isinstance(raw, dict):
            raise HoldoutPipelineError(f"Ground Truth 第 {index + 1} 项不是对象")
        case_id = str(raw.get("case_id", queries[index]["case_id"]))
        missing = [field for field in GROUND_TRUTH_REQUIRED if raw.get(field) in (None, "")]
        if missing:
            raise HoldoutPipelineError(f"Ground Truth {case_id} 缺少字段: {missing}")
        try:
            expected_page = int(raw["expected_page"])
        except (TypeError, ValueError) as exc:
            raise HoldoutPipelineError(f"Ground Truth {case_id} expected_page 无效") from exc
        if expected_page < 1 or case_id in by_id:
            raise HoldoutPipelineError(f"Ground Truth case_id/page 无效: {case_id}")
        by_id[case_id] = {
            "case_id": case_id,
            **{field: raw[field] for field in GROUND_TRUTH_REQUIRED},
            "expected_page": expected_page,
        }
    expected_ids = [case["case_id"] for case in queries]
    if set(by_id) != set(expected_ids):
        raise HoldoutPipelineError("Ground Truth case_id 集合与 query-only 不一致")
    normalized = [by_id[case_id] for case_id in expected_ids]
    for query, truth in zip(queries, normalized):
        if truth["question"] != query["question"]:
            raise HoldoutPipelineError(f"Ground Truth question 不匹配: {query['case_id']}")
    return normalized, metadata


def _attestation(path: Path) -> tuple[dict[str, Any] | None, bool, list[str]]:
    if not path.is_file():
        return None, False, ["ground_truth_attestation_missing"]
    payload = load_json(path)
    if not isinstance(payload, dict):
        raise HoldoutPipelineError("ground_truth_attestation.json 必须是对象")
    requirements = {
        "ranking_not_viewed": True,
        "human_review_status": "accepted",
        "reviewer_independence_declared": True,
    }
    failures = [key for key, expected in requirements.items() if payload.get(key) != expected]
    origin_fields = ("attestation_type", "draft_origin", "reviewer_type", "created_by")
    declared_origin = " ".join(str(payload.get(key, "")).casefold() for key in origin_fields)
    if any(marker in declared_origin for marker in ("ai_agent", "ai-agent", "agent_draft")):
        failures.append("ai_agent_draft_not_official")
    return payload, not failures, failures


def validate_ground_truth_stage(
    root: Path, run_dir: Path, ground_truth_path: Path, attestation_path: Path
) -> dict[str, Any]:
    freeze_path = artifact_path(run_dir, "freeze-pre-gt")
    freeze = _require_pre_gt(load_json(freeze_path), "freeze-pre-gt")
    queries = _load_queries(root)
    raw_ground_truth = load_json(ground_truth_path)
    if isinstance(raw_ground_truth, dict) and raw_ground_truth.get("schema_version") == GROUND_TRUTH_SCHEMA:
        manifest, _prereg = _manifest_and_prereg(root)
        metadata, cases = validate_ground_truth(ground_truth_path, queries, manifest)
    else:
        cases, metadata = _ground_truth_cases(ground_truth_path, queries)
    attestation, _legacy_eligible, legacy_failures = _attestation(attestation_path)
    manifest, prereg = _manifest_and_prereg(root)
    failures = list(legacy_failures)
    official_eligible = False
    try:
        official_cases, official_attestation = validate_official_bundle(
            ground_truth_path=ground_truth_path,
            attestation_path=attestation_path,
            query_only_path=root / "query_only.jsonl",
            source_manifest_path=root / "source_manifest.json",
            preregistration_path=root / "preregistration.json",
            queries=queries,
            source_manifest=manifest,
        )
        cases = official_cases
        attestation = official_attestation
        official_eligible = True
        failures = []
    except GroundTruthContractError as exc:
        failures.append(str(exc))
    payload = {
        "schema_version": "router-v2-holdout-ground-truth-validation-v1",
        "status": "accepted_for_official_score" if official_eligible else "provisional_only",
        "ground_truth_loaded": True,
        "official_score_eligible": official_eligible,
        "official_score_blockers": failures,
        "inputs": {
            "pre_gt_freeze_file_sha256": file_sha256(freeze_path),
            "ground_truth_file_sha256": file_sha256(ground_truth_path),
            "ground_truth_attestation_file_sha256": (
                file_sha256(attestation_path) if attestation is not None else None
            ),
        },
        "counts": {"case_count": len(cases)},
        "ground_truth_metadata": metadata,
        "attestation": attestation,
    }
    write_new_json(artifact_path(run_dir, "validate-ground-truth"), payload)
    return payload


def gate_b_decision(
    metrics: dict[str, dict[str, Any]], preregistration: dict[str, Any]
) -> dict[str, Any]:
    gates = preregistration["release_gates"]
    legacy = metrics["legacy"]
    financial = metrics["financial_v2"]
    checks = {
        "overall_min": financial["overall"]["recall_at_5"]
        >= float(gates["overall_row_aware_recall_at_5_min"]),
        "new_company_min": financial["subsets"]["new_company"]["recall_at_5"]
        >= float(gates["new_company_row_aware_recall_at_5_min"]),
        "new_year_min": financial["subsets"]["new_year"]["recall_at_5"]
        >= float(gates["new_year_row_aware_recall_at_5_min"]),
        "not_underperform_legacy": financial["overall"]["recall_at_5"]
        >= legacy["overall"]["recall_at_5"],
    }
    if not gates.get("must_not_underperform_legacy", False):
        checks["not_underperform_legacy"] = True
    return {"passed": all(checks.values()), "checks": checks}


def _profile_metrics(
    scored_cases: list[dict[str, Any]], subset_ids: dict[str, set[str]], profile: str
) -> dict[str, Any]:
    def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
        hits = sum(int(row["scores"][profile]["hit"]) for row in rows)
        bindable = sum(int(row["scores"][profile].get("evidence_bindable_at_5", False)) for row in rows)
        reciprocal = sum(
            1 / row["scores"][profile]["hit_rank"]
            for row in rows if row["scores"][profile]["hit_rank"]
        )
        total = len(rows)
        return {
            "cases": total,
            "hits_at_5": hits,
            "recall_at_5": round(hits / total, 6) if total else 0.0,
            "evidence_bindable_at_5": round(bindable / total, 6) if total else 0.0,
            "mrr": round(reciprocal / total, 6) if total else 0.0,
        }
    return {
        "overall": aggregate(scored_cases),
        "subsets": {
            name: aggregate([row for row in scored_cases if row["case_id"] in ids])
            for name, ids in subset_ids.items()
        },
    }


def score_stage(
    root: Path, run_dir: Path, ground_truth_path: Path, attestation_path: Path
) -> dict[str, Any]:
    freeze_path = artifact_path(run_dir, "freeze-pre-gt")
    validation_path = artifact_path(run_dir, "validate-ground-truth")
    candidate_path = artifact_path(run_dir, "candidate")
    freeze = _require_pre_gt(load_json(freeze_path), "freeze-pre-gt")
    validation = load_json(validation_path)
    if validation.get("ground_truth_loaded") is not True:
        raise HoldoutPipelineError("Ground Truth validation 状态无效")
    candidate = _require_pre_gt(load_json(candidate_path), "candidate")
    validate_candidate_identity(candidate)
    if freeze["identities"]["candidate_file_sha256"] != file_sha256(candidate_path):
        raise HoldoutPipelineError("candidate 与 pre-GT freeze identity 不一致")
    if validation["inputs"]["ground_truth_file_sha256"] != file_sha256(ground_truth_path):
        raise HoldoutPipelineError("Ground Truth 与 validation identity 不一致")
    queries = _load_queries(root)
    raw_ground_truth = load_json(ground_truth_path)
    if isinstance(raw_ground_truth, dict) and raw_ground_truth.get("schema_version") == GROUND_TRUTH_SCHEMA:
        manifest, _ = _manifest_and_prereg(root)
        _, truths = validate_ground_truth(ground_truth_path, queries, manifest)
    else:
        truths, _ = _ground_truth_cases(ground_truth_path, queries)
    attestation, attestation_contract_ok, blockers = _attestation(attestation_path)
    validation_official = validation.get("official_score_eligible") is True
    validation_attestation_sha = validation["inputs"].get("ground_truth_attestation_file_sha256")
    actual_attestation_sha = file_sha256(attestation_path) if attestation is not None else None
    official = validation_official and attestation_contract_ok
    if attestation is not None and validation_attestation_sha != actual_attestation_sha:
        raise HoldoutPipelineError("attestation 与 validation identity 不一致")
    if validation_official and not attestation_contract_ok:
        raise HoldoutPipelineError("validation official 状态与当前 attestation 不一致")
    if not validation_official:
        blockers = list(dict.fromkeys([
            *blockers,
            *validation.get("official_score_blockers", []),
            "ground_truth_validation_not_official",
        ]))

    scorer = _load_script("holdout_row_scorer", "compare_table_retrieval.py")
    truth_by_id = {case["case_id"]: case for case in truths}
    scored_cases = []
    for case in candidate["cases"]:
        truth = truth_by_id[case["case_id"]]
        scores = {}
        for profile in ("legacy", "financial_v2"):
            contexts = case["profiles"][profile]["top_k"]
            row_score = scorer.score_case(contexts, truth, scorer="row_strict")
            bindable = False
            if not truth.get("should_refuse") and all(
                context.get("source") and context.get("content") for context in contexts
            ):
                bindable = evidence_preflight(
                    case["question"], build_citation_ledger(contexts)
                ).passed
            scores[profile] = {
                **row_score,
                "evidence_bindable_at_5": bindable,
            }
        scored_cases.append({"case_id": case["case_id"], "scores": scores})
    _, prereg = _manifest_and_prereg(root)
    subset_ids = {
        name: set(values) for name, values in prereg["subsets"].items()
    }
    metrics = {
        profile: _profile_metrics(scored_cases, subset_ids, profile)
        for profile in ("legacy", "financial_v2")
    }
    gate = gate_b_decision(metrics, prereg)
    payload = {
        "schema_version": "router-v2-holdout-score-v1",
        "status": "official" if official else "provisional",
        "provisional": not official,
        "ground_truth_loaded": True,
        "official_score_blockers": blockers,
        "inputs": {
            "pre_gt_freeze_file_sha256": file_sha256(freeze_path),
            "candidate_file_sha256": file_sha256(candidate_path),
            "candidate_canonical_sha256": candidate["candidate_canonical_sha256"],
            "candidate_ranking_sha256": candidate["ranking_sha256"],
            "ground_truth_file_sha256": file_sha256(ground_truth_path),
            "ground_truth_validation_file_sha256": file_sha256(validation_path),
            "ground_truth_attestation_file_sha256": (
                file_sha256(attestation_path) if attestation is not None else None
            ),
            "scorer_file_sha256": file_sha256(Path(__file__).resolve()),
        },
        "attestation": attestation,
        "metrics": metrics,
        "gate_b": gate,
        "cases": scored_cases,
    }
    output = artifact_path(run_dir, "score" if official else "score-provisional")
    write_new_json(output, payload)
    return payload


def finalize_stage(root: Path, run_dir: Path) -> dict[str, Any]:
    freeze_path = artifact_path(run_dir, "freeze-pre-gt")
    score_path = artifact_path(run_dir, "score")
    freeze = _require_pre_gt(load_json(freeze_path), "freeze-pre-gt")
    score = load_json(score_path)
    if score.get("schema_version") != "router-v2-holdout-score-v1":
        raise HoldoutPipelineError("Gate B score schema 无效")
    if score.get("status") != "official" or score.get("provisional") is not False:
        raise HoldoutPipelineError("finalize 只接受满足 attestation 的 official score")
    score_inputs = score.get("inputs") or {}
    required_links = {
        "pre_gt_freeze_file_sha256": file_sha256(freeze_path),
        "candidate_file_sha256": freeze["identities"].get("candidate_file_sha256"),
        "ground_truth_validation_file_sha256": file_sha256(artifact_path(run_dir, "validate-ground-truth")),
        "scorer_file_sha256": file_sha256(Path(__file__).resolve()),
    }
    for name, expected in required_links.items():
        if not expected or score_inputs.get(name) != expected:
            raise HoldoutPipelineError(f"Gate B score identity 不一致: {name}")
    validation = load_json(artifact_path(run_dir, "validate-ground-truth"))
    if validation.get("official_score_eligible") is not True:
        raise HoldoutPipelineError("Ground Truth validation 不具备 official 资格")
    for name in ("ground_truth_file_sha256", "ground_truth_attestation_file_sha256"):
        if score_inputs.get(name) != validation.get("inputs", {}).get(name):
            raise HoldoutPipelineError(f"Gate B score 与 validation identity 不一致: {name}")
    attestation = score.get("attestation") or {}
    required = (
        attestation.get("schema_version") == "router-ground-truth-attestation-v2"
        and attestation.get("ranking_not_viewed") is True
        and attestation.get("human_review_status") == "accepted"
        and attestation.get("reviewer_independence_declared") is True
    )
    if not required:
        raise HoldoutPipelineError("official attestation 边界不满足")
    _, prereg = _manifest_and_prereg(root)
    recomputed_gate = gate_b_decision(score.get("metrics") or {}, prereg)
    if recomputed_gate.get("passed") is not True or score.get("gate_b") != recomputed_gate:
        raise HoldoutPipelineError("Gate B decision 与冻结门槛重算不一致")
    payload = {
        "schema_version": "router-v2-holdout-final-manifest-v1",
        "status": "finalized",
        "immutable": True,
        "run_id": run_dir.name,
        "ground_truth_loaded": True,
        "gate_b_passed": True,
        "inputs": {
            "preregistration_file_sha256": file_sha256(root / "preregistration.json"),
            "pre_gt_freeze_file_sha256": file_sha256(freeze_path),
            "official_score_file_sha256": file_sha256(score_path),
        },
        "frozen_identities": freeze["identities"],
        "metrics": score["metrics"],
    }
    write_new_json(artifact_path(run_dir, "finalize"), payload)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gate B Router V2 holdout 安全流水线")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--run-id", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inventory")
    select = sub.add_parser("select")
    select.add_argument("--max-pages-per-report", type=int, default=80)
    preflight = sub.add_parser("ocr-preflight")
    preflight.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_FILE)
    preflight.add_argument("--device", default="gpu")
    ocr = sub.add_parser("ocr")
    ocr.add_argument("--max-errors", type=int, default=10)
    ocr.add_argument("--device", default="gpu")
    sub.add_parser("ocr-audit")
    l1 = sub.add_parser("build-l1")
    l1.add_argument("--chunk-size", type=int, default=400)
    l1.add_argument("--chunk-overlap", type=int, default=80)
    corpus = sub.add_parser("build-corpus")
    corpus.add_argument("--chunk-size", type=int, default=400)
    corpus.add_argument("--table-row-overlap", type=int, default=1)
    embed = sub.add_parser("embed")
    embed.add_argument("--batch-size", type=int, default=20)
    candidate = sub.add_parser("candidate")
    candidate.add_argument("--diagnostic-k", type=int, default=100)
    sub.add_parser("freeze-pre-gt")
    validate_gt = sub.add_parser("validate-ground-truth")
    validate_gt.add_argument("--ground-truth", type=Path)
    validate_gt.add_argument("--attestation", type=Path)
    score = sub.add_parser("score")
    score.add_argument("--ground-truth", type=Path)
    score.add_argument("--attestation", type=Path)
    sub.add_parser("finalize")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    try:
        run_dir = run_directory(root, args.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        command = args.command
        if command == "inventory":
            payload = inventory_stage(root, run_dir)
        elif command == "select":
            payload = select_stage(root, run_dir, args.max_pages_per_report)
        elif command == "ocr-preflight":
            payload = ocr_preflight_stage(root, run_dir, args.lock_file.resolve(), args.device)
        elif command == "ocr":
            payload = ocr_stage(root, run_dir, args.max_errors, args.device)
        elif command == "ocr-audit":
            payload = ocr_audit_stage(root, run_dir)
        elif command == "build-l1":
            payload = build_l1_stage(root, run_dir, args.chunk_size, args.chunk_overlap)
        elif command == "build-corpus":
            payload = build_corpus_stage(run_dir, args.chunk_size, args.table_row_overlap)
        elif command == "embed":
            payload = asyncio.run(embed_stage(root, run_dir, args.batch_size))
        elif command == "candidate":
            payload = candidate_stage(root, run_dir, args.diagnostic_k)
        elif command == "freeze-pre-gt":
            payload = freeze_pre_gt_stage(root, run_dir)
        elif command in {"validate-ground-truth", "score"}:
            ground_truth = (args.ground_truth or root / "private" / "ground_truth.json").resolve()
            attestation = (
                args.attestation or root / "private" / "ground_truth_attestation.json"
            ).resolve()
            payload = (
                validate_ground_truth_stage(root, run_dir, ground_truth, attestation)
                if command == "validate-ground-truth"
                else score_stage(root, run_dir, ground_truth, attestation)
            )
        elif command == "finalize":
            payload = finalize_stage(root, run_dir)
        else:
            raise HoldoutPipelineError(f"未知命令: {command}")
        print(json.dumps({
            "status": payload.get("status"),
            "command": command,
            "run_id": args.run_id,
            "ground_truth_loaded": payload.get("ground_truth_loaded"),
        }, ensure_ascii=False, indent=2))
        return 0
    except (HoldoutPipelineError, GroundTruthContractError, ValueError, KeyError) as exc:
        print(json.dumps({
            "status": "blocked", "command": args.command, "reason": str(exc)
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
