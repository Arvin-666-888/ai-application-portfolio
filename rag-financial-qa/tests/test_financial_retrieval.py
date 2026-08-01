from app.utils.financial_retrieval import (
    collapse_provenance,
    extract_terms,
    financial_guardrail_refusal,
    financial_v2_rank,
    financial_v3_rank,
    lexical_overlap_score,
    normalize_relevance,
    numeric_overlap_score,
    parse_query_intent,
    rank_contexts,
    weighted_rrf,
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


def test_numeric_overlap_uses_boundaries_and_normalizes_commas():
    assert numeric_overlap_score("2025 revenue 416,161", "2025 revenue was 416161") == 1.0
    assert numeric_overlap_score("2024年营业收入", "公司2024年营业收入为100") == 1.0
    assert numeric_overlap_score("revenue 100", "revenue 1000") == 0.0
    assert numeric_overlap_score("revenue", "revenue 100") == 0.0


def test_numeric_weight_promotes_matching_year():
    contexts = [
        {"content": "Revenue for 2024 was 100", "distance": 0.1, "source": "wrong-year.pdf"},
        {"content": "Revenue for 2025 was 90", "distance": 0.3, "source": "right-year.pdf"},
    ]

    ranked = rank_contexts(
        "What was revenue in 2025?",
        contexts,
        top_k=1,
        lexical_weight=0.0,
        numeric_weight=0.3,
    )

    assert ranked[0]["source"] == "right-year.pdf"
    assert ranked[0]["numeric_score"] == 1.0


def test_query_intent_extracts_financial_signals_without_labels():
    intent = parse_query_intent("2024年度招商银行集团经营活动产生的现金流量净额是多少？")

    assert intent.years == ("2024",)
    assert "经营活动产生的现金流量净额" in intent.metric_aliases
    assert "集团" in intent.scopes
    assert intent.answer_type == "amount"
    assert intent.table_fact is True


def test_query_intent_does_not_treat_ownership_metric_as_company_or_scope():
    intent = parse_query_intent("海尔智家2024年度归属于母公司股东的净利润是多少？")

    assert intent.company_terms == ("海尔智家",)
    assert intent.scopes == ()
    assert "归属于母公司股东的净利润" in intent.metric_aliases
    assert "净利润" not in intent.metric_aliases


def test_query_intent_distinguishes_company_name_from_explicit_scope():
    company = parse_query_intent("美的集团2024年度净利润是多少？")
    scoped = parse_query_intent("招商银行2024年度集团口径经营活动产生的现金流量净额是多少？")

    assert company.company_terms == ("美的集团",)
    assert company.scopes == ()
    assert scoped.company_terms == ("招商银行",)
    assert scoped.scopes == ("集团",)


def test_rerank_does_not_trust_well_formed_but_false_semantic_digest():
    from app.utils.financial_retrieval import financial_rerank_features

    intent = parse_query_intent("2024年合并利润表营业收入是多少？")
    features = financial_rerank_features(intent, {
        "content": "营业收入 100",
        "source": "report.pdf",
        "content_type": "table",
        "table_semantic_schema_version": "financial-table-semantic-context-v1",
        "table_semantic_canonical_sha256": "0" * 64,
        "statement_title": "2024年合并利润表",
        "statement_type": "利润表",
        "table_scope": "合并",
        "statement_period": "2024",
    })

    assert features["statement_scope_score"] == 0.0
    assert features["year_score"] == 0.0


def test_weighted_rrf_uses_rank_not_raw_score_scale():
    table = {"candidate_id": "table", "distance": 999.0}
    text = {"candidate_id": "text", "distance": 0.0}

    scores = weighted_rrf(
        {"table_lexical": [table], "text_dense": [text]},
        {"table_lexical": 1.25, "text_dense": 0.75},
    )

    assert scores["table"] > scores["text"]


def test_collapse_provenance_keeps_one_chunk_per_table_and_backfills():
    contexts = [
        {"candidate_id": "a", "table_id": "t1"},
        {"candidate_id": "b", "table_id": "t1"},
        {"candidate_id": "c", "table_id": "t2"},
        {"candidate_id": "d", "content_type": "text", "chunk_index": 1},
    ]

    result = collapse_provenance(contexts, top_k=3)

    assert [item["candidate_id"] for item in result] == ["a", "c", "d"]


def test_financial_v2_rank_prefers_metric_row_and_table_without_answer_value():
    table = {
        "candidate_id": "table",
        "content": "| 指标 | 2024年 |\n| 营业收入 | 100 |",
        "content_type": "table",
        "source": "美的集团_2024年年度报告.pdf",
        "page_number": 10,
        "table_id": "t10",
        "artifact_chunk_index": 1,
    }
    text = {
        "candidate_id": "text",
        "content": "2024年公司经营情况良好",
        "content_type": "text",
        "source": "美的集团_2024年年度报告.pdf",
        "page_number": 2,
        "chunk_index": 2,
    }

    ranked = financial_v2_rank(
        "美的集团2024年度营业收入是多少？",
        {
            "table_lexical": [table],
            "table_dense": [table],
            "text_lexical": [text],
            "text_dense": [text],
        },
        top_k=2,
    )

    assert ranked[0]["candidate_id"] == "table"
    assert ranked[0]["metric_row_score"] == 1.0


def test_financial_v3_rank_uses_query_visible_source_and_statement_signals():
    wrong_source = {
        "candidate_id": "wrong-source",
        "content": "2024年主要会计数据和财务指标 营业收入 100",
        "content_type": "table",
        "source": "其他公司_2024年年度报告.pdf",
        "page_number": 1,
        "table_id": "wrong-table",
    }
    right_source = {
        "candidate_id": "right-source",
        "content": "合并利润表\n| 指标 | 2024年 |\n| 营业收入 | 100 |",
        "content_type": "table",
        "source": "美的集团_2024年年度报告.pdf",
        "page_number": 10,
        "table_id": "right-table",
    }

    ranked = financial_v3_rank(
        "美的集团2024年度合并利润表中的营业收入是多少？",
        {
            "table_lexical": [wrong_source, right_source],
            "table_dense": [wrong_source, right_source],
            "text_lexical": [],
            "text_dense": [],
        },
        top_k=2,
    )

    assert ranked[0]["candidate_id"] == "right-source"
    assert ranked[0]["source_filter_score"] == 1.0
    assert ranked[0]["expected_statement_score"] == 1.0
    assert ranked[1]["financial_noise_score"] == 1.0


def test_normalize_relevance_clamps_invalid_distance():
    assert normalize_relevance(0.2) == 0.8
    assert normalize_relevance(2.0) == 0.0
    assert normalize_relevance(None) == 0.0


def test_financial_guardrail_refuses_stock_prediction():
    answer = financial_guardrail_refusal("请预测公司明年股价会涨到多少？")

    assert answer is not None
    assert "不提供股价预测" in answer
