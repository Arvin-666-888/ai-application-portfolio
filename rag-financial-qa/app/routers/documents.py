import logging
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.models import Document, KnowledgeBase, User
from app.routers.auth import get_current_user_dependency
from app.schemas.schemas import DocumentResponse
from app.services.document_service import process_document, delete_document

logger = logging.getLogger("kb_qa.doc_router")

router = APIRouter(prefix="/api/documents", tags=["文档管理"])


@router.post("/upload", response_model=DocumentResponse)
async def upload_document(
    kb_id: int = Query(..., description="知识库ID"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
    if not kb or kb.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="知识库不存在或无权访问")

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in settings.ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {ext}，支持: {settings.ALLOWED_EXTENSIONS}",
        )

    content = await file.read()
    if len(content) > settings.MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"文件大小超过限制: {settings.MAX_FILE_SIZE // 1024 // 1024}MB",
        )

    doc = Document(
        filename=file.filename,
        file_type=ext,
        file_size=len(content),
        status="processing",
        error_message="",
        kb_id=kb_id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    file_path = os.path.join(settings.UPLOAD_DIR, f"{doc.id}_{doc.filename}")
    with open(file_path, "wb") as f:
        f.write(content)

    import asyncio

    asyncio.create_task(_process_doc_background(doc.id))

    return doc


async def _process_doc_background(doc_id: int):
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        await process_document(db, doc_id)
    except Exception as e:
        logger.error(f"Background processing failed for doc {doc_id}: {e}")
    finally:
        db.close()


@router.get("", response_model=list[DocumentResponse])
async def list_documents(
    kb_id: int = Query(..., description="知识库ID"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
    if not kb or kb.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="知识库不存在或无权访问")

    docs = (
        db.query(Document)
        .filter(Document.kb_id == kb_id)
        .order_by(Document.created_at.desc())
        .all()
    )
    return docs


@router.delete("/{doc_id}")
async def remove_document(
    doc_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    try:
        delete_document(db, doc_id, current_user.id)
        return {"code": 0, "message": "删除成功"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
