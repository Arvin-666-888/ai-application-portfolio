from __future__ import annotations

import argparse
import builtins
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_SCRIPT = ROOT / "scripts/08_evaluate_langchain_parent_retrieval.py"
SPEC = importlib.util.spec_from_file_location("langchain_parent_eval_test", CANDIDATE_SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


SCORER_SCRIPT = ROOT / "scripts/09_score_langchain_parent_retrieval.py"
SCORER_SPEC = importlib.util.spec_from_file_location("langchain_parent_score_test", SCORER_SCRIPT)
assert SCORER_SPEC and SCORER_SPEC.loader
scorer = importlib.util.module_from_spec(SCORER_SPEC)
sys.modules[SCORER_SPEC.name] = scorer
SCORER_SPEC.loader.exec_module(scorer)


def _candidate_payload(case_count: int = 30) -> dict:
    cases = []
    for index in range(case_count):
        ranking = [
            {
                "candidate_id": f"case-{index}-candidate-{rank}",
                "source": "report.pdf",
                "page_number": rank + 1,
                "content": "content",
            }
            for rank in range(6)
        ]
        cases.append({
            "case_id": f"case_{index:02d}",
            "question": f"question-{index}",
            "langchain_parent": {
                "ranking": ranking,
                "top_k": ranking[:5],
            },
        })
    payload = {
        "schema_version": module.SCHEMA_VERSION,
        "status": "completed",
        "ground_truth_loaded": False,
        "api_called": False,
        "inputs": {"config_sha256": "a" * 64},
        "configuration": {},
        "embedding_cache": {},
        "ranking_sha256": module.evaluator.canonical_sha256([
            {
                "case_id": case["case_id"],
                "langchain_parent": [
                    item["candidate_id"]
                    for item in case["langchain_parent"]["ranking"]
                ],
            }
            for case in cases
        ]),
        "cases": cases,
    }
    payload["candidate_canonical_sha256"] = module.evaluator.canonical_sha256(payload)
    return payload


def _write_payload(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_parent_features_reward_query_visible_company_and_penalize_noise():
    good = module._parent_features(
        "美的集团2024年度合并利润表中的营业收入是多少？",
        "美的集团_2024年年度报告.pdf",
        "合并利润表 2024年 营业收入 100",
        [("dense", 2), ("lexical", 3)],
    )
    noisy = module._parent_features(
        "美的集团2024年度合并利润表中的营业收入是多少？",
        "其他公司_2024年年度报告.pdf",
        "2024年主要会计数据和财务指标 营业收入 100",
        [("dense", 1), ("lexical", 1)],
    )

    assert good["company_source_score"] == 1.0
    assert good["statement_score"] == 1.0
    assert noisy["company_source_score"] == 0.0
    assert noisy["noise_score"] == 1.0
    assert module._parent_score(good) > module._parent_score(noisy)


def test_scorer_rejects_truncated_case_set_even_with_recomputed_hashes(tmp_path):
    path = tmp_path / "candidate.json"
    payload = _candidate_payload(case_count=1)
    _write_payload(path, payload)

    with pytest.raises(ValueError, match="30个cases"):
        scorer.load_candidates(path)


def test_scorer_rejects_top_k_not_equal_to_ranking_prefix(tmp_path):
    path = tmp_path / "candidate.json"
    payload = _candidate_payload()
    payload["cases"][0]["langchain_parent"]["top_k"] = [
        payload["cases"][0]["langchain_parent"]["ranking"][5]
    ] * 5
    payload["candidate_canonical_sha256"] = module.evaluator.canonical_sha256({
        key: value
        for key, value in payload.items()
        if key != "candidate_canonical_sha256"
    })
    _write_payload(path, payload)

    with pytest.raises(ValueError, match="冻结ranking前5项"):
        scorer.load_candidates(path)


def test_scorer_rejects_ground_truth_count_mismatch():
    candidates = _candidate_payload()

    with pytest.raises(ValueError, match="Ground Truth必须包含30个cases"):
        scorer.score(candidates, [])


def test_candidate_script_requires_recall_at_five_contract(tmp_path, monkeypatch):
    args = argparse.Namespace(
        top_k=3,
        dense_k=100,
        lexical_k=100,
        corpus=tmp_path / "missing.json",
        questions=tmp_path / "missing.jsonl",
        embedding_cache_dir=tmp_path,
    )
    original_import = builtins.__import__

    def fail_on_optional_import(name, *args, **kwargs):
        if name == "langchain_chroma":
            raise AssertionError("optional dependencies must load after contract validation")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_on_optional_import)

    with pytest.raises(module.evaluator.RetrievalInputError, match="top_k=5"):
        module.run(args)
