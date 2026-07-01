"""LangChain comparison demo for the financial RAG project.

This script is intentionally separate from the FastAPI application. The main
project keeps its hand-written RAG pipeline, while this demo shows the same
ideas with LangChain abstractions: Document, RecursiveCharacterTextSplitter,
Chroma retriever, optional OpenAI-compatible embeddings and chat model.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Iterable

try:
    from langchain_chroma import Chroma
    from langchain_core.documents import Document
    from langchain_core.embeddings import Embeddings
    from langchain_text_splitters import RecursiveCharacterTextSplitter
except ImportError as exc:  # pragma: no cover - exercised by users without optional deps
    raise SystemExit(
        "Missing LangChain optional dependencies. Install them with:\n"
        "  pip install -r requirements-langchain.txt"
    ) from exc

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = PROJECT_ROOT / "evals" / "fixtures"


class HashEmbeddings(Embeddings):
    """Small deterministic lexical hashing embeddings for offline demos.

    It is not a semantic embedding model. It only lets the LangChain retriever
    run without an API key and tends to rank chunks that share Chinese keywords
    or English tokens with the question.
    """

    def __init__(self, dimension: int = 768):
        self.dimension = dimension

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for feature in self._features(text):
            digest = hashlib.md5(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return vector
        return [value / norm for value in vector]

    def _features(self, text: str) -> Iterable[str]:
        normalized = re.sub(r"\s+", "", text.lower())
        for token in re.findall(r"[a-z0-9_]+", normalized):
            yield token
        chinese_chars = re.findall(r"[\u4e00-\u9fff]", normalized)
        for char in chinese_chars:
            yield char
        compact = "".join(chinese_chars)
        for size in (2, 3, 4):
            for index in range(0, max(0, len(compact) - size + 1)):
                yield compact[index:index + size]


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_fixture_documents() -> list[Document]:
    documents: list[Document] = []
    for path in sorted(FIXTURE_DIR.glob("*.txt")):
        content = path.read_text(encoding="utf-8")
        documents.append(Document(page_content=content, metadata={"source": path.name}))
    if not documents:
        raise FileNotFoundError(f"No fixture documents found in {FIXTURE_DIR}")
    return documents


def split_documents(documents: list[Document]) -> list[Document]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=400,
        chunk_overlap=80,
        separators=["\n\n", "\n", "。", "；", "，", " ", ""],
    )
    chunks = splitter.split_documents(documents)
    for index, chunk in enumerate(chunks):
        chunk.metadata.setdefault("chunk_index", index)
    return chunks


def build_embeddings(force_mock: bool) -> Embeddings:
    load_env_file(PROJECT_ROOT / ".env")
    api_key = os.getenv("API_KEY", "")
    if force_mock or not api_key:
        return HashEmbeddings()

    try:
        from langchain_openai import OpenAIEmbeddings
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("Install langchain-openai to use real embeddings.") from exc

    return OpenAIEmbeddings(
        api_key=api_key,
        base_url=os.getenv("BASE_URL", "https://api.openai.com/v1"),
        model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
    )


def build_vector_store(chunks: list[Document], embeddings: Embeddings) -> Chroma:
    collection_name = "langchain_rag_financial_demo"
    vector_store = Chroma(collection_name=collection_name, embedding_function=embeddings)
    vector_store.add_documents(chunks)
    return vector_store


def is_entity_mismatch(question: str, docs: list[Document]) -> bool:
    corpus = "\n".join(doc.page_content for doc in docs)
    competitor_terms = ["竞争对手", "A公司", "同行公司", "其他公司"]
    return any(term in question for term in competitor_terms) and not any(term in corpus for term in competitor_terms)


def normalize_relevance(raw_score: float) -> float:
    """Convert vector-store distance/score into a stable 0-1 demo relevance."""
    score = max(float(raw_score), 0.0)
    return round(1.0 / (1.0 + score), 4)


def make_sources(results: list[tuple[Document, float]]) -> list[dict]:
    sources = []
    for document, score in results:
        sources.append({
            "document": document.metadata.get("source", "unknown"),
            "chunk_index": document.metadata.get("chunk_index", 0),
            "relevance": normalize_relevance(score),
            "snippet": document.page_content[:300],
        })
    return sources


def generate_answer(question: str, sources: list[dict], docs: list[Document], force_mock: bool) -> str:
    load_env_file(PROJECT_ROOT / ".env")
    api_key = os.getenv("API_KEY", "")
    if is_entity_mismatch(question, docs):
        return "根据现有资料无法回答该问题：样例资料只包含本公司信息，没有竞争对手A公司的披露数据。"
    if not sources:
        return "根据现有资料无法回答该问题：没有检索到相关片段。"

    context = "\n\n".join(f"[来源{i + 1}: {item['document']}]\n{item['snippet']}" for i, item in enumerate(sources))

    if force_mock or not api_key:
        return (
            f"[LangChain mock回答] 问题：{question}\n"
            f"已通过 LangChain Chroma retriever 找到 {len(sources)} 个相关片段。"
            "真实语义答案需要配置 API_KEY；当前输出用于演示 LangChain RAG 对照链路。"
        )

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("Install langchain-openai to use a real chat model.") from exc

    llm = ChatOpenAI(
        api_key=api_key,
        base_url=os.getenv("BASE_URL", "https://api.openai.com/v1"),
        model=os.getenv("MODEL", "gpt-4o-mini"),
        temperature=0.2,
    )
    response = llm.invoke([
        SystemMessage(content="你是一个金融文档 RAG 助手，只能根据参考资料回答。资料不足时必须拒答。"),
        HumanMessage(content=f"参考资料：\n{context}\n\n问题：{question}"),
    ])
    return str(response.content)


def run_demo(question: str, top_k: int = 3, force_mock: bool = False) -> dict:
    docs = load_fixture_documents()
    chunks = split_documents(docs)
    embeddings = build_embeddings(force_mock=force_mock)
    vector_store = build_vector_store(chunks, embeddings)
    results = vector_store.similarity_search_with_score(question, k=top_k)
    sources = make_sources(results)
    answer = generate_answer(question, sources, docs, force_mock=force_mock)
    return {
        "mode": "mock" if force_mock or not os.getenv("API_KEY", "") else "real_llm",
        "question": question,
        "answer": answer,
        "sources": sources,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the LangChain comparison RAG demo.")
    parser.add_argument("--question", default="2024年公司营业收入是多少？")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--mock", action="store_true", help="Force offline HashEmbeddings/mock answer mode.")
    args = parser.parse_args()

    result = run_demo(question=args.question, top_k=args.top_k, force_mock=args.mock)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
