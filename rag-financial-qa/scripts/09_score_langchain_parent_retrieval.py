from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.atomic_json import write_json_atomic  # noqa: E402
from scripts.audit_paddleocr_candidate_coverage import file_sha256  # noqa: E402
from scripts.compare_table_retrieval import (  # noqa: E402
    load_ground_truth,
    score_case,
)

CANDIDATE_PATH = PROJECT_ROOT / "scripts/08_evaluate_langchain_parent_retrieval.py"
SPEC = importlib.util.spec_from_file_location("langchain_parent_candidates", CANDIDATE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("无法加载LangChain候选脚本")
candidate_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = candidate_module
SPEC.loader.exec_module(candidate_module)

SCORE_SCHEMA = "langchain-parent-retrieval-score-v1"
DEFAULT_GROUND_TRUTH = PROJECT_ROOT / "evals/table_ground_truth.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score frozen LangChain parent candidates.")
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--ground-truth", type=Path, default=DEFAULT_GROUND_TRUTH)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def load_candidates(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("schema_version") != candidate_module.SCHEMA_VERSION
        or payload.get("status") != "completed"
        or payload.get("ground_truth_loaded") is not False
        or payload.get("api_called") is not False
    ):
        raise ValueError("LangChain candidate合同无效")
    cases = payload.get("cases")
    if not isinstance(cases, list) or len(cases) != candidate_module.evaluator.EXPECTED_CASES:
        raise ValueError("LangChain candidate必须包含30个cases")
    expected_ids = [
        f"case_{index:02d}"
        for index in range(candidate_module.evaluator.EXPECTED_CASES)
    ]
    if [str(case.get("case_id")) for case in cases] != expected_ids:
        raise ValueError("LangChain candidate case_id顺序无效")
    for case in cases:
        arm = case.get("langchain_parent")
        if not isinstance(arm, dict):
            raise ValueError("LangChain parent arm缺失")
        ranking = arm.get("ranking")
        top_k = arm.get("top_k")
        if not isinstance(ranking, list) or len(ranking) < 5:
            raise ValueError("LangChain ranking不足5项")
        if not isinstance(top_k, list) or len(top_k) != 5:
            raise ValueError("LangChain top_k必须恰好为5项")
        if [item.get("candidate_id") for item in top_k] != [
            item.get("candidate_id") for item in ranking[:5]
        ]:
            raise ValueError("LangChain top_k必须等于冻结ranking前5项")
    expected_canonical = dict(payload)
    expected_canonical.pop("runtime_seconds", None)
    recorded = expected_canonical.pop("candidate_canonical_sha256", None)
    if candidate_module.evaluator.canonical_sha256(expected_canonical) != recorded:
        raise ValueError("LangChain candidate canonical SHA不一致")
    ranking_identity = [
        {
            "case_id": case["case_id"],
            "langchain_parent": [
                item["candidate_id"]
                for item in case["langchain_parent"]["ranking"]
            ],
        }
        for case in payload["cases"]
    ]
    if candidate_module.evaluator.canonical_sha256(ranking_identity) != payload["ranking_sha256"]:
        raise ValueError("LangChain candidate ranking SHA不一致")
    return payload


def score(candidates: dict[str, Any], truth: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_cases = candidates["cases"]
    if len(truth) != candidate_module.evaluator.EXPECTED_CASES:
        raise ValueError("Ground Truth必须包含30个cases")
    if len(candidate_cases) != len(truth):
        raise ValueError("候选与Ground Truth case数量不一致")
    expected_ids = [f"case_{index:02d}" for index in range(len(truth))]
    if [str(case.get("case_id")) for case in candidate_cases] != expected_ids:
        raise ValueError("候选case_id顺序无效")
    rows = []
    source_page_hits = row_strict_hits = 0
    for candidate_case, ground_truth in zip(candidate_cases, truth, strict=True):
        if candidate_case["question"] != ground_truth["question"]:
            raise ValueError(f"question不一致: {candidate_case['case_id']}")
        top_k = candidate_case["langchain_parent"]["top_k"]
        source_page = score_case(top_k, ground_truth, scorer="source_page")
        row_strict = score_case(top_k, ground_truth, scorer="row_strict")
        source_page_hits += int(source_page["hit"])
        row_strict_hits += int(row_strict["hit"])
        rows.append({
            "case_id": candidate_case["case_id"],
            "ground_truth": ground_truth,
            "source_page": source_page,
            "row_strict": row_strict,
        })
    total = len(rows)
    return {
        "schema_version": SCORE_SCHEMA,
        "status": "completed",
        "metrics": {
            "langchain_parent": {
                "source_page_hits_at_5": source_page_hits,
                "source_page_recall_at_5": round(source_page_hits / total, 6),
                "row_strict_hits_at_5": row_strict_hits,
                "row_strict_recall_at_5": round(row_strict_hits / total, 6),
            }
        },
        "cases": rows,
    }


def main() -> int:
    args = parse_args()
    output = args.output.resolve()
    candidate_module.evaluator.ensure_output_writable(
        output, force=False, canonical_paths=()
    )
    candidates_path = args.candidates.resolve()
    candidates = load_candidates(candidates_path)
    truth_path = args.ground_truth.resolve()
    result = score(candidates, load_ground_truth(truth_path))
    result["inputs"] = {
        "candidate_file_sha256": file_sha256(candidates_path),
        "candidate_canonical_sha256": candidates["candidate_canonical_sha256"],
        "ranking_sha256": candidates["ranking_sha256"],
        "ground_truth_sha256": file_sha256(truth_path),
        "config_sha256": candidates["inputs"]["config_sha256"],
    }
    write_json_atomic(output, result, overwrite=False)
    print(json.dumps({
        "status": "COMPLETED",
        "output": str(output),
        "metrics": result["metrics"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
