import pytest

pytest.importorskip("langchain_chroma")
pytest.importorskip("langchain_core")
pytest.importorskip("langchain_text_splitters")

from examples.langchain_rag_demo import HashEmbeddings, run_demo


def test_hash_embeddings_are_deterministic():
    embeddings = HashEmbeddings(dimension=64)
    first = embeddings.embed_query("SKU-A100 商品价格")
    second = embeddings.embed_query("SKU-A100 商品价格")
    assert first == second
    assert len(first) == 64


def test_langchain_rag_demo_returns_ecommerce_sources_in_mock_mode():
    result = run_demo("2026-07-15 Amazon 美国市场 SKU-A100 的价格是多少？", top_k=2, force_mock=True)
    assert result["mode"] == "mock"
    assert result["sources"]
    assert any(source["document"] == "ecommerce_product_manual.txt" for source in result["sources"])
    assert result["sources"][0]["snippet"]


def test_langchain_demo_fails_closed_for_unknown_sku():
    result = run_demo("SKU-Z999 的价格是多少？", top_k=2, force_mock=True)
    assert "无法回答" in result["answer"]
    assert "SKU-Z999" in result["answer"]
    assert result["sources"] == []


def test_langchain_demo_fails_closed_for_unsupported_fact():
    result = run_demo("SKU-A100 的重量是多少？", top_k=2, force_mock=True)
    assert "无法回答" in result["answer"]
    assert "只发布商品价格" in result["answer"]
    assert result["sources"] == []
