from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FORBIDDEN_QUERY_FIELDS = {
    "expected_value",
    "expected_page",
    "expected_source",
    "expected_table_id",
    "expected_unit",
    "expected_year",
    "expected_company",
    "expected_scope",
    "should_refuse",
    "evidence_excerpt",
    "review_notes",
    "refusal_reason",
    "answer",
    "ground_truth",
}


class HoldoutValidationError(ValueError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HoldoutValidationError(f"无法读取 JSON: {path}") from exc


def load_query_only(path: Path) -> list[dict[str, str]]:
    cases: list[dict[str, str]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HoldoutValidationError(f"query-only 第 {line_number} 行不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise HoldoutValidationError(f"query-only 第 {line_number} 行必须是对象")
        leaked = FORBIDDEN_QUERY_FIELDS & set(payload)
        if leaked:
            raise HoldoutValidationError(
                f"query-only 第 {line_number} 行含标签字段: {sorted(leaked)}"
            )
        if set(payload) != {"case_id", "question"}:
            raise HoldoutValidationError(
                f"query-only 第 {line_number} 行只允许 case_id/question"
            )
        if not all(isinstance(payload[key], str) and payload[key].strip() for key in payload):
            raise HoldoutValidationError(f"query-only 第 {line_number} 行字段不能为空")
        cases.append(payload)
    if len({case["case_id"] for case in cases}) != len(cases):
        raise HoldoutValidationError("query-only case_id 重复")
    return cases


def validate(root: Path, require_pdfs: bool = False) -> dict[str, Any]:
    preregistration_path = root / "preregistration.json"
    query_path = root / "query_only.jsonl"
    manifest_path = root / "source_manifest.json"
    ground_truth_path = root / "private" / "ground_truth.json"

    preregistration = load_json(preregistration_path)
    manifest = load_json(manifest_path)
    cases = load_query_only(query_path)
    if not isinstance(preregistration, dict) or not isinstance(manifest, list):
        raise HoldoutValidationError("冻结配置或 source manifest 格式无效")
    allowed_statuses = {
        "frozen_before_ground_truth",
        "frozen_before_candidate_generation_and_ground_truth",
    }
    if preregistration.get("status") not in allowed_statuses:
        raise HoldoutValidationError("holdout 尚未处于答案解封前冻结状态")
    if preregistration.get("case_count") != len(cases):
        raise HoldoutValidationError("冻结 case_count 与 query-only 不一致")
    if preregistration.get("report_count") != len(manifest):
        raise HoldoutValidationError("冻结 report_count 与 source manifest 不一致")
    case_prefix = str(preregistration.get("case_id_prefix", "holdout"))
    expected_ids = {f"{case_prefix}_{index:02d}" for index in range(len(cases))}
    if {case["case_id"] for case in cases} != expected_ids:
        raise HoldoutValidationError("query-only case_id 必须连续且不可变")
    subsets = preregistration.get("subsets") or {}
    if subsets:
        subset_ids = [case_id for values in subsets.values() for case_id in values]
        if len(subset_ids) != len(set(subset_ids)) or set(subset_ids) != expected_ids:
            raise HoldoutValidationError("子集必须无重复覆盖全部 case")
    isolation = preregistration.get("data_isolation") or {}
    if isolation.get("selected_reports_use_only_new_companies_and_new_year") is True:
        selected_year = int(isolation.get("selected_report_year", 0))
        excluded_years = {int(value) for value in isolation.get("excluded_report_years") or []}
        excluded_names = {
            str(value).strip().casefold()
            for value in isolation.get("excluded_company_names") or []
            if str(value).strip()
        }
        if len(cases) % len(manifest):
            raise HoldoutValidationError("query cases cannot be evenly mapped to reports")
        cases_per_report = len(cases) // len(manifest)
        if not selected_year or selected_year in excluded_years:
            raise HoldoutValidationError("holdout data isolation contract is inconsistent")
        for index, item in enumerate(manifest):
            company = str(item.get("company", "")).casefold()
            year = int(item.get("report_year", 0))
            filename = str(item.get("filename", ""))
            suffix = f"_{selected_year}年年度报告.pdf"
            alias = filename[:-len(suffix)] if filename.endswith(suffix) else ""
            if year != selected_year or year in excluded_years:
                raise HoldoutValidationError("source manifest contains an excluded report year")
            if any(name in company for name in excluded_names):
                raise HoldoutValidationError("source manifest contains an excluded company")
            report_cases = cases[index * cases_per_report:(index + 1) * cases_per_report]
            if not alias or any(alias not in case["question"] or str(year) not in case["question"] for case in report_cases):
                raise HoldoutValidationError("query company/year mapping does not match manifest")
    forbidden_pre_gt_paths = (
        ground_truth_path,
        root / "private" / "ground_truth_attestation.json",
        root / "ground_truth_unsealed.json",
    )
    existing = [str(path) for path in forbidden_pre_gt_paths if path.exists()]
    if existing:
        raise HoldoutValidationError(
            "pre-GT artifact already exists: " + ", ".join(existing)
        )

    verified_reports = 0
    for item in manifest:
        if not isinstance(item, dict):
            raise HoldoutValidationError("source manifest item 必须是对象")
        required = {"subset", "company", "stock_code", "report_year", "filename", "pdf_url"}
        if not required <= set(item):
            raise HoldoutValidationError("source manifest 缺少必需字段")
        if not str(item["pdf_url"]).startswith("https://static.cninfo.com.cn/"):
            raise HoldoutValidationError("holdout PDF 必须来自冻结的巨潮资讯 HTTPS URL")
        pdf_path = root / "pdfs" / str(item["filename"])
        if not pdf_path.is_file():
            if require_pdfs:
                raise HoldoutValidationError(f"holdout PDF 不存在: {pdf_path}")
            continue
        actual_size = pdf_path.stat().st_size
        actual_sha = file_sha256(pdf_path)
        if item.get("identity_status") != "verified":
            raise HoldoutValidationError(f"已下载 PDF 尚未冻结 identity: {pdf_path.name}")
        if item.get("size_bytes") != actual_size or item.get("sha256") != actual_sha:
            raise HoldoutValidationError(f"PDF identity 不匹配: {pdf_path.name}")
        if not SHA256_RE.fullmatch(actual_sha):
            raise HoldoutValidationError(f"PDF SHA 无效: {pdf_path.name}")
        if not isinstance(item.get("page_count"), int) or item["page_count"] < 1:
            raise HoldoutValidationError(f"PDF page_count 无效: {pdf_path.name}")
        verified_reports += 1

    return {
        "schema_version": "router-v2-holdout-freeze-validation-v1",
        "status": "passed",
        "ground_truth_loaded": False,
        "query_only_sha256": file_sha256(query_path),
        "preregistration_sha256": file_sha256(preregistration_path),
        "source_manifest_sha256": file_sha256(manifest_path),
        "case_count": len(cases),
        "report_count": len(manifest),
        "verified_report_count": verified_reports,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 Router V2 holdout 的答案解封前冻结契约。")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--require-pdfs", action="store_true")
    args = parser.parse_args()
    summary = validate(args.root.resolve(), require_pdfs=args.require_pdfs)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
