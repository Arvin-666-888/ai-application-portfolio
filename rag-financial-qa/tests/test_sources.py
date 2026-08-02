from app.utils.retrieval import build_source_items


def test_build_sources_includes_snippet_and_relevance():
    contexts = [
        {
            "source": "finance_summary_2024.txt",
            "content": "公司 2024 年营业收入为 8.6 亿元，同比增长 12%。",
            "distance": 0.2,
            "chunk_index": 1,
        }
    ]

    sources = build_source_items(contexts)

    assert sources == [
        {
            "document": "finance_summary_2024.txt",
            "relevance": 0.8,
            "citation_id": "C1",
            "snippet": "公司 2024 年营业收入为 8.6 亿元，同比增长 12%。",
        }
    ]


def test_build_sources_keeps_different_chunks_from_same_document():
    contexts = [
        {"source": "risk_notice.txt", "content": "云资源价格波动。", "distance": 0.1, "chunk_index": 0},
        {"source": "risk_notice.txt", "content": "客户集中度风险。", "distance": 0.3, "chunk_index": 1},
    ]

    sources = build_source_items(contexts)

    assert len(sources) == 2
    assert sources[0]["citation_id"] == "C1"
    assert sources[1]["citation_id"] == "C2"
    assert sources[0]["snippet"] == "云资源价格波动。"
    assert sources[1]["snippet"] == "客户集中度风险。"


def test_build_sources_does_not_leak_full_content():
    contexts = [
        {
            "source": "annual.pdf",
            "content": "A" * 400 + "PRIVATE_TAIL",
            "distance": 0.1,
            "chunk_index": 1,
        }
    ]

    sources = build_source_items(contexts)

    assert sources[0]["citation_id"] == "C1"
    assert len(sources[0]["snippet"]) == 300
    assert "PRIVATE_TAIL" not in str(sources)
    assert "content" not in sources[0]


def test_build_sources_preserves_optional_table_provenance():
    contexts = [
        {
            "source": "annual.pdf",
            "content": "| 营业收入 | 8.6亿元 |",
            "distance": 0.1,
            "chunk_index": 2,
            "page_number": 12,
            "content_type": "table",
            "provenance_id": "p12",
            "table_id": "t12",
        }
    ]

    sources = build_source_items(contexts)

    assert sources[0]["page_number"] == 12
    assert sources[0]["content_type"] == "table"
    assert sources[0]["provenance_id"] == "p12"
    assert sources[0]["table_id"] == "t12"
