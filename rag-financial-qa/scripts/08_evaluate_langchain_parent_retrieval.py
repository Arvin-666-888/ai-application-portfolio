from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.utils.financial_retrieval import parse_query_intent  # noqa: E402
from app.utils.retrieval import lexical_overlap_score  # noqa: E402
from scripts.atomic_json import write_json_atomic  # noqa: E402
from scripts.audit_paddleocr_candidate_coverage import file_sha256  # noqa: E402

EVALUATOR_PATH = PROJECT_ROOT / "scripts" / "05_evaluate_paddleocr_retrieval.py"
SPEC = importlib.util.spec_from_file_location("paddle_retrieval_candidates", EVALUATOR_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("无法加载候选检索脚本")
evaluator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = evaluator
SPEC.loader.exec_module(evaluator)

SCHEMA_VERSION = "langchain-parent-retrieval-candidates-v1"
DEFAULT_CORPUS = (
    PROJECT_ROOT
    / "evals/task2_paddleocr/chunks/router_v1_frozen_l1_corpus_v2.json"
)
DEFAULT_QUESTIONS = (
    PROJECT_ROOT / "evals/task2_paddleocr/development_questions.jsonl"
)
DEFAULT_CACHE = PROJECT_ROOT / "evals/task2_paddleocr/embedding_cache"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a cache-only LangChain Chroma parent-page retrieval baseline."
    )
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--embedding-cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dense-k", type=int, default=100)
    parser.add_argument("--lexical-k", type=int, default=100)
    parser.add_argument("--top-k", type=int, default=5)
    return parser.parse_args()


def _statement_terms(metric_families: tuple[str, ...], fallback: tuple[str, ...]) -> tuple[str, ...]:
    families = set(metric_families)
    if families & {"营业收入", "净利润", "归母净利润"}:
        return ("合并利润表", "利润表")
    if families & {"资产合计", "负债合计"}:
        return ("合并资产负债表", "资产负债表")
    if families & {"经营现金流"}:
        return ("合并现金流量表", "现金流量表")
    if families & {"毛利率"}:
        return ("毛利率", "分行业", "分产品")
    return fallback


def _parent_features(
    question: str,
    source: str,
    content: str,
    evidence: list[tuple[str, int]],
) -> dict[str, float]:
    intent = parse_query_intent(question)
    statements = _statement_terms(intent.metric_families, intent.statement_types)
    reciprocal = [1 / (60 + rank) for _channel, rank in evidence]
    dense_ranks = [rank for channel, rank in evidence if channel == "dense"]
    lexical_ranks = [rank for channel, rank in evidence if channel == "lexical"]
    return {
        "best_child_rr": max(reciprocal),
        "top3_child_rr": sum(sorted(reciprocal, reverse=True)[:3]),
        "parent_lexical_score": lexical_overlap_score(question, content),
        "metric_alias_score": float(
            any(alias in content for alias in intent.metric_aliases)
        ),
        "statement_score": float(any(term in content for term in statements)),
        "exact_statement_score": float(
            bool(statements) and statements[0] in content
        ),
        "scope_score": float(
            not intent.scopes or any(scope in content for scope in intent.scopes)
        ),
        "year_score": float(any(year in content for year in intent.years)),
        "noise_score": float(any(term in content for term in (
            "主要会计数据和财务指标",
            "季度主要财务指标",
            "非经常性损益",
            "前十名股东",
        ))),
        "child_evidence_count": float(len(evidence)),
        "company_source_score": float(
            not intent.company_terms
            or any(term in source for term in intent.company_terms)
        ),
        "best_dense_rr": 1 / (60 + min(dense_ranks)) if dense_ranks else 0.0,
        "best_lexical_rr": 1 / (60 + min(lexical_ranks)) if lexical_ranks else 0.0,
    }


def _parent_score(features: dict[str, float]) -> float:
    return (
        0.70 * features["best_child_rr"]
        + 0.07 * features["top3_child_rr"]
        + 0.90 * features["parent_lexical_score"]
        + 0.70 * features["metric_alias_score"]
        + 0.40 * features["statement_score"]
        + 0.10 * features["exact_statement_score"]
        + 0.03 * features["scope_score"]
        + 0.60 * features["year_score"]
        - 0.60 * features["noise_score"]
        + 0.06 * features["child_evidence_count"]
        + 2.00 * features["company_source_score"]
        + 0.50 * features["best_dense_rr"]
        + 0.50 * features["best_lexical_rr"]
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    import chromadb
    from langchain_chroma import Chroma
    from langchain_core.documents import Document

    if args.top_k != 5:
        raise evaluator.RetrievalInputError("正式对照固定 top_k=5")
    if args.dense_k < args.top_k or args.lexical_k < args.top_k:
        raise evaluator.RetrievalInputError("候选池必须大于等于 top_k")

    corpus_path = args.corpus.resolve()
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    if (
        corpus.get("schema_version") != evaluator.ROUTED_CORPUS_SCHEMA
        or corpus.get("builder_version") != "router-v1-routed-corpus-builder-v2"
        or corpus.get("status") not in {"completed", "degraded"}
        or corpus.get("ground_truth_loaded") is not False
        or corpus.get("api_called") is not False
        or (corpus.get("counts") or {}).get("chunk_count") != 5292
        or (corpus.get("counts") or {}).get("l1_chunk_count") != 4125
        or (corpus.get("counts") or {}).get("l3_chunk_count") != 1167
    ):
        raise evaluator.RetrievalInputError("routed corpus合同无效")
    chunks = corpus.get("chunks")
    if not isinstance(chunks, list) or len(chunks) != 5292:
        raise evaluator.RetrievalInputError("routed corpus必须包含5292个chunks")

    questions = evaluator.load_query_only_cases(args.questions.resolve())
    documents = [
        Document(page_content=item["content"], metadata=item["metadata"])
        for item in chunks
    ]
    texts = [document.page_content for document in documents]
    question_texts = [item["question"] for item in questions]
    vectors, cache_stats = evaluator.get_embeddings_cache_only(
        texts + question_texts,
        args.embedding_cache_dir.resolve(),
        evaluator.embedding_identity(),
    )
    document_vectors = vectors[: len(texts)]
    query_vectors = vectors[len(texts) :]

    client = chromadb.EphemeralClient()
    store = Chroma(
        collection_name="langchain_parent_dev30",
        client=client,
        embedding_function=None,
        collection_metadata={"hnsw:space": "cosine"},
    )
    store._collection.add(
        ids=[f"chunk-{index}" for index in range(len(documents))],
        documents=texts,
        metadatas=[document.metadata for document in documents],
        embeddings=document_vectors,
    )

    parent_parts: dict[tuple[str, int], list[str]] = {}
    for document in documents:
        source = Path(str(document.metadata.get("source", ""))).name
        page = int(document.metadata.get("page_number", 0))
        parent_parts.setdefault((source, page), []).append(document.page_content)
    parent_texts = {
        key: "\n".join(parts)
        for key, parts in parent_parts.items()
    }

    cases = []
    for question, query_vector in zip(questions, query_vectors):
        dense = store.similarity_search_by_vector(query_vector, k=args.dense_k)
        lexical = sorted(
            documents,
            key=lambda document: (
                -lexical_overlap_score(question["question"], document.page_content),
                str(document.metadata.get("source", "")),
                int(document.metadata.get("page_number", 0)),
                int(document.metadata.get("chunk_index", 0)),
            ),
        )[: args.lexical_k]

        evidence_by_parent: dict[tuple[str, int], list[tuple[str, int]]] = {}
        for channel, channel_documents in (("dense", dense), ("lexical", lexical)):
            for rank, document in enumerate(channel_documents, 1):
                key = (
                    Path(str(document.metadata.get("source", ""))).name,
                    int(document.metadata.get("page_number", 0)),
                )
                evidence_by_parent.setdefault(key, []).append((channel, rank))

        ranking = []
        for (source, page), evidence in evidence_by_parent.items():
            content = parent_texts[(source, page)]
            features = _parent_features(
                question["question"], source, content, evidence
            )
            ranking.append({
                "candidate_id": evaluator.canonical_sha256({
                    "source": source,
                    "page_number": page,
                }),
                "source": source,
                "page_number": page,
                "content": content[:2_000],
                "content_type": "parent_page",
                "parent_score": round(_parent_score(features), 8),
                **{key: round(value, 8) for key, value in features.items()},
            })
        ranking.sort(key=lambda item: (
            -item["parent_score"],
            item["source"],
            item["page_number"],
        ))
        cases.append({
            "case_id": question["case_id"],
            "question": question["question"],
            "query_text_sha256": evaluator.text_sha256(question["question"]),
            "langchain_parent": {
                "ranking": ranking,
                "top_k": ranking[: args.top_k],
            },
        })

    configuration = {
        "framework": "langchain",
        "vector_store": "langchain_chroma.Chroma",
        "document_type": "langchain_core.documents.Document",
        "retrieval": "dense+lexical child retrieval with parent physical-page aggregation",
        "dense_k": args.dense_k,
        "lexical_k": args.lexical_k,
        "top_k": args.top_k,
        "embedding_identity": evaluator.embedding_identity(),
        "query_expansion": "disabled_after_negative_ablation",
        "parent_score_weights": {
            "best_child_rr": 0.70,
            "top3_child_rr": 0.07,
            "parent_lexical_score": 0.90,
            "metric_alias_score": 0.70,
            "statement_score": 0.40,
            "exact_statement_score": 0.10,
            "scope_score": 0.03,
            "year_score": 0.60,
            "noise_score": -0.60,
            "child_evidence_count": 0.06,
            "company_source_score": 2.00,
            "best_dense_rr": 0.50,
            "best_lexical_rr": 0.50,
        },
        "development_fitted": True,
    }
    ranking_identity = [
        {
            "case_id": case["case_id"],
            "langchain_parent": [
                item["candidate_id"]
                for item in case["langchain_parent"]["ranking"]
            ],
        }
        for case in cases
    ]
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "completed",
        "ground_truth_loaded": False,
        "api_called": False,
        "inputs": {
            "corpus_file_sha256": file_sha256(corpus_path),
            "corpus_sha256": corpus.get("corpus_sha256"),
            "questions_sha256": evaluator.canonical_sha256(questions),
            "config_sha256": evaluator.canonical_sha256(configuration),
        },
        "configuration": configuration,
        "embedding_cache": cache_stats,
        "ranking_sha256": evaluator.canonical_sha256(ranking_identity),
        "cases": cases,
    }
    result["candidate_canonical_sha256"] = evaluator.canonical_sha256(result)
    return result


def main() -> int:
    args = parse_args()
    output = args.output.resolve()
    try:
        evaluator.ensure_output_writable(output, force=False, canonical_paths=())
        started = time.perf_counter()
        result = run(args)
        result["runtime_seconds"] = round(time.perf_counter() - started, 4)
        write_json_atomic(output, result, overwrite=False)
        print(json.dumps({
            "status": "COMPLETED",
            "output": str(output),
            "api_called": False,
            "ground_truth_loaded": False,
            "embedding_cache": result["embedding_cache"],
            "candidate_file_sha256": file_sha256(output),
            "candidate_canonical_sha256": result["candidate_canonical_sha256"],
            "ranking_sha256": result["ranking_sha256"],
        }, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({
            "status": "BLOCKED",
            "reason": str(exc),
            "output_written": output.exists(),
        }, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
