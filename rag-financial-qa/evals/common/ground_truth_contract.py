from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


GROUND_TRUTH_SCHEMA = "router-ground-truth-v2"
ATTESTATION_SCHEMA = "router-ground-truth-attestation-v2"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_CASE_FIELDS = (
    "case_id", "pdf", "question", "metric", "expected_value", "expected_page",
    "should_refuse", "expected_unit", "expected_year", "expected_company",
    "expected_scope", "expected_source", "evidence_excerpt", "review_notes",
)
TRUE_DECLARATIONS = (
    "ranking_not_viewed", "candidate_artifacts_not_viewed", "generation_not_viewed",
    "scores_not_viewed", "ai_draft_not_used", "reviewer_independence_declared",
)


class GroundTruthContractError(ValueError):
    pass


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> Any:
    if not path.is_file():
        raise GroundTruthContractError(f"missing file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GroundTruthContractError(f"invalid JSON: {path}") from exc


def _cases(payload: Any) -> list[dict[str, Any]]:
    cases = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(cases, list) or not cases:
        raise GroundTruthContractError("Ground Truth cases must be a non-empty array")
    return cases


def validate_ground_truth(
    path: Path,
    queries: list[dict[str, Any]],
    source_manifest: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = _load(path)
    if not isinstance(payload, dict) or payload.get("schema_version") != GROUND_TRUTH_SCHEMA:
        raise GroundTruthContractError("Ground Truth v2 schema is required for official scoring")
    raw_cases = _cases(payload)
    expected_ids = [str(item.get("case_id", "")) for item in queries]
    if len(raw_cases) != len(expected_ids):
        raise GroundTruthContractError("Ground Truth case count mismatch")
    by_id = {}
    source_pages = {
        str(item.get("filename", "")): int(item.get("page_count") or 0)
        for item in source_manifest
    }
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise GroundTruthContractError("Ground Truth case must be an object")
        missing = [field for field in REQUIRED_CASE_FIELDS if field not in raw]
        if missing:
            raise GroundTruthContractError(f"Ground Truth case missing fields: {missing}")
        case_id = str(raw["case_id"])
        if not case_id or case_id in by_id:
            raise GroundTruthContractError("Ground Truth case_id must be unique")
        should_refuse = raw["should_refuse"]
        if not isinstance(should_refuse, bool):
            raise GroundTruthContractError(f"{case_id}: should_refuse must be boolean")
        if should_refuse:
            if not str(raw.get("refusal_reason", "")).strip():
                raise GroundTruthContractError(f"{case_id}: refusal_reason is required")
        else:
            required_values = REQUIRED_CASE_FIELDS[1:]
            empty = [field for field in required_values if raw.get(field) in (None, "")]
            if empty:
                raise GroundTruthContractError(f"{case_id}: answerable case has empty fields: {empty}")
        pdf = str(raw["pdf"])
        if pdf not in source_pages:
            raise GroundTruthContractError(f"{case_id}: PDF is not in source manifest")
        if not should_refuse:
            page = raw["expected_page"]
            if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= source_pages[pdf]:
                raise GroundTruthContractError(f"{case_id}: expected_page must be 1-based physical page")
            if not isinstance(raw["expected_value"], str):
                raise GroundTruthContractError(f"{case_id}: expected_value must be a string")
        by_id[case_id] = dict(raw)
    if set(by_id) != set(expected_ids):
        raise GroundTruthContractError("Ground Truth case IDs mismatch query-only")
    normalized = [by_id[case_id] for case_id in expected_ids]
    for query, truth in zip(queries, normalized):
        if truth["question"] != query.get("question"):
            raise GroundTruthContractError(f"{truth['case_id']}: question mismatch")
    return payload, normalized


def validate_attestation(
    path: Path,
    *,
    ground_truth_path: Path,
    query_only_path: Path,
    source_manifest_path: Path,
    preregistration_path: Path,
    case_count: int,
    report_count: int,
) -> dict[str, Any]:
    payload = _load(path)
    if not isinstance(payload, dict) or payload.get("schema_version") != ATTESTATION_SCHEMA:
        raise GroundTruthContractError("attestation v2 schema is required for official scoring")
    requirements = {
        "human_review_status": "accepted",
        "reviewer_type": "human",
        "review_mode": "independent_reconstruction_before_comparison",
        "page_number_basis": "1-based-physical",
    }
    failures = [key for key, value in requirements.items() if payload.get(key) != value]
    failures.extend(key for key in TRUE_DECLARATIONS if payload.get(key) is not True)
    origin = " ".join(
        str(payload.get(key, "")).casefold()
        for key in ("attestation_type", "draft_origin", "reviewer_type", "created_by")
    )
    if any(marker in origin for marker in ("ai_agent", "ai-agent", "agent_draft")):
        failures.append("ai_agent_draft_not_official")
    author = str(payload.get("author_id", "")).strip()
    reviewer = str(payload.get("reviewer_id", "")).strip()
    if not author or not reviewer or author == reviewer:
        failures.append("distinct_author_reviewer")
    if payload.get("case_count") != case_count or payload.get("reviewed_case_count") != case_count:
        failures.append("reviewed_case_count")
    if payload.get("report_count") != report_count:
        failures.append("report_count")
    if not str(payload.get("completed_at", "")).strip():
        failures.append("completed_at")
    if not str(payload.get("signed_declaration", "")).strip():
        failures.append("signed_declaration")
    expected_shas = {
        "ground_truth_file_sha256": file_sha256(ground_truth_path),
        "query_only_file_sha256": file_sha256(query_only_path),
        "source_manifest_file_sha256": file_sha256(source_manifest_path),
        "preregistration_file_sha256": file_sha256(preregistration_path),
    }
    for key, expected in expected_shas.items():
        if payload.get(key) != expected or not SHA256_RE.fullmatch(str(payload.get(key, ""))):
            failures.append(key)
    if failures:
        raise GroundTruthContractError("attestation contract failed: " + ", ".join(dict.fromkeys(failures)))
    return payload


def validate_official_bundle(
    *,
    ground_truth_path: Path,
    attestation_path: Path,
    query_only_path: Path,
    source_manifest_path: Path,
    preregistration_path: Path,
    queries: list[dict[str, Any]],
    source_manifest: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    _payload, cases = validate_ground_truth(ground_truth_path, queries, source_manifest)
    attestation = validate_attestation(
        attestation_path,
        ground_truth_path=ground_truth_path,
        query_only_path=query_only_path,
        source_manifest_path=source_manifest_path,
        preregistration_path=preregistration_path,
        case_count=len(cases),
        report_count=len(source_manifest),
    )
    return cases, attestation
