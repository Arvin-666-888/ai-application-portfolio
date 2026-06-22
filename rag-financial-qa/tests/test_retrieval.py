from app.utils.retrieval import (
    extract_terms,
    financial_guardrail_refusal,
    lexical_overlap_score,
    normalize_relevance,
    rank_contexts,
)


def test_extract_terms_handles_chinese_and_numbers():
    terms = extract_terms("公司2024年营业收入是多少？")

    assert "2024" in terms
    assert "营业收入" in terms
    assert "收入" in terms


def test_lexical_overlap_scores_related_content_higher():
    query = "2024年营业收入是多少？"
    related = "公司 2024 年营业收入为 8.6 亿元，同比增长 12%。"
    unrelated = "公司加强权限管理和日志审计。"

    assert lexical_overlap_score(query, related) > lexical_overlap_score(query, unrelated)


def test_rank_contexts_combines_vector_and_lexical_signals():
    contexts = [
        {"content": "权限管理和日志审计能力提升。", "distance": 0.1, "source": "a.txt"},
        {"content": "2024 年营业收入为 8.6 亿元。", "distance": 0.8, "source": "b.txt"},
    ]

    ranked = rank_contexts("2024年营业收入是多少？", contexts, top_k=1, lexical_weight=0.7)

    assert ranked[0]["source"] == "b.txt"
    assert "relevance" in ranked[0]


def test_normalize_relevance_clamps_invalid_distance():
    assert normalize_relevance(0.2) == 0.8
    assert normalize_relevance(2.0) == 0.0
    assert normalize_relevance(None) == 0.0


def test_financial_guardrail_refuses_stock_prediction():
    answer = financial_guardrail_refusal("请预测公司明年股价会涨到多少？")

    assert answer is not None
    assert "不提供股价预测" in answer
