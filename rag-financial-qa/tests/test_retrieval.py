from app.utils.retrieval import (
    collapse_provenance,
    ecommerce_guardrail_refusal,
    ecommerce_rerank_features,
    ecommerce_v2_rank,
    extract_terms,
    lexical_overlap_score,
    normalize_relevance,
    numeric_overlap_score,
    parse_query_intent,
    rank_contexts,
    weighted_rrf,
)


def test_extract_terms_handles_chinese_and_numbers():
    terms = extract_terms("SKU-A100 2026-07-15价格是多少？")
    assert "sku-a100" in terms
    assert "价格" in terms


def test_lexical_overlap_scores_related_content_higher():
    query = "SKU-A100价格是多少？"
    related = "SKU-A100价格为USD 79.90。"
    unrelated = "该商品包装设计简洁。"
    assert lexical_overlap_score(query, related) > lexical_overlap_score(query, unrelated)


def test_rank_contexts_combines_vector_lexical_and_numeric_signals():
    contexts = [
        {"content": "无关商品介绍。", "distance": 0.1, "source": "a.txt"},
        {"content": "SKU-A100价格为USD 79.90。", "distance": 0.8, "source": "b.txt"},
    ]
    ranked = rank_contexts("SKU-A100价格79.90", contexts, top_k=1, lexical_weight=0.7, numeric_weight=0.1)
    assert ranked[0]["source"] == "b.txt"


def test_numeric_overlap_uses_boundaries_and_normalizes_commas():
    assert numeric_overlap_score("price 1,299", "price 1299") == 1.0
    assert numeric_overlap_score("SKU 100", "SKU 1000") == 0.0


def test_query_intent_extracts_ecommerce_identity_and_fact():
    intent = parse_query_intent("2026-07-15 Amazon美国市场 SKU-A100 轻量旅行背包的价格是多少？")
    assert intent.skus == ("SKU-A100",)
    assert intent.platforms == ("Amazon",)
    assert intent.markets == ("美国",)
    assert intent.dates == ("2026-07-15",)
    assert intent.fact_types == ("price",)
    assert intent.answer_type == "price"


def test_query_intent_distinguishes_all_four_fact_types():
    assert parse_query_intent("库存数量有多少？").fact_types == ("inventory_quantity",)
    assert parse_query_intent("配送时长多久？").fact_types == ("delivery_duration",)
    assert parse_query_intent("关税税率是多少？").fact_types == ("customs_duty_rate",)


def test_rerank_does_not_trust_false_semantic_digest():
    intent = parse_query_intent("2026-07-15 Amazon SKU-A100价格是多少？")
    features = ecommerce_rerank_features(intent, {
        "content": "价格 USD 79.90",
        "source": "catalog.pdf",
        "content_type": "table",
        "table_semantic_schema_version": "ecommerce-table-semantic-context-v1",
        "table_semantic_canonical_sha256": "0" * 64,
        "table_title": "2026-07-15 Amazon 商品价格表",
        "platform": "Amazon",
        "effective_date": "2026-07-15",
    })
    assert features["platform_market_score"] == 0.0
    assert features["date_score"] == 0.0


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
    assert [item["candidate_id"] for item in collapse_provenance(contexts, 3)] == ["a", "c", "d"]


def test_ecommerce_v2_rank_prefers_bound_table_row():
    table = {
        "candidate_id": "table", "content": "| SKU | 价格 |\n| SKU-A100 | USD 79.90 |",
        "content_type": "table", "source": "catalog.pdf", "page_number": 10, "table_id": "t10",
    }
    text = {
        "candidate_id": "text", "content": "SKU-A100商品介绍", "content_type": "text",
        "source": "catalog.pdf", "page_number": 2, "chunk_index": 2,
    }
    ranked = ecommerce_v2_rank(
        "SKU-A100价格是多少？",
        {"table_lexical": [table], "table_dense": [table], "text_lexical": [text], "text_dense": [text]},
        top_k=2,
    )
    assert ranked[0]["candidate_id"] == "table"
    assert ranked[0]["fact_row_score"] == 1.0
    assert ranked[0]["ecommerce_v2_score"] > 0


def test_normalize_relevance_clamps_invalid_distance():
    assert normalize_relevance(0.2) == 0.8
    assert normalize_relevance(2.0) == 0.0
    assert normalize_relevance(None) == 0.0


def test_ecommerce_guardrail_limits_answerable_surface():
    assert ecommerce_guardrail_refusal("SKU-A100价格是多少？") is None
    assert ecommerce_guardrail_refusal("SKU-A100重量是多少？") is not None
    assert ecommerce_guardrail_refusal("SKU-A100价格和库存是多少？") is not None
