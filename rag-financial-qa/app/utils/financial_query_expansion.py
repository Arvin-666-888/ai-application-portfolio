from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from app.utils.financial_retrieval import QueryIntent, parse_query_intent


@dataclass(frozen=True)
class ExpandedFinancialQuery:
    original: str
    lexical_query: str
    dense_queries: tuple[str, ...]
    intent: QueryIntent
    method: str = "answer-free-financial-query-expansion-v1"


def _join(values: tuple[str, ...]) -> str:
    return " ".join(dict.fromkeys(value for value in values if value))


def expand_financial_query(query: str) -> ExpandedFinancialQuery:
    original = unicodedata.normalize("NFKC", str(query)).strip()
    intent = parse_query_intent(original)
    fields = [original]
    if intent.company_terms:
        fields.append(f"公司 {_join(intent.company_terms)}")
    if intent.years:
        fields.append(f"年度 {_join(intent.years)}")
    if intent.statement_types:
        fields.append(f"报表 {_join(intent.statement_types)}")
    if intent.scopes:
        fields.append(f"口径 {_join(intent.scopes)}")
    if intent.metric_aliases:
        fields.append(f"指标 {_join(intent.metric_aliases)}")
    fields.append(f"答案类型 {'比例' if intent.answer_type == 'ratio' else '金额'}")
    lexical_query = " ".join(fields)

    dense_queries = [original]
    for alias in intent.metric_aliases:
        parts = [
            _join(intent.company_terms),
            _join(intent.years),
            _join(intent.scopes),
            _join(intent.statement_types),
            alias,
        ]
        expanded = " ".join(part for part in parts if part)
        if expanded and expanded not in dense_queries:
            dense_queries.append(expanded)

    return ExpandedFinancialQuery(
        original=original,
        lexical_query=lexical_query,
        dense_queries=tuple(dense_queries),
        intent=intent,
    )
