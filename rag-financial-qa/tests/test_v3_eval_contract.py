from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "evals" / "v3" / "validate_run.py"
PREREG = SCRIPT.with_name("preregistration.json")
spec = importlib.util.spec_from_file_location("validate_v3_run", SCRIPT)
validator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = validator
spec.loader.exec_module(validator)
PREREG_PAYLOAD = json.loads(PREREG.read_text(encoding="utf-8"))
REQUIRED_IDENTITIES = PREREG_PAYLOAD["artifact_contract"]["required_identities"]


def _write(path: Path, payload: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return validator.file_sha256(path)


def _bundle(tmp_path: Path) -> Path:
    root = tmp_path / "bundle"
    query = root / "query_only.jsonl"
    query.parent.mkdir(parents=True)
    query.write_text(
        '{"case_id":"c1","question":"甲公司2024年合并营业收入是多少？"}\n',
        encoding="utf-8",
    )
    source = root / "source_manifest.json"
    _write(source, [{"filename": "甲公司.pdf", "page_count": 10}])
    prereg = root / "preregistration.json"
    _write(prereg, {"case_count": 1, "report_count": 1})
    ground_truth = root / "private" / "ground_truth.json"
    _write(
        ground_truth,
        {
            "schema_version": "router-ground-truth-v2",
            "metadata": {"page_number_basis": "1-based-physical"},
            "cases": [
                {
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
                }
            ],
        },
    )
    _write(
        root / "private" / "ground_truth_attestation.json",
        {
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
            "ground_truth_file_sha256": validator.file_sha256(ground_truth),
            "query_only_file_sha256": validator.file_sha256(query),
            "source_manifest_file_sha256": validator.file_sha256(source),
            "preregistration_file_sha256": validator.file_sha256(prereg),
            "ranking_not_viewed": True,
            "candidate_artifacts_not_viewed": True,
            "generation_not_viewed": True,
            "scores_not_viewed": True,
            "ai_draft_not_used": True,
            "reviewer_independence_declared": True,
            "completed_at": "2026-07-29T18:00:00+08:00",
            "signed_declaration": "I independently reviewed all cases.",
        },
    )
    return root


def _output(profile: str) -> dict:
    structured = (
        {
            "facts": [
                {
                    "value_text": "100",
                    "unit": "万元",
                    "year": "2024",
                    "company": "甲公司",
                    "metric": "营业收入",
                    "scope": "合并",
                    "citation_ids": ["C1"],
                }
            ]
        }
        if profile == "verified_v3"
        else None
    )
    return {
        "status": "accepted",
        "answer_text": "甲公司2024年合并营业收入为100万元 [C1]",
        "structured_output": structured,
        "verification": {"passed": True, "errors": []},
        "latency_ms": 10,
        "token_usage": {"input_tokens": 10, "output_tokens": 5},
        "estimated_cost": {"status": "available", "value": 0.001},
    }


def _gate_b_provenance(run_dir: Path, candidate_payload: dict) -> dict:
    gate_b = run_dir / "gate_b"
    paired = gate_b / "paired_candidates.json"
    _write(
        paired,
        {
            "schema_version": "router-v2-holdout-paired-candidates-v2",
            "ground_truth_loaded": False,
            "ranking_sha256": candidate_payload["identities"]["ranking_sha256"],
            "cases": [
                {
                    "case_id": case["case_id"],
                    "question": case["question"],
                    "profiles": {
                        "financial_v2": {"top_k": case["contexts"]},
                        "legacy": {"top_k": []},
                    },
                }
                for case in candidate_payload["cases"]
            ],
        },
    )
    freeze = gate_b / "pre_gt_freeze.json"
    _write(
        freeze,
        {
            "schema_version": "router-v2-holdout-pre-gt-freeze-v1",
            "status": "frozen",
            "ground_truth_loaded": False,
            "identities": {
                "candidate_file_sha256": validator.file_sha256(paired),
                "corpus_file_sha256": candidate_payload["identities"][
                    "corpus_file_sha256"
                ],
                "retrieval_config_canonical_sha256": candidate_payload["identities"][
                    "retrieval_config_sha256"
                ],
                "embedding_identity": candidate_payload["identities"][
                    "embedding_identity"
                ],
            },
        },
    )
    score = gate_b / "score.json"
    _write(
        score,
        {
            "schema_version": "router-v2-holdout-score-v1",
            "status": "official",
            "provisional": False,
            "gate_b": {"passed": True},
            "inputs": {
                "candidate_file_sha256": validator.file_sha256(paired),
                "ground_truth_file_sha256": candidate_payload["identities"][
                    "ground_truth_file_sha256"
                ],
                "ground_truth_attestation_file_sha256": candidate_payload["identities"][
                    "ground_truth_attestation_file_sha256"
                ],
            },
        },
    )
    final_manifest = gate_b / "final_manifest.json"
    _write(
        final_manifest,
        {
            "schema_version": "router-v2-holdout-final-manifest-v1",
            "status": "finalized",
            "immutable": True,
            "gate_b_passed": True,
            "inputs": {
                "official_score_file_sha256": validator.file_sha256(score),
                "pre_gt_freeze_file_sha256": validator.file_sha256(freeze),
            },
            "frozen_identities": {
                "candidate_file_sha256": validator.file_sha256(paired)
            },
        },
    )
    candidate_payload["identities"].update(
        {
            "gate_b_candidate_file_sha256": validator.file_sha256(paired),
            "gate_b_pre_gt_freeze_file_sha256": validator.file_sha256(freeze),
            "gate_b_official_score_file_sha256": validator.file_sha256(score),
            "gate_b_final_manifest_file_sha256": validator.file_sha256(final_manifest),
        }
    )
    return validator.validate_gate_b_provenance(run_dir, candidate_payload)


def _run(tmp_path: Path) -> tuple[Path, Path]:
    bundle_root = _bundle(tmp_path)
    validator.OFFICIAL_BUNDLE_ROOT = bundle_root
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()

    candidate_path = run_dir / "candidate.json"
    candidate_cases = [
        {
            "case_id": "c1",
            "question": "甲公司2024年合并营业收入是多少？",
            "contexts": [
                {"source": "甲公司.pdf", "page_number": 3, "content": "甲公司2024年合并营业收入100万元"}
            ],
        }
    ]
    candidate_payload = {
        "schema_version": "rag-answer-v3-candidate-v2",
        "status": "frozen",
        "run_id": "run-1",
        "case_count": 1,
        "cases": candidate_cases,
        "ground_truth_loaded": False,
        "retrieval_profile": "financial_v2",
        "identities": {
            "corpus_file_sha256": "a" * 64,
            "candidate_canonical_identity_sha256": validator.canonical_sha256(candidate_cases),
            "ranking_sha256": "c" * 64,
            "retrieval_config_sha256": "d" * 64,
            "embedding_identity": "embedding:model:v1",
            "ground_truth_file_sha256": validator.file_sha256(
                bundle_root / "private" / "ground_truth.json"
            ),
            "ground_truth_attestation_file_sha256": validator.file_sha256(
                bundle_root / "private" / "ground_truth_attestation.json"
            ),
        },
    }
    gate_b_provenance = _gate_b_provenance(run_dir, candidate_payload)
    candidate_payload["gate_b_provenance"] = gate_b_provenance
    candidate_sha = _write(candidate_path, candidate_payload)

    generation_path = run_dir / "generation.json"
    generation_payload = {
        "schema_version": "rag-answer-v3-generation-v1",
        "status": "completed",
        "run_id": "run-1",
        "case_count": 1,
        "cases": [
            {
                "case_id": "c1",
                "outputs": {profile: _output(profile) for profile in ("legacy", "verified_v3")},
            }
        ],
        "ground_truth_loaded": False,
        "pricing": {
            "input_cost_per_1m": "10",
            "output_cost_per_1m": "10",
            "currency": "USD",
        },
        "identities": {
            "candidate_file_sha256": candidate_sha,
            "prompt_config_sha256": "e" * 64,
            "model_identity": "provider:model:v1",
        },
    }
    generation_sha = _write(generation_path, generation_payload)

    truths, attestation, bundle = validator.load_official_bundle(bundle_root)
    thresholds = json.loads(PREREG.with_name("thresholds.json").read_text(encoding="utf-8"))
    cases, metrics, gate = validator.recompute_score(
        candidate_cases=candidate_cases,
        generation_cases=generation_payload["cases"],
        truths=truths,
        preregistration=PREREG_PAYLOAD,
        thresholds=thresholds,
        pricing=generation_payload["pricing"],
    )
    assert gate["passed"] is True
    implementation = validator.implementation_descriptor(PROJECT_ROOT)
    score_path = run_dir / "score.json"
    score_payload = {
        "schema_version": "rag-answer-v3-score-v1",
        "status": "official",
        "run_id": "run-1",
        "case_count": 1,
        "cases": cases,
        "provisional": False,
        "ground_truth_loaded": True,
        "official_score_blockers": [],
        "attestation": attestation,
        "official_bundle": bundle,
        "gate_b_provenance": gate_b_provenance,
        "implementation": implementation,
        "gate_c": gate,
        "metrics": metrics,
        "identities": {
            "generation_file_sha256": generation_sha,
            "scorer_file_sha256": implementation["sources"]["evals/v3/scoring_contract.py"],
            "ground_truth_file_sha256": bundle["files"]["ground_truth"]["file_sha256"],
            "ground_truth_attestation_file_sha256": bundle["files"]["attestation"]["file_sha256"],
            "official_bundle_canonical_sha256": bundle["canonical_sha256"],
            "gate_b_provenance_canonical_sha256": gate_b_provenance[
                "canonical_sha256"
            ],
            "implementation_canonical_sha256": implementation["canonical_sha256"],
        },
    }
    score_sha = _write(score_path, score_payload)

    identities = {
        **candidate_payload["identities"],
        "candidate_file_sha256": candidate_sha,
        "generation_file_sha256": generation_sha,
        "score_file_sha256": score_sha,
        "prompt_config_sha256": generation_payload["identities"]["prompt_config_sha256"],
        "model_identity": generation_payload["identities"]["model_identity"],
        "scorer_file_sha256": score_payload["identities"]["scorer_file_sha256"],
        "official_bundle_canonical_sha256": bundle["canonical_sha256"],
        "gate_b_provenance_canonical_sha256": gate_b_provenance["canonical_sha256"],
        "implementation_canonical_sha256": implementation["canonical_sha256"],
    }
    assert set(REQUIRED_IDENTITIES).issubset(identities)
    _write(
        run_dir / "manifest.json",
        {
            "schema_version": "rag-answer-v3-run-manifest-v2",
            "run_id": "run-1",
            "status": "finalized",
            "immutable": True,
            "gate_c_passed": True,
            "preregistration_sha256": validator.file_sha256(PREREG),
            "thresholds_sha256": validator.file_sha256(PREREG.with_name("thresholds.json")),
            "case_count": 1,
            "identities": identities,
            "official_bundle": bundle,
            "gate_b_provenance": gate_b_provenance,
            "implementation": implementation,
            "stages": {
                "candidate": {"path": candidate_path.name, "file_sha256": candidate_sha},
                "generation": {"path": generation_path.name, "file_sha256": generation_sha},
                "score": {"path": score_path.name, "file_sha256": score_sha},
            },
        },
    )
    return run_dir, bundle_root


def _manifest(run_dir: Path) -> tuple[Path, dict]:
    path = run_dir / "manifest.json"
    return path, json.loads(path.read_text(encoding="utf-8"))


def _rehash_score(run_dir: Path) -> None:
    path, manifest = _manifest(run_dir)
    score_sha = validator.file_sha256(run_dir / "score.json")
    manifest["stages"]["score"]["file_sha256"] = score_sha
    manifest["identities"]["score_file_sha256"] = score_sha
    _write(path, manifest)


def test_v3_run_contract_accepts_complete_frozen_chain(tmp_path):
    run_dir, bundle_root = _run(tmp_path)
    manifest = validator.validate_run(run_dir)
    assert manifest["run_id"] == "run-1"


@pytest.mark.parametrize("stage", ["candidate", "generation", "score"])
def test_v3_run_contract_rejects_missing_or_changed_stage(tmp_path, stage):
    run_dir, bundle_root = _run(tmp_path)
    (run_dir / f"{stage}.json").write_text("{}", encoding="utf-8")
    with pytest.raises(validator.V3ManifestError, match="SHA mismatch"):
        validator.validate_run(run_dir)


@pytest.mark.parametrize(
    ("section", "mutate", "message"),
    [
        (
            "cases",
            lambda score: score["cases"][0]["scores"]["verified_v3"].update(
                {"strict_correct": False}
            ),
            "score cases",
        ),
        (
            "metrics",
            lambda score: score["metrics"]["verified_v3"].update(
                {"accepted_answer_strict_precision": 0.0}
            ),
            "score metrics",
        ),
        (
            "gate",
            lambda score: score["gate_c"].update({"checks": {}, "passed": True}),
            "Gate C decision",
        ),
    ],
)
def test_v3_run_contract_rejects_semantic_score_tamper_with_updated_sha(
    tmp_path, section, mutate, message
):
    run_dir, bundle_root = _run(tmp_path)
    score_path = run_dir / "score.json"
    score = json.loads(score_path.read_text(encoding="utf-8"))
    mutate(score)
    _write(score_path, score)
    _rehash_score(run_dir)

    with pytest.raises(validator.V3ManifestError, match=message):
        validator.validate_run(run_dir)


def test_v3_run_contract_rejects_gate_b_source_identity_substitution(tmp_path):
    run_dir, _bundle_root = _run(tmp_path)
    candidate_path = run_dir / "candidate.json"
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["identities"]["corpus_file_sha256"] = "f" * 64
    candidate_sha = _write(candidate_path, candidate)

    generation_path = run_dir / "generation.json"
    generation = json.loads(generation_path.read_text(encoding="utf-8"))
    generation["identities"]["candidate_file_sha256"] = candidate_sha
    generation_sha = _write(generation_path, generation)

    score_path = run_dir / "score.json"
    score = json.loads(score_path.read_text(encoding="utf-8"))
    score["identities"]["generation_file_sha256"] = generation_sha
    score_sha = _write(score_path, score)

    manifest_path, manifest = _manifest(run_dir)
    manifest["stages"]["candidate"]["file_sha256"] = candidate_sha
    manifest["stages"]["generation"]["file_sha256"] = generation_sha
    manifest["stages"]["score"]["file_sha256"] = score_sha
    manifest["identities"]["candidate_file_sha256"] = candidate_sha
    manifest["identities"]["generation_file_sha256"] = generation_sha
    manifest["identities"]["score_file_sha256"] = score_sha
    manifest["identities"]["corpus_file_sha256"] = "f" * 64
    _write(manifest_path, manifest)

    with pytest.raises(validator.V3ManifestError, match="Gate B source identity mismatch"):
        validator.validate_run(run_dir)


def test_v3_run_contract_rejects_post_gt_candidate_rewrite_with_updated_chain(tmp_path):
    run_dir, _bundle_root = _run(tmp_path)
    candidate_path = run_dir / "candidate.json"
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["cases"][0]["contexts"][0]["content"] += " 正确答案已知"
    candidate["identities"]["candidate_canonical_identity_sha256"] = validator.canonical_sha256(
        candidate["cases"]
    )
    candidate_sha = _write(candidate_path, candidate)

    generation_path = run_dir / "generation.json"
    generation = json.loads(generation_path.read_text(encoding="utf-8"))
    generation["identities"]["candidate_file_sha256"] = candidate_sha
    generation_sha = _write(generation_path, generation)

    score_path = run_dir / "score.json"
    score = json.loads(score_path.read_text(encoding="utf-8"))
    score["identities"]["generation_file_sha256"] = generation_sha
    score_sha = _write(score_path, score)

    manifest_path, manifest = _manifest(run_dir)
    manifest["stages"]["candidate"]["file_sha256"] = candidate_sha
    manifest["stages"]["generation"]["file_sha256"] = generation_sha
    manifest["stages"]["score"]["file_sha256"] = score_sha
    manifest["identities"]["candidate_file_sha256"] = candidate_sha
    manifest["identities"]["candidate_canonical_identity_sha256"] = candidate[
        "identities"
    ]["candidate_canonical_identity_sha256"]
    manifest["identities"]["generation_file_sha256"] = generation_sha
    manifest["identities"]["score_file_sha256"] = score_sha
    _write(manifest_path, manifest)

    with pytest.raises(validator.V3ManifestError, match="frozen Gate B artifacts"):
        validator.validate_run(run_dir)


def test_v3_run_contract_rejects_bundle_byte_tamper(tmp_path):
    run_dir, bundle_root = _run(tmp_path)
    ground_truth = bundle_root / "private" / "ground_truth.json"
    ground_truth.write_text(ground_truth.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(validator.V3ManifestError, match="attestation contract failed"):
        validator.validate_run(run_dir)


def test_v3_run_contract_rejects_forged_source_identity(tmp_path):
    run_dir, bundle_root = _run(tmp_path)
    score_path = run_dir / "score.json"
    score = json.loads(score_path.read_text(encoding="utf-8"))
    score["implementation"]["canonical_sha256"] = "f" * 64
    score["identities"]["implementation_canonical_sha256"] = "f" * 64
    _write(score_path, score)
    _rehash_score(run_dir)
    manifest_path, manifest = _manifest(run_dir)
    manifest["implementation"] = score["implementation"]
    manifest["identities"]["implementation_canonical_sha256"] = "f" * 64
    _write(manifest_path, manifest)

    with pytest.raises(validator.V3ManifestError, match="implementation source identity"):
        validator.validate_run(run_dir)


def test_v3_run_contract_rejects_forbidden_field_and_path_escape(tmp_path):
    run_dir, bundle_root = _run(tmp_path)
    candidate = run_dir / "candidate.json"
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    payload["cases"][0]["expected_value"] = "x"
    candidate_sha = _write(candidate, payload)
    path, manifest = _manifest(run_dir)
    manifest["stages"]["candidate"]["file_sha256"] = candidate_sha
    manifest["identities"]["candidate_file_sha256"] = candidate_sha
    generation = run_dir / "generation.json"
    generation_payload = json.loads(generation.read_text(encoding="utf-8"))
    generation_payload["identities"]["candidate_file_sha256"] = candidate_sha
    generation_sha = _write(generation, generation_payload)
    manifest["stages"]["generation"]["file_sha256"] = generation_sha
    manifest["identities"]["generation_file_sha256"] = generation_sha
    score = run_dir / "score.json"
    score_payload = json.loads(score.read_text(encoding="utf-8"))
    score_payload["identities"]["generation_file_sha256"] = generation_sha
    score_sha = _write(score, score_payload)
    manifest["stages"]["score"]["file_sha256"] = score_sha
    manifest["identities"]["score_file_sha256"] = score_sha
    _write(path, manifest)
    with pytest.raises(validator.V3ManifestError, match="forbidden fields"):
        validator.validate_run(run_dir)

    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    manifest["stages"]["candidate"] = {
        "path": "../outside.json",
        "file_sha256": validator.file_sha256(outside),
    }
    _write(path, manifest)
    with pytest.raises(validator.V3ManifestError, match="escapes run directory"):
        validator.validate_run(run_dir)


def test_v3_run_contract_requires_score_ground_truth_and_finalized_status(tmp_path):
    run_dir, bundle_root = _run(tmp_path)
    score = run_dir / "score.json"
    payload = json.loads(score.read_text(encoding="utf-8"))
    payload["ground_truth_loaded"] = False
    _write(score, payload)
    _rehash_score(run_dir)
    with pytest.raises(validator.V3ManifestError, match="score stage must load"):
        validator.validate_run(run_dir)

    path, manifest = _manifest(run_dir)
    manifest["status"] = "draft"
    _write(path, manifest)
    with pytest.raises(validator.V3ManifestError, match="finalized and immutable"):
        validator.validate_run(run_dir)
