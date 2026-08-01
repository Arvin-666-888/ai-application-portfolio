import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from app.utils.table_semantic_context import semantic_digest_valid


_TEXT_PATTERN = re.compile(r"[a-zA-Z0-9_.%-]+|[\u4e00-\u9fff]+")
_NUMERIC_PATTERN = re.compile(
    r"(?<![\d.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?(?![\d.])"
)

_METRIC_ALIASES = {
    "营业收入": ("营业收入", "营业收入合计", "其中：营业收入"),
    "净利润": ("净利润",),
    "归母净利润": (
        "归属于母公司股东的净利润",
        "归属于母公司所有者的净利润",
        "归属于本行股东的净利润",
        "本行股东的净利润",
    ),
    "经营现金流": ("经营活动产生的现金流量净额",),
    "资产合计": ("资产合计", "资产总计"),
    "负债合计": ("负债合计", "负债总计"),
    "毛利率": ("毛利率",),
}
_STATEMENT_TERMS = {
    "利润表": ("利润表",),
    "资产负债表": ("资产负债表",),
    "现金流量表": ("现金流量表",),
    "毛利率表": ("毛利率", "分行业", "分产品"),
}
_SCOPE_PATTERNS = {
    "合并": re.compile(r"合并(?:口径|财务报表|报表|利润表|资产负债表|现金流量表)?"),
    "集团": re.compile(r"集团(?:口径|财务报表|报表|利润表|资产负债表|现金流量表)"),
    "母公司": re.compile(r"母公司(?:口径|财务报表|报表|利润表|资产负债表|现金流量表)"),
    "本行": re.compile(r"本行(?:口径|财务报表|报表)"),
}
_KNOWN_COMPANIES = ("海尔智家", "格力电器", "美的集团", "贵州茅台", "五粮液", "比亚迪", "招商银行")
_COMPANY_SUFFIXES = ("集团股份有限公司", "股份有限公司", "有限责任公司", "有限公司")
_COMPANY_NOISE = re.compile(r"^(?:请问|请查询|查询|根据|截至|在|该|报告期内)+|(?:的)?(?:年度报告|年报)$")


@dataclass(frozen=True)
class QueryIntent:
    company_terms: tuple[str, ...]
    years: tuple[str, ...]
    metric_aliases: tuple[str, ...]
    statement_types: tuple[str, ...]
    scopes: tuple[str, ...]
    answer_type: str
    table_fact: bool
    metric_families: tuple[str, ...] = ()


def _matched_metric_family(text: str) -> tuple[tuple[str, ...], list[tuple[int, int]], tuple[str, ...]]:
    matches = []
    for family, aliases in _METRIC_ALIASES.items():
        for alias in aliases:
            for match in re.finditer(re.escape(alias), text):
                matches.append((len(alias), family, alias, match.span()))
    if not matches:
        return (), [], ()
    matches.sort(key=lambda item: (-item[0], item[3][0], item[1]))
    selected = []
    occupied: list[tuple[int, int]] = []
    for _length, family, _alias, span in matches:
        if any(not (span[1] <= start or span[0] >= end) for start, end in occupied):
            continue
        if family not in selected:
            selected.append(family)
        occupied.append(span)
    selected_family = selected[0]
    family_aliases = _METRIC_ALIASES[selected_family]
    spans = [span for _length, family, _alias, span in matches if family == selected_family]
    return tuple(family_aliases), spans, tuple(selected)


def _blank_spans(text: str, spans: list[tuple[int, int]]) -> str:
    characters = list(text)
    for start, end in spans:
        characters[start:end] = " " * (end - start)
    return "".join(characters)


def _clean_company_candidate(value: str) -> str:
    candidate = re.sub(r"\s+", "", value)
    candidate = re.sub(r"(?:20\d{2})(?:年|年度)?$", "", candidate)
    candidate = _COMPANY_NOISE.sub("", candidate)
    for suffix in _COMPANY_SUFFIXES:
        candidate = candidate.removesuffix(suffix)
    if (
        len(candidate) < 2
        or len(candidate) > 16
        or candidate in {"母公司", "本公司", "该公司", "公司", "集团", "本行"}
        or any(noise in candidate for noise in ("年度归属于", "归属于母", "是多少", "金额"))
    ):
        return ""
    return candidate


def _company_terms(text: str, protected_spans: list[tuple[int, int]]) -> tuple[str, ...]:
    scrubbed = _blank_spans(text, protected_spans)
    scrubbed = re.sub(r"(?<!\d)20\d{2}(?:年|年度)?(?!\d)", " ", scrubbed)
    scrubbed = re.sub(r"[，。？?：:；;（）()、]", " ", scrubbed)
    candidates = []
    for match in re.finditer(r"([一-鿿]{2,24})(集团股份有限公司|股份有限公司|有限责任公司|有限公司)", scrubbed):
        candidate = _clean_company_candidate(match.group(1) + match.group(2))
        if candidate:
            candidates.append(candidate)
    for part in scrubbed.split():
        candidate = _clean_company_candidate(part)
        if candidate and not any(term in candidate for term in ("利润表", "资产负债表", "现金流量表", "口径", "报告")):
            candidates.append(candidate)
    known_terms = [name for name in _KNOWN_COMPANIES if name in text]
    if known_terms:
        return tuple(dict.fromkeys(known_terms))
    return tuple(dict.fromkeys(candidates[:2]))


def parse_query_intent(query: Any) -> QueryIntent:
    text = unicodedata.normalize("NFKC", str(query)).strip()
    normalized = text.casefold()
    years = tuple(dict.fromkeys(re.findall(r"(?<!\d)(20\d{2})(?!\d)", normalized)))

    metric_aliases, metric_spans, metric_families = _matched_metric_family(text)
    statement_types = tuple(
        name
        for name, aliases in _STATEMENT_TERMS.items()
        if any(alias in text for alias in aliases)
    )
    protected_text = _blank_spans(text, metric_spans)
    protected_text = _blank_spans(
        protected_text,
        [match.span() for name in _KNOWN_COMPANIES for match in re.finditer(re.escape(name), text)],
    )
    scope_matches = []
    scope_spans = []
    for scope, pattern in _SCOPE_PATTERNS.items():
        matches = list(pattern.finditer(protected_text))
        if matches:
            scope_matches.append(scope)
            scope_spans.extend(match.span() for match in matches)
    if "集团" not in scope_matches and any(
        not name.endswith("集团") and f"{name}集团" in text for name in _KNOWN_COMPANIES
    ):
        scope_matches.append("集团")
    statement_spans = [
        match.span()
        for aliases in _STATEMENT_TERMS.values()
        for alias in aliases
        for match in re.finditer(re.escape(alias), text)
    ]
    company_terms = _company_terms(text, metric_spans + scope_spans + statement_spans)

    answer_type = "ratio" if any(term in text for term in ("毛利率", "比例", "%")) else "amount"
    table_fact = bool(metric_aliases or statement_types or answer_type == "ratio")
    return QueryIntent(
        company_terms=company_terms,
        years=years,
        metric_aliases=metric_aliases,
        statement_types=statement_types,
        scopes=tuple(scope_matches),
        answer_type=answer_type,
        table_fact=table_fact,
        metric_families=metric_families,
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


def _metric_row_score(intent: QueryIntent, content: Any) -> float:
    if not intent.metric_aliases:
        return 0.0
    for line in unicodedata.normalize("NFKC", str(content)).splitlines():
        if any(alias in line for alias in intent.metric_aliases) and any(
            not number.rstrip("%").isdigit() or len(number.rstrip("%")) != 4
            for number in _NUMERIC_PATTERN.findall(line)
        ):
            return 1.0
    return 0.0


def financial_rerank_features(intent: QueryIntent, context: dict[str, Any]) -> dict[str, float]:
    content = str(context.get("content", ""))
    source = str(context.get("source", ""))
    semantic = {
        "schema_version": context.get("table_semantic_schema_version"),
        "statement_title": context.get("statement_title"),
        "statement_type": context.get("statement_type"),
        "table_scope": context.get("table_scope"),
        "unit_text": context.get("unit_text"),
        "unit": context.get("unit"),
        "currency": context.get("currency"),
        "statement_period": context.get("statement_period"),
        "column_bindings": context.get("column_bindings"),
        "binding_source_page": context.get("binding_source_page"),
        "binding_method": context.get("binding_method"),
        "binding_confidence": context.get("binding_confidence"),
        "continuation_from_page": context.get("continuation_from_page"),
        "statement_anchor_bbox": context.get("statement_anchor_bbox"),
        "unit_anchor_bbox": context.get("unit_anchor_bbox"),
        "table_bbox": context.get("table_bbox"),
        "canonical_sha256": context.get("table_semantic_canonical_sha256"),
    }
    semantic_trusted = bool(
        semantic["schema_version"] == "financial-table-semantic-context-v1"
        and semantic_digest_valid(semantic)
    )
    semantic_text = " ".join(
        str(context.get(key, ""))
        for key in ("statement_title", "statement_type", "table_scope", "statement_period")
    ) if semantic_trusted else ""
    return {
        "metric_row_score": _metric_row_score(intent, content),
        "statement_scope_score": float(
            any(statement in semantic_text or statement in content for statement in intent.statement_types)
            or any(scope in semantic_text or scope in content for scope in intent.scopes)
        ),
        "year_score": float(any(year in semantic_text or year in content for year in intent.years)),
        "company_source_score": float(any(term in source for term in intent.company_terms)),
        "content_type_score": float(
            intent.table_fact and context.get("content_type") == "table"
        ),
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


def _expected_statement_terms(intent: QueryIntent) -> tuple[str, ...]:
    families = set(intent.metric_families)
    if families & {"营业收入", "净利润", "归母净利润"}:
        return ("合并利润表", "利润表")
    if families & {"资产合计", "负债合计"}:
        return ("合并资产负债表", "资产负债表")
    if families & {"经营现金流"}:
        return ("合并现金流量表", "现金流量表")
    if families & {"毛利率"}:
        return ("毛利率", "分行业", "分产品")
    return intent.statement_types


def _financial_noise_score(content: str) -> float:
    return float(any(term in content for term in (
        "主要会计数据和财务指标",
        "季度主要财务指标",
        "非经常性损益",
        "前十名股东",
    )))


def financial_v2_rank(
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
        features = financial_rerank_features(intent, context)
        normalized_rrf = rrf_scores[candidate_id] / max_rrf if max_rrf else 0.0
        final_score = (
            0.55 * normalized_rrf
            + 0.20 * features["metric_row_score"]
            + 0.10 * features["statement_scope_score"]
            + 0.05 * features["year_score"]
            + 0.05 * features["company_source_score"]
            + 0.05 * features["content_type_score"]
        )
        enriched = dict(context)
        enriched.update(features)
        enriched["rrf_score"] = round(rrf_scores[candidate_id], 8)
        enriched["normalized_rrf"] = round(normalized_rrf, 8)
        enriched["financial_v2_score"] = round(final_score, 8)
        enriched["best_channel_rank"] = best_channel_rank[candidate_id]
        ranked.append(enriched)
    ranked.sort(key=lambda item: (
        -item["financial_v2_score"],
        -item["rrf_score"],
        item["best_channel_rank"],
        str(item.get("source", "")),
        int(item.get("page_number", 0) or 0),
        str(item.get("table_id", "")),
        int(item.get("artifact_chunk_index", item.get("chunk_index", 0)) or 0),
        item["candidate_id"],
    ))
    return collapse_provenance(ranked, top_k)


def financial_v3_rank(
    query: str,
    ranked_channels: dict[str, list[dict]],
    top_k: int = 5,
) -> list[dict]:
    intent = parse_query_intent(query)
    channel_weights = {
        "table_lexical": 1.25,
        "table_dense": 1.0,
        "text_lexical": 0.75,
        "text_dense": 0.75,
    }
    rrf_scores = weighted_rrf(ranked_channels, channel_weights, k=60)
    max_rrf = max(rrf_scores.values(), default=1.0)
    by_id: dict[str, dict] = {}
    best_channel_rank: dict[str, int] = {}
    for contexts in ranked_channels.values():
        for rank, context in enumerate(contexts, 1):
            candidate_id = str(context["candidate_id"])
            by_id.setdefault(candidate_id, dict(context))
            best_channel_rank[candidate_id] = min(
                best_channel_rank.get(candidate_id, rank), rank
            )

    statement_terms = _expected_statement_terms(intent)
    ranked = []
    for candidate_id, context in by_id.items():
        content = str(context.get("content", ""))
        source = str(context.get("source", ""))
        features = financial_rerank_features(intent, context)
        normalized_rrf = rrf_scores[candidate_id] / max_rrf if max_rrf else 0.0
        lexical_score = lexical_overlap_score(query, content)
        source_score = float(
            not intent.company_terms
            or any(term in source for term in intent.company_terms)
        )
        statement_score = float(
            any(term in content for term in statement_terms)
        )
        exact_statement_score = float(
            bool(statement_terms) and statement_terms[0] in content
        )
        scope_score = float(
            not intent.scopes or any(scope in content for scope in intent.scopes)
        )
        noise_score = _financial_noise_score(content)
        final_score = (
            0.40 * normalized_rrf
            + 0.60 * lexical_score
            + 1.20 * source_score
            + 0.30 * features["year_score"]
            + 1.00 * features["metric_row_score"]
            + 0.20 * statement_score
            + 0.10 * exact_statement_score
            + 0.30 * scope_score
            + 0.30 * features["content_type_score"]
            - 0.50 * noise_score
        )
        enriched = dict(context)
        enriched.update(features)
        enriched.update({
            "rrf_score": round(rrf_scores[candidate_id], 8),
            "normalized_rrf": round(normalized_rrf, 8),
            "query_lexical_score": round(lexical_score, 8),
            "source_filter_score": source_score,
            "expected_statement_score": statement_score,
            "exact_statement_score": exact_statement_score,
            "scope_score": scope_score,
            "financial_noise_score": noise_score,
            "financial_v3_score": round(final_score, 8),
            "best_channel_rank": best_channel_rank[candidate_id],
        })
        ranked.append(enriched)
    ranked.sort(key=lambda item: (
        -item["financial_v3_score"],
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


def _source_identity(context: dict) -> tuple:
    name = context["source"]
    return (
        ("table", str(context.get("table_id")))
        if context.get("table_id")
        else ("provenance", str(context.get("provenance_id")))
        if context.get("provenance_id")
        else ("chunk", name, context.get("chunk_index", 0))
    )


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
        relevance = context.get("financial_v2_score")
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
            "financial_v2_score",
        ):
            value = context.get(field)
            if value not in (None, ""):
                source[field] = value
        sources.append(source)
    return sources
