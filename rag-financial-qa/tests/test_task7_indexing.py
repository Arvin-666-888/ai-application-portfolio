import asyncio
import math

import chromadb
import pytest

from app.services import document_service
from app.utils.vector_store import VectorStore


def _vector(value: float) -> list[float]:
    return [value, 1.0 - value, 0.5]


def test_versioned_upsert_is_deterministic_and_filters_staging_versions():
    store = VectorStore(client=chromadb.EphemeralClient(), collection_prefix="task7_versions")
    store.add_documents(1, ["old one", "old two"], [_vector(.1), _vector(.2)], 7, "a.pdf", index_version="v1")
    store.add_documents(1, ["new one"], [_vector(.9)], 7, "a.pdf", index_version="v2")
    store.add_documents(1, ["old changed"], [_vector(.3)], 7, "a.pdf", index_version="v1")

    collection = store.get_or_create_collection(1)
    data = collection.get(include=["documents", "metadatas"])
    assert collection.count() == 2
    assert all("version_" in value for value in data["ids"])
    assert {meta["index_version"] for meta in data["metadatas"]} == {"v1", "v2"}

    v2 = store.query(1, _vector(.9), top_k=5, active_index_versions=["v2"])
    assert [item["content"] for item in v2] == ["new one"]


def test_exact_active_targets_keep_legacy_for_but_hide_upgraded_document():
    store = VectorStore(
        client=chromadb.EphemeralClient(),
        collection_prefix="task7_exact_targets",
    )
    store.add_documents(
        1, ["A legacy revenue"], [_vector(.1)], 1, "a.pdf", index_version="legacy",
    )
    store.add_documents(
        1, ["A upgraded revenue"], [_vector(.9)], 1, "a.pdf", index_version="a-v2",
    )
    store.add_documents(
        1, ["B legacy revenue"], [_vector(.5)], 2, "b.pdf", index_version="legacy",
    )
    targets = [(1, "a-v2"), (2, "legacy")]

    dense = store.query(1, _vector(.9), top_k=10, active_index_targets=targets)
    hybrid = store.query(
        1, _vector(.9), top_k=10, query_text="revenue", active_index_targets=targets,
    )
    diagnostics = store.query_diagnostics(
        1, _vector(.9), "revenue", active_index_targets=targets,
    )
    financial = store.query_financial_v2(
        1, _vector(.9), "revenue", top_k=10, active_index_targets=targets,
    )

    expected = {"A upgraded revenue", "B legacy revenue"}
    assert {item["content"] for item in dense} == expected
    assert {item["content"] for item in hybrid} == expected
    assert {item["content"] for item in diagnostics["dense"]} == expected
    assert {item["content"] for item in diagnostics["lexical"]} == expected
    assert {
        item["content"]
        for channel in financial["channels"].values()
        for item in channel
    } == expected
    assert "A legacy revenue" not in {
        item["content"] for item in diagnostics["union"]
    }


@pytest.mark.parametrize("method", ["query", "query_diagnostics", "query_financial_v2"])
def test_active_version_compatibility_filter_cannot_mix_with_exact_targets(method):
    store = VectorStore(client=chromadb.EphemeralClient(), collection_prefix=f"task7_{method}")
    store.add_documents(1, ["content"], [_vector(.1)], 1, "a.pdf", index_version="legacy")
    kwargs = {
        "active_index_versions": ["legacy"],
        "active_index_targets": [(1, "legacy")],
    }
    with pytest.raises(ValueError, match="禁止同时传入"):
        if method == "query":
            store.query(1, _vector(.1), **kwargs)
        elif method == "query_diagnostics":
            store.query_diagnostics(1, _vector(.1), "content", **kwargs)
        else:
            store.query_financial_v2(1, _vector(.1), "content", **kwargs)


def test_failed_staging_version_is_not_visible_to_active_query():
    store = VectorStore(
        client=chromadb.EphemeralClient(),
        collection_prefix="task7_staging_fence",
    )
    store.add_documents(
        1, ["published"], [_vector(.1)], 1, "a.pdf", index_version="active-a",
    )
    store.add_documents(
        1, ["partial failed build"], [_vector(.9)], 2, "b.pdf",
        index_version="job-staging-b",
    )

    active = store.query(
        1, _vector(.9), top_k=10, active_index_versions=["active-a"],
    )
    assert [item["content"] for item in active] == ["published"]

    store.delete_document_version(1, 2, "job-staging-b")
    assert store.get_collection_count(1) == 1


def test_legacy_migration_preserves_old_and_new_documents():
    store = VectorStore(
        client=chromadb.EphemeralClient(),
        collection_prefix="task7_legacy_migration",
    )
    store.add_documents(1, ["legacy"], [_vector(.1)], 1, "old.pdf")
    assert store.migrate_legacy_document(1, 1) == 1
    store.add_documents(1, ["new"], [_vector(.9)], 2, "new.pdf", index_version="v2")

    result = store.query(
        1, _vector(.9), top_k=10, active_index_versions=["legacy", "v2"],
    )
    assert {item["content"] for item in result} == {"legacy", "new"}


def test_delete_collection_is_idempotent_but_propagates_other_failures():
    store = VectorStore(
        client=chromadb.EphemeralClient(),
        collection_prefix="task7_delete_collection",
    )
    store.get_or_create_collection(1)
    store.delete_collection(1)
    store.delete_collection(1)

    class BrokenClient:
        def delete_collection(self, name):
            raise OSError("storage failure")

    broken = VectorStore(client=BrokenClient(), collection_prefix="broken")
    with pytest.raises(OSError, match="storage failure"):
        broken.delete_collection(1)


def test_vector_store_rejects_nonfinite_and_dimension_mismatch():
    store = VectorStore(client=chromadb.EphemeralClient(), collection_prefix="task7_validation")
    with pytest.raises(ValueError, match="非有限"):
        store.add_documents(1, ["bad"], [[1.0, math.nan]], 1, "a.pdf")

    store.add_documents(1, ["good"], [[1.0, 0.0]], 1, "a.pdf")
    with pytest.raises(ValueError, match="维度"):
        store.add_documents(1, ["bad dimension"], [[1.0, 0.0, 1.0]], 2, "b.pdf")


def test_diagnostic_cache_is_invalidated_after_upsert():
    store = VectorStore(client=chromadb.EphemeralClient(), collection_prefix="task7_cache")
    store.add_documents(1, ["first"], [_vector(.1)], 1, "a.pdf", index_version="v1")
    store.query_diagnostics(1, _vector(.1), "first")
    assert store._diagnostic_indexes

    store.add_documents(1, ["second"], [_vector(.2)], 2, "b.pdf", index_version="v1")
    assert not store._diagnostic_indexes
    result = store.query_diagnostics(1, _vector(.2), "second")
    assert any(item["content"] == "second" for item in result["dense"])


def test_financial_v2_online_path_does_not_use_exact_diagnostics(monkeypatch):
    store = VectorStore(client=chromadb.EphemeralClient(), collection_prefix="task7_online")
    store.add_documents(
        1,
        ["普通文本", "| 指标 | 2024 |\n| 营业收入 | 100 |"],
        [_vector(.9), _vector(.2)],
        1,
        "annual.pdf",
        [{"content_type": "text"}, {"content_type": "table", "table_id": "t1"}],
    )
    monkeypatch.setattr(store, "query_diagnostics", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("exact scan")))

    result = store.query_financial_v2(1, _vector(.9), "2024营业收入", top_k=2)
    assert result["channels"]["table_lexical"][0]["table_id"] == "t1"
    assert result["top_k"][0].get("financial_v2_score") is not None


def test_batch_embed_reorders_api_items_by_index(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [
                {"index": 1, "embedding": [0.0, 1.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(document_service.settings, "API_KEY", "test")
    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: Client())
    vectors = asyncio.run(document_service._batch_embed(["a", "b"]))
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]


def test_batch_embed_rejects_invalid_batch(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"index": 0, "embedding": [1.0, float("inf")]}]}

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, *args, **kwargs): return Response()

    monkeypatch.setattr(document_service.settings, "API_KEY", "test")
    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: Client())
    with pytest.raises(ValueError, match="非有限"):
        asyncio.run(document_service._batch_embed(["a"]))
