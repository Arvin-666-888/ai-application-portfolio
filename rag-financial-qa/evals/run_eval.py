import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DEBUG", "false")
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings

REFUSAL_PHRASES = (
    "无法回答",
    "资料不足",
    "没有相关信息",
    "根据现有资料无法回答",
    "无法根据现有资料回答",
)


def get_default_top_k() -> int:
    try:
        return int(settings.TOP_K)
    except Exception:
        return 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline evaluation for the cross-border ecommerce RAG knowledge-base demo.")
    parser.add_argument("--kb-id", type=int, help="Knowledge base ID that already contains indexed eval documents.")
    parser.add_argument("--questions", default="evals/questions.jsonl", help="Path to JSONL eval questions.")
    parser.add_argument("--top-k", type=int, default=get_default_top_k(), help="Number of chunks to retrieve per question.")
    parser.add_argument("--retrieval-only", action="store_true", help="Only run retrieval metrics, skip answer generation.")
    parser.add_argument("--max-answer-chars", type=int, default=300, help="Maximum answer characters to print per case.")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Only validate eval dataset structure; does not require a running API or kb-id.",
    )
    return parser.parse_args()


def load_cases(path: str) -> list[dict[str, Any]]:
    cases = []
    with open(path, encoding="utf-8-sig") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {path}:{line_no}: {exc}") from exc
            if not case.get("id") or not case.get("question"):
                raise ValueError(f"Missing id/question at {path}:{line_no}")
            case.setdefault("should_refuse", False)
            case.setdefault("expected_keywords", [])
            case.setdefault("expected_sources", [])
            case.setdefault("expected_context_keywords", [])
            case.setdefault("category", "uncategorized")
            case.setdefault("difficulty", "medium")
            case.setdefault("answer_type", "fact")
            cases.append(case)
    return cases


def dataset_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    by_category: dict[str, int] = {}
    by_difficulty: dict[str, int] = {}
    for case in cases:
        category = case.get("category", "uncategorized")
        difficulty = case.get("difficulty", "medium")
        by_category[category] = by_category.get(category, 0) + 1
        by_difficulty[difficulty] = by_difficulty.get(difficulty, 0) + 1
    return {
        "total_cases": len(cases),
        "answerable_cases": sum(1 for case in cases if not case.get("should_refuse")),
        "refusal_cases": sum(1 for case in cases if case.get("should_refuse")),
        "by_category": by_category,
        "by_difficulty": by_difficulty,
    }


def validate_dataset(cases: list[dict[str, Any]]) -> list[str]:
    errors = []
    seen_ids = set()
    for index, case in enumerate(cases, 1):
        case_id = case.get("id")
        if case_id in seen_ids:
            errors.append(f"duplicate id: {case_id}")
        seen_ids.add(case_id)

        for field in ("category", "difficulty", "answer_type"):
            if not case.get(field):
                errors.append(f"{case_id or index} missing {field}")
        if case.get("difficulty") not in {"easy", "medium", "hard"}:
            errors.append(f"{case_id or index} invalid difficulty: {case.get('difficulty')}")

        if not case.get("should_refuse"):
            for field in (
                "expected_keywords",
                "expected_sources",
                "expected_context_keywords",
                "expected_fact_type",
                "expected_value",
                "expected_sku",
            ):
                if not case.get(field):
                    errors.append(f"{case_id or index} missing {field} for answerable case")
            fact_type = case.get("expected_fact_type")
            if fact_type == "price" and not case.get("expected_currency"):
                errors.append(f"{case_id or index} price case missing expected_currency")
            if fact_type in {"delivery_duration", "customs_duty_rate"} and not case.get("expected_unit"):
                errors.append(f"{case_id or index} unit-bearing case missing expected_unit")

    summary = dataset_summary(cases)
    required_categories = {
        "ecommerce_price", "ecommerce_inventory", "ecommerce_logistics",
        "ecommerce_compliance", "out_of_corpus_sku", "unsupported_fact",
        "multi_fact_guardrail", "complex_formula_guardrail", "insufficient_evidence",
    }
    if summary["answerable_cases"] != 4:
        errors.append("active ecommerce dataset should contain exactly four answerable fact cases")
    if summary["refusal_cases"] < 6:
        errors.append("active ecommerce dataset should include at least 6 refusal cases")
    if not required_categories <= set(summary["by_category"]):
        errors.append("active ecommerce dataset is missing required refusal categories")
    if not {"medium", "hard"} <= set(summary["by_difficulty"]):
        errors.append("active ecommerce dataset should include medium and hard cases")
    return errors


def print_dataset_summary(cases: list[dict[str, Any]]) -> None:
    summary = dataset_summary(cases)
    print("Dataset")
    print(f"total_cases: {summary['total_cases']}")
    print(f"answerable_cases: {summary['answerable_cases']}")
    print(f"refusal_cases: {summary['refusal_cases']}")
    print("by_category:")
    for category, count in sorted(summary["by_category"].items()):
        print(f"  {category}: {count}")
    print("by_difficulty:")
    for difficulty, count in sorted(summary["by_difficulty"].items()):
        print(f"  {difficulty}: {count}")


def normalize_text(text: Any) -> str:
    normalized = str(text).lower()
    return re.sub(r"\s+", "", normalized)


def contains_keyword(text: Any, keyword: Any) -> bool:
    keyword_text = normalize_text(keyword)
    return bool(keyword_text) and keyword_text in normalize_text(text)


def source_name(source: Any) -> str:
    return Path(str(source)).name


def source_matches(source: Any, expected_sources: list[str]) -> bool:
    actual = source_name(source)
    return any(actual == expected or contains_keyword(actual, expected) for expected in expected_sources)


def context_has_keywords(contexts: list[dict[str, Any]], keywords: list[str]) -> bool:
    if not keywords:
        return True
    return any(
        all(contains_keyword(ctx.get("content", ""), keyword) for keyword in keywords)
        for ctx in contexts
    )


def contexts_match_contract(contexts: list[dict[str, Any]], case: dict[str, Any]) -> bool:
    expected_sources = case.get("expected_sources", [])
    expected_keywords = case.get("expected_context_keywords", [])
    return any(
        source_matches(ctx.get("source", ""), expected_sources)
        and all(contains_keyword(ctx.get("content", ""), keyword) for keyword in expected_keywords)
        for ctx in contexts
    )


def has_retrieval_hit(contexts: list[dict[str, Any]], case: dict[str, Any]) -> bool | None:
    if case.get("should_refuse"):
        return None

    expected_sources = case.get("expected_sources", [])
    expected_context_keywords = case.get("expected_context_keywords", [])
    if not expected_sources and not expected_context_keywords:
        return None

    return contexts_match_contract(contexts, case)


def has_source_support(contexts: list[dict[str, Any]], sources: list[dict[str, Any]], case: dict[str, Any]) -> bool | None:
    if case.get("should_refuse"):
        return None

    expected_sources = case.get("expected_sources", [])
    expected_context_keywords = case.get("expected_context_keywords", [])
    if not expected_sources and not expected_context_keywords:
        return None

    source_hit = any(source_matches(source.get("document", ""), expected_sources) for source in sources)
    return source_hit and contexts_match_contract(contexts, case)


def detect_refusal(answer: str) -> bool:
    return any(phrase in answer for phrase in REFUSAL_PHRASES)


def keyword_match_rate(answer: str, expected_keywords: list[str]) -> float | None:
    if not expected_keywords:
        return None
    matched = sum(1 for keyword in expected_keywords if contains_keyword(answer, keyword))
    return matched / len(expected_keywords)


def context_relevance(ctx: dict[str, Any]) -> float:
    distance = float(ctx.get("distance", 1.0))
    return round(1 - distance, 4) if distance <= 1 else 0.0


def pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:.1f}%"


def mark(value: bool | None) -> str:
    if value is None:
        return "N/A"
    return "PASS" if value else "FAIL"


async def evaluate_case(case: dict[str, Any], kb_id: int, top_k: int, retrieval_only: bool) -> dict[str, Any]:
    from app.services.answer_verification_service import build_citation_ledger, evidence_preflight
    from app.services.rag_service import (
        build_sources,
        ecommerce_guardrail_refusal,
        execute_answer_from_contexts,
        retrieve_context,
    )
    result: dict[str, Any] = {
        "case": case,
        "contexts": [],
        "sources": [],
        "retrieval_hit": None,
        "source_support": None,
        "answer": "",
        "detected_refusal": None,
        "refusal_correct": None,
        "keyword_rate": None,
        "evidence_preflight": None,
        "structured_contract": None,
        "citation_verified": None,
        "answer_status": None,
        "error": None,
    }

    try:
        guardrail_answer = ecommerce_guardrail_refusal(case["question"])
        if guardrail_answer:
            result["answer"] = guardrail_answer
            result["detected_refusal"] = True
            result["refusal_correct"] = bool(case.get("should_refuse", False))
            return result

        contexts = await retrieve_context(case["question"], kb_id, top_k=top_k)
        sources = build_sources(contexts)
        result["contexts"] = contexts
        result["sources"] = sources
        result["retrieval_hit"] = has_retrieval_hit(contexts, case)
        result["source_support"] = has_source_support(contexts, sources, case)

        if not case.get("should_refuse"):
            preflight = evidence_preflight(case["question"], build_citation_ledger(contexts))
            result["evidence_preflight"] = preflight.passed

        if retrieval_only:
            return result

        execution = await execute_answer_from_contexts(
            case["question"],
            contexts,
            history=[],
            answer_profile="verified_v3",
        )
        result["answer"] = execution.answer
        result["answer_status"] = execution.answer_status
        result["sources"] = execution.sources
        detected_refusal = execution.answer_status == "refused" or detect_refusal(execution.answer)
        result["detected_refusal"] = detected_refusal
        result["refusal_correct"] = detected_refusal == bool(case.get("should_refuse", False))
        if not case.get("should_refuse"):
            fact = execution.structured_answer.facts[0] if execution.structured_answer and len(execution.structured_answer.facts) == 1 else None
            result["structured_contract"] = bool(
                execution.answer_status == "verified"
                and fact
                and fact.fact_type == case.get("expected_fact_type")
                and contains_keyword(fact.value_text, case.get("expected_value"))
                and fact.currency == case.get("expected_currency")
                and fact.unit == case.get("expected_unit")
                and fact.sku == case.get("expected_sku")
            )
            result["citation_verified"] = bool(
                fact
                and fact.citation_ids
                and execution.verification
                and execution.verification.passed
                and set(fact.citation_ids) <= set(execution.verification.verified_citation_ids)
            )
            result["keyword_rate"] = keyword_match_rate(execution.answer, case.get("expected_keywords", []))
            result["source_support"] = has_source_support(contexts, execution.sources, case)
    except Exception as exc:
        result["error"] = str(exc)

    return result


def average_bool(values: list[bool | None]) -> float | None:
    valid = [value for value in values if value is not None]
    if not valid:
        return None
    return sum(1 for value in valid if value) / len(valid)


def average_float(values: list[float | None]) -> float | None:
    valid = [value for value in values if value is not None]
    if not valid:
        return None
    return sum(valid) / len(valid)


def print_case_result(result: dict[str, Any], max_answer_chars: int, retrieval_only: bool) -> None:
    case = result["case"]
    print("\\n" + "=" * 80)
    print(f"[{case['id']}] {case['question']}")
    print(f"category: {case.get('category')} difficulty: {case.get('difficulty')} answer_type: {case.get('answer_type')}")
    print(f"should_refuse: {case.get('should_refuse')}")

    if result["error"]:
        print(f"error: {result['error']}")
        return

    contexts = result["contexts"]
    if contexts:
        print("retrieved_contexts:")
        for index, ctx in enumerate(contexts, 1):
            content = str(ctx.get("content", "")).replace("\\n", " ")[:120]
            print(
                f"  {index}. source={ctx.get('source')} "
                f"doc_id={ctx.get('doc_id')} "
                f"distance={float(ctx.get('distance', 0.0)):.4f} "
                f"relevance={context_relevance(ctx):.4f} "
                f"snippet={content}"
            )
    else:
        print("retrieved_contexts: []")

    print(f"retrieval_hit: {mark(result['retrieval_hit'])}")
    print(f"source_support: {mark(result['source_support'])}")

    if retrieval_only:
        return

    answer = result["answer"]
    preview = answer[:max_answer_chars]
    if len(answer) > max_answer_chars:
        preview += "..."
    print(f"answer: {preview}")
    print(f"answer_status: {result['answer_status']}")
    print(f"detected_refusal: {result['detected_refusal']}")
    print(f"refusal_correct: {mark(result['refusal_correct'])}")
    print(f"evidence_preflight: {mark(result['evidence_preflight'])}")
    print(f"structured_contract: {mark(result['structured_contract'])}")
    print(f"citation_verified: {mark(result['citation_verified'])}")
    print(f"keyword_match_rate: {pct(result['keyword_rate'])}")


def print_summary(results: list[dict[str, Any]], top_k: int, retrieval_only: bool) -> None:
    total_cases = len(results)
    answerable_cases = sum(1 for result in results if not result["case"].get("should_refuse"))
    refusal_cases = sum(1 for result in results if result["case"].get("should_refuse"))
    errors = sum(1 for result in results if result["error"])

    retrieval_hit_rate = average_bool([result["retrieval_hit"] for result in results])
    source_support_rate = average_bool([result["source_support"] for result in results])
    refusal_accuracy = None if retrieval_only else average_bool([result["refusal_correct"] for result in results])
    answer_keyword_match_rate = None if retrieval_only else average_float([result["keyword_rate"] for result in results])

    print("\\n" + "=" * 80)
    print("Summary")
    print(f"total_cases: {total_cases}")
    print(f"answerable_cases: {answerable_cases}")
    print(f"refusal_cases: {refusal_cases}")
    print(f"errors: {errors}")
    print(f"retrieval_hit_rate@{top_k}: {pct(retrieval_hit_rate)}")
    print(f"source_support_rate: {pct(source_support_rate)}")
    print(f"refusal_accuracy: {pct(refusal_accuracy)}")
    print(f"answer_keyword_match_rate: {pct(answer_keyword_match_rate)}")

    categories: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        categories.setdefault(result["case"].get("category", "uncategorized"), []).append(result)
    print("by_category:")
    for category, category_results in sorted(categories.items()):
        category_refusal_accuracy = None if retrieval_only else average_bool(
            [item["refusal_correct"] for item in category_results]
        )
        print(
            f"  {category}: cases={len(category_results)} "
            f"retrieval_hit={pct(average_bool([item['retrieval_hit'] for item in category_results]))} "
            f"refusal_accuracy={pct(category_refusal_accuracy)}"
        )

    if not settings.API_KEY:
        print("\\n注意：当前未配置 API_KEY，embedding 和 answer 使用 mock mode；结果只适合检查流程是否跑通，不代表真实语义质量。")


async def main() -> None:
    args = parse_args()
    cases = load_cases(args.questions)

    if args.validate_only:
        print_dataset_summary(cases)
        errors = validate_dataset(cases)
        if errors:
            print("validation: FAIL")
            for error in errors:
                print(f"- {error}")
            raise SystemExit(1)
        print("validation: PASS")
        return

    if args.kb_id is None:
        raise SystemExit("--kb-id is required unless --validate-only is used")

    if not settings.API_KEY:
        print("注意：API_KEY 未配置，当前处于 mock mode。")

    results = []
    for case in cases:
        result = await evaluate_case(case, args.kb_id, args.top_k, args.retrieval_only)
        results.append(result)
        print_case_result(result, args.max_answer_chars, args.retrieval_only)

    print_summary(results, args.top_k, args.retrieval_only)


if __name__ == "__main__":
    asyncio.run(main())


