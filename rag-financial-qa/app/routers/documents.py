import hashlib
import logging
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.models import Document, DocumentJob, KnowledgeBase, User
from app.repositories import document_job_repository as job_repository
from app.routers.auth import get_current_user_dependency
from app.schemas.schemas import DocumentJobResponse, DocumentResponse
from app.services import document_job_service
from app.services.document_ingestion_service import enqueue_ingest_job
from app.services.document_service import delete_document


logger = logging.getLogger("kb_qa.doc_router")
router = APIRouter(prefix="/api/documents", tags=["文档管理"])


def _owned_document(db: Session, doc_id: int, user_id: int) -> Document:
    document = db.query(Document).filter(Document.id == doc_id).first()
    if not document or document.knowledge_base.user_id != user_id:
        raise HTTPException(status_code=404, detail="文档不存在或无权访问")
    return document


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

    filename = Path(file.filename or "upload").name
    ext = Path(filename).suffix.lower()
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
    file_sha256 = hashlib.sha256(content).hexdigest()
    document = Document(
        filename=filename,
        file_type=ext,
        file_size=len(content),
        status="queued",
        error_message="",
        kb_id=kb_id,
        file_sha256=file_sha256,
        ingestion_status="queued",
        enrichment_status="pending",
        parse_profile=settings.PDF_PARSE_PROFILE,
    )
    storage_key = uuid.uuid4().hex
    final_path: Path | None = None
    temporary_path: Path | None = None
    try:
        db.add(document)
        db.flush()
        final_path = Path(settings.UPLOAD_DIR) / f"{storage_key}_{filename}"
        final_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = final_path.with_suffix(final_path.suffix + f".{uuid.uuid4().hex}.tmp")
        temporary_path.write_bytes(content)
        temporary_path.replace(final_path)
        document.storage_path = str(final_path)
        job_repository.enqueue(
            db,
            document_id=document.id,
            job_type="document_ingest_v2",
            idempotency_key=hashlib.sha256(
                f"{document.id}:document_ingest_v2:{file_sha256}:{settings.PDF_PARSE_PROFILE}".encode()
            ).hexdigest(),
            pdf_sha256=file_sha256,
            schema_version="document-parse-snapshot-v2",
            profile_version=settings.PDF_PARSE_PROFILE,
            payload={"storage_path": str(final_path)},
            priority=100,
            max_attempts=settings.DOCUMENT_JOB_MAX_ATTEMPTS,
        )
        db.commit()
        db.refresh(document)
        return document
    except Exception:
        db.rollback()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        if final_path is not None:
            final_path.unlink(missing_ok=True)
        logger.exception("Failed to persist upload and ingest job")
        raise HTTPException(status_code=500, detail="文档上传入队失败")


@router.get("", response_model=list[DocumentResponse])
async def list_documents(
    kb_id: int = Query(..., description="知识库ID"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
    if not kb or kb.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="知识库不存在或无权访问")
    return (
        db.query(Document)
        .filter(Document.kb_id == kb_id)
        .order_by(Document.created_at.desc())
        .all()
    )


@router.get("/{doc_id}/jobs", response_model=list[DocumentJobResponse])
async def list_document_jobs(
    doc_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    _owned_document(db, doc_id, current_user.id)
    return document_job_service.list_document_jobs(db, document_id=doc_id)


@router.post("/{doc_id}/jobs/{job_id}/requeue", response_model=DocumentJobResponse)
async def requeue_document_job(
    doc_id: int,
    job_id: int,
    reset_attempts: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    _owned_document(db, doc_id, current_user.id)
    job = db.query(DocumentJob).filter(
        DocumentJob.id == job_id, DocumentJob.document_id == doc_id,
    ).first()
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not document_job_service.requeue_job(
        db, job_id=job_id, reset_attempts=reset_attempts,
    ):
        raise HTTPException(status_code=409, detail="当前任务状态不可重放")
    db.refresh(job)
    return job


@router.delete("/{doc_id}")
async def remove_document(
    doc_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    document = _owned_document(db, doc_id, current_user.id)
    document.status = "deleting"
    db.commit()
    document_job_service.cancel_document_jobs(db, document_id=doc_id)
    try:
        delete_document(db, doc_id, current_user.id)
        return {"code": 0, "message": "删除成功"}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
