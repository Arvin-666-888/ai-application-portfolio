import re
from typing import Any


_TEXT_PATTERN = re.compile(r"[a-zA-Z0-9_.%-]+|[\u4e00-\u9fff]+")


def normalize_relevance(distance: float | int | None) -> float:
    if distance is None:
        return 0.0
    try:
        value = 1 - float(distance)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(1.0, value)), 4)


def extract_terms(text: Any) -> set[str]:
    normalized = str(text).lower()
    terms: set[str] = set()

    for token in _TEXT_PATTERN.findall(normalized):
        if not token:
            continue
        if re.fullmatch(r"[a-zA-Z0-9_.%-]+", token):
            if len(token) >= 2:
                terms.add(token)
            continue

        if len(token) <= 8:
            terms.add(token)
        for size in (2, 3, 4):
            if len(token) >= size:
                for index in range(0, len(token) - size + 1):
                    terms.add(token[index:index + size])

    return {term for term in terms if len(term.strip()) >= 2}


def lexical_overlap_score(query: Any, content: Any) -> float:
    query_terms = extract_terms(query)
    if not query_terms:
        return 0.0

    content_terms = extract_terms(content)
    if not content_terms:
        return 0.0

    matched = query_terms & content_terms
    if not matched:
        return 0.0

    matched_weight = sum(len(term) for term in matched)
    total_weight = sum(len(term) for term in query_terms)
    return round(matched_weight / total_weight, 4)


def dedupe_contexts(contexts: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for ctx in contexts:
        key = (
            ctx.get("doc_id"),
            ctx.get("chunk_index"),
            ctx.get("source"),
            str(ctx.get("content", ""))[:120],
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ctx)
    return deduped


def rank_contexts(
    query: str,
    contexts: list[dict],
    top_k: int,
    lexical_weight: float = 0.35,
    min_relevance_score: float = 0.0,
) -> list[dict]:
    weighted = []
    lexical_weight = max(0.0, min(1.0, lexical_weight))

    for ctx in dedupe_contexts(contexts):
        vector_score = normalize_relevance(ctx.get("distance"))
        lexical_score = ctx.get("lexical_score")
        if lexical_score is None:
            lexical_score = lexical_overlap_score(query, ctx.get("content", ""))

        hybrid_score = round(
            (1 - lexical_weight) * vector_score + lexical_weight * float(lexical_score),
            4,
        )
        if hybrid_score < min_relevance_score:
            continue

        enriched = dict(ctx)
        enriched["vector_relevance"] = vector_score
        enriched["lexical_score"] = round(float(lexical_score), 4)
        enriched["relevance"] = hybrid_score
        weighted.append(enriched)

    weighted.sort(
        key=lambda item: (
            item.get("relevance", 0.0),
            item.get("lexical_score", 0.0),
            item.get("vector_relevance", 0.0),
        ),
        reverse=True,
    )
    return weighted[:top_k]


def financial_guardrail_refusal(question: str) -> str | None:
    normalized = str(question).lower()
    has_market_target = any(term in normalized for term in ("股价", "股票", "目标价"))
    has_trading_advice = any(term in normalized for term in ("买入", "卖出", "投资建议", "推荐", "持有"))
    has_prediction = any(term in normalized for term in ("预测", "涨到", "跌到", "会涨", "会跌", "明年", "未来"))

    if has_trading_advice or (has_market_target and has_prediction):
        return (
            "根据现有资料无法回答该问题。本系统只基于已上传资料做事实问答，"
            "不提供股价预测、买卖建议或目标价判断。"
        )
    return None


def build_source_items(contexts: list[dict]) -> list[dict]:
    seen = set()
    sources = []
    for ctx in contexts:
        name = ctx["source"]
        chunk_index = ctx.get("chunk_index", 0)
        key = (name, chunk_index)
        if key in seen:
            continue
        seen.add(key)
        snippet = str(ctx.get("content", "")).strip()
        relevance = ctx.get("relevance")
        if relevance is None:
            relevance = normalize_relevance(ctx.get("distance"))
        sources.append({
            "document": name,
            "relevance": round(float(relevance), 2),
            "snippet": snippet[:300],
        })
    return sources
