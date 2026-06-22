import logging
import os
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.models.models import Document, KnowledgeBase
from app.utils.text_splitter import RecursiveTextSplitter
from app.utils.vector_store import vector_store

logger = logging.getLogger("kb_qa.document")

splitter = RecursiveTextSplitter(
    chunk_size=settings.CHUNK_SIZE,
    chunk_overlap=settings.CHUNK_OVERLAP,
)


def parse_file(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()
    if ext in (".txt", ".md"):
        encodings = ["utf-8", "gbk", "gb2312", "latin-1"]
        for enc in encodings:
            try:
                with open(file_path, "r", encoding=enc) as f:
                    return f.read()
            except (UnicodeDecodeError, UnicodeError):
                continue
        raise ValueError(f"无法解析文件编码: {file_path}")

    elif ext == ".pdf":
        try:
            import pdfplumber
            text = ""
            with pdfplumber.open(file_path) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text + "\n"
            if not text.strip():
                raise ValueError("PDF 文件内容为空或无法提取文本")
            return text
        except ImportError:
            raise ValueError("请安装 pdfplumber: pip install pdfplumber")

    else:
        raise ValueError(f"不支持的文件格式: {ext}")


async def process_document(db: Session, doc_id: int):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        return

    try:
        doc.status = "processing"
        doc.error_message = ""
        db.commit()

        file_path = os.path.join(settings.UPLOAD_DIR, f"{doc.id}_{doc.filename}")
        text = parse_file(file_path)

        if len(text.strip()) < 10:
            doc.status = "failed"
            doc.error_message = "文档内容过短，无法生成有效知识片段"
            db.commit()
            logger.warning(f"Document {doc_id} content too short")
            return

        chunks = splitter.split_text(text)
        if not chunks:
            doc.status = "failed"
            doc.error_message = "文档切分后没有可用内容"
            db.commit()
            return

        embeddings = await _batch_embed(chunks)

        vector_store.add_documents(
            kb_id=doc.kb_id,
            chunks=chunks,
            embeddings=embeddings,
            doc_id=doc.id,
            filename=doc.filename,
        )

        doc.chunk_count = len(chunks)
        doc.status = "ready"
        doc.error_message = ""
        db.commit()
        logger.info(f"Document {doc_id} processed: {len(chunks)} chunks")

    except Exception as e:
        doc.status = "failed"
        doc.error_message = str(e)[:500]
        db.commit()
        logger.error(f"Document {doc_id} processing failed: {e}")


async def _batch_embed(texts: list[str], batch_size: int = 20) -> list[list[float]]:
    import httpx

    if not settings.API_KEY:
        import hashlib
        import numpy as np
        result = []
        for text in texts:
            hash_val = hashlib.md5(text.encode()).digest()
            fake_embedding = np.random.RandomState(int.from_bytes(hash_val[:4], 'big')).randn(1536).tolist()
            result.append(fake_embedding)
        return result

    all_embeddings = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            response = await client.post(
                f"{settings.BASE_URL}/embeddings",
                headers={"Authorization": f"Bearer {settings.API_KEY}"},
                json={"model": settings.EMBEDDING_MODEL, "input": batch},
            )
            response.raise_for_status()
            data = response.json()
            embeddings = [item["embedding"] for item in data["data"]]
            all_embeddings.extend(embeddings)

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

    file_path = os.path.join(settings.UPLOAD_DIR, f"{doc.id}_{doc.filename}")
    if os.path.exists(file_path):
        os.remove(file_path)

    db.delete(doc)
    db.commit()
    logger.info(f"Document {doc_id} deleted")
