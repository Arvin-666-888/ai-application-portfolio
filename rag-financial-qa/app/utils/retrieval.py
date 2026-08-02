import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from app.utils.table_semantic_context import semantic_digest_valid


_TEXT_PATTERN = re.compile(r"[a-zA-Z0-9_.%-]+|[\u4e00-\u9fff]+")
_NUMERIC_PATTERN = re.compile(
    r"(?<![\d.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?(?![\d.])"
)

_FACT_ALIASES = {
    "price": ("价格", "售价", "单价", "多少钱", "price"),
    "inventory_quantity": ("库存数量", "现货数量", "库存", "inventory quantity", "stock quantity", "inventory", "stock"),
    "delivery_duration": ("配送时长", "交付时长", "送达时间", "配送时间", "物流时效", "delivery"),
    "customs_duty_rate": ("关税税率", "关税率", "进口税率", "customs duty", "duty rate"),
}
_TABLE_TERMS = {
    "product_catalog": ("商品目录", "SKU", "产品", "商品"),
    "platform_listing": ("平台商品", "店铺商品", "平台", "marketplace"),
    "shipping_policy": ("配送政策", "物流时效", "配送", "delivery"),
    "customs_policy": ("关税表", "进口税率", "关税", "customs"),
}
_PLATFORM_TERMS = (
    "天猫", "淘宝", "京东", "拼多多", "抖音", "Amazon", "亚马逊",
    "eBay", "Shopee", "Lazada", "AliExpress", "速卖通",
)
_MARKET_ALIASES = {
    "中国": ("中国", "中国大陆", "国内", "CN"),
    "美国": ("美国", "US", "USA"),
    "英国": ("英国", "UK"),
    "欧盟": ("欧盟", "EU"),
    "中国香港": ("中国香港", "香港", "HK"),
    "日本": ("日本", "JP"),
    "新加坡": ("新加坡", "SG"),
}
_SKU_RE = re.compile(r"(?i)(SKU\s*(?:编号|编码|ID)?\s*[:：#-]?\s*[A-Z0-9][A-Z0-9._-]{1,63})")
_DATE_RE = re.compile(r"(?<!\d)(20\d{2})(?:[-/.年](\d{1,2})(?:[-/.月](\d{1,2})日?)?)?(?!\d)")
_QUESTION_NOISE = re.compile(
    r"^(?:请问|请查询|查询|根据|截至|在|该|这个|这款)+|"
    r"(?:的)?(?:价格|售价|单价|多少钱|库存数量|现货数量|配送时长|交付时长|配送时间|送达时间|物流时效|关税税率|关税率|进口税率)?"
    r"(?:是多少|有多少|需要多久|多久|吗|呢)[？?]?$"
)


@dataclass(frozen=True)
class QueryIntent:
    skus: tuple[str, ...]
    product_terms: tuple[str, ...]
    platforms: tuple[str, ...]
    markets: tuple[str, ...]
    dates: tuple[str, ...]
    fact_aliases: tuple[str, ...]
    table_types: tuple[str, ...]
    answer_type: str
    table_fact: bool
    fact_types: tuple[str, ...] = ()


def _matched_fact_types(text: str) -> tuple[tuple[str, ...], list[tuple[int, int]], tuple[str, ...]]:
    matches = []
    folded = text.casefold()
    excluded_inventory = any(
        term in folded
        for term in ("库存费", "库存周转", "inventory fee", "inventory turnover")
    )
    for fact_type, aliases in _FACT_ALIASES.items():
        for alias in aliases:
            if fact_type == "inventory_quantity" and alias.casefold() in {"库存", "inventory"} and excluded_inventory:
                continue
            for match in re.finditer(re.escape(alias.casefold()), folded):
                matches.append((len(alias), fact_type, alias, match.span()))
    if not matches:
        return (), [], ()
    matches.sort(key=lambda item: (-item[0], item[3][0], item[1]))
    selected: list[str] = []
    occupied: list[tuple[int, int]] = []
    for _length, fact_type, _alias, span in matches:
        if any(not (span[1] <= start or span[0] >= end) for start, end in occupied):
            continue
        if fact_type not in selected:
            selected.append(fact_type)
        occupied.append(span)
    primary = selected[0]
    spans = [span for _length, fact_type, _alias, span in matches if fact_type == primary]
    return tuple(_FACT_ALIASES[primary]), spans, tuple(selected)


def _blank_spans(text: str, spans: list[tuple[int, int]]) -> str:
    characters = list(text)
    for start, end in spans:
        characters[start:end] = " " * (end - start)
    return "".join(characters)


def _date_text(match: re.Match[str]) -> str:
    year, month, day = match.groups()
    if day:
        return f"{year}-{int(month):02d}-{int(day):02d}"
    if month:
        return f"{year}-{int(month):02d}"
    return year


def _product_terms(text: str, protected_spans: list[tuple[int, int]]) -> tuple[str, ...]:
    scrubbed = _blank_spans(text, protected_spans)
    scrubbed = _SKU_RE.sub(" ", scrubbed)
    scrubbed = _DATE_RE.sub(" ", scrubbed)
    for term in (*_PLATFORM_TERMS, *[alias for aliases in _MARKET_ALIASES.values() for alias in aliases]):
        scrubbed = re.sub(re.escape(term), " ", scrubbed, flags=re.IGNORECASE)
    scrubbed = re.sub(r"[，。？?：:；;（）()、]", " ", scrubbed)
    candidates = []
    for part in scrubbed.split():
        candidate = _QUESTION_NOISE.sub("", part).strip()
        candidate = re.sub(r"(?:的|商品|产品|这款|该款)$", "", candidate)
        if 2 <= len(candidate) <= 64 and candidate not in {"平台", "市场", "日期"}:
            candidates.append(candidate)
    return tuple(dict.fromkeys(candidates[:3]))


def parse_query_intent(query: Any) -> QueryIntent:
    text = unicodedata.normalize("NFKC", str(query)).strip()
    folded = text.casefold()
    dates = tuple(dict.fromkeys(_date_text(match) for match in _DATE_RE.finditer(text)))
    skus = tuple(dict.fromkeys(match.group(1).upper() for match in _SKU_RE.finditer(text)))
    fact_aliases, fact_spans, fact_types = _matched_fact_types(text)
    table_types = tuple(
        name
        for name, aliases in _TABLE_TERMS.items()
        if any(alias.casefold() in folded for alias in aliases)
    )
    platforms = tuple(term for term in _PLATFORM_TERMS if term.casefold() in folded)
    markets = tuple(
        market
        for market, aliases in _MARKET_ALIASES.items()
        if any(
            re.search(rf"(?<![A-Za-z]){re.escape(alias)}(?![A-Za-z])", text, re.IGNORECASE)
            or (not re.search(r"[A-Za-z]", alias) and alias in text)
            for alias in aliases
        )
    )
    protected_spans = fact_spans + [match.span() for match in _SKU_RE.finditer(text)]
    product_terms = _product_terms(text, protected_spans)
    answer_type = fact_types[0] if len(fact_types) == 1 else "unsupported"
    return QueryIntent(
        skus=skus,
        product_terms=product_terms,
        platforms=platforms,
        markets=markets,
        dates=dates,
        fact_aliases=fact_aliases,
        table_types=table_types,
        answer_type=answer_type,
        table_fact=bool(fact_aliases or table_types),
        fact_types=fact_types,
    )


def weighted_rrf(
    ranked_channels: dict[str, list[dict]],
    weights: dict[str, float],
    k: int = 60,
) -> dict[str, float]:
    scores: dict[str, float] = {}
    for channel, contexts in ranked_channels.items():
        weight = float(weights.get(channel, 0.0))
        for rank, context in enumerate(contexts, 1):
            candidate_id = str(context["candidate_id"])
            scores[candidate_id] = scores.get(candidate_id, 0.0) + weight / (k + rank)
    return scores


def _fact_row_score(intent: QueryIntent, content: Any) -> float:
    if not intent.fact_aliases:
        return 0.0
    folded = unicodedata.normalize("NFKC", str(content)).casefold()
    return float(
        any(alias.casefold() in folded for alias in intent.fact_aliases)
        and bool(_NUMERIC_PATTERN.search(folded))
    )


def ecommerce_rerank_features(intent: QueryIntent, context: dict[str, Any]) -> dict[str, float]:
    content = str(context.get("content", ""))
    source = str(context.get("source", ""))
    semantic = {
        "schema_version": context.get("table_semantic_schema_version"),
        "table_title": context.get("table_title"),
        "table_type": context.get("table_type"),
        "platform": context.get("platform"),
        "market": context.get("market"),
        "effective_date": context.get("effective_date"),
        "column_bindings": context.get("column_bindings"),
        "binding_source_page": context.get("binding_source_page"),
        "binding_method": context.get("binding_method"),
        "binding_confidence": context.get("binding_confidence"),
        "continuation_from_page": context.get("continuation_from_page"),
        "table_anchor_bbox": context.get("table_anchor_bbox"),
        "context_anchor_bbox": context.get("context_anchor_bbox"),
        "table_bbox": context.get("table_bbox"),
        "canonical_sha256": context.get("table_semantic_canonical_sha256"),
    }
    trusted = bool(
        semantic["schema_version"] == "ecommerce-table-semantic-context-v1"
        and semantic_digest_valid(semantic)
    )
    semantic_text = " ".join(
        str(context.get(key, ""))
        for key in ("table_title", "table_type", "platform", "market", "effective_date")
    ) if trusted else ""
    identity_text = f"{semantic_text} {content} {source}".casefold()
    return {
        "fact_row_score": _fact_row_score(intent, content),
        "sku_score": float(any(sku.casefold() in identity_text for sku in intent.skus)),
        "product_score": float(any(term.casefold() in identity_text for term in intent.product_terms)),
        "platform_market_score": float(
            any(term.casefold() in identity_text for term in (*intent.platforms, *intent.markets))
        ),
        "date_score": float(any(date in identity_text for date in intent.dates)),
        "content_type_score": float(intent.table_fact and context.get("content_type") == "table"),
    }


def collapse_provenance(contexts: list[dict], top_k: int) -> list[dict]:
    seen = set()
    result = []
    for context in contexts:
        table_id = context.get("table_id")
        if table_id:
            key = ("table", str(table_id))
        else:
            key = (
                "text",
                context.get("provenance_id"),
                context.get("source"),
                context.get("page_number"),
                context.get("chunk_index"),
            )
        if key in seen:
            continue
        seen.add(key)
        result.append(context)
        if len(result) >= top_k:
            break
    return result


def ecommerce_v2_rank(
    query: str,
    ranked_channels: dict[str, list[dict]],
    top_k: int = 5,
) -> list[dict]:
    intent = parse_query_intent(query)
    weights = {
        "table_lexical": 1.25,
        "table_dense": 1.0,
        "text_lexical": 0.75,
        "text_dense": 0.75,
    }
    rrf_scores = weighted_rrf(ranked_channels, weights, k=60)
    by_id: dict[str, dict] = {}
    best_channel_rank: dict[str, int] = {}
    for contexts in ranked_channels.values():
        for rank, context in enumerate(contexts, 1):
            candidate_id = str(context["candidate_id"])
            by_id.setdefault(candidate_id, dict(context))
            best_channel_rank[candidate_id] = min(best_channel_rank.get(candidate_id, rank), rank)
    max_rrf = max(rrf_scores.values(), default=1.0)
    ranked = []
    for candidate_id, context in by_id.items():
        features = ecommerce_rerank_features(intent, context)
        normalized_rrf = rrf_scores[candidate_id] / max_rrf if max_rrf else 0.0
        final_score = (
            0.50 * normalized_rrf
            + 0.20 * features["fact_row_score"]
            + 0.10 * features["sku_score"]
            + 0.05 * features["product_score"]
            + 0.05 * features["platform_market_score"]
            + 0.05 * features["date_score"]
            + 0.05 * features["content_type_score"]
        )
        enriched = dict(context)
        enriched.update(features)
        enriched["rrf_score"] = round(rrf_scores[candidate_id], 8)
        enriched["normalized_rrf"] = round(normalized_rrf, 8)
        enriched["ecommerce_v2_score"] = round(final_score, 8)
        enriched["best_channel_rank"] = best_channel_rank[candidate_id]
        ranked.append(enriched)
    ranked.sort(key=lambda item: (
        -item["ecommerce_v2_score"],
        -item["rrf_score"],
        item["best_channel_rank"],
        str(item.get("source", "")),
        int(item.get("page_number", 0) or 0),
        str(item.get("table_id", "")),
        int(item.get("artifact_chunk_index", item.get("chunk_index", 0)) or 0),
        item["candidate_id"],
    ))
    return collapse_provenance(ranked, top_k)


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


def extract_numeric_terms(text: Any) -> set[str]:
    normalized = unicodedata.normalize("NFKC", str(text))
    return {match.group(0).replace(",", "") for match in _NUMERIC_PATTERN.finditer(normalized)}


def numeric_overlap_score(query: Any, content: Any) -> float:
    query_numbers = extract_numeric_terms(query)
    if not query_numbers:
        return 0.0

    content_numbers = extract_numeric_terms(content)
    return round(len(query_numbers & content_numbers) / len(query_numbers), 4)


def dedupe_contexts(contexts: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for ctx in contexts:
        provenance_id = ctx.get("provenance_id")
        if provenance_id:
            key = (provenance_id, ctx.get("chunk_index"), str(ctx.get("content", ""))[:120])
        else:
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
    numeric_weight: float = 0.0,
    min_relevance_score: float = 0.0,
) -> list[dict]:
    weighted = []
    lexical_weight = max(0.0, min(1.0, lexical_weight))
    numeric_weight = max(0.0, min(1.0 - lexical_weight, numeric_weight))
    vector_weight = 1.0 - lexical_weight - numeric_weight

    for ctx in dedupe_contexts(contexts):
        vector_score = normalize_relevance(ctx.get("distance"))
        lexical_score = ctx.get("lexical_score")
        if lexical_score is None:
            lexical_score = lexical_overlap_score(query, ctx.get("content", ""))
        numeric_score = numeric_overlap_score(query, ctx.get("content", ""))

        hybrid_score = round(
            vector_weight * vector_score
            + lexical_weight * float(lexical_score)
            + numeric_weight * numeric_score,
            4,
        )
        if hybrid_score < min_relevance_score:
            continue

        enriched = dict(ctx)
        enriched["vector_relevance"] = vector_score
        enriched["lexical_score"] = round(float(lexical_score), 4)
        enriched["numeric_score"] = numeric_score
        enriched["relevance"] = hybrid_score
        weighted.append(enriched)

    weighted.sort(
        key=lambda item: (
            item.get("relevance", 0.0),
            item.get("numeric_score", 0.0),
            item.get("lexical_score", 0.0),
            item.get("vector_relevance", 0.0),
        ),
        reverse=True,
    )
    return weighted[:top_k]


def ecommerce_guardrail_refusal(question: str) -> str | None:
    intent = parse_query_intent(question)
    if not intent.fact_types:
        return (
            "根据现有资料无法回答该问题。本系统只回答商品价格、库存数量、"
            "配送时长和关税税率四类明确事实。"
        )
    if len(intent.fact_types) > 1:
        return "根据现有资料无法回答该问题。请一次只查询一类商品事实。"
    return None


# Historical scripts may still import the old public names.
financial_guardrail_refusal = ecommerce_guardrail_refusal
financial_rerank_features = ecommerce_rerank_features
financial_v2_rank = ecommerce_v2_rank


def _source_identity(context: dict) -> tuple:
    name = context["source"]
    chunk_index = context.get("table_chunk_index", context.get("chunk_index"))
    page_number = context.get("page_number")
    if context.get("table_id"):
        return ("table", str(context["table_id"])) if chunk_index is None else (
            "table", str(context["table_id"]), chunk_index
        )
    if context.get("provenance_id"):
        return ("provenance", str(context["provenance_id"])) if chunk_index is None else (
            "provenance", str(context["provenance_id"]), chunk_index
        )
    if chunk_index is not None:
        return ("chunk", name, page_number, chunk_index)
    content_sha = hashlib.sha256(str(context.get("content", "")).encode("utf-8")).hexdigest()
    return ("content", name, page_number, content_sha)


def enumerate_citation_contexts(contexts: list[dict]) -> list[tuple[str, tuple, dict]]:
    seen = set()
    citations = []
    for context in contexts:
        identity = _source_identity(context)
        if identity in seen:
            continue
        seen.add(identity)
        citations.append((f"C{len(citations) + 1}", identity, context))
    return citations


def build_source_items(contexts: list[dict]) -> list[dict]:
    sources = []
    for citation_id, _identity, context in enumerate_citation_contexts(contexts):
        name = context["source"]
        snippet = str(context.get("content", "")).strip()
        relevance = context.get("ecommerce_v2_score")
        if relevance is None:
            relevance = context.get("relevance")
        if relevance is None:
            relevance = normalize_relevance(context.get("distance"))
        source = {
            "document": name,
            "relevance": round(float(relevance), 4),
            "citation_id": citation_id,
            "snippet": snippet[:300],
        }
        artifact = context.get("artifact_file_sha256") or context.get("artifact_id")
        if artifact:
            source["artifact"] = artifact
        for field in (
            "page_number",
            "content_type",
            "provenance_id",
            "table_id",
            "parser_layer",
            "parse_profile",
            "index_version",
            "ecommerce_v2_score",
        ):
            value = context.get(field)
            if value not in (None, ""):
                source[field] = value
        sources.append(source)
    return sources
