from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "evals" / "v3" / "run_eval.py"
SPEC = importlib.util.spec_from_file_location("v3_runner", SCRIPT)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _gate_b(run: Path) -> tuple[Path, Path]:
    paired = run / "paired_candidates.json"
    payload = {
        "schema_version": "router-v2-holdout-paired-candidates-v2",
        "ground_truth_loaded": False,
        "ranking_sha256": "b" * 64,
        "cases": [{
            "case_id": "c1",
            "question": "甲公司2024年营业收入是多少？",
            "profiles": {
                "legacy": {"top_k": [{"source": "a.pdf", "page_number": 1, "content": "旧"}]},
                "financial_v2": {"top_k": [{"source": "a.pdf", "page_number": 2, "content": "甲公司2024年营业收入100万元"}]},
            },
        }],
    }
    _write(paired, payload)
    freeze = run / "pre_gt_freeze.json"
    _write(freeze, {
        "schema_version": "router-v2-holdout-pre-gt-freeze-v1",
        "status": "frozen",
        "ground_truth_loaded": False,
        "identities": {
            "candidate_file_sha256": runner.file_sha256(paired),
            "corpus_file_sha256": "a" * 64,
            "retrieval_config_canonical_sha256": "c" * 64,
            "embedding_identity": "embedding:test:v1",
        },
    })
    score = run / "score.json"
    _write(score, {
        "schema_version": "router-v2-holdout-score-v1",
        "status": "official",
        "provisional": False,
        "gate_b": {"passed": True},
        "inputs": {
            "candidate_file_sha256": runner.file_sha256(paired),
            "ground_truth_file_sha256": "d" * 64,
            "ground_truth_attestation_file_sha256": "e" * 64,
        },
    })
    _write(run / "final_manifest.json", {
        "schema_version": "router-v2-holdout-final-manifest-v1",
        "status": "finalized",
        "immutable": True,
        "gate_b_passed": True,
        "inputs": {
            "official_score_file_sha256": runner.file_sha256(score),
            "pre_gt_freeze_file_sha256": runner.file_sha256(freeze),
        },
        "frozen_identities": {"candidate_file_sha256": runner.file_sha256(paired)},
    })
    return paired, freeze


def test_candidate_copies_only_selected_frozen_contexts(tmp_path):
    paired, freeze = _gate_b(tmp_path / "gate-b")
    payload = runner.candidate_stage(tmp_path / "runs" / "r1", paired, freeze, "financial_v2")

    assert payload["ground_truth_loaded"] is False
    assert payload["retrieval_profile"] == "financial_v2"
    assert payload["cases"][0]["contexts"][0]["page_number"] == 2
    assert "profiles" not in payload["cases"][0]


def test_generate_uses_context_executor_twice_without_retrieval(tmp_path, monkeypatch):
    paired, freeze = _gate_b(tmp_path / "gate-b")
    run_dir = tmp_path / "runs" / "r1"
    runner.candidate_stage(run_dir, paired, freeze, "financial_v2")
    calls = []

    async def fake_execute(question, contexts, history=None, *, answer_profile=None):
        calls.append((question, contexts, answer_profile))
        return SimpleNamespace(
            answer="100万元 [C1]",
            answer_status="verified" if answer_profile == "verified_v3" else "unverified",
            structured_answer=None,
            verification=None,
            refusal_code=None,
            contexts=contexts,
            generation_ms=1,
            verification_ms=1,
            usage={"input_tokens": 10, "output_tokens": 5},
        )

    monkeypatch.setattr(runner, "execute_answer_from_contexts", fake_execute)
    monkeypatch.setattr(runner.settings, "API_KEY", "test-key")
    payload = asyncio.run(runner.generate_stage(run_dir))

    assert [call[2] for call in calls] == ["legacy", "verified_v3"]
    assert all(call[1][0]["page_number"] == 2 for call in calls)
    serialized = json.dumps(payload, ensure_ascii=False)
    assert '"answer"' not in serialized
    assert payload["cases"][0]["outputs"]["verified_v3"]["estimated_cost"]["status"] == "unavailable"


def test_score_rejects_attestation_different_from_gate_b(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    gt = tmp_path / "gt.json"
    _write(gt, {"cases": [{"case_id": "c1", "should_refuse": True}]})
    candidate = {
        "status": "frozen", "ground_truth_loaded": False, "case_count": 1,
        "identities": {
            "ground_truth_file_sha256": runner.file_sha256(gt),
            "ground_truth_attestation_file_sha256": "f" * 64,
        },
        "cases": [{"case_id": "c1", "question": "q", "contexts": []}],
    }
    _write(run_dir / "candidate.json", candidate)
    outputs = {
        profile: {
            "status": "refused", "answer_text": "无法回答", "structured_output": None,
            "verification": {"passed": False, "status": "failed", "errors": ["no_fact_binding"]},
            "latency_ms": 1, "token_usage": {}, "estimated_cost": {"status": "unavailable"},
        } for profile in runner.PROFILES
    }
    _write(run_dir / "generation.json", {
        "identities": {"candidate_file_sha256": runner.file_sha256(run_dir / "candidate.json")},
        "cases": [{"case_id": "c1", "outputs": outputs}],
    })
    monkeypatch.setattr(runner, "_gate", lambda metrics: {"passed": False, "checks": {}})

    with pytest.raises(runner.V3EvalError, match="attestation"):
        runner.score_stage(run_dir, gt, tmp_path / "missing-attestation.json")

    assert not (run_dir / "score_provisional.json").exists()
    assert not (run_dir / "score.json").exists()


def test_verified_score_uses_exact_financial_and_identity_matches():
    truth = {
        "should_refuse": False,
        "expected_value": "100",
        "expected_unit": "元",
        "expected_year": "2024",
        "expected_company": "甲公司",
        "metric": "净利润",
        "expected_scope": "合并",
    }
    output = {
        "status": "accepted",
        "answer_text": "1000万元 [C1]",
        "structured_output": {"facts": [{
            "value_text": "1000", "unit": "万元", "year": "2024",
            "company": "甲公司集团", "metric": "归母净利润", "scope": "合并口径",
        }]},
        "verification": {"passed": True, "errors": []},
    }

    row = runner._score_output(output, truth, "verified_v3")

    assert row["numeric_correct"] is False
    assert row["unit_correct"] is False
    assert row["company_correct"] is False
    assert row["metric_correct"] is False
    assert row["scope_correct"] is False
    assert row["strict_correct"] is False


def test_agent_attestation_is_not_official(tmp_path):
    path = tmp_path / "attestation.json"
    _write(path, {
        "ranking_not_viewed": True,
        "human_review_status": "accepted",
        "reviewer_independence_declared": True,
        "draft_origin": "ai_agent_draft",
    })
    _, official, blockers = runner._attestation(path)
    assert official is False
    assert blockers == ["ai_agent_draft_not_official"]


def test_finalize_rejects_failed_gate_and_overwrite(tmp_path):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    _write(run_dir / "candidate.json", {"identities": {}, "case_count": 1})
    _write(run_dir / "generation.json", {"identities": {}})
    _write(run_dir / "score.json", {
        "status": "official", "provisional": False, "gate_c": {"passed": False},
    })
    with pytest.raises(runner.V3EvalError, match="Gate C"):
        runner.finalize_stage(run_dir)

    _write(run_dir / "manifest.json", {"status": "finalized", "immutable": True})
    with pytest.raises(runner.V3EvalError, match="immutable"):
        runner.finalize_stage(run_dir)
