import chromadb

from app.utils.table_pdf_parser import IndexChunk
from app.utils.vector_store import VectorStore


def _vector(value: float) -> list[float]:
    return [value, 1.0 - value, 0.5]


def test_chroma_ingests_text_and_table_metadata_in_one_collection():
    store = VectorStore(client=chromadb.EphemeralClient(), collection_prefix="test_mixed")
    chunks = [
        "公司管理层讨论。",
        "| 指标 | 2024 |\n| --- | --- |\n| 营业收入 | 8.6亿元 |",
    ]
    metadatas = [
        {"content_type": "text", "page_number": 3, "provenance_id": "text-1"},
        {
            "content_type": "table",
            "page_number": 12,
            "provenance_id": "table-1",
            "table_id": "table-1",
            "nested": {"unit": "亿元"},
        },
    ]

    store.add_documents(1, chunks, [_vector(0.1), _vector(0.9)], 7, "report.pdf", metadatas)
    collection = store.get_or_create_collection(1)
    data = collection.get(include=["documents", "metadatas"])

    assert data["ids"] == ["doc_7_chunk_0", "doc_7_chunk_1"]
    assert {meta["content_type"] for meta in data["metadatas"]} == {"text", "table"}
    assert all(isinstance(value, (str, int, float, bool)) for meta in data["metadatas"] for value in meta.values())


def test_vector_and_lexical_paths_preserve_table_provenance():
    store = VectorStore(client=chromadb.EphemeralClient(), collection_prefix="test_query")
    store.add_documents(
        1,
        ["无关权限文本", "| 指标 | 2024 |\n| 营业收入 | 8.6亿元 |"],
        [_vector(0.1), _vector(0.9)],
        4,
        "annual.pdf",
        [
            {"content_type": "text", "page_number": 2, "provenance_id": "p2"},
            {"content_type": "table", "page_number": 12, "provenance_id": "p12", "table_id": "t12"},
        ],
    )

    contexts = store.query(
        1,
        query_embedding=_vector(0.9),
        top_k=2,
        query_text="营业收入 8.6亿元",
        candidate_multiplier=2,
    )

    table = next(ctx for ctx in contexts if ctx["content_type"] == "table")
    assert table["page_number"] == 12
    assert table["provenance_id"] == "p12"
    assert table["table_id"] == "t12"
    assert "营业收入" in table["content"]


def test_query_uses_exact_candidate_pool_then_returns_final_top_k():
    store = VectorStore(client=chromadb.EphemeralClient(), collection_prefix="test_candidates")
    chunks = [f"Revenue 2025 candidate {index}" for index in range(12)]
    store.add_documents(
        1,
        chunks,
        [_vector(index / 12) for index in range(12)],
        3,
        "annual.pdf",
    )
    collection = store.get_or_create_collection(1)
    requested = []
    original_query = collection.query

    def recording_query(**kwargs):
        requested.append(kwargs["n_results"])
        return original_query(**kwargs)

    collection.query = recording_query
    store.get_or_create_collection = lambda kb_id: collection

    contexts = store.query(
        1,
        query_embedding=_vector(0.5),
        top_k=5,
        query_text="Revenue 2025",
        candidate_k=10,
        numeric_weight=0.15,
    )

    assert requested == [10]
    assert len(contexts) == 5
    assert all("numeric_score" in context for context in contexts)


def test_query_diagnostics_returns_ranked_channels_and_stable_identities():
    store = VectorStore(client=chromadb.EphemeralClient(), collection_prefix="test_diagnostics")
    store.add_documents(
        1,
        ["营业收入 100", "净利润 20", "无关文本"],
        [_vector(0.9), _vector(0.5), _vector(0.1)],
        1,
        "annual.pdf",
        [
            {"content_type": "table", "page_number": 10, "table_id": "t10"},
            {"content_type": "text", "page_number": 11},
            {"content_type": "text", "page_number": 12},
        ],
    )

    first = store.query_diagnostics(
        1,
        query_embedding=_vector(0.9),
        query_text="营业收入",
        dense_k=3,
        lexical_k=3,
    )
    second = store.query_diagnostics(
        1,
        query_embedding=_vector(0.9),
        query_text="营业收入",
        dense_k=3,
        lexical_k=3,
    )

    assert [item["candidate_id"] for item in first["fusion"]] == [
        item["candidate_id"] for item in second["fusion"]
    ]
    assert [item["dense_rank"] for item in first["dense"]] == [1, 2, 3]
    assert first["lexical"][0]["lexical_rank"] == 1
    assert first["fusion"][0]["fusion_rank"] == 1
    matching = next(
        item for item in first["fusion"] if item["content"] == "营业收入 100"
    )
    assert matching["dense_rank"] >= 1
    assert matching["lexical_rank"] == 1
    assert len(matching["candidate_id"]) == 64


def test_query_diagnostics_lazily_loads_persistent_embeddings():
    client = chromadb.EphemeralClient()
    writer = VectorStore(client=client, collection_prefix="test_persistent_diagnostics")
    writer.add_documents(
        1,
        ["营业收入 100", "净利润 20"],
        [_vector(0.9), _vector(0.1)],
        1,
        "annual.pdf",
    )
    reader = VectorStore(client=client, collection_prefix="test_persistent_diagnostics")

    diagnostics = reader.query_diagnostics(
        1,
        query_embedding=_vector(0.9),
        query_text="营业收入",
        dense_k=2,
        lexical_k=2,
    )

    assert diagnostics["dense"][0]["content"] == "营业收入 100"
    assert reader._diagnostic_indexes[1]["embeddings"]


def test_financial_v2_preserves_table_channel_under_text_competition():
    store = VectorStore(client=chromadb.EphemeralClient(), collection_prefix="test_financial_v2")
    text_chunks = [f"营业收入 2024 文本干扰 {index}" for index in range(100)]
    table_chunk = "| 指标 | 2024年 |\n| 营业收入 | 100 |"
    chunks = text_chunks + [table_chunk]
    metadatas = [
        {"content_type": "text", "page_number": index + 1}
        for index in range(100)
    ] + [{"content_type": "table", "page_number": 101, "table_id": "target"}]
    store.add_documents(
        1,
        chunks,
        [_vector(0.9) for _ in text_chunks] + [_vector(0.1)],
        1,
        "annual.pdf",
        metadatas,
    )

    result = store.query_financial_v2(
        1,
        query_embedding=_vector(0.9),
        query_text="2024年营业收入是多少？",
        top_k=5,
    )

    assert any(item.get("table_id") == "target" for item in result["channels"]["table_lexical"])
    assert any(item.get("table_id") == "target" for item in result["top_k"])


def test_financial_v2_top_five_has_unique_table_ids():
    store = VectorStore(client=chromadb.EphemeralClient(), collection_prefix="test_table_collapse")
    chunks = [
        "| 指标 | 2024年 |\n| 营业收入 | 100 |",
        "| 指标 | 2023年 |\n| 营业收入 | 90 |",
        "| 指标 | 2024年 |\n| 净利润 | 20 |",
    ]
    store.add_documents(
        1,
        chunks,
        [_vector(0.9), _vector(0.8), _vector(0.7)],
        1,
        "annual.pdf",
        [
            {"content_type": "table", "page_number": 1, "table_id": "same"},
            {"content_type": "table", "page_number": 1, "table_id": "same"},
            {"content_type": "table", "page_number": 2, "table_id": "other"},
        ],
    )

    result = store.query_financial_v2(
        1,
        query_embedding=_vector(0.9),
        query_text="2024年营业收入是多少？",
        top_k=5,
    )

    table_ids = [item["table_id"] for item in result["top_k"]]
    assert table_ids.count("same") == 1
    assert "other" in table_ids


def test_delete_document_removes_text_and_table_chunks_by_existing_prefix():
    store = VectorStore(client=chromadb.EphemeralClient(), collection_prefix="test_delete")
    store.add_documents(
        1,
        ["text", "table"],
        [_vector(0.1), _vector(0.2)],
        9,
        "report.pdf",
        [{"content_type": "text"}, {"content_type": "table"}],
    )

    store.delete_document(1, 9)

    assert store.get_collection_count(1) == 0


def test_add_documents_rejects_mismatched_metadata_before_write():
    store = VectorStore(client=chromadb.EphemeralClient(), collection_prefix="test_lengths")

    try:
        store.add_documents(1, ["one"], [_vector(0.1)], 1, "x.pdf", [])
    except ValueError as exc:
        assert "metadatas" in str(exc)
    else:
        raise AssertionError("expected metadata length validation")

    assert store.get_collection_count(1) == 0


def test_query_propagates_chroma_failures_instead_of_returning_empty():
    class BrokenClient:
        def get_or_create_collection(self, **kwargs):
            raise RuntimeError("db corrupt")

    store = VectorStore(client=BrokenClient())

    try:
        store.query(1, [0.1], top_k=1, query_text="营业收入")
    except RuntimeError as exc:
        assert "db corrupt" in str(exc)
    else:
        raise AssertionError("expected Chroma query failure")


def test_query_propagates_lexical_loading_failure():
    class BrokenLexicalCollection:
        def count(self):
            return 1

        def query(self, **kwargs):
            return {
                "documents": [["营业收入 100"]],
                "metadatas": [[{"source": "annual.pdf"}]],
                "distances": [[0.1]],
            }

        def get(self, **kwargs):
            raise RuntimeError("lexical backend failed")

    store = VectorStore(client=chromadb.EphemeralClient())
    store.get_or_create_collection = lambda kb_id: BrokenLexicalCollection()

    try:
        store.query(1, [0.1], top_k=1, query_text="营业收入")
    except RuntimeError as exc:
        assert "lexical backend failed" in str(exc)
    else:
        raise AssertionError("expected lexical Chroma failure")


def test_legacy_metadata_remains_queryable():
    store = VectorStore(client=chromadb.EphemeralClient(), collection_prefix="test_legacy")
    store.add_documents(1, ["营业收入 100"], [_vector(0.5)], 1, "legacy.txt")

    contexts = store.query(1, _vector(0.5), top_k=1, query_text="营业收入")

    assert contexts[0]["source"] == "legacy.txt"
    assert contexts[0]["doc_id"] == 1
    assert "content_type" not in contexts[0]
