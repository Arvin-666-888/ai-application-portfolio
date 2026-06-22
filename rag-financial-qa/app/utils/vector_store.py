import logging
from typing import Optional

import chromadb

from app.config import settings
from app.utils.retrieval import lexical_overlap_score, rank_contexts

logger = logging.getLogger("kb_qa.vector_store")


class VectorStore:
    def __init__(self):
        self._client: Optional[chromadb.PersistentClient] = None

    @property
    def client(self) -> chromadb.PersistentClient:
        if self._client is None:
            self._client = chromadb.PersistentClient(path=settings.CHROMA_DIR)
        return self._client

    def get_or_create_collection(self, kb_id: int) -> chromadb.Collection:
        collection_name = f"kb_{kb_id}"
        return self.client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"}
        )

    def add_documents(
        self,
        kb_id: int,
        chunks: list[str],
        embeddings: list[list[float]],
        doc_id: int,
        filename: str,
    ):
        collection = self.get_or_create_collection(kb_id)
        ids = [f"doc_{doc_id}_chunk_{i}" for i in range(len(chunks))]
        metadatas = [{"source": filename, "doc_id": doc_id, "chunk_index": i} for i in range(len(chunks))]

        batch_size = 100
        for i in range(0, len(ids), batch_size):
            batch_ids = ids[i:i + batch_size]
            batch_docs = chunks[i:i + batch_size]
            batch_embeddings = embeddings[i:i + batch_size]
            batch_metas = metadatas[i:i + batch_size]

            collection.add(
                ids=batch_ids,
                documents=batch_docs,
                embeddings=batch_embeddings,
                metadatas=batch_metas,
            )

        logger.info(f"Added {len(ids)} chunks to collection kb_{kb_id}")

    def query(
        self,
        kb_id: int,
        query_embedding: list[float],
        top_k: int = 3,
        query_text: str = "",
        candidate_multiplier: int = 1,
    ) -> list[dict]:
        try:
            collection = self.get_or_create_collection(kb_id)
            total_count = collection.count()
            if total_count == 0:
                return []

            candidate_count = min(max(top_k * candidate_multiplier, top_k), total_count)
            results = collection.query(
                query_embeddings=[query_embedding],
                n_results=candidate_count,
                include=["documents", "metadatas", "distances"],
            )

            contexts = []
            if results["documents"] and results["documents"][0]:
                for doc, meta, dist in zip(
                    results["documents"][0],
                    results["metadatas"][0],
                    results["distances"][0],
                ):
                    contexts.append({
                        "content": doc,
                        "source": meta.get("source", "未知"),
                        "doc_id": meta.get("doc_id", 0),
                        "chunk_index": meta.get("chunk_index", 0),
                        "distance": dist,
                    })

            if query_text:
                lexical_contexts = self._lexical_candidates(collection, query_text, top_k)
                contexts.extend(lexical_contexts)
                return rank_contexts(
                    query=query_text,
                    contexts=contexts,
                    top_k=top_k,
                    lexical_weight=settings.LEXICAL_WEIGHT,
                    min_relevance_score=settings.MIN_RELEVANCE_SCORE,
                )

            return contexts[:top_k]
        except Exception as e:
            logger.error(f"Query failed for kb_{kb_id}: {e}")
            return []

    def _lexical_candidates(self, collection: chromadb.Collection, query_text: str, top_k: int) -> list[dict]:
        try:
            data = collection.get(include=["documents", "metadatas"])
        except Exception as e:
            logger.warning(f"Lexical candidate loading failed: {e}")
            return []

        candidates = []
        for doc, meta in zip(data.get("documents", []), data.get("metadatas", [])):
            lexical_score = lexical_overlap_score(query_text, doc)
            if lexical_score <= 0:
                continue
            candidates.append({
                "content": doc,
                "source": meta.get("source", "未知"),
                "doc_id": meta.get("doc_id", 0),
                "chunk_index": meta.get("chunk_index", 0),
                "distance": 1.0,
                "lexical_score": lexical_score,
            })

        candidates.sort(key=lambda item: item["lexical_score"], reverse=True)
        return candidates[:top_k]

    def delete_document(self, kb_id: int, doc_id: int):
        try:
            collection = self.get_or_create_collection(kb_id)
            prefix = f"doc_{doc_id}_chunk_"
            all_ids = collection.get()["ids"]
            ids_to_delete = [id_ for id_ in all_ids if id_.startswith(prefix)]

            if ids_to_delete:
                collection.delete(ids=ids_to_delete)
                logger.info(f"Deleted {len(ids_to_delete)} chunks for doc_{doc_id}")
        except Exception as e:
            logger.error(f"Delete document failed: {e}")

    def delete_collection(self, kb_id: int):
        try:
            collection_name = f"kb_{kb_id}"
            self.client.delete_collection(name=collection_name)
            logger.info(f"Deleted collection {collection_name}")
        except Exception as e:
            logger.error(f"Delete collection failed: {e}")

    def get_collection_count(self, kb_id: int) -> int:
        try:
            collection = self.get_or_create_collection(kb_id)
            return collection.count()
        except Exception:
            return 0


vector_store = VectorStore()
