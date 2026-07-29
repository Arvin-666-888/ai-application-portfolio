from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.audit_paddleocr_candidate_coverage import file_sha256  # noqa: E402
from scripts.compare_table_retrieval import (  # noqa: E402
    EvaluationBlocked,
    load_ground_truth,
    row_strict_context_hit,
    score_case,
)
from scripts.atomic_json import write_json_atomic  # noqa: E402

EVALUATOR_PATH = PROJECT_ROOT / "scripts" / "05_evaluate_paddleocr_retrieval.py"
SPEC = importlib.util.spec_from_file_location("paddle_retrieval_candidates", EVALUATOR_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("无法加载候选检索脚本")
evaluator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = evaluator
SPEC.loader.exec_module(evaluator)

CANDIDATE_SCHEMA = evaluator.CANDIDATE_SCHEMA
SCORE_SCHEMA = "paddleocr-retrieval-row-score-v1"
DEFAULT_CANDIDATES = (
    PROJECT_ROOT / "evals" / "task2_paddleocr" / "reports" / "retrieval_v1_candidates.json"
)
DEFAULT_GROUND_TRUTH = PROJECT_ROOT / "evals" / "table_ground_truth.json"
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "evals" / "task2_paddleocr" / "reports" / "retrieval_v1_row_strict.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score a frozen retrieval artifact without rerunning retrieval.")
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--ground-truth", type=Path, default=DEFAULT_GROUND_TRUTH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--force",
        action="store_true",
        help="允许覆盖显式指定的非canonical输出；默认canonical路径永不允许覆盖。",
    )
    return parser.parse_args()


def validate_candidate_identity(payload: dict[str, Any]) -> dict[str, str]:
    try:
        ranking_sha = evaluator.canonical_sha256(
            evaluator.candidate_ranking_identity(payload["cases"])
        )
        canonical_sha = evaluator.candidate_canonical_sha256(payload)
        cache_identity = evaluator.candidate_cache_identity(payload)
    except (KeyError, TypeError, AttributeError) as exc:
        raise EvaluationBlocked("候选artifact identity结构无效") from exc
    if (payload.get("inputs") or {}).get("candidate_cache_identity") != cache_identity:
        raise EvaluationBlocked("候选artifact cache identity不一致")
    if payload.get("ranking_sha256") != ranking_sha:
        raise EvaluationBlocked("候选artifact ranking SHA不一致")
    if payload.get("candidate_canonical_sha256") != canonical_sha:
        raise EvaluationBlocked("候选artifact canonical SHA不一致")
    return {
        "candidate_canonical_sha256": canonical_sha,
        "ranking_sha256": ranking_sha,
    }


def load_frozen_candidates(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise EvaluationBlocked(f"候选artifact不存在: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvaluationBlocked("候选artifact不是有效JSON") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != CANDIDATE_SCHEMA
        or payload.get("status") != "completed"
        or payload.get("ground_truth_loaded") is not False
        or payload.get("api_called") is not False
    ):
        raise EvaluationBlocked("候选artifact合同无效")
    cases = payload.get("cases")
    if not isinstance(cases, list) or len(cases) != evaluator.EXPECTED_CASES:
        raise EvaluationBlocked("候选artifact case数量无效")
    validate_candidate_identity(payload)
    return payload


def first_relevant_rank(contexts: list[dict[str, Any]], case: dict[str, Any]) -> int | None:
    for rank, context in enumerate(contexts, 1):
        if row_strict_context_hit(context, case):
            return rank
    return None


def stage_diagnostics(arm: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    ranks = {
        channel: first_relevant_rank(arm[channel], case)
        for channel in ("dense", "lexical", "union", "fusion")
    }
    union_ids = {item["candidate_id"] for item in arm["union"]}
    fusion_ids = {item["candidate_id"] for item in arm["fusion"]}
    oracle_present = ranks["union"] is not None
    if not oracle_present:
        failure_stage = "strict_chunk_absent"
    elif not any(ranks[channel] is not None for channel in ("dense", "lexical")):
        failure_stage = "not_in_union"
    elif not union_ids <= fusion_ids:
        failure_stage = "lost_during_fusion"
    elif ranks["fusion"] is None or ranks["fusion"] > 5:
        failure_stage = "lost_during_top5"
    else:
        failure_stage = None
    return {
        "first_relevant_rank": ranks,
        "candidate_recall_at": {
            "5": ranks["fusion"] is not None and ranks["fusion"] <= 5,
            "20": ranks["fusion"] is not None and ranks["fusion"] <= 20,
            "50": ranks["fusion"] is not None and ranks["fusion"] <= 50,
            "100": ranks["fusion"] is not None and ranks["fusion"] <= 100,
        },
        "failure_stage": failure_stage,
    }


def score_artifact(candidates: dict[str, Any], ground_truth: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_cases = candidates["cases"]
    expected_ids = [f"case_{index:02d}" for index in range(len(ground_truth))]
    actual_ids = [str(item.get("case_id")) for item in candidate_cases]
    if actual_ids != expected_ids:
        raise EvaluationBlocked("候选artifact case_id顺序无效")

    cases = []
    arm_hits = Counter()
    arm_mrr = Counter()
    failure_stages: dict[str, Counter] = {
        "baseline": Counter(),
        "paddle": Counter(),
        "baseline_v2": Counter(),
        "paddle_v2": Counter(),
    }
    for candidate_case, truth in zip(candidate_cases, ground_truth):
        if candidate_case.get("question") != truth.get("question"):
            raise EvaluationBlocked(f"question不匹配: {candidate_case.get('case_id')}")
        scored = {
            "case_id": candidate_case["case_id"],
            "ground_truth": truth,
        }
        for arm_name in ("baseline", "paddle"):
            arm = candidate_case[arm_name]
            top_five = arm["fusion"][:5]
            historical = score_case(top_five, truth, scorer="historical_chunk_strict")
            row_score = score_case(top_five, truth, scorer="row_strict")
            diagnostics = stage_diagnostics(arm, truth)
            arm_hits[arm_name] += int(row_score["hit"])
            if row_score["hit_rank"]:
                arm_mrr[arm_name] += 1 / row_score["hit_rank"]
            if diagnostics["failure_stage"]:
                failure_stages[arm_name][diagnostics["failure_stage"]] += 1
            scored[arm_name] = {
                "historical_chunk_strict": historical,
                "row_strict": row_score,
                **diagnostics,
            }

        for arm_name in ("baseline_v2", "paddle_v2"):
            if arm_name not in candidate_case:
                continue
            arm = candidate_case[arm_name]
            top_five = arm["top_k"]
            row_score = score_case(top_five, truth, scorer="row_strict")
            ranking_rank = first_relevant_rank(arm["ranking"], truth)
            union_contexts = []
            seen = set()
            for contexts in arm["channels"].values():
                for context in contexts:
                    candidate_id = context["candidate_id"]
                    if candidate_id not in seen:
                        seen.add(candidate_id)
                        union_contexts.append(context)
            oracle_rank = first_relevant_rank(union_contexts, truth)
            diagnostics = {
                "first_relevant_rank": {
                    "union": oracle_rank,
                    "fusion": ranking_rank,
                },
                "candidate_recall_at": {
                    str(cutoff): ranking_rank is not None and ranking_rank <= cutoff
                    for cutoff in (5, 20, 50, 100)
                },
                "failure_stage": (
                    "strict_chunk_absent"
                    if oracle_rank is None
                    else "lost_during_top5"
                    if not row_score["hit"]
                    else None
                ),
            }
            arm_hits[arm_name] += int(row_score["hit"])
            if row_score["hit_rank"]:
                arm_mrr[arm_name] += 1 / row_score["hit_rank"]
            if diagnostics["failure_stage"]:
                failure_stages[arm_name][diagnostics["failure_stage"]] += 1
            scored[arm_name] = {
                "row_strict": row_score,
                **diagnostics,
            }
        cases.append(scored)

    total = len(cases)
    metrics = {}
    arm_names = ["baseline", "paddle"]
    if cases and "baseline_v2" in cases[0]:
        arm_names.extend(("baseline_v2", "paddle_v2"))
    for arm_name in arm_names:
        metrics[arm_name] = {
            "row_strict_hits_at_5": arm_hits[arm_name],
            "row_strict_recall_at_5": round(arm_hits[arm_name] / total, 6),
            "mrr": round(arm_mrr[arm_name] / total, 6),
            "oracle_coverage": sum(
                int(case[arm_name]["first_relevant_rank"]["union"] is not None)
                for case in cases
            ),
            "candidate_recall_at_20": sum(
                int(case[arm_name]["candidate_recall_at"]["20"])
                for case in cases
            ),
            "candidate_recall_at_50": sum(
                int(case[arm_name]["candidate_recall_at"]["50"])
                for case in cases
            ),
            "candidate_recall_at_100": sum(
                int(case[arm_name]["candidate_recall_at"]["100"])
                for case in cases
            ),
            "failure_stages": dict(failure_stages[arm_name]),
        }
    return {
        "schema_version": SCORE_SCHEMA,
        "status": "completed",
        "scorer": "row_strict",
        "inputs": {
            "candidate_artifact_file_sha256": None,
            "candidate_artifact_canonical_sha256": candidates["candidate_canonical_sha256"],
            "candidate_ranking_sha256": candidates["ranking_sha256"],
            "questions_sha256": candidates["inputs"]["questions_sha256"],
            "corpus_sha256": {
                "baseline": candidates["inputs"]["baseline_corpus_sha256"],
                "paddle": candidates["inputs"]["paddle_corpus_sha256"],
            },
            "config_sha256": candidates["inputs"]["config_sha256"],
        },
        "metrics": metrics,
        "cases": cases,
    }


def main() -> int:
    args = parse_args()
    output = args.output.resolve()
    force = bool(getattr(args, "force", False))
    try:
        evaluator.ensure_output_writable(
            output,
            force=force,
            canonical_paths=(DEFAULT_OUTPUT,),
        )
        candidates_path = args.candidates.resolve()
        candidates = load_frozen_candidates(candidates_path)
        ground_truth_path = args.ground_truth.resolve()
        ground_truth = load_ground_truth(ground_truth_path)
        result = score_artifact(candidates, ground_truth)
        result["inputs"]["candidate_artifact_file_sha256"] = file_sha256(candidates_path)
        result["inputs"]["ground_truth_sha256"] = file_sha256(ground_truth_path)
        try:
            write_json_atomic(
                output,
                result,
                overwrite=force and not evaluator._same_path(output, DEFAULT_OUTPUT),
            )
        except FileExistsError as exc:
            raise EvaluationBlocked(f"输出在写入期间已出现，拒绝覆盖: {output}") from exc
        print(json.dumps({
            "status": "COMPLETED",
            "output": str(output),
            "metrics": result["metrics"],
        }, ensure_ascii=False, indent=2))
        return 0
    except EvaluationBlocked as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
