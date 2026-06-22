import logging

from sqlalchemy.orm import Session

from app.models.models import KnowledgeBase, Document
from app.utils.vector_store import vector_store

logger = logging.getLogger("kb_qa.kb_service")


def create_knowledge_base(db: Session, name: str, description: str, user_id: int) -> KnowledgeBase:
    kb = KnowledgeBase(name=name, description=description, user_id=user_id)
    db.add(kb)
    db.commit()
    db.refresh(kb)
    vector_store.get_or_create_collection(kb.id)
    logger.info(f"Knowledge base created: {kb.id} - {name}")
    return kb


def list_knowledge_bases(db: Session, user_id: int) -> list[dict]:
    kbs = db.query(KnowledgeBase).filter(KnowledgeBase.user_id == user_id).all()
    result = []
    for kb in kbs:
        doc_count = db.query(Document).filter(Document.kb_id == kb.id).count()
        result.append({
            "id": kb.id,
            "name": kb.name,
            "description": kb.description,
            "user_id": kb.user_id,
            "document_count": doc_count,
            "created_at": kb.created_at,
        })
    return result


def delete_knowledge_base(db: Session, kb_id: int, user_id: int):
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
    if not kb:
        raise ValueError("知识库不存在")
    if kb.user_id != user_id:
        raise ValueError("无权操作此知识库")

    vector_store.delete_collection(kb_id)
    db.delete(kb)
    db.commit()
    logger.info(f"Knowledge base {kb_id} deleted")
