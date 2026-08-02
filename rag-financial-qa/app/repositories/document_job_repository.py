from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.models import DocumentJob

JOB_STATUSES = frozenset({"queued", "running", "completed", "failed", "stale", "cancelled"})
TERMINAL_JOB_STATUSES = frozenset({"completed", "cancelled"})
ACTIVE_BUILD_JOB_TYPES = frozenset({"document_finalize_v2"})
ACTIVE_BUILD_JOB_STATUSES = frozenset({"queued", "running", "stale"})


def _index_target(job: DocumentJob) -> tuple[int, str] | None:
    result = job.result or {}
    version = str(result.get("pending_index_version") or "").strip()
    if not version:
        return None
    return int(job.document_id), version


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def get_by_idempotency_key(db: Session, idempotency_key: str) -> DocumentJob | None:
    return db.scalar(select(DocumentJob).where(DocumentJob.idempotency_key == idempotency_key))


def enqueue(
    db: Session, *, document_id: int, job_type: str, idempotency_key: str,
    physical_page_number: int | None = None, pdf_sha256: str = "",
    engine_fingerprint: str = "", schema_version: str = "v1",
    profile_version: str = "", payload: dict[str, Any] | None = None,
    priority: int = 0, max_attempts: int = 3,
    available_at: datetime | None = None,
) -> tuple[DocumentJob, bool]:
    existing = get_by_idempotency_key(db, idempotency_key)
    if existing is not None:
        return existing, False
    now = utcnow()
    job = DocumentJob(
        document_id=document_id, job_type=job_type,
        physical_page_number=physical_page_number, pdf_sha256=pdf_sha256,
        engine_fingerprint=engine_fingerprint, schema_version=schema_version,
        profile_version=profile_version, payload=payload,
        idempotency_key=idempotency_key, status="queued", priority=priority,
        max_attempts=max_attempts, available_at=available_at or now,
        created_at=now, updated_at=now,
    )
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        db.expire_all()
        existing = get_by_idempotency_key(db, idempotency_key)
        if existing is None:
            raise
        return existing, False
    return job, True


def _claim_candidate_query(now: datetime, job_type: str | None) -> Select:
    query = (
        select(DocumentJob.id)
        .where(
            DocumentJob.status.in_(("queued", "stale")),
            DocumentJob.available_at <= now,
            DocumentJob.attempt_count < DocumentJob.max_attempts,
        )
        .order_by(DocumentJob.priority.desc(), DocumentJob.available_at, DocumentJob.created_at)
        .limit(1)
    )
    return query.where(DocumentJob.job_type == job_type) if job_type else query


def claim(
    db: Session, *, worker_id: str, lease_seconds: int,
    job_type: str | None = None, now: datetime | None = None,
    max_contention_attempts: int = 20,
) -> DocumentJob | None:
    now = now or utcnow()
    lease_expires_at = now + timedelta(seconds=lease_seconds)
    for _ in range(max_contention_attempts):
        candidate_id = db.scalar(_claim_candidate_query(now, job_type))
        if candidate_id is None:
            return None
        changed = db.execute(
            update(DocumentJob)
            .where(
                DocumentJob.id == candidate_id,
                DocumentJob.status.in_(("queued", "stale")),
                DocumentJob.available_at <= now,
                DocumentJob.attempt_count < DocumentJob.max_attempts,
            )
            .values(
                status="running", attempt_count=DocumentJob.attempt_count + 1,
                claimed_by=worker_id, started_at=now, heartbeat_at=now,
                lease_expires_at=lease_expires_at, completed_at=None, updated_at=now,
            )
        )
        if changed.rowcount == 1:
            db.flush()
            job = db.get(DocumentJob, candidate_id)
            if job is not None:
                db.refresh(job)
            return job
        db.expire_all()
    return None


def heartbeat(
    db: Session, *, job_id: int, worker_id: str, lease_seconds: int,
    now: datetime | None = None,
) -> bool:
    now = now or utcnow()
    changed = db.execute(
        update(DocumentJob)
        .where(DocumentJob.id == job_id, DocumentJob.status == "running",
               DocumentJob.claimed_by == worker_id)
        .values(heartbeat_at=now, lease_expires_at=now + timedelta(seconds=lease_seconds),
                updated_at=now)
    )
    return changed.rowcount == 1


def complete(
    db: Session, *, job_id: int, worker_id: str,
    result: dict[str, Any] | None = None, artifact_locator: str = "",
    artifact_sha256: str = "", now: datetime | None = None,
) -> bool:
    now = now or utcnow()
    changed = db.execute(
        update(DocumentJob)
        .where(DocumentJob.id == job_id, DocumentJob.status == "running",
               DocumentJob.claimed_by == worker_id)
        .values(status="completed", result=result, artifact_locator=artifact_locator,
                artifact_sha256=artifact_sha256, last_error="", completed_at=now,
                lease_expires_at=None, updated_at=now)
    )
    return changed.rowcount == 1


def fail_or_retry(
    db: Session, *, job_id: int, worker_id: str, error: str,
    retry_base_seconds: int, retry_max_seconds: int,
    now: datetime | None = None,
) -> str | None:
    now = now or utcnow()
    job = db.scalar(select(DocumentJob).where(
        DocumentJob.id == job_id, DocumentJob.status == "running",
        DocumentJob.claimed_by == worker_id,
    ))
    if job is None:
        return None
    retryable = job.attempt_count < job.max_attempts
    if retryable:
        delay = min(retry_max_seconds, retry_base_seconds * (2 ** max(0, job.attempt_count - 1)))
        values = {"status": "queued", "available_at": now + timedelta(seconds=delay),
                  "completed_at": None}
    else:
        values = {"status": "failed", "completed_at": now}
    values.update(claimed_by=None, lease_expires_at=None, heartbeat_at=None,
                  last_error=error[:4000], updated_at=now)
    changed = db.execute(
        update(DocumentJob)
        .where(DocumentJob.id == job_id, DocumentJob.status == "running",
               DocumentJob.claimed_by == worker_id)
        .values(**values)
    )
    return values["status"] if changed.rowcount == 1 else None


def recover_stale(
    db: Session, *, stale_before: datetime | None = None,
    now: datetime | None = None,
) -> int:
    now = now or utcnow()
    stale_filter = DocumentJob.lease_expires_at < now
    if stale_before is not None:
        stale_filter = or_(
            DocumentJob.lease_expires_at < now,
            and_(DocumentJob.lease_expires_at.is_(None),
                 DocumentJob.heartbeat_at < stale_before),
        )
    recoverable = db.execute(
        update(DocumentJob)
        .where(
            DocumentJob.status == "running",
            stale_filter,
            DocumentJob.attempt_count < DocumentJob.max_attempts,
        )
        .values(
            status="stale",
            claimed_by=None,
            lease_expires_at=None,
            heartbeat_at=None,
            last_error="worker lease expired",
            available_at=now,
            updated_at=now,
        )
    ).rowcount
    exhausted = db.execute(
        update(DocumentJob)
        .where(
            DocumentJob.status == "running",
            stale_filter,
            DocumentJob.attempt_count >= DocumentJob.max_attempts,
        )
        .values(
            status="failed",
            claimed_by=None,
            lease_expires_at=None,
            heartbeat_at=None,
            last_error="worker lease expired after retry budget exhausted",
            completed_at=now,
            updated_at=now,
        )
    ).rowcount
    exhausted_historical = db.execute(
        update(DocumentJob)
        .where(
            DocumentJob.status == "stale",
            DocumentJob.attempt_count >= DocumentJob.max_attempts,
        )
        .values(
            status="failed",
            claimed_by=None,
            lease_expires_at=None,
            heartbeat_at=None,
            last_error="stale job retry budget exhausted",
            completed_at=now,
            updated_at=now,
        )
    ).rowcount
    return recoverable + exhausted + exhausted_historical


def manual_requeue(
    db: Session, *, job_id: int, reset_attempts: bool = False,
    now: datetime | None = None,
) -> bool:
    now = now or utcnow()
    values: dict[str, Any] = {
        "status": "queued", "available_at": now, "claimed_by": None,
        "lease_expires_at": None, "heartbeat_at": None, "completed_at": None,
        "updated_at": now,
    }
    if reset_attempts:
        values["attempt_count"] = 0
    changed = db.execute(
        update(DocumentJob)
        .where(DocumentJob.id == job_id,
               DocumentJob.status.in_(("failed", "stale", "cancelled")))
        .values(**values)
    )
    return changed.rowcount == 1


def cancel(db: Session, *, job_id: int, now: datetime | None = None) -> bool:
    now = now or utcnow()
    changed = db.execute(
        update(DocumentJob)
        .where(
            DocumentJob.id == job_id,
            DocumentJob.status.in_(("queued", "running", "stale")),
        )
        .values(
            status="cancelled",
            claimed_by=None,
            lease_expires_at=None,
            heartbeat_at=None,
            completed_at=now,
            updated_at=now,
        )
    )
    return changed.rowcount == 1


def list_document_jobs(
    db: Session,
    *,
    document_id: int,
    job_type: str | None = None,
) -> list[DocumentJob]:
    query = select(DocumentJob).where(DocumentJob.document_id == document_id)
    if job_type is not None:
        query = query.where(DocumentJob.job_type == job_type)
    return list(db.scalars(query.order_by(DocumentJob.created_at, DocumentJob.id)).all())


def cancel_document_jobs(
    db: Session, *, document_id: int, now: datetime | None = None,
) -> int:
    now = now or utcnow()
    changed = db.execute(
        update(DocumentJob)
        .where(
            DocumentJob.document_id == document_id,
            DocumentJob.status.in_(("queued", "running", "stale")),
        )
        .values(
            status="cancelled",
            claimed_by=None,
            lease_expires_at=None,
            heartbeat_at=None,
            completed_at=now,
            updated_at=now,
        )
    )
    return changed.rowcount


def list_active_index_builds(db: Session) -> list[dict[str, Any]]:
    """Return persisted index builds that cleanup must treat as protected."""
    jobs = db.scalars(
        select(DocumentJob).where(
            DocumentJob.job_type.in_(ACTIVE_BUILD_JOB_TYPES),
            DocumentJob.status.in_(ACTIVE_BUILD_JOB_STATUSES),
        )
    ).all()
    builds = []
    for job in jobs:
        target = _index_target(job)
        if target is None:
            continue
        document_id, index_version = target
        builds.append({
            "kb_id": int(job.document.kb_id),
            "document_id": document_id,
            "index_version": index_version,
            "job_id": int(job.id),
            "status": str(job.status),
        })
    return sorted(
        builds,
        key=lambda item: (
            item["document_id"], item["index_version"], item["job_id"],
        ),
    )


def active_index_build_targets(db: Session) -> set[tuple[int, int, str]]:
    """Return ``(kb_id, document_id, version)`` targets protected by builds."""
    return {
        (
            int(item["kb_id"]),
            int(item["document_id"]),
            str(item["index_version"]),
        )
        for item in list_active_index_builds(db)
    }


def document_job_counts(db: Session, *, document_id: int) -> dict[str, int]:
    jobs = db.scalars(
        select(DocumentJob).where(DocumentJob.document_id == document_id)
    ).all()
    counts = {status: 0 for status in JOB_STATUSES}
    for job in jobs:
        counts[job.status] += 1
    counts["total"] = len(jobs)
    return counts
