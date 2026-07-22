import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tables import AuditLogTable


def record_audit(
    db: Session,
    *,
    user_id: int,
    request_id: str,
    action: str,
    success: bool,
    order_id: int | None = None,
    input_summary: dict | None = None,
    result_summary: dict | None = None,
) -> None:
    db.add(
        AuditLogTable(
            user_id=user_id,
            order_id=order_id,
            request_id=request_id,
            action=action,
            input_summary=json.dumps(input_summary or {}, ensure_ascii=False),
            result_summary=json.dumps(result_summary or {}, ensure_ascii=False),
            success=success,
        )
    )
    db.commit()


def list_user_audits(db: Session, *, user_id: int, limit: int = 20) -> list[dict]:
    rows = db.scalars(
        select(AuditLogTable)
        .where(AuditLogTable.user_id == user_id)
        .order_by(AuditLogTable.created_at.desc(), AuditLogTable.id.desc())
        .limit(max(1, min(limit, 100)))
    ).all()
    return [
        {
            "id": row.id,
            "request_id": row.request_id,
            "action": row.action,
            "success": row.success,
            "input_summary": json.loads(row.input_summary or "{}"),
            "result_summary": json.loads(row.result_summary or "{}"),
            "created_at": row.created_at,
        }
        for row in rows
    ]
