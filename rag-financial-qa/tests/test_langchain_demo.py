import pytest

pytest.importorskip("langchain_chroma")
pytest.importorskip("langchain_core")
pytest.importorskip("langchain_text_splitters")

from examples.langchain_rag_demo import HashEmbeddings, run_demo


def test_hash_embeddings_are_deterministic():
    embeddings = HashEmbeddings(dimension=64)
    first = embeddings.embed_query("营业收入")
    second = embeddings.embed_query("营业收入")
    assert first == second
    assert len(first) == 64


def test_langchain_rag_demo_returns_sources_in_mock_mode():
    result = run_demo("2024年公司营业收入是多少？", top_k=2, force_mock=True)
    assert result["mode"] == "mock"
    assert result["sources"]
    assert any(source["document"] == "finance_summary_2024.txt" for source in result["sources"])
    assert result["sources"][0]["snippet"]
