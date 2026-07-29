from __future__ import annotations

import hashlib
import json

import pytest

from evals.common.ground_truth_contract import (
    GroundTruthContractError,
    validate_official_bundle,
)


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bundle(tmp_path):
    query = tmp_path / "query_only.jsonl"
    query.write_text('{"case_id":"c1","question":"甲公司2024年合并营业收入是多少？"}\n', encoding="utf-8")
    source = tmp_path / "source_manifest.json"
    _write(source, [{"filename": "甲公司.pdf", "page_count": 10}])
    prereg = tmp_path / "preregistration.json"
    _write(prereg, {"case_count": 1, "report_count": 1})
    gt = tmp_path / "private" / "ground_truth.json"
    _write(gt, {
        "schema_version": "router-ground-truth-v2",
        "metadata": {"page_number_basis": "1-based-physical"},
        "cases": [{
            "case_id": "c1",
            "pdf": "甲公司.pdf",
            "question": "甲公司2024年合并营业收入是多少？",
            "metric": "营业收入",
            "expected_value": "100",
            "expected_page": 3,
            "should_refuse": False,
            "expected_unit": "万元",
            "expected_year": "2024",
            "expected_company": "甲公司",
            "expected_scope": "合并",
            "expected_source": "甲公司.pdf",
            "evidence_excerpt": "合并营业收入 100 万元",
            "review_notes": "核对 2024 列与合并口径",
        }],
    })
    attestation = tmp_path / "private" / "ground_truth_attestation.json"
    _write(attestation, {
        "schema_version": "router-ground-truth-attestation-v2",
        "human_review_status": "accepted",
        "reviewer_type": "human",
        "review_mode": "independent_reconstruction_before_comparison",
        "author_id": "author-a",
        "reviewer_id": "reviewer-b",
        "case_count": 1,
        "report_count": 1,
        "reviewed_case_count": 1,
        "page_number_basis": "1-based-physical",
        "ground_truth_file_sha256": _sha(gt),
        "query_only_file_sha256": _sha(query),
        "source_manifest_file_sha256": _sha(source),
        "preregistration_file_sha256": _sha(prereg),
        "ranking_not_viewed": True,
        "candidate_artifacts_not_viewed": True,
        "generation_not_viewed": True,
        "scores_not_viewed": True,
        "ai_draft_not_used": True,
        "reviewer_independence_declared": True,
        "completed_at": "2026-07-29T18:00:00+08:00",
        "signed_declaration": "I independently reviewed all cases.",
    })
    return query, source, prereg, gt, attestation


def test_official_bundle_requires_exact_sha_and_distinct_people(tmp_path):
    query, source, prereg, gt, attestation = _bundle(tmp_path)
    cases, payload = validate_official_bundle(
        ground_truth_path=gt,
        attestation_path=attestation,
        query_only_path=query,
        source_manifest_path=source,
        preregistration_path=prereg,
        queries=[{"case_id": "c1", "question": "甲公司2024年合并营业收入是多少？"}],
        source_manifest=[{"filename": "甲公司.pdf", "page_count": 10}],
    )

    assert cases[0]["expected_scope"] == "合并"
    assert payload["reviewer_id"] == "reviewer-b"

    gt.write_text(gt.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(GroundTruthContractError, match="ground_truth_file_sha256"):
        validate_official_bundle(
            ground_truth_path=gt,
            attestation_path=attestation,
            query_only_path=query,
            source_manifest_path=source,
            preregistration_path=prereg,
            queries=[{"case_id": "c1", "question": "甲公司2024年合并营业收入是多少？"}],
            source_manifest=[{"filename": "甲公司.pdf", "page_count": 10}],
        )


def test_official_bundle_rejects_same_author_and_reviewer(tmp_path):
    query, source, prereg, gt, attestation = _bundle(tmp_path)
    payload = json.loads(attestation.read_text(encoding="utf-8"))
    payload["reviewer_id"] = payload["author_id"]
    _write(attestation, payload)

    with pytest.raises(GroundTruthContractError, match="distinct_author_reviewer"):
        validate_official_bundle(
            ground_truth_path=gt,
            attestation_path=attestation,
            query_only_path=query,
            source_manifest_path=source,
            preregistration_path=prereg,
            queries=[{"case_id": "c1", "question": "甲公司2024年合并营业收入是多少？"}],
            source_manifest=[{"filename": "甲公司.pdf", "page_count": 10}],
        )
