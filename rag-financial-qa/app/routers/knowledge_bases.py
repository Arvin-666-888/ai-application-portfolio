import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import User
from app.routers.auth import get_current_user_dependency
from app.schemas.schemas import KnowledgeBaseCreate, KnowledgeBaseResponse
from app.services.knowledge_base_service import (
    create_knowledge_base, list_knowledge_bases, delete_knowledge_base,
)

logger = logging.getLogger("kb_qa.kb_router")

router = APIRouter(prefix="/api/knowledge-bases", tags=["知识库管理"])


@router.get("", response_model=list[KnowledgeBaseResponse])
async def get_knowledge_bases(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    result = list_knowledge_bases(db, current_user.id)
    return result


@router.post("", response_model=KnowledgeBaseResponse)
async def create_kb(
    req: KnowledgeBaseCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    try:
        kb = create_knowledge_base(db, req.name, req.description, current_user.id)
        doc_count = 0
        return KnowledgeBaseResponse(
            id=kb.id, name=kb.name, description=kb.description,
            user_id=kb.user_id, document_count=doc_count, created_at=kb.created_at,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{kb_id}")
async def delete_kb(
    kb_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_dependency),
):
    try:
        delete_knowledge_base(db, kb_id, current_user.id)
        return {"code": 0, "message": "删除成功"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
