import asyncio
import logging
import math
import os
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.models.models import Document, KnowledgeBase
from app.utils.paddle_artifact_adapter import PaddleArtifactAdapter
from app.utils.pdf_parse_router import PDFParseResult, PDFParseRouter
from app.utils.table_pdf_parser import ParsedBlock, TablePDFParser, build_index_chunks
from app.utils.text_splitter import RecursiveTextSplitter
from app.utils.vector_store import vector_store

logger = logging.getLogger("kb_qa.document")

splitter = RecursiveTextSplitter(
    chunk_size=settings.CHUNK_SIZE,
    chunk_overlap=settings.CHUNK_OVERLAP,
)


@dataclass(frozen=True)
class ParseRuntimeOptions:
    parse_profile: str
    hi_res_enabled: bool
    paddle_artifact_enabled: bool
    paddle_artifact_dir: str
    paddle_expected_engine_fingerprint: str
    hi_res_max_pages_per_document: int
    table_numeric_ratio_min: float
    table_line_count_min: int
    native_text_min_chars: int
    table_title_neighbor_before: int
    table_title_neighbor_after: int

    @classmethod
    def from_settings(cls) -> "ParseRuntimeOptions":
        return cls(
            parse_profile=settings.PDF_PARSE_PROFILE,
            hi_res_enabled=settings.PDF_HI_RES_ENABLED,
            paddle_artifact_enabled=settings.PDF_PADDLE_ARTIFACT_ENABLED,
            paddle_artifact_dir=settings.PDF_PADDLE_ARTIFACT_DIR,
            paddle_expected_engine_fingerprint=(
                settings.PDF_PADDLE_EXPECTED_ENGINE_FINGERPRINT
            ),
            hi_res_max_pages_per_document=settings.PDF_HI_RES_MAX_PAGES_PER_DOCUMENT,
            table_numeric_ratio_min=settings.PDF_TABLE_NUMERIC_RATIO_MIN,
            table_line_count_min=settings.PDF_TABLE_LINE_COUNT_MIN,
            native_text_min_chars=settings.PDF_NATIVE_TEXT_MIN_CHARS,
            table_title_neighbor_before=settings.PDF_TABLE_TITLE_NEIGHBOR_BEFORE,
            table_title_neighbor_after=settings.PDF_TABLE_TITLE_NEIGHBOR_AFTER,
        )

    def with_artifacts(
        self, *, enabled: bool, artifact_dir: str | Path | None = None,
    ) -> "ParseRuntimeOptions":
        return replace(
            self,
            paddle_artifact_enabled=enabled,
            paddle_artifact_dir=(
                str(artifact_dir) if artifact_dir is not None else self.paddle_artifact_dir
            ),
        )


def _parse_hi_res_page(
    file_path: str | Path,
    page_number: int,
    *,
    doc_id: int,
    source: str,
    pdf_sha256: str = "",
) -> list[ParsedBlock]:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(file_path))
    if page_number < 1 or page_number > len(reader.pages):
        raise ValueError(f"PDF 物理页超出范围: {page_number}")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        writer = PdfWriter()
        writer.add_page(reader.pages[page_number - 1])
        with temporary_path.open("wb") as output:
            writer.write(output)
        return TablePDFParser(use_hi_res=True).parse_page(
            temporary_path,
            doc_id=doc_id,
            source=source,
            physical_page_number=page_number,
        )
    finally:
        temporary_path.unlink(missing_ok=True)


def _build_pdf_router(
    runtime_options: ParseRuntimeOptions | None = None,
) -> PDFParseRouter:
    options = runtime_options or ParseRuntimeOptions.from_settings()
    hi_res_parser = _parse_hi_res_page if options.hi_res_enabled else None
    artifact_adapter = (
        PaddleArtifactAdapter(
            options.paddle_artifact_dir,
            expected_engine_fingerprint=(
                options.paddle_expected_engine_fingerprint
            ),
        )
        if options.paddle_artifact_enabled
        else None
    )
    return PDFParseRouter(
        hi_res_parser,
        artifact_adapter,
        max_pages=options.hi_res_max_pages_per_document,
        numeric_ratio_min=options.table_numeric_ratio_min,
        line_count_min=options.table_line_count_min,
        low_text_min_chars=options.native_text_min_chars,
        title_neighbor_before=options.table_title_neighbor_before,
        title_neighbor_after=options.table_title_neighbor_after,
    )


def _parse_file_with_result(
    file_path: str,
    *,
    doc_id: int = 0,
    source: str | None = None,
    use_hi_res: bool | None = None,
    runtime_options: ParseRuntimeOptions | None = None,
) -> tuple[list[ParsedBlock], PDFParseResult | None]:
    options = runtime_options or ParseRuntimeOptions.from_settings()
    ext = Path(file_path).suffix.lower()
    source = source or Path(file_path).name
    if ext in (".txt", ".md"):
        encodings = ["utf-8", "gbk", "gb2312", "latin-1"]
        for enc in encodings:
            try:
                with open(file_path, "r", encoding=enc) as f:
                    text = f.read()
                return [ParsedBlock(text, {
                    "source": source,
                    "doc_id": doc_id,
                    "content_type": "text",
                    "page_number": 0,
                    "element_type": "Text",
                    "provenance_id": f"doc_{doc_id}:text",
                    "parser": "plain_text",
                })], None
            except (UnicodeDecodeError, UnicodeError):
                continue
        raise ValueError(f"无法解析文件编码: {file_path}")

    if ext == ".pdf":
        if use_hi_res is not None:
            blocks = TablePDFParser(use_hi_res=use_hi_res).parse(
                file_path,
                doc_id=doc_id,
                source=source,
            )
            return blocks, None
        if options.parse_profile == "unstructured_fast":
            blocks = TablePDFParser(use_hi_res=False).parse(
                file_path, doc_id=doc_id, source=source,
            )
            return blocks, None
        if options.parse_profile == "unstructured_hi_res":
            blocks = TablePDFParser(use_hi_res=True).parse(
                file_path, doc_id=doc_id, source=source,
            )
            return blocks, None

        router = (
            _build_pdf_router(runtime_options)
            if runtime_options is not None
            else _build_pdf_router()
        )
        result = router.parse(file_path, doc_id=doc_id, source=source)
        return list(result.blocks), result

    raise ValueError(f"不支持的文件格式: {ext}")


def parse_file(
    file_path: str,
    *,
    doc_id: int = 0,
    source: str | None = None,
    use_hi_res: bool | None = None,
    runtime_options: ParseRuntimeOptions | None = None,
) -> list[ParsedBlock]:
    blocks, _ = _parse_file_with_result(
        file_path,
        doc_id=doc_id,
        source=source,
        use_hi_res=use_hi_res,
        runtime_options=runtime_options,
    )
    return blocks


async def process_document(
    db: Session,
    doc_id: int,
    *,
    index_version: str | None = None,
    publish_document_state: bool = True,
    parse_runtime_options: ParseRuntimeOptions | None = None,
):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        return

    try:
        if publish_document_state:
            doc.status = "processing"
            doc.error_message = ""
            db.commit()

        file_path = getattr(doc, "storage_path", "") or os.path.join(
            settings.UPLOAD_DIR, f"{doc.id}_{doc.filename}"
        )
        parse_kwargs = {"doc_id": doc.id, "source": doc.filename}
        if parse_runtime_options is not None:
            parse_kwargs["runtime_options"] = parse_runtime_options
        blocks, parse_result = await asyncio.to_thread(
            _parse_file_with_result,
            file_path,
            **parse_kwargs,
        )
        chunks = build_index_chunks(
            blocks,
            splitter,
            table_row_overlap=settings.PDF_TABLE_ROW_OVERLAP,
        )

        if parse_result is not None:
            logger.info(
                "Document %s PDF route status=%s pages=%s selected=%s dropped=%s "
                "warnings=%s policy=%s",
                doc_id,
                parse_result.status,
                parse_result.page_count,
                parse_result.selected_page_count,
                parse_result.dropped_page_count,
                len(parse_result.warnings),
                parse_result.policy_fingerprint,
            )
            if parse_result.status == "failed":
                raise ValueError("PDF 三层解析未生成可用内容")

        if sum(len(chunk.content.strip()) for chunk in chunks) < 10:
            if publish_document_state:
                doc.status = "failed"
                doc.error_message = "文档内容过短，无法生成有效知识片段"
                db.commit()
                logger.warning(f"Document {doc_id} content too short")
                return
            raise ValueError("文档内容过短，无法生成有效知识片段")

        if not chunks:
            if publish_document_state:
                doc.status = "failed"
                doc.error_message = "文档切分后没有可用内容"
                db.commit()
                return
            raise ValueError("文档切分后没有可用内容")

        chunk_texts = [chunk.content for chunk in chunks]
        chunk_metadatas = [chunk.metadata for chunk in chunks]
        embeddings = await _batch_embed(chunk_texts)
        if len(embeddings) != len(chunks):
            raise RuntimeError(
                f"Embedding 数量与知识片段不一致: {len(embeddings)} != {len(chunks)}"
            )

        vector_store.add_documents(
            kb_id=doc.kb_id,
            chunks=chunk_texts,
            embeddings=embeddings,
            doc_id=doc.id,
            filename=doc.filename,
            metadatas=chunk_metadatas,
            index_version=(
                index_version
                or getattr(doc, "active_index_version", "")
                or "legacy"
            ),
        )

        if publish_document_state:
            doc.chunk_count = len(chunks)
            doc.status = "ready"
            doc.error_message = ""
            db.commit()
        logger.info(f"Document {doc_id} processed: {len(chunks)} chunks")
        return len(chunks)

    except Exception as e:
        if publish_document_state:
            doc.status = "failed"
            doc.error_message = str(e)[:500]
            db.commit()
        logger.error(f"Document {doc_id} processing failed: {e}")
        if not publish_document_state:
            raise


async def _batch_embed(texts: list[str], batch_size: int = 20) -> list[list[float]]:
    import httpx

    def validate(
        vectors: list[list[float]],
        expected_count: int,
        expected_dimension: int | None,
    ) -> int:
        if len(vectors) != expected_count:
            raise ValueError(
                f"Embedding API 返回数量不一致: expected={expected_count}, actual={len(vectors)}"
            )
        dimension = expected_dimension
        for index, vector in enumerate(vectors):
            if not isinstance(vector, list) or not vector:
                raise ValueError(f"Embedding API 返回空向量: index={index}")
            if dimension is None:
                dimension = len(vector)
            if len(vector) != dimension:
                raise ValueError(
                    f"Embedding API 返回维度不一致: expected={dimension}, "
                    f"actual={len(vector)}, index={index}"
                )
            try:
                finite = all(math.isfinite(float(value)) for value in vector)
            except (TypeError, ValueError):
                finite = False
            if not finite:
                raise ValueError(f"Embedding API 返回非有限数值: index={index}")
        return dimension or 0

    if not texts:
        return []
    if batch_size <= 0:
        raise ValueError("batch_size 必须大于 0")
    if not settings.API_KEY:
        import hashlib
        import numpy as np
        result = []
        for text in texts:
            hash_val = hashlib.md5(text.encode()).digest()
            fake_embedding = np.random.RandomState(
                int.from_bytes(hash_val[:4], "big")
            ).randn(1536).tolist()
            result.append(fake_embedding)
        validate(result, len(texts), None)
        return result

    all_embeddings: list[list[float]] = []
    embedding_dimension: int | None = None
    async with httpx.AsyncClient(timeout=60.0) as client:
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            response = await client.post(
                f"{settings.BASE_URL}/embeddings",
                headers={"Authorization": f"Bearer {settings.API_KEY}"},
                json={"model": settings.EMBEDDING_MODEL, "input": batch},
            )
            response.raise_for_status()
            items = response.json().get("data")
            if not isinstance(items, list):
                raise ValueError("Embedding API 响应缺少 data 列表")
            try:
                ordered = sorted(items, key=lambda item: int(item["index"]))
                indexes = [int(item["index"]) for item in ordered]
                embeddings = [item["embedding"] for item in ordered]
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "Embedding API 响应缺少有效的 item.index/embedding"
                ) from exc
            if indexes != list(range(len(batch))):
                raise ValueError(
                    f"Embedding API 返回 index 不连续: "
                    f"expected={list(range(len(batch)))}, actual={indexes}"
                )
            embedding_dimension = validate(
                embeddings, len(batch), embedding_dimension,
            )
            all_embeddings.extend(embeddings)

    validate(all_embeddings, len(texts), embedding_dimension)
    return all_embeddings


async def get_embedding(text: str) -> list[float]:
    results = await _batch_embed([text])
    return results[0]


def delete_document(db: Session, doc_id: int, user_id: int):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise ValueError("文档不存在")

    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == doc.kb_id).first()
    if kb.user_id != user_id:
        raise ValueError("无权操作此文档")

    vector_store.delete_document(kb_id=doc.kb_id, doc_id=doc.id)

    file_path = doc.storage_path or os.path.join(
        settings.UPLOAD_DIR, f"{doc.id}_{doc.filename}"
    )
    if os.path.exists(file_path):
        os.remove(file_path)

    snapshot_root = Path(settings.DOCUMENT_PARSE_SNAPSHOT_DIR).resolve()
    snapshot_dir = (snapshot_root / f"doc_{doc.id}").resolve()
    if snapshot_dir != snapshot_root and snapshot_root in snapshot_dir.parents:
        if snapshot_dir.is_dir():
            shutil.rmtree(snapshot_dir)
    else:
        raise RuntimeError("文档 snapshot 清理路径越界")

    db.delete(doc)
    db.commit()
    logger.info(f"Document {doc_id} deleted")
