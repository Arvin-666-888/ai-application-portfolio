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

DEFAULT_CANDIDATES = (
    PROJECT_ROOT
    / "evals"
    / "task2_paddleocr"
    / "manifest"
    / "candidate_pages.json"
)
DEFAULT_GROUND_TRUTH = PROJECT_ROOT / "evals" / "table_ground_truth.json"
DEFAULT_OUTPUT = DEFAULT_CANDIDATES.with_name("candidate_coverage_audit.json")
EXPECTED_CASES = 30
EXPECTED_UNIQUE_TARGETS = 20


class AuditInputError(ValueError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="事后审计 PaddleOCR 候选页覆盖率；不修改候选清单。",
    )
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--ground-truth", type=Path, default=DEFAULT_GROUND_TRUTH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    if not path.is_file():
        raise AuditInputError(f"输入文件不存在: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise AuditInputError(f"JSON 无效 {path}: {exc}") from exc


def validate_ground_truth(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or not payload:
        raise AuditInputError("ground truth 必须是非空数组")
    required = ("pdf", "question", "metric", "expected_value", "expected_page")
    cases = []
    for index, case in enumerate(payload, 1):
        if not isinstance(case, dict):
            raise AuditInputError(f"ground truth 第 {index} 项不是对象")
        missing = [key for key in required if case.get(key) in (None, "")]
        if missing:
            raise AuditInputError(
                f"ground truth 第 {index} 项缺少字段: {', '.join(missing)}"
            )
        page = case["expected_page"]
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise AuditInputError(
                f"ground truth 第 {index} 项 expected_page 必须是正整数"
            )
        cases.append(case)
    return cases


def _validate_page_item(
    item: Any,
    *,
    report_source: str,
    page_count: int,
    bucket: str,
) -> tuple[str, int]:
    if not isinstance(item, dict):
        raise AuditInputError(f"{report_source} {bucket} 页面项不是对象")
    source = item.get("source")
    page = item.get("page_number")
    if source != report_source:
        raise AuditInputError(
            f"{report_source} {bucket} 子项 source 不一致: {source}"
        )
    if isinstance(page, bool) or not isinstance(page, int):
        raise AuditInputError(f"{report_source} {bucket} page_number 不是整数")
    if not 1 <= page <= page_count:
        raise AuditInputError(
            f"{report_source} {bucket} 页码超出范围: {page}/{page_count}"
        )
    return source, page


def validate_candidate_manifest(
    payload: Any,
) -> tuple[dict[tuple[str, int], dict[str, Any]], set[str]]:
    if not isinstance(payload, dict):
        raise AuditInputError("candidate manifest 不是对象")
    if payload.get("schema_version") != "candidate-pages-v1":
        raise AuditInputError("candidate manifest schema_version 不受支持")
    policy = payload.get("selection_policy")
    if not isinstance(policy, dict) or policy.get("uses_ground_truth") is not False:
        raise AuditInputError("候选选择必须显式声明 uses_ground_truth=false")
    reports = payload.get("reports")
    if not isinstance(reports, list) or not reports:
        raise AuditInputError("candidate manifest reports 必须是非空数组")

    index: dict[tuple[str, int], dict[str, Any]] = {}
    report_names: set[str] = set()
    selected_total = dropped_total = 0

    for report in reports:
        if not isinstance(report, dict):
            raise AuditInputError("candidate report 不是对象")
        source = report.get("source")
        page_count = report.get("page_count")
        if not isinstance(source, str) or not source:
            raise AuditInputError("candidate report source 无效")
        if source in report_names:
            raise AuditInputError(f"candidate report source 重复: {source}")
        report_names.add(source)
        if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 1:
            raise AuditInputError(f"{source} page_count 无效")

        selected = report.get("selected_pages")
        dropped = report.get("dropped_pages")
        if not isinstance(selected, list) or not isinstance(dropped, list):
            raise AuditInputError(f"{source} selected/dropped 必须是数组")
        if report.get("selected_count") != len(selected):
            raise AuditInputError(f"{source} selected_count 与数组长度不一致")
        if report.get("dropped_count") != len(dropped):
            raise AuditInputError(f"{source} dropped_count 与数组长度不一致")

        local_seen: set[tuple[str, int]] = set()
        for bucket, items in (("selected", selected), ("dropped", dropped)):
            for item in items:
                key = _validate_page_item(
                    item,
                    report_source=source,
                    page_count=page_count,
                    bucket=bucket,
                )
                if key in local_seen or key in index:
                    raise AuditInputError(
                        f"候选页重复或 selected/dropped 交叉: {source} p{key[1]}"
                    )
                local_seen.add(key)
                index[key] = {
                    "bucket": bucket,
                    "reasons": list(item.get("reasons") or []),
                }
        selected_total += len(selected)
        dropped_total += len(dropped)

    if payload.get("report_count") != len(reports):
        raise AuditInputError("顶层 report_count 与数组长度不一致")
    if payload.get("selected_page_count") != selected_total:
        raise AuditInputError("顶层 selected_page_count 与分报告合计不一致")
    if payload.get("dropped_page_count") != dropped_total:
        raise AuditInputError("顶层 dropped_page_count 与分报告合计不一致")
    return index, report_names


def build_audit(
    cases: list[dict[str, Any]],
    candidate_payload: dict[str, Any],
    *,
    candidate_sha256: str,
    ground_truth_sha256: str,
) -> dict[str, Any]:
    candidate_index, report_names = validate_candidate_manifest(candidate_payload)
    unknown = sorted({str(case["pdf"]) for case in cases} - report_names)
    if unknown:
        raise AuditInputError(
            "ground truth 引用了候选清单不存在的报告: " + ", ".join(unknown)
        )

    target_case_counts = Counter(
        (str(case["pdf"]), int(case["expected_page"])) for case in cases
    )
    targets = []
    report_stats: dict[str, Counter[str]] = {
        source: Counter() for source in sorted(report_names)
    }

    for (source, page), case_count in sorted(target_case_counts.items()):
        candidate = candidate_index.get((source, page))
        bucket = candidate["bucket"] if candidate else "missing"
        report_stats[source]["unique_target_count"] += 1
        report_stats[source][f"{bucket}_target_count"] += 1
        report_stats[source][f"{bucket}_case_count"] += case_count
        targets.append({
            "source": source,
            "page_number": page,
            "case_count": case_count,
            "status": bucket,
            "candidate_reasons": candidate["reasons"] if candidate else [],
        })

    selected_targets = sum(item["status"] == "selected" for item in targets)
    dropped_targets = sum(item["status"] == "dropped" for item in targets)
    missing_targets = sum(item["status"] == "missing" for item in targets)
    selected_cases = sum(
        item["case_count"] for item in targets if item["status"] == "selected"
    )
    dropped_cases = sum(
        item["case_count"] for item in targets if item["status"] == "dropped"
    )
    missing_cases = sum(
        item["case_count"] for item in targets if item["status"] == "missing"
    )
    expected_shape = (
        len(cases) == EXPECTED_CASES
        and len(targets) == EXPECTED_UNIQUE_TARGETS
    )
    all_selected = dropped_targets == 0 and missing_targets == 0

    violations = []
    if len(cases) != EXPECTED_CASES:
        violations.append(
            f"ground_truth_case_count={len(cases)}, expected={EXPECTED_CASES}"
        )
    if len(targets) != EXPECTED_UNIQUE_TARGETS:
        violations.append(
            "unique_target_count="
            f"{len(targets)}, expected={EXPECTED_UNIQUE_TARGETS}"
        )
    for item in targets:
        if item["status"] != "selected":
            violations.append(
                f"{item['status']}:{item['source']}:p{item['page_number']}"
            )

    return {
        "schema_version": "candidate-coverage-audit-v1",
        "status": "passed" if expected_shape and all_selected else "failed",
        "inputs": {
            "candidate_manifest_sha256": candidate_sha256,
            "ground_truth_sha256": ground_truth_sha256,
        },
        "selection_independence": {
            "selector_declares_uses_ground_truth": False,
            "audit_output_is_pipeline_input": False,
        },
        "counts": {
            "ground_truth_cases": len(cases),
            "unique_target_pages": len(targets),
            "selected_target_pages": selected_targets,
            "dropped_target_pages": dropped_targets,
            "missing_target_pages": missing_targets,
            "selected_cases": selected_cases,
            "dropped_cases": dropped_cases,
            "missing_cases": missing_cases,
        },
        "rates": {
            "unique_target_selected_recall": round(
                selected_targets / max(len(targets), 1), 6
            ),
            "case_selected_recall": round(
                selected_cases / max(len(cases), 1), 6
            ),
        },
        "reports": [
            {
                "source": source,
                "unique_target_pages": stats["unique_target_count"],
                "selected_target_pages": stats["selected_target_count"],
                "dropped_target_pages": stats["dropped_target_count"],
                "missing_target_pages": stats["missing_target_count"],
            }
            for source, stats in report_stats.items()
            if stats["unique_target_count"]
        ],
        "targets": targets,
        "violations": violations,
        "next_step": (
            "覆盖审计通过；可进入 OCR runner validate-only。"
            if expected_shape and all_selected
            else "覆盖审计未通过；停止，不启动批量 OCR。"
        ),
    }


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    candidate_path = args.candidates.resolve()
    ground_truth_path = args.ground_truth.resolve()
    try:
        candidate_payload = load_json(candidate_path)
        cases = validate_ground_truth(load_json(ground_truth_path))
        audit = build_audit(
            cases,
            candidate_payload,
            candidate_sha256=file_sha256(candidate_path),
            ground_truth_sha256=file_sha256(ground_truth_path),
        )
        write_json_atomic(args.output.resolve(), audit)
    except AuditInputError as exc:
        print(f"[FAILED] {exc}", file=sys.stderr)
        return 1

    counts = audit["counts"]
    print(
        f"[{audit['status'].upper()}] cases={counts['ground_truth_cases']} "
        f"unique={counts['unique_target_pages']} "
        f"selected={counts['selected_target_pages']} "
        f"dropped={counts['dropped_target_pages']} "
        f"missing={counts['missing_target_pages']}"
    )
    return 0 if audit["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
