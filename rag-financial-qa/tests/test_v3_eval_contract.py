from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "evals" / "v3" / "validate_run.py"
PREREG = SCRIPT.with_name("preregistration.json")
spec = importlib.util.spec_from_file_location("validate_v3_run", SCRIPT)
validator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(validator)
PREREG_PAYLOAD = json.loads(PREREG.read_text(encoding="utf-8"))
REQUIRED_IDENTITIES = PREREG_PAYLOAD["artifact_contract"]["required_identities"]


def _write(path: Path, payload: dict) -> str:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return validator.file_sha256(path)


def _run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    candidate_path = run_dir / "candidate.json"
    candidate_payload = {
        "schema_version": "rag-answer-v3-candidate-v2",
        "status": "frozen",
        "run_id": "run-1",
        "case_count": 1,
        "cases": [{"case_id": "c1"}],
        "ground_truth_loaded": False,
        "identities": {
            "corpus_file_sha256": "a" * 64,
            "candidate_canonical_identity_sha256": "b" * 64,
            "ranking_sha256": "c" * 64,
            "retrieval_config_sha256": "d" * 64,
            "embedding_identity": "embedding:model:v1",
            "gate_b_official_score_file_sha256": "1" * 64,
            "gate_b_final_manifest_file_sha256": "2" * 64,
            "ground_truth_file_sha256": "3" * 64,
            "ground_truth_attestation_file_sha256": "4" * 64,
        },
    }
    candidate_sha = _write(candidate_path, candidate_payload)

    generation_path = run_dir / "generation.json"
    generation_payload = {
        "schema_version": "rag-answer-v3-generation-v1",
        "status": "completed",
        "run_id": "run-1",
        "case_count": 1,
        "cases": [{"case_id": "c1"}],
        "ground_truth_loaded": False,
        "identities": {
            "candidate_file_sha256": candidate_sha,
            "prompt_config_sha256": "e" * 64,
            "model_identity": "provider:model:v1",
        },
    }
    generation_sha = _write(generation_path, generation_payload)

    score_path = run_dir / "score.json"
    score_payload = {
        "schema_version": "rag-answer-v3-score-v1",
        "status": "official",
        "run_id": "run-1",
        "case_count": 1,
        "cases": [{"case_id": "c1"}],
        "provisional": False,
        "ground_truth_loaded": True,
        "attestation": {
            "ranking_not_viewed": True,
            "human_review_status": "accepted",
            "reviewer_independence_declared": True,
            "reviewer_type": "human",
        },
        "gate_c": {"passed": True},
        "metrics": {
            profile: {name: 1 for name in PREREG_PAYLOAD["reported_metrics"]}
            for profile in ("legacy", "verified_v3")
        },
        "identities": {
            "generation_file_sha256": generation_sha,
            "scorer_file_sha256": "f" * 64,
            "ground_truth_file_sha256": "3" * 64,
            "ground_truth_attestation_file_sha256": "4" * 64,
        },
    }
    score_sha = _write(score_path, score_payload)

    stages = {
        "candidate": {"path": candidate_path.name, "file_sha256": candidate_sha},
        "generation": {"path": generation_path.name, "file_sha256": generation_sha},
        "score": {"path": score_path.name, "file_sha256": score_sha},
    }
    identities = {
        **candidate_payload["identities"],
        **{key: value for key, value in generation_payload["identities"].items() if key != "candidate_file_sha256"},
        **{key: value for key, value in score_payload["identities"].items() if key != "generation_file_sha256"},
        "candidate_file_sha256": candidate_sha,
        "generation_file_sha256": generation_sha,
        "score_file_sha256": score_sha,
        "gate_b_official_score_file_sha256": "1" * 64,
        "gate_b_final_manifest_file_sha256": "2" * 64,
        "ground_truth_file_sha256": "3" * 64,
        "ground_truth_attestation_file_sha256": "4" * 64,
    }
    assert set(REQUIRED_IDENTITIES).issubset(identities)
    _write(
        run_dir / "manifest.json",
        {
            "schema_version": "rag-answer-v3-run-manifest-v1",
            "run_id": "run-1",
            "status": "finalized",
            "immutable": True,
            "preregistration_sha256": validator.file_sha256(PREREG),
            "thresholds_sha256": validator.file_sha256(PREREG.with_name("thresholds.json")),
            "case_count": 1,
            "identities": identities,
            "stages": stages,
        },
    )
    return run_dir


def _manifest(run_dir: Path) -> tuple[Path, dict]:
    path = run_dir / "manifest.json"
    return path, json.loads(path.read_text(encoding="utf-8"))


def test_v3_run_contract_accepts_complete_frozen_chain(tmp_path):
    manifest = validator.validate_run(_run(tmp_path), PREREG)
    assert manifest["run_id"] == "run-1"


@pytest.mark.parametrize("stage", ["candidate", "generation", "score"])
def test_v3_run_contract_rejects_missing_or_changed_stage(tmp_path, stage):
    run_dir = _run(tmp_path)
    (run_dir / f"{stage}.json").write_text("{}", encoding="utf-8")
    with pytest.raises(validator.V3ManifestError, match="SHA mismatch"):
        validator.validate_run(run_dir, PREREG)


@pytest.mark.parametrize("identity", REQUIRED_IDENTITIES)
def test_v3_run_contract_rejects_tampered_identity(tmp_path, identity):
    run_dir = _run(tmp_path)
    path, manifest = _manifest(run_dir)
    manifest["identities"][identity] = "tampered"
    _write(path, manifest)
    with pytest.raises(validator.V3ManifestError, match="does not match"):
        validator.validate_run(run_dir, PREREG)


def test_v3_run_contract_rejects_missing_required_identity(tmp_path):
    run_dir = _run(tmp_path)
    path, manifest = _manifest(run_dir)
    manifest["identities"].pop(REQUIRED_IDENTITIES[0])
    _write(path, manifest)
    with pytest.raises(validator.V3ManifestError, match="required identities missing"):
        validator.validate_run(run_dir, PREREG)


def test_v3_run_contract_rejects_forbidden_field_and_path_escape(tmp_path):
    run_dir = _run(tmp_path)
    candidate = run_dir / "candidate.json"
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    payload["cases"] = [{"case_id": "c1", "expected_value": "x"}]
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
        validator.validate_run(run_dir, PREREG)

    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    manifest["stages"]["candidate"] = {
        "path": "../outside.json",
        "file_sha256": validator.file_sha256(outside),
    }
    _write(path, manifest)
    with pytest.raises(validator.V3ManifestError, match="escapes run directory"):
        validator.validate_run(run_dir, PREREG)


def test_v3_run_contract_requires_score_ground_truth_and_finalized_status(tmp_path):
    run_dir = _run(tmp_path)
    score = run_dir / "score.json"
    payload = json.loads(score.read_text(encoding="utf-8"))
    payload["ground_truth_loaded"] = False
    score_sha = _write(score, payload)
    path, manifest = _manifest(run_dir)
    manifest["stages"]["score"]["file_sha256"] = score_sha
    manifest["identities"]["score_file_sha256"] = score_sha
    _write(path, manifest)
    with pytest.raises(validator.V3ManifestError, match="score stage must load"):
        validator.validate_run(run_dir, PREREG)

    manifest["status"] = "draft"
    _write(path, manifest)
    with pytest.raises(validator.V3ManifestError, match="finalized and immutable"):
        validator.validate_run(run_dir, PREREG)


def test_v3_run_contract_rejects_ground_truth_in_candidate_stage(tmp_path):
    run_dir = _run(tmp_path)
    candidate = run_dir / "candidate.json"
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    payload["ground_truth_loaded"] = True
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
    with pytest.raises(validator.V3ManifestError, match="must not load ground truth"):
        validator.validate_run(run_dir, PREREG)
