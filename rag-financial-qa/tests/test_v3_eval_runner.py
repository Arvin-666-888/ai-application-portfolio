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


def _official_bundle(tmp_path: Path, *, ai_attestation: bool = False) -> Path:
    root = tmp_path / "bundle"
    query = root / "query_only.jsonl"
    query.parent.mkdir(parents=True, exist_ok=True)
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
    attestation = {
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
        "ground_truth_file_sha256": runner.file_sha256(ground_truth),
        "query_only_file_sha256": runner.file_sha256(query),
        "source_manifest_file_sha256": runner.file_sha256(source),
        "preregistration_file_sha256": runner.file_sha256(prereg),
        "ranking_not_viewed": True,
        "candidate_artifacts_not_viewed": True,
        "generation_not_viewed": True,
        "scores_not_viewed": True,
        "ai_draft_not_used": True,
        "reviewer_independence_declared": True,
        "completed_at": "2026-07-29T18:00:00+08:00",
        "signed_declaration": "I independently reviewed all cases.",
    }
    if ai_attestation:
        attestation["draft_origin"] = "ai_agent_draft"
    _write(root / "private" / "ground_truth_attestation.json", attestation)
    return root


def _passing_output(profile: str) -> dict:
    return {
        "status": "accepted",
        "answer_text": "甲公司2024年合并营业收入为100万元 [C1]",
        "structured_output": (
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
        ),
        "verification": {"passed": True, "errors": []},
        "latency_ms": 10,
        "token_usage": {"input_tokens": 10, "output_tokens": 5},
        "estimated_cost": {"status": "available", "value": 0.001},
    }


def _scorable_run(tmp_path: Path, bundle_root: Path) -> Path:
    runner.OFFICIAL_BUNDLE_ROOT = bundle_root
    run_dir = tmp_path / "runs" / "r1"
    paired, freeze = _gate_b(
        tmp_path / "gate-b",
        ground_truth_sha=runner.file_sha256(
            bundle_root / "private" / "ground_truth.json"
        ),
        attestation_sha=runner.file_sha256(
            bundle_root / "private" / "ground_truth_attestation.json"
        ),
    )
    runner.candidate_stage(run_dir, paired, freeze, "financial_v2")
    _write(
        run_dir / "generation.json",
        {
            "schema_version": "rag-answer-v3-generation-v1",
            "status": "completed",
            "run_id": "r1",
            "ground_truth_loaded": False,
            "case_count": 1,
            "pricing": {
                "input_cost_per_1m": "10",
                "output_cost_per_1m": "10",
                "currency": "USD",
            },
            "identities": {
                "candidate_file_sha256": runner.file_sha256(run_dir / "candidate.json"),
                "prompt_config_sha256": "e" * 64,
                "model_identity": "provider:model:v1",
            },
            "cases": [
                {
                    "case_id": "c1",
                    "outputs": {
                        profile: _passing_output(profile) for profile in runner.PROFILES
                    },
                }
            ],
        },
    )
    return run_dir


def _gate_b(
    run: Path,
    *,
    ground_truth_sha: str = "d" * 64,
    attestation_sha: str = "e" * 64,
) -> tuple[Path, Path]:
    paired = run / "paired_candidates.json"
    payload = {
        "schema_version": "router-v2-holdout-paired-candidates-v2",
        "ground_truth_loaded": False,
        "ranking_sha256": "b" * 64,
        "cases": [{
            "case_id": "c1",
            "question": "甲公司2024年合并营业收入是多少？",
            "profiles": {
                "legacy": {"top_k": [{"source": "甲公司.pdf", "page_number": 1, "content": "旧"}]},
                "financial_v2": {"top_k": [{"source": "甲公司.pdf", "page_number": 3, "content": "甲公司2024年合并营业收入100万元"}]},
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
            "ground_truth_file_sha256": ground_truth_sha,
            "ground_truth_attestation_file_sha256": attestation_sha,
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
    assert payload["cases"][0]["contexts"][0]["page_number"] == 3
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
    assert all(call[1][0]["page_number"] == 3 for call in calls)
    serialized = json.dumps(payload, ensure_ascii=False)
    assert '"answer"' not in serialized
    assert payload["cases"][0]["outputs"]["verified_v3"]["estimated_cost"]["status"] == "unavailable"


def test_score_rejects_attestation_different_from_gate_b(tmp_path):
    bundle_root = _official_bundle(tmp_path)
    run_dir = _scorable_run(tmp_path, bundle_root)
    candidate_path = run_dir / "candidate.json"
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["identities"]["ground_truth_attestation_file_sha256"] = "f" * 64
    _write(candidate_path, candidate)
    generation_path = run_dir / "generation.json"
    generation = json.loads(generation_path.read_text(encoding="utf-8"))
    generation["identities"]["candidate_file_sha256"] = runner.file_sha256(candidate_path)
    _write(generation_path, generation)

    with pytest.raises(runner.V3EvalError, match="attestation"):
        runner.score_stage(run_dir)

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

    row = runner._score_output(
        output,
        truth,
        "verified_v3",
        question="甲公司2024年合并净利润是多少？",
        contexts=[
            {
                "source": "甲公司.pdf",
                "page_number": 1,
                "content": "甲公司2024年合并净利润100元",
            }
        ],
    )

    assert row["numeric_correct"] is False
    assert row["unit_correct"] is False
    assert row["company_correct"] is False
    assert row["metric_correct"] is False
    assert row["scope_correct"] is False
    assert row["strict_correct"] is False


def test_ecommerce_verified_score_uses_current_schema_and_ledger():
    truth = {
        "should_refuse": False,
        "fact_type": "price",
        "expected_value": "79.90",
        "expected_currency": "USD",
        "expected_date": "2026-07-15",
        "expected_product": "轻量旅行背包",
        "expected_sku": "SKU-A100",
        "expected_platform": "Amazon",
        "expected_market": "美国",
    }
    output = {
        "status": "accepted",
        "answer_text": "SKU-A100价格为USD 79.90 [C1]。",
        "structured_output": {"facts": [{
            "fact_type": "price", "value_text": "79.90", "currency": "USD",
            "date": "2026-07-15", "product": "轻量旅行背包", "sku": "SKU-A100",
            "platform": "Amazon", "market": "美国", "citation_ids": ["C1"],
        }]},
        "citation_ledger": [{"citation_id": "C1"}],
        "verification": {"passed": True, "errors": []},
    }

    question = "2026-07-15 Amazon美国市场SKU-A100轻量旅行背包价格是多少？"
    contexts = [{
        "source": "catalog.pdf",
        "page_number": 1,
        "content": "2026-07-15 Amazon美国市场轻量旅行背包 SKU-A100 的价格为 USD 79.90。",
    }]
    row = runner._score_output(
        output,
        truth,
        "verified_v3",
        question=question,
        contexts=contexts,
    )
    assert row["strict_correct"] is True
    assert row["citation_valid"] is True

    output["answer_text"] = "SKU-A100价格为USD 79.90 [C99]。"
    output["structured_output"]["facts"][0]["citation_ids"] = ["C99"]
    forged = runner._score_output(
        output,
        truth,
        "verified_v3",
        question=question,
        contexts=contexts,
    )
    assert forged["citation_valid"] is False
    assert forged["unknown_citation_accepted"] is True


def test_agent_attestation_is_not_official(tmp_path):
    bundle_root = _official_bundle(tmp_path, ai_attestation=True)
    run_dir = _scorable_run(tmp_path, bundle_root)

    with pytest.raises(runner.V3EvalError, match="ai_agent_draft_not_official"):
        runner.score_stage(run_dir)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda score: score["cases"][0]["scores"]["verified_v3"].update(
                {"strict_correct": False}
            ),
            "score cases",
        ),
        (
            lambda score: score["metrics"]["verified_v3"].update(
                {"accepted_answer_strict_precision": 0.0}
            ),
            "score metrics",
        ),
        (
            lambda score: score["gate_c"].update({"passed": True, "checks": {}}),
            "Gate C decision",
        ),
    ],
)
def test_finalize_recomputes_cases_metrics_and_gate(tmp_path, mutate, message):
    bundle_root = _official_bundle(tmp_path)
    run_dir = _scorable_run(tmp_path, bundle_root)
    score = runner.score_stage(run_dir)
    assert score["gate_c"]["passed"] is True

    score_path = run_dir / "score.json"
    tampered = json.loads(score_path.read_text(encoding="utf-8"))
    mutate(tampered)
    _write(score_path, tampered)
    with pytest.raises(runner.V3EvalError, match=message):
        runner.finalize_stage(run_dir)


def test_score_recomputes_verification_instead_of_trusting_generation(tmp_path):
    bundle_root = _official_bundle(tmp_path)
    run_dir = _scorable_run(tmp_path, bundle_root)
    generation_path = run_dir / "generation.json"
    generation = json.loads(generation_path.read_text(encoding="utf-8"))
    verified = generation["cases"][0]["outputs"]["verified_v3"]
    verified["structured_output"]["facts"][0]["citation_ids"] = ["C999"]
    verified["verification"] = {"passed": True, "status": "passed", "errors": []}
    _write(generation_path, generation)

    score = runner.score_stage(run_dir)

    case_score = score["cases"][0]["scores"]["verified_v3"]
    assert case_score["citation_valid"] is False
    assert case_score["unknown_citation_accepted"] is True
    assert score["gate_c"]["passed"] is False


def test_score_ignores_self_reported_cost_and_verification_metadata(tmp_path):
    bundle_root = _official_bundle(tmp_path)
    run_dir = _scorable_run(tmp_path, bundle_root)
    generation_path = run_dir / "generation.json"
    generation = json.loads(generation_path.read_text(encoding="utf-8"))
    verified = generation["cases"][0]["outputs"]["verified_v3"]
    verified["estimated_cost"] = {"status": "available", "value": 999.0}
    verified["verification"] = {
        "passed": False,
        "status": "failed",
        "errors": ["forged_reason"],
    }
    _write(generation_path, generation)

    score = runner.score_stage(run_dir)
    metrics = score["metrics"]["verified_v3"]

    assert metrics["estimated_cost"]["total"] == 0.00015
    assert "forged_reason" not in metrics["verification_reason_distribution"]
    assert metrics["verification_reason_distribution"] == {"passed": 1}


def test_answer_coverage_excludes_accepted_refusal_cases():
    from evals.v3.scoring_contract import aggregate

    rows = [
        {
            "accepted": False,
            "refused": True,
            "should_refuse": False,
            "strict_correct": False,
            "numeric_correct": False,
            "unit_correct": False,
            "period_correct": False,
            "company_correct": False,
            "metric_correct": False,
            "scope_correct": False,
            "citation_valid": None,
            "unknown_citation_accepted": False,
            "unsupported_numeric_accepted": False,
            "schema_error": False,
        },
        {
            "accepted": True,
            "refused": False,
            "should_refuse": True,
            "strict_correct": False,
            "numeric_correct": False,
            "unit_correct": False,
            "period_correct": False,
            "company_correct": False,
            "metric_correct": False,
            "scope_correct": False,
            "citation_valid": None,
            "unknown_citation_accepted": False,
            "unsupported_numeric_accepted": False,
            "schema_error": False,
        },
    ]
    outputs = [
        {
            "status": "refused",
            "latency_ms": 1,
            "token_usage": {},
            "estimated_cost": {"status": "unavailable"},
        },
        {
            "status": "accepted",
            "latency_ms": 1,
            "token_usage": {},
            "estimated_cost": {"status": "unavailable"},
        },
    ]

    metrics = aggregate(rows, outputs, pricing={})

    assert metrics["answer_coverage"] == 0.0


def test_finalize_records_fixed_bundle_and_source_identity(tmp_path):
    bundle_root = _official_bundle(tmp_path)
    run_dir = _scorable_run(tmp_path, bundle_root)
    runner.score_stage(run_dir)

    manifest = runner.finalize_stage(run_dir)

    assert manifest["schema_version"] == "rag-answer-v3-run-manifest-v2"
    assert manifest["official_bundle"]["root_locator"] == runner.BUNDLE_ROOT_LOCATOR
    assert manifest["implementation"]["canonical_sha256"] == manifest["identities"][
        "implementation_canonical_sha256"
    ]
    assert manifest["identities"]["scorer_file_sha256"] == manifest[
        "implementation"
    ]["sources"]["evals/v3/scoring_contract.py"]

    with pytest.raises(runner.V3EvalError, match="immutable"):
        runner.finalize_stage(run_dir)


def test_finalize_rejects_failed_gate_and_overwrite(tmp_path):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    _write(run_dir / "candidate.json", {"identities": {}, "case_count": 1})
    _write(run_dir / "generation.json", {"identities": {}})
    _write(
        run_dir / "score.json",
        {"status": "official", "provisional": False, "gate_c": {"passed": False}},
    )
    with pytest.raises(runner.V3EvalError, match="Gate C"):
        runner.finalize_stage(run_dir)

    _write(run_dir / "manifest.json", {"status": "finalized", "immutable": True})
    with pytest.raises(runner.V3EvalError, match="immutable"):
        runner.finalize_stage(run_dir)
