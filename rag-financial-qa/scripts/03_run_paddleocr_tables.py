from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as package_metadata
import json
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from packaging.utils import canonicalize_name

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.audit_paddleocr_candidate_coverage import (  # noqa: E402
    AuditInputError,
    file_sha256,
    load_json,
    validate_candidate_manifest,
)
from scripts.paddleocr_target_page_smoke import (  # noqa: E402
    extract_page_as_pdf,
    result_json_payload,
)

DEFAULT_CANDIDATES = (
    PROJECT_ROOT
    / "evals"
    / "task2_paddleocr"
    / "manifest"
    / "candidate_pages.json"
)
DEFAULT_PDF_DIR = PROJECT_ROOT / "evals" / "task2_chinese_financial_reports"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "evals" / "task2_paddleocr"
DEFAULT_SMOKE_SUMMARY = DEFAULT_OUTPUT_DIR / "reports" / "smoke_summary.json"
DEFAULT_LOCK_FILE = PROJECT_ROOT / "requirements-paddleocr-windows-py312.lock.txt"
EXPECTED_PAGES = 400
EXPECTED_REPORTS = 5
EXPECTED_PAGES_PER_REPORT = 80
PAGE_SCHEMA = "paddleocr-table-page-v1"
AUDIT_SCHEMA = "paddleocr-batch-audit-v1"
DIAGNOSTIC_LIMIT = 20

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


class BatchInputError(ValueError):
    pass


class StaleArtifactError(BatchInputError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="对已冻结的 400 个候选页运行可恢复 PP-StructureV3 表格解析。",
    )
    parser.add_argument("--candidate-manifest", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--smoke-summary", type=Path, default=DEFAULT_SMOKE_SUMMARY)
    parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_FILE)
    parser.add_argument("--device", default="gpu")
    parser.add_argument("--max-errors", type=int, default=10)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--validate-only", action="store_true")
    mode.add_argument("--audit-only", action="store_true")
    return parser.parse_args()


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _parse_locked_versions(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise BatchInputError(f"PaddleOCR 锁定文件不存在: {path}")
    versions = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if "==" not in line or line.startswith("#"):
            continue
        name, version = line.split("==", 1)
        normalized = canonicalize_name(name.strip())
        if not normalized or not version.strip():
            raise BatchInputError(f"PaddleOCR 锁定文件存在无效依赖行: {line}")
        if normalized in versions:
            raise BatchInputError(f"PaddleOCR 锁定文件依赖重复: {normalized}")
        versions[normalized] = version.strip()
    required = {
        "paddleocr",
        "paddlex",
        "paddlepaddle-gpu",
        "pymupdf",
    }
    missing = sorted(required - set(versions))
    if missing:
        raise BatchInputError(
            "PaddleOCR 锁定文件缺少版本: " + ", ".join(missing)
        )
    return versions


def build_engine_profile(
    device: str,
    lock_file: Path,
) -> dict[str, Any]:
    if not device.strip():
        raise BatchInputError("device 不能为空")
    profile = {
        "configuration": {**ENGINE_CONFIGURATION, "device": device},
        "locked_versions": _parse_locked_versions(lock_file),
        "lock_file_sha256": file_sha256(lock_file),
    }
    canonical = json.dumps(
        profile,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    profile["configuration_fingerprint"] = hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    return profile


def _validate_smoke_summary(path: Path) -> dict[str, Any]:
    payload = load_json(path)
    if not isinstance(payload, dict):
        raise BatchInputError("smoke summary 不是对象")
    gate = payload.get("gate")
    if (
        payload.get("status") != "passed"
        or not isinstance(gate, dict)
        or int(payload.get("passed_pages", 0))
        < int(gate.get("required_passed_pages", 0))
    ):
        raise BatchInputError("PP-StructureV3 五页 smoke 门禁未通过")
    return payload


def _load_jobs(
    manifest_path: Path,
    pdf_dir: Path,
) -> tuple[list[dict[str, Any]], str]:
    payload = load_json(manifest_path)
    if not isinstance(payload, dict):
        raise BatchInputError("candidate manifest 不是对象")
    try:
        validate_candidate_manifest(payload)
    except AuditInputError as exc:
        raise BatchInputError(str(exc)) from exc

    reports = payload["reports"]
    if len(reports) != EXPECTED_REPORTS:
        raise BatchInputError(
            f"候选报告数必须为 {EXPECTED_REPORTS}，实际 {len(reports)}"
        )
    jobs = []
    for doc_id, report in enumerate(reports, 1):
        selected = report["selected_pages"]
        if len(selected) != EXPECTED_PAGES_PER_REPORT:
            raise BatchInputError(
                f"{report['source']} 候选页必须为 {EXPECTED_PAGES_PER_REPORT}，"
                f"实际 {len(selected)}"
            )
        source_path = pdf_dir / report["source"]
        if not source_path.is_file():
            raise BatchInputError(f"PDF 不存在: {source_path}")
        actual_sha = file_sha256(source_path)
        if actual_sha != report["pdf_sha256"]:
            raise BatchInputError(f"PDF SHA-256 不匹配: {report['source']}")
        for item in sorted(selected, key=lambda value: value["page_number"]):
            page_number = int(item["page_number"])
            jobs.append({
                "doc_id": doc_id,
                "source": report["source"],
                "source_path": source_path,
                "pdf_sha256": actual_sha,
                "page_number": page_number,
                "reasons": sorted(set(item.get("reasons") or [])),
            })
    if len(jobs) != EXPECTED_PAGES:
        raise BatchInputError(
            f"候选页总数必须为 {EXPECTED_PAGES}，实际 {len(jobs)}"
        )
    keys = {(job["source"], job["page_number"]) for job in jobs}
    if len(keys) != len(jobs):
        raise BatchInputError("候选任务存在重复 source/page")
    return jobs, file_sha256(manifest_path)


def artifact_path(raw_dir: Path, job: dict[str, Any]) -> Path:
    return (
        raw_dir
        / job["pdf_sha256"][:12]
        / f"p{job['page_number']:04d}.json"
    )


def _artifact_identity_matches(
    payload: dict[str, Any],
    job: dict[str, Any],
    profile: dict[str, Any],
) -> bool:
    engine = payload.get("engine")
    return (
        payload.get("schema_version") == PAGE_SCHEMA
        and payload.get("source") == job["source"]
        and payload.get("pdf_sha256") == job["pdf_sha256"]
        and payload.get("physical_page_number") == job["page_number"]
        and isinstance(engine, dict)
        and engine.get("configuration_fingerprint")
        == profile["configuration_fingerprint"]
    )


def _validate_completed_artifact(payload: dict[str, Any]) -> bool:
    mapping = payload.get("single_page_result")
    tables = payload.get("tables")
    table_count = payload.get("table_count")
    if (
        not isinstance(mapping, dict)
        or mapping.get("page_index") != 0
        or mapping.get("page_count") != 1
        or mapping.get("page_mapping_ok") is not True
        or not isinstance(tables, list)
        or isinstance(table_count, bool)
        or not isinstance(table_count, int)
        or table_count != len(tables)
        or payload.get("error") is not None
    ):
        return False
    for index, table in enumerate(tables):
        if not isinstance(table, dict):
            return False
        html = table.get("pred_html")
        ocr_text = table.get("ocr_text")
        digest = table.get("table_content_sha256")
        if (
            table.get("table_index") != index
            or not isinstance(html, str)
            or not isinstance(ocr_text, str)
            or not isinstance(digest, str)
            or digest != table_content_digest(html, ocr_text)
        ):
            return False
    return True


def classify_artifact(
    path: Path,
    job: dict[str, Any],
    profile: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    if not path.is_file():
        return "missing", None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "stale", None
    if not isinstance(payload, dict) or not _artifact_identity_matches(
        payload, job, profile
    ):
        return "stale", payload if isinstance(payload, dict) else None
    status = payload.get("status")
    if status == "completed":
        return (
            ("completed", payload)
            if _validate_completed_artifact(payload)
            else ("stale", payload)
        )
    if status == "failed":
        error = payload.get("error")
        if (
            isinstance(payload.get("tables"), list)
            and payload.get("tables") == []
            and payload.get("table_count") == 0
            and isinstance(error, dict)
            and isinstance(error.get("type"), str)
            and isinstance(error.get("message"), str)
        ):
            return "failed", payload
        return "stale", payload
    return "stale", payload


def validate_preflight(
    candidate_manifest: Path,
    pdf_dir: Path,
    output_dir: Path,
    smoke_summary: Path,
    lock_file: Path,
    device: str,
) -> dict[str, Any]:
    _validate_smoke_summary(smoke_summary)
    jobs, candidate_sha = _load_jobs(candidate_manifest, pdf_dir)
    profile = build_engine_profile(device, lock_file)
    raw_dir = output_dir / "raw"
    artifact_states = Counter()
    stale_keys = []
    expected_paths = {
        artifact_path(raw_dir, job).resolve() for job in jobs
    }
    actual_paths = (
        {path.resolve() for path in raw_dir.rglob("*.json")}
        if raw_dir.is_dir()
        else set()
    )
    unexpected = sorted(str(path) for path in actual_paths - expected_paths)
    if unexpected:
        raise StaleArtifactError(
            "检测到候选清单之外的 OCR artifacts，禁止在长时运行后才失败: "
            + ", ".join(unexpected[:DIAGNOSTIC_LIMIT])
        )
    for job in jobs:
        state, _ = classify_artifact(
            artifact_path(raw_dir, job),
            job,
            profile,
        )
        artifact_states[state] += 1
        if state == "stale" and len(stale_keys) < DIAGNOSTIC_LIMIT:
            stale_keys.append(f"{job['source']}:p{job['page_number']}")
    if artifact_states["stale"]:
        raise StaleArtifactError(
            "检测到与当前 PDF/配置不兼容的 OCR artifacts，禁止静默覆盖: "
            + ", ".join(stale_keys)
        )
    return {
        "status": "passed",
        "mode": "validate-only",
        "candidate_manifest_sha256": candidate_sha,
        "expected_pages": len(jobs),
        "expected_reports": len({job["source"] for job in jobs}),
        "artifact_states": dict(artifact_states),
        "engine": profile,
        "jobs": jobs,
        "raw_dir": raw_dir,
    }


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _iter_strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_strings(child)


def table_content_digest(pred_html: str, ocr_text: str) -> str:
    content_identity = json.dumps(
        {"pred_html": pred_html, "ocr_text": ocr_text},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(content_identity.encode("utf-8")).hexdigest()


def project_tables(payload: dict[str, Any]) -> list[dict[str, Any]]:
    tables = payload.get("table_res_list", [])
    if tables is None:
        tables = []
    if not isinstance(tables, list):
        raise BatchInputError("PP-StructureV3 table_res_list 不是数组")
    projected = []
    for table_index, table in enumerate(tables):
        if not isinstance(table, dict):
            raise BatchInputError("table_res_list 子项不是对象")
        html = table.get("pred_html")
        if not isinstance(html, str):
            html = ""
        ocr_source = table.get("table_ocr_pred", {})
        ocr_text = "\n".join(_iter_strings(ocr_source))
        projected.append({
            "table_index": table_index,
            "pred_html": html,
            "ocr_text": ocr_text,
            "table_content_sha256": table_content_digest(html, ocr_text),
        })
    return projected


def build_completed_artifact(
    job: dict[str, Any],
    profile: dict[str, Any],
    result_payload: dict[str, Any],
    elapsed_seconds: float,
) -> dict[str, Any]:
    page_index = result_payload.get("page_index")
    page_count = result_payload.get("page_count")
    page_mapping_ok = page_index == 0 and page_count == 1
    if not page_mapping_ok:
        raise BatchInputError(
            f"单页结果页码映射错误: page_index={page_index}, page_count={page_count}"
        )
    tables = project_tables(result_payload)
    return {
        "schema_version": PAGE_SCHEMA,
        "status": "completed",
        "source": job["source"],
        "pdf_sha256": job["pdf_sha256"],
        "physical_page_number": job["page_number"],
        "candidate_reasons": job["reasons"],
        "single_page_result": {
            "page_index": page_index,
            "page_count": page_count,
            "page_mapping_ok": True,
        },
        "engine": profile,
        "elapsed_seconds": round(elapsed_seconds, 4),
        "table_count": len(tables),
        "tables": tables,
        "error": None,
    }


def build_failed_artifact(
    job: dict[str, Any],
    profile: dict[str, Any],
    exc: Exception,
    elapsed_seconds: float,
) -> dict[str, Any]:
    return {
        "schema_version": PAGE_SCHEMA,
        "status": "failed",
        "source": job["source"],
        "pdf_sha256": job["pdf_sha256"],
        "physical_page_number": job["page_number"],
        "candidate_reasons": job["reasons"],
        "single_page_result": {
            "page_index": None,
            "page_count": None,
            "page_mapping_ok": False,
        },
        "engine": profile,
        "elapsed_seconds": round(elapsed_seconds, 4),
        "table_count": 0,
        "tables": [],
        "error": {
            "type": type(exc).__name__,
            "message": str(exc)[:500],
        },
    }


def audit_artifacts(
    jobs: list[dict[str, Any]],
    raw_dir: Path,
    profile: dict[str, Any],
    candidate_manifest_sha256: str,
) -> dict[str, Any]:
    expected_paths = {artifact_path(raw_dir, job).resolve() for job in jobs}
    counters = Counter()
    diagnostics: dict[str, list[str]] = {
        "failed_page_keys": [],
        "missing_page_keys": [],
        "stale_page_keys": [],
        "page_mapping_error_keys": [],
        "no_table_page_keys_first_20": [],
        "unexpected_artifacts_first_20": [],
    }
    report_stats: dict[str, Counter[str]] = {
        source: Counter()
        for source in sorted({job["source"] for job in jobs})
    }

    for job in jobs:
        key = f"{job['source']}:p{job['page_number']}"
        state, payload = classify_artifact(
            artifact_path(raw_dir, job), job, profile
        )
        counters[f"{state}_pages"] += 1
        report_stats[job["source"]]["expected_pages"] += 1
        report_stats[job["source"]][f"{state}_pages"] += 1
        if state == "completed" and payload is not None:
            mapping = payload.get("single_page_result") or {}
            if mapping.get("page_mapping_ok") is not True:
                counters["page_mapping_errors"] += 1
                if len(diagnostics["page_mapping_error_keys"]) < DIAGNOSTIC_LIMIT:
                    diagnostics["page_mapping_error_keys"].append(key)
            table_count = int(payload.get("table_count", 0))
            counters["total_tables"] += table_count
            report_stats[job["source"]]["total_tables"] += table_count
            if table_count:
                counters["pages_with_tables"] += 1
                report_stats[job["source"]]["pages_with_tables"] += 1
            else:
                counters["pages_without_tables"] += 1
                report_stats[job["source"]]["pages_without_tables"] += 1
                if len(diagnostics["no_table_page_keys_first_20"]) < DIAGNOSTIC_LIMIT:
                    diagnostics["no_table_page_keys_first_20"].append(key)
        elif state in {"failed", "missing", "stale"}:
            field = f"{state}_page_keys"
            if len(diagnostics[field]) < DIAGNOSTIC_LIMIT:
                diagnostics[field].append(key)

    actual_paths = (
        {path.resolve() for path in raw_dir.rglob("*.json")}
        if raw_dir.is_dir()
        else set()
    )
    unexpected = sorted(str(path) for path in actual_paths - expected_paths)
    counters["unexpected_pages"] = len(unexpected)
    diagnostics["unexpected_artifacts_first_20"] = unexpected[:DIAGNOSTIC_LIMIT]

    passed = (
        counters["completed_pages"] == len(jobs)
        and counters["failed_pages"] == 0
        and counters["missing_pages"] == 0
        and counters["stale_pages"] == 0
        and counters["unexpected_pages"] == 0
        and counters["page_mapping_errors"] == 0
        and counters["total_tables"] > 0
    )
    return {
        "schema_version": AUDIT_SCHEMA,
        "status": "passed" if passed else "incomplete",
        "inputs": {
            "candidate_manifest_sha256": candidate_manifest_sha256,
            "engine_configuration_fingerprint": profile[
                "configuration_fingerprint"
            ],
        },
        "counts": {
            "expected_pages": len(jobs),
            "artifact_pages": len(actual_paths & expected_paths),
            "completed_pages": counters["completed_pages"],
            "failed_pages": counters["failed_pages"],
            "missing_pages": counters["missing_pages"],
            "stale_pages": counters["stale_pages"],
            "unexpected_pages": counters["unexpected_pages"],
            "page_mapping_errors": counters["page_mapping_errors"],
            "pages_with_tables": counters["pages_with_tables"],
            "pages_without_tables": counters["pages_without_tables"],
            "total_tables": counters["total_tables"],
        },
        "per_report": [
            {
                "source": source,
                "expected_pages": stats["expected_pages"],
                "completed_pages": stats["completed_pages"],
                "failed_pages": stats["failed_pages"],
                "missing_pages": stats["missing_pages"],
                "stale_pages": stats["stale_pages"],
                "pages_with_tables": stats["pages_with_tables"],
                "pages_without_tables": stats["pages_without_tables"],
                "total_tables": stats["total_tables"],
            }
            for source, stats in report_stats.items()
        ],
        "bounded_diagnostics": diagnostics,
        "next_step": (
            "OCR 产物完整；可进入表格 chunk 构建。"
            if passed
            else "OCR 产物不完整；重新运行 03，仅补失败或缺失页。"
        ),
    }


def _installed_runtime_versions(
    distributions: Iterable[str],
) -> dict[str, str | None]:
    versions = {}
    for distribution in distributions:
        try:
            versions[distribution] = package_metadata.version(distribution)
        except package_metadata.PackageNotFoundError:
            versions[distribution] = None
    return versions


def run_batch(
    preflight: dict[str, Any],
    output_dir: Path,
    max_errors: int,
    device: str,
) -> dict[str, Any]:
    if max_errors < 1:
        raise BatchInputError("max_errors 必须大于 0")
    profile = dict(preflight["engine"])
    locked = profile["locked_versions"]
    profile["runtime_versions"] = _installed_runtime_versions(locked)
    mismatches = {
        name: {"locked": version, "runtime": profile["runtime_versions"].get(name)}
        for name, version in locked.items()
        if profile["runtime_versions"].get(name) != version
    }
    if mismatches:
        sample = dict(list(mismatches.items())[:DIAGNOSTIC_LIMIT])
        raise BatchInputError(
            "当前 PaddleOCR 环境与锁定版本不一致: "
            + json.dumps(sample, ensure_ascii=False)
        )

    configuration = profile["configuration"]
    try:
        from paddleocr import PPStructureV3

        engine = PPStructureV3(
            device=device,
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
        raise BatchInputError(
            f"PP-StructureV3 导入或初始化失败: {exc}"
        ) from exc

    jobs = preflight["jobs"]
    raw_dir = preflight["raw_dir"]
    jobs_by_state: dict[str, list[dict[str, Any]]] = {
        "missing": [],
        "failed": [],
        "completed": [],
    }
    for job in jobs:
        state, _ = classify_artifact(
            artifact_path(raw_dir, job),
            job,
            preflight["engine"],
        )
        if state == "stale":
            raise StaleArtifactError(
                f"检测到 stale artifact，禁止覆盖: {artifact_path(raw_dir, job)}"
            )
        jobs_by_state[state].append(job)

    ordered_jobs = jobs_by_state["missing"] + jobs_by_state["failed"]
    completed_before_run = len(jobs_by_state["completed"])
    print(
        f"[RESUME] completed={completed_before_run} "
        f"missing={len(jobs_by_state['missing'])} "
        f"retry_failed={len(jobs_by_state['failed'])} "
        f"queued={len(ordered_jobs)}"
    )

    errors = processed = 0
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="paddleocr_batch_") as temporary:
        page_pdf = Path(temporary) / "page.pdf"
        for index, job in enumerate(ordered_jobs, 1):
            target = artifact_path(raw_dir, job)

            page_started = time.perf_counter()
            try:
                if page_pdf.exists():
                    page_pdf.unlink()
                extract_page_as_pdf(
                    job["source_path"],
                    job["page_number"] - 1,
                    page_pdf,
                )
                results = list(engine.predict(str(page_pdf)))
                if len(results) != 1:
                    raise RuntimeError(
                        f"单页 PDF 返回 {len(results)} 个页面结果"
                    )
                payload = result_json_payload(results[0])
                artifact = build_completed_artifact(
                    job,
                    profile,
                    payload,
                    time.perf_counter() - page_started,
                )
                processed += 1
            except Exception as exc:
                artifact = build_failed_artifact(
                    job,
                    profile,
                    exc,
                    time.perf_counter() - page_started,
                )
                errors += 1
            write_json_atomic(target, artifact)

            if index % 10 == 0 or index == len(ordered_jobs) or artifact["status"] == "failed":
                print(
                    f"[OCR] {index}/{len(ordered_jobs)} processed={processed} "
                    f"reused={completed_before_run} errors={errors} "
                    f"tables={artifact['table_count']}"
                )
            if errors >= max_errors:
                break

    summary = audit_artifacts(
        jobs,
        raw_dir,
        profile,
        preflight["candidate_manifest_sha256"],
    )
    summary["run"] = {
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "processed_this_run": processed,
        "reused_completed_pages": completed_before_run,
        "queued_this_run": len(ordered_jobs),
        "errors_this_run": errors,
    }
    write_json_atomic(
        output_dir / "reports" / "ocr_batch_summary.json",
        summary,
    )
    return summary


def main() -> int:
    args = parse_args()
    try:
        preflight = validate_preflight(
            args.candidate_manifest.resolve(),
            args.pdf_dir.resolve(),
            args.output_dir.resolve(),
            args.smoke_summary.resolve(),
            args.lock_file.resolve(),
            args.device,
        )
        if args.validate_only:
            states = preflight["artifact_states"]
            print(
                f"[PASSED] validate-only expected={preflight['expected_pages']} "
                f"completed={states.get('completed', 0)} "
                f"failed={states.get('failed', 0)} "
                f"missing={states.get('missing', 0)}"
            )
            return 0

        if args.audit_only:
            summary = audit_artifacts(
                preflight["jobs"],
                preflight["raw_dir"],
                preflight["engine"],
                preflight["candidate_manifest_sha256"],
            )
            write_json_atomic(
                args.output_dir.resolve()
                / "reports"
                / "ocr_batch_summary.json",
                summary,
            )
            counts = summary["counts"]
            print(
                f"[{summary['status'].upper()}] completed={counts['completed_pages']} "
                f"failed={counts['failed_pages']} missing={counts['missing_pages']} "
                f"tables={counts['total_tables']}"
            )
            return 0 if summary["status"] == "passed" else 2

        summary = run_batch(
            preflight,
            args.output_dir.resolve(),
            args.max_errors,
            args.device,
        )
    except (BatchInputError, AuditInputError) as exc:
        print(f"[FAILED] {exc}", file=sys.stderr)
        return 1

    return 0 if summary["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
