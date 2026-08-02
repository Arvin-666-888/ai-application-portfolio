from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "06_score_retrieval_artifact.py"
SPEC = importlib.util.spec_from_file_location("paddle_retrieval_score", SCRIPT)
assert SPEC and SPEC.loader
scorer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = scorer
SPEC.loader.exec_module(scorer)


def _candidate_payload() -> dict:
    candidate = {"candidate_id": "candidate-1", "source": "report.pdf"}
    arm = {
        "dense": [candidate],
        "lexical": [candidate],
        "union": [candidate],
        "fusion": [candidate],
    }
    payload = {
        "schema_version": scorer.CANDIDATE_SCHEMA,
        "status": "completed",
        "ground_truth_loaded": False,
        "api_called": False,
        "inputs": {
            "questions_sha256": "a" * 64,
            "paddle_chunks_sha256": "b" * 64,
            "baseline_corpus_sha256": "c" * 64,
            "paddle_corpus_sha256": "d" * 64,
            "routed_corpus_sha256": None,
            "config_sha256": "e" * 64,
            "candidate_cache_identity": None,
        },
        "configuration": {"retrieval_profile": "legacy"},
        "embedding_cache": {"final_hits": 1},
        "cases": [{
            "case_id": "case_00",
            "question": "营业收入是多少？",
            "baseline": arm,
            "paddle": arm,
        }],
        "runtime_seconds": 1.0,
    }
    payload["inputs"]["candidate_cache_identity"] = scorer.evaluator.candidate_cache_identity(
        payload
    )
    payload["ranking_sha256"] = scorer.evaluator.canonical_sha256(
        scorer.evaluator.candidate_ranking_identity(payload["cases"])
    )
    return scorer.evaluator.attach_candidate_identity(payload)


def test_scorer_rejects_ranking_sha_mismatch_before_loading_ground_truth(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(scorer.evaluator, "EXPECTED_CASES", 1)
    candidate_path = tmp_path / "candidate.json"
    payload = _candidate_payload()
    payload["cases"][0]["baseline"]["fusion"][0]["candidate_id"] = "tampered"
    candidate_path.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "score.json"
    monkeypatch.setattr(
        scorer,
        "parse_args",
        lambda: argparse.Namespace(
            candidates=candidate_path,
            ground_truth=tmp_path / "must-not-load.json",
            output=output,
            force=False,
        ),
    )

    def forbidden_ground_truth(_path):
        raise AssertionError("ground truth must not load after candidate identity failure")

    monkeypatch.setattr(scorer, "load_ground_truth", forbidden_ground_truth)

    assert scorer.main() == 2
    assert not output.exists()


def test_scorer_rejects_canonical_sha_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer.evaluator, "EXPECTED_CASES", 1)
    candidate_path = tmp_path / "candidate.json"
    payload = _candidate_payload()
    payload["embedding_cache"]["final_hits"] = 2
    candidate_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(scorer.EvaluationBlocked, match="canonical SHA"):
        scorer.load_frozen_candidates(candidate_path)


def test_scorer_defaults_to_refuse_existing_output_before_inputs(tmp_path, monkeypatch):
    output = tmp_path / "existing-score.json"
    output.write_text('{"frozen": true}', encoding="utf-8")
    monkeypatch.setattr(
        scorer,
        "parse_args",
        lambda: argparse.Namespace(
            candidates=tmp_path / "must-not-load.json",
            ground_truth=tmp_path / "must-not-load-gt.json",
            output=output,
            force=False,
        ),
    )
    monkeypatch.setattr(
        scorer,
        "load_frozen_candidates",
        lambda _path: (_ for _ in ()).throw(AssertionError("input must not load")),
    )

    assert scorer.main() == 2
    assert json.loads(output.read_text(encoding="utf-8")) == {"frozen": True}


def test_scorer_records_actual_file_canonical_and_ranking_sha(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer.evaluator, "EXPECTED_CASES", 1)
    candidate_path = tmp_path / "candidate.json"
    payload = _candidate_payload()
    candidate_path.write_text(json.dumps(payload), encoding="utf-8")
    ground_truth_path = tmp_path / "gt.json"
    ground_truth_path.write_text("[]", encoding="utf-8")
    output = tmp_path / "score.json"
    monkeypatch.setattr(
        scorer,
        "parse_args",
        lambda: argparse.Namespace(
            candidates=candidate_path,
            ground_truth=ground_truth_path,
            output=output,
            force=False,
        ),
    )
    monkeypatch.setattr(scorer, "load_ground_truth", lambda _path: [])
    monkeypatch.setattr(
        scorer,
        "score_artifact",
        lambda candidates, _truth: {
            "schema_version": scorer.SCORE_SCHEMA,
            "status": "completed",
            "inputs": {
                "candidate_artifact_file_sha256": None,
                "candidate_artifact_canonical_sha256": candidates[
                    "candidate_canonical_sha256"
                ],
                "candidate_ranking_sha256": candidates["ranking_sha256"],
            },
            "metrics": {},
            "cases": [],
        },
    )

    assert scorer.main() == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["inputs"]["candidate_artifact_file_sha256"] == scorer.file_sha256(
        candidate_path
    )
    assert result["inputs"]["candidate_artifact_canonical_sha256"] == payload[
        "candidate_canonical_sha256"
    ]
    assert result["inputs"]["candidate_ranking_sha256"] == payload["ranking_sha256"]
