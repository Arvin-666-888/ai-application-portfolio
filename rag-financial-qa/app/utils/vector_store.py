import hashlib
import json
import logging
import math
from typing import Any, Optional

import chromadb
from chromadb.errors import NotFoundError

from app.config import settings
from app.utils.financial_retrieval import financial_v3_rank
from app.utils.retrieval import ecommerce_v2_rank, lexical_overlap_score, rank_contexts

logger = logging.getLogger("kb_qa.vector_store")


class VectorStore:
    def __init__(self, client: Any | None = None, collection_prefix: str = "kb"):
        self._client = client
        self.collection_prefix = collection_prefix
        self._diagnostic_indexes: dict[Any, dict[str, Any]] = {}
        self._embedding_dimensions: dict[int, int] = {}

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = chromadb.PersistentClient(path=settings.CHROMA_DIR)
        return self._client

    def _collection_name(self, kb_id: int) -> str:
        return f"{self.collection_prefix}_{kb_id}"

    def get_or_create_collection(self, kb_id: int) -> chromadb.Collection:
        return self.client.get_or_create_collection(
            name=self._collection_name(kb_id),
            metadata={"hnsw:space": "cosine"},
        )

    @staticmethod
    def _scalarize_metadata(metadata: dict[str, Any]) -> dict[str, str | int | float | bool]:
        result: dict[str, str | int | float | bool] = {}
        for key, value in metadata.items():
            if value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                result[str(key)] = value
            else:
                result[str(key)] = json.dumps(value, ensure_ascii=False, sort_keys=True)
        return result

    @staticmethod
    def _validate_embeddings(
        embeddings: list[list[float]],
        expected_count: int,
        expected_dimension: int | None = None,
    ) -> int:
        if len(embeddings) != expected_count:
            raise ValueError(f"chunks 与 embeddings 数量不一致: {expected_count} != {len(embeddings)}")
        if expected_count == 0:
            raise ValueError("chunks 不能为空")

        dimension: int | None = None
        for index, vector in enumerate(embeddings):
            if not isinstance(vector, (list, tuple)) or not vector:
                raise ValueError(f"embedding[{index}] 不能为空")
            if dimension is None:
                dimension = len(vector)
            elif len(vector) != dimension:
                raise ValueError(
                    f"embedding维度不一致: embedding[0]={dimension}, embedding[{index}]={len(vector)}"
                )
            for value in vector:
                try:
                    finite = math.isfinite(float(value))
                except (TypeError, ValueError):
                    finite = False
                if not finite:
                    raise ValueError(f"embedding[{index}] 包含非有限数值")

        assert dimension is not None
        if expected_dimension is not None and dimension != expected_dimension:
            raise ValueError(
                f"embedding维度与现有索引不一致: expected={expected_dimension}, actual={dimension}"
            )
        return dimension

    @staticmethod
    def _validate_query_embedding(query_embedding: list[float], expected_dimension: int | None) -> None:
        dimension = VectorStore._validate_embeddings([query_embedding], 1)
        if expected_dimension is not None and dimension != expected_dimension:
            raise ValueError(
                f"query embedding维度与索引不一致: expected={expected_dimension}, actual={dimension}"
            )

    def _known_dimension(self, kb_id: int, collection: Any) -> int | None:
        cached = self._embedding_dimensions.get(kb_id)
        if cached is not None:
            return cached
        if collection.count() == 0:
            return None
        if not hasattr(collection, "peek"):
            return None
        sample = collection.peek(limit=1)
        vectors = sample.get("embeddings")
        if vectors is not None and len(vectors):
            dimension = len(vectors[0])
            self._embedding_dimensions[kb_id] = dimension
            return dimension
        return None

    @staticmethod
    def _normalize_index_version(index_version: str | None) -> str:
        normalized = str(index_version or "legacy").strip()
        if not normalized:
            raise ValueError("index_version 不能为空")
        return normalized

    @staticmethod
    def _versioned_id(doc_id: int, index_version: str, chunk_index: int) -> str:
        version_digest = hashlib.sha256(index_version.encode("utf-8")).hexdigest()[:16]
        return f"doc_{doc_id}_version_{version_digest}_chunk_{chunk_index}"

    @staticmethod
    def _version_where(active_index_versions: list[str] | tuple[str, ...] | None) -> dict | None:
        if active_index_versions is None:
            return None
        versions = sorted({str(value).strip() for value in active_index_versions if str(value).strip()})
        if not versions:
            return {"index_version": "__no_published_version__"}
        if len(versions) == 1:
            return {"index_version": versions[0]}
        return {"index_version": {"$in": versions}}

    @staticmethod
    def _normalize_index_targets(
        active_index_targets: list[tuple[int, str]] | tuple[tuple[int, str], ...] | None,
    ) -> tuple[tuple[int, str], ...] | None:
        if active_index_targets is None:
            return None
        normalized: set[tuple[int, str]] = set()
        for target in active_index_targets:
            if not isinstance(target, (list, tuple)) or len(target) != 2:
                raise ValueError("active_index_targets 必须是 (doc_id, index_version) pair")
            doc_id, raw_version = target
            if isinstance(doc_id, bool):
                raise ValueError("active_index_targets doc_id 必须是正整数")
            try:
                normalized_doc_id = int(doc_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("active_index_targets doc_id 必须是正整数") from exc
            if normalized_doc_id <= 0 or str(doc_id).strip() != str(normalized_doc_id):
                raise ValueError("active_index_targets doc_id 必须是正整数")
            version = str(raw_version).strip()
            if not version:
                raise ValueError("active_index_targets index_version 不能为空")
            normalized.add((normalized_doc_id, version))
        return tuple(sorted(normalized))

    @classmethod
    def _active_index_where(
        cls,
        active_index_versions: list[str] | tuple[str, ...] | None,
        active_index_targets: list[tuple[int, str]] | tuple[tuple[int, str], ...] | None,
    ) -> tuple[dict | None, tuple[tuple[int, str], ...] | None]:
        if active_index_versions is not None and active_index_targets is not None:
            raise ValueError("active_index_versions 与 active_index_targets 禁止同时传入")
        normalized_targets = cls._normalize_index_targets(active_index_targets)
        if normalized_targets is None:
            return cls._version_where(active_index_versions), None
        if not normalized_targets:
            return {"index_version": "__no_published_version__"}, normalized_targets
        clauses = [
            {"$and": [{"doc_id": doc_id}, {"index_version": version}]}
            for doc_id, version in normalized_targets
        ]
        return (clauses[0] if len(clauses) == 1 else {"$or": clauses}), normalized_targets

    @staticmethod
    def _combine_where(*clauses: dict | None) -> dict | None:
        present = [clause for clause in clauses if clause]
        if not present:
            return None
        if len(present) == 1:
            return present[0]
        return {"$and": present}

    def _invalidate_diagnostics(self, kb_id: int) -> None:
        stale = [
            key
            for key in self._diagnostic_indexes
            if key == kb_id or (isinstance(key, tuple) and key[0] == kb_id)
        ]
        for key in stale:
            self._diagnostic_indexes.pop(key, None)

    def add_documents(
        self,
        kb_id: int,
        chunks: list[str],
        embeddings: list[list[float]],
        doc_id: int,
        filename: str,
        metadatas: list[dict] | None = None,
        index_version: str | None = None,
    ) -> None:
        if metadatas is not None and len(chunks) != len(metadatas):
            raise ValueError("chunks 与 metadatas 数量不一致")

        collection = self.get_or_create_collection(kb_id)
        version = self._normalize_index_version(index_version)
        dimension = self._validate_embeddings(
            embeddings,
            len(chunks),
            self._known_dimension(kb_id, collection),
        )
        ids = (
            [self._versioned_id(doc_id, version, index) for index in range(len(chunks))]
            if index_version is not None
            else [f"doc_{doc_id}_chunk_{index}" for index in range(len(chunks))]
        )
        supplied = metadatas or [{} for _ in chunks]
        stored_metadatas = []
        for index, metadata in enumerate(supplied):
            merged = dict(metadata)
            merged.update({
                "source": filename,
                "doc_id": doc_id,
                "chunk_index": index,
                "index_version": version,
            })
            stored_metadatas.append(self._scalarize_metadata(merged))

        version_filter = {"$and": [{"doc_id": doc_id}, {"index_version": version}]}
        existing_ids = collection.get(where=version_filter, include=[]).get("ids", [])
        stale_ids = [stored_id for stored_id in existing_ids if stored_id not in set(ids)]
        if stale_ids:
            collection.delete(ids=stale_ids)

        for start in range(0, len(ids), 100):
            collection.upsert(
                ids=ids[start:start + 100],
                documents=chunks[start:start + 100],
                embeddings=embeddings[start:start + 100],
                metadatas=stored_metadatas[start:start + 100],
            )

        self._embedding_dimensions[kb_id] = dimension
        self._invalidate_diagnostics(kb_id)
        logger.info(
            "Upserted %s chunks to collection %s at index version %s",
            len(ids),
            self._collection_name(kb_id),
            version,
        )

    @staticmethod
    def _context_from_result(
        document: str,
        metadata: dict | None,
        distance: float,
        **extra: Any,
    ) -> dict:
        metadata = metadata or {}
        context = {
            "content": document,
            "source": metadata.get("source", "未知"),
            "doc_id": metadata.get("doc_id", 0),
            "chunk_index": metadata.get("chunk_index", 0),
            "distance": distance,
        }
        for key, value in metadata.items():
            if key not in context:
                context[key] = value
        context.update(extra)
        return context

    def _dense_candidates(
        self,
        collection: Any,
        query_embedding: list[float],
        top_k: int,
        where: dict | None = None,
    ) -> list[dict]:
        if top_k <= 0 or collection.count() == 0:
            return []
        kwargs: dict[str, Any] = {
            "query_embeddings": [query_embedding],
            "n_results": min(top_k, collection.count()),
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where
        results = collection.query(**kwargs)
        contexts = []
        if results.get("documents") and results["documents"][0]:
            for document, metadata, distance in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            ):
                context = self._context_from_result(document, metadata, distance)
                context["candidate_id"] = self._candidate_identity(context)
                contexts.append(context)
        return contexts

    def query(
        self,
        kb_id: int,
        query_embedding: list[float],
        top_k: int = 3,
        query_text: str = "",
        candidate_multiplier: int = 1,
        candidate_k: int | None = None,
        numeric_weight: float | None = None,
        active_index_versions: list[str] | tuple[str, ...] | None = None,
        active_index_targets: list[tuple[int, str]] | tuple[tuple[int, str], ...] | None = None,
    ) -> list[dict]:
        where, _ = self._active_index_where(
            active_index_versions, active_index_targets,
        )
        try:
            collection = self.get_or_create_collection(kb_id)
            if collection.count() == 0:
                return []
            self._validate_query_embedding(query_embedding, self._known_dimension(kb_id, collection))
            requested = candidate_k or top_k * candidate_multiplier
            contexts = self._dense_candidates(collection, query_embedding, max(requested, top_k), where)

            if query_text:
                contexts.extend(self._lexical_candidates(collection, query_text, max(requested, top_k), where))
                return rank_contexts(
                    query=query_text,
                    contexts=contexts,
                    top_k=top_k,
                    lexical_weight=settings.LEXICAL_WEIGHT,
                    numeric_weight=getattr(settings, "NUMERIC_WEIGHT", 0.0) if numeric_weight is None else numeric_weight,
                    min_relevance_score=settings.MIN_RELEVANCE_SCORE,
                )
            return contexts[:top_k]
        except Exception as exc:
            logger.exception("Query failed for kb_%s: %s", kb_id, exc)
            raise RuntimeError(f"Query failed for kb_{kb_id}: {exc}") from exc

    @staticmethod
    def _candidate_identity(context: dict[str, Any]) -> str:
        identity = {
            "source": context.get("source"),
            "doc_id": context.get("doc_id"),
            "chunk_index": context.get("chunk_index"),
            "artifact_chunk_index": context.get("artifact_chunk_index"),
            "content_type": context.get("content_type"),
            "provenance_id": context.get("provenance_id"),
            "table_id": context.get("table_id"),
            "index_version": context.get("index_version"),
            "content_sha256": hashlib.sha256(str(context.get("content", "")).encode("utf-8")).hexdigest(),
        }
        serialized = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _ensure_diagnostic_index(
        self,
        kb_id: int,
        collection: Any,
        where: dict | None,
        *,
        active_index_versions: list[str] | tuple[str, ...] | None,
        normalized_targets: tuple[tuple[int, str], ...] | None,
    ) -> Any:
        normalized_versions = (
            tuple(sorted({str(value).strip() for value in active_index_versions}))
            if active_index_versions is not None
            else None
        )
        filter_key = (
            ("targets", normalized_targets)
            if normalized_targets is not None
            else ("versions", normalized_versions)
        )
        cache_key = (kb_id, filter_key)
        if cache_key in self._diagnostic_indexes:
            return cache_key
        kwargs: dict[str, Any] = {"include": ["documents", "metadatas", "embeddings"]}
        if where:
            kwargs["where"] = where
        data = collection.get(**kwargs)
        documents = list(data.get("documents") or [])
        metadatas = list(data.get("metadatas") or [])
        raw_embeddings = data.get("embeddings")
        embeddings = [] if raw_embeddings is None else [list(vector) for vector in raw_embeddings]
        if not (len(documents) == len(metadatas) == len(embeddings)):
            raise RuntimeError("persistent diagnostic index数据不完整")
        self._diagnostic_indexes[cache_key] = {
            "documents": documents,
            "metadatas": metadatas,
            "embeddings": embeddings,
        }
        if active_index_versions is None and normalized_targets is None:
            self._diagnostic_indexes[kb_id] = self._diagnostic_indexes[cache_key]
        return cache_key

    @staticmethod
    def _cosine_distance(left: list[float], right: list[float]) -> float:
        VectorStore._validate_embeddings([left, right], 2)
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if left_norm == 0 or right_norm == 0:
            return 1.0
        return 1.0 - dot / (left_norm * right_norm)

    def query_diagnostics(
        self,
        kb_id: int,
        query_embedding: list[float],
        query_text: str,
        dense_k: int = 100,
        lexical_k: int = 100,
        lexical_weight: float | None = None,
        numeric_weight: float | None = None,
        active_index_versions: list[str] | tuple[str, ...] | None = None,
        active_index_targets: list[tuple[int, str]] | tuple[tuple[int, str], ...] | None = None,
    ) -> dict[str, list[dict]]:
        collection = self.get_or_create_collection(kb_id)
        where, normalized_targets = self._active_index_where(
            active_index_versions, active_index_targets,
        )
        if collection.count() == 0:
            return {"dense": [], "lexical": [], "union": [], "fusion": []}
        self._validate_query_embedding(query_embedding, self._known_dimension(kb_id, collection))
        cache_key = self._ensure_diagnostic_index(
            kb_id,
            collection,
            where,
            active_index_versions=active_index_versions,
            normalized_targets=normalized_targets,
        )
        diagnostic = self._diagnostic_indexes[cache_key]

        dense = []
        for document, metadata, embedding in zip(
            diagnostic["documents"], diagnostic["metadatas"], diagnostic["embeddings"]
        ):
            context = self._context_from_result(
                document, metadata, self._cosine_distance(query_embedding, embedding)
            )
            context["candidate_id"] = self._candidate_identity(context)
            dense.append(context)
        dense.sort(key=lambda item: (item["distance"], item["candidate_id"]))
        dense = dense[:max(1, dense_k)]
        for rank, context in enumerate(dense, 1):
            context["dense_rank"] = rank

        lexical = self._lexical_candidates(collection, query_text, max(1, lexical_k), where)
        for rank, context in enumerate(lexical, 1):
            context["lexical_rank"] = rank

        merged: dict[str, dict[str, Any]] = {}
        for context in dense + lexical:
            candidate_id = str(context["candidate_id"])
            existing = merged.setdefault(candidate_id, dict(context))
            for field in ("dense_rank", "lexical_rank", "lexical_score"):
                if field in context:
                    existing[field] = context[field]
        union = list(merged.values())
        fusion = rank_contexts(
            query=query_text,
            contexts=union,
            top_k=len(union),
            lexical_weight=settings.LEXICAL_WEIGHT if lexical_weight is None else lexical_weight,
            numeric_weight=getattr(settings, "NUMERIC_WEIGHT", 0.0) if numeric_weight is None else numeric_weight,
            min_relevance_score=0.0,
        )
        for rank, context in enumerate(fusion, 1):
            context["fusion_rank"] = rank
        return {"dense": dense, "lexical": lexical, "union": union, "fusion": fusion}

    def _lexical_candidates(
        self,
        collection: Any,
        query_text: str,
        top_k: int,
        where: dict | None = None,
    ) -> list[dict]:
        try:
            kwargs: dict[str, Any] = {"include": ["documents", "metadatas"]}
            if where:
                kwargs["where"] = where
            data = collection.get(**kwargs)
        except Exception as exc:
            raise RuntimeError(f"Lexical candidate loading failed: {exc}") from exc

        candidates = []
        for document, metadata in zip(data.get("documents", []), data.get("metadatas", [])):
            lexical_score = lexical_overlap_score(query_text, document)
            if lexical_score <= 0:
                continue
            context = self._context_from_result(
                document, metadata, 1.0, lexical_score=lexical_score
            )
            context["candidate_id"] = self._candidate_identity(context)
            candidates.append(context)
        candidates.sort(key=lambda item: (-item["lexical_score"], item["candidate_id"]))
        return candidates[:top_k]

    def query_ecommerce_v2(
        self,
        kb_id: int,
        query_embedding: list[float],
        query_text: str,
        top_k: int = 5,
        table_dense_k: int = 50,
        table_lexical_k: int = 50,
        text_dense_k: int = 30,
        text_lexical_k: int = 30,
        diagnostic_k: int | None = None,
        active_index_versions: list[str] | tuple[str, ...] | None = None,
        active_index_targets: list[tuple[int, str]] | tuple[tuple[int, str], ...] | None = None,
    ) -> dict[str, Any]:
        collection = self.get_or_create_collection(kb_id)
        version_where, _ = self._active_index_where(
            active_index_versions, active_index_targets,
        )
        if collection.count() == 0:
            empty_channels = {name: [] for name in ("table_dense", "table_lexical", "text_dense", "text_lexical")}
            return {"channels": empty_channels, "ranking": [], "top_k": []}
        self._validate_query_embedding(query_embedding, self._known_dimension(kb_id, collection))
        table_where = self._combine_where(version_where, {"content_type": "table"})
        text_where = self._combine_where(version_where, {"content_type": {"$ne": "table"}})

        channels = {
            "table_dense": self._dense_candidates(collection, query_embedding, table_dense_k, table_where),
            "table_lexical": self._lexical_candidates(collection, query_text, table_lexical_k, table_where),
            "text_dense": self._dense_candidates(collection, query_embedding, text_dense_k, text_where),
            "text_lexical": self._lexical_candidates(collection, query_text, text_lexical_k, text_where),
        }
        candidate_limit = diagnostic_k or top_k
        ranking = ecommerce_v2_rank(query_text, channels, top_k=max(top_k, candidate_limit))
        return {"channels": channels, "ranking": ranking, "top_k": ranking[:top_k]}

    def query_financial_v2(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Compatibility entry point for historical ecommerce evaluation tooling."""
        result = self.query_ecommerce_v2(*args, **kwargs)
        for item in result["ranking"]:
            item["financial_v2_score"] = item.get("ecommerce_v2_score")
        return result

    def query_financial_v3(
        self,
        kb_id: int,
        query_embedding: list[float],
        query_text: str,
        top_k: int = 5,
        candidate_k: int = 100,
        table_dense_k: int = 50,
        table_lexical_k: int = 50,
        text_dense_k: int = 30,
        text_lexical_k: int = 30,
        active_index_versions: list[str] | tuple[str, ...] | None = None,
        active_index_targets: list[tuple[int, str]] | tuple[tuple[int, str], ...] | None = None,
    ) -> dict[str, Any]:
        collection = self.get_or_create_collection(kb_id)
        version_where, _ = self._active_index_where(
            active_index_versions, active_index_targets,
        )
        if collection.count() == 0:
            empty_channels = {
                name: []
                for name in ("table_dense", "table_lexical", "text_dense", "text_lexical")
            }
            return {"channels": empty_channels, "ranking": [], "top_k": []}
        self._validate_query_embedding(
            query_embedding, self._known_dimension(kb_id, collection)
        )
        table_where = self._combine_where(version_where, {"content_type": "table"})
        text_where = self._combine_where(
            version_where, {"content_type": {"$ne": "table"}}
        )
        channels = {
            "table_dense": self._dense_candidates(
                collection, query_embedding, table_dense_k, table_where
            ),
            "table_lexical": self._lexical_candidates(
                collection, query_text, table_lexical_k, table_where
            ),
            "text_dense": self._dense_candidates(
                collection, query_embedding, text_dense_k, text_where
            ),
            "text_lexical": self._lexical_candidates(
                collection, query_text, text_lexical_k, text_where
            ),
        }
        ranking = financial_v3_rank(query_text, channels, top_k=candidate_k)
        return {"channels": channels, "ranking": ranking, "top_k": ranking[:top_k]}

    def delete_document_version(
        self, kb_id: int, doc_id: int, index_version: str,
    ) -> None:
        collection = self.get_or_create_collection(kb_id)
        where = {
            "$and": [
                {"doc_id": doc_id},
                {"index_version": index_version},
            ]
        }
        ids = collection.get(where=where, include=[]).get("ids", [])
        if ids:
            collection.delete(ids=ids)
            self._invalidate_diagnostics(kb_id)

    def migrate_legacy_document(
        self, kb_id: int, doc_id: int, *, legacy_version: str = "legacy",
    ) -> int:
        collection = self.get_or_create_collection(kb_id)
        data = collection.get(where={"doc_id": doc_id}, include=[
            "documents", "metadatas", "embeddings",
        ])
        ids = list(data.get("ids") or [])
        documents = list(data.get("documents") or [])
        metadatas = list(data.get("metadatas") or [])
        embeddings_raw = data.get("embeddings")
        embeddings = [] if embeddings_raw is None else [list(item) for item in embeddings_raw]
        if not ids:
            return 0
        if not (len(ids) == len(documents) == len(metadatas) == len(embeddings)):
            raise RuntimeError("legacy Chroma data is incomplete")
        updated = []
        for metadata in metadatas:
            item = dict(metadata or {})
            item.setdefault("index_version", legacy_version)
            updated.append(item)
        collection.upsert(
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=updated,
        )
        self._invalidate_diagnostics(kb_id)
        return len(ids)

    def delete_document(self, kb_id: int, doc_id: int) -> None:
        collection = self.get_or_create_collection(kb_id)
        ids = collection.get(where={"doc_id": doc_id}, include=[]).get("ids", [])
        if ids:
            collection.delete(ids=ids)
            self._invalidate_diagnostics(kb_id)
            logger.info("Deleted %s chunks for doc_%s", len(ids), doc_id)

    def delete_collection(self, kb_id: int) -> None:
        try:
            self.client.delete_collection(name=self._collection_name(kb_id))
        except NotFoundError:
            logger.info("Collection %s was already deleted", self._collection_name(kb_id))
        self._invalidate_diagnostics(kb_id)
        self._embedding_dimensions.pop(kb_id, None)
        logger.info("Deleted collection %s", self._collection_name(kb_id))

    def get_collection_count(self, kb_id: int) -> int:
        try:
            return self.get_or_create_collection(kb_id).count()
        except Exception:
            return 0


vector_store = VectorStore()
