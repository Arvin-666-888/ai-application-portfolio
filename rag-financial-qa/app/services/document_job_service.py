from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.models import DocumentJob
from app.repositories import document_job_repository as repository


def enqueue_job(
    db: Session, *, document_id: int, job_type: str, idempotency_key: str,
    physical_page_number: int | None = None, pdf_sha256: str = "",
    engine_fingerprint: str = "", schema_version: str = "v1",
    profile_version: str = "", payload: dict[str, Any] | None = None,
    priority: int = 0, max_attempts: int | None = None,
    available_at: datetime | None = None,
) -> tuple[DocumentJob, bool]:
    job, created = repository.enqueue(
        db, document_id=document_id, job_type=job_type,
        idempotency_key=idempotency_key,
        physical_page_number=physical_page_number, pdf_sha256=pdf_sha256,
        engine_fingerprint=engine_fingerprint, schema_version=schema_version,
        profile_version=profile_version, payload=payload, priority=priority,
        max_attempts=max_attempts or settings.DOCUMENT_JOB_MAX_ATTEMPTS,
        available_at=available_at,
    )
    db.commit()
    if created:
        db.refresh(job)
    return job, created


def claim_next_job(
    db: Session, *, worker_id: str, job_type: str | None = None,
    now: datetime | None = None,
) -> DocumentJob | None:
    job = repository.claim(
        db, worker_id=worker_id, lease_seconds=settings.DOCUMENT_JOB_LEASE_SECONDS,
        job_type=job_type, now=now,
    )
    db.commit()
    if job is not None:
        db.refresh(job)
    return job


def heartbeat_job(
    db: Session, *, job_id: int, worker_id: str,
    now: datetime | None = None,
) -> bool:
    changed = repository.heartbeat(
        db, job_id=job_id, worker_id=worker_id,
        lease_seconds=settings.DOCUMENT_JOB_LEASE_SECONDS, now=now,
    )
    db.commit()
    return changed


def complete_job(
    db: Session, *, job_id: int, worker_id: str,
    result: dict[str, Any] | None = None, artifact_locator: str = "",
    artifact_sha256: str = "", now: datetime | None = None,
) -> bool:
    changed = repository.complete(
        db, job_id=job_id, worker_id=worker_id, result=result,
        artifact_locator=artifact_locator, artifact_sha256=artifact_sha256, now=now,
    )
    db.commit()
    return changed


def fail_job(
    db: Session, *, job_id: int, worker_id: str, error: str,
    now: datetime | None = None,
) -> str | None:
    status = repository.fail_or_retry(
        db, job_id=job_id, worker_id=worker_id, error=error,
        retry_base_seconds=settings.DOCUMENT_JOB_RETRY_BASE_SECONDS,
        retry_max_seconds=settings.DOCUMENT_JOB_RETRY_MAX_SECONDS, now=now,
    )
    db.commit()
    return status


def recover_stale_jobs(
    db: Session, *, now: datetime | None = None,
) -> int:
    now = now or repository.utcnow()
    count = repository.recover_stale(
        db, now=now,
        stale_before=now - timedelta(seconds=settings.DOCUMENT_JOB_STALE_AFTER_SECONDS),
    )
    db.commit()
    return count


def requeue_job(
    db: Session, *, job_id: int, reset_attempts: bool = False,
    now: datetime | None = None,
) -> bool:
    changed = repository.manual_requeue(
        db, job_id=job_id, reset_attempts=reset_attempts, now=now,
    )
    db.commit()
    return changed


def cancel_job(
    db: Session, *, job_id: int, now: datetime | None = None,
) -> bool:
    changed = repository.cancel(db, job_id=job_id, now=now)
    db.commit()
    return changed


def list_document_jobs(
    db: Session, *, document_id: int, job_type: str | None = None,
) -> list[DocumentJob]:
    return repository.list_document_jobs(
        db, document_id=document_id, job_type=job_type,
    )


def cancel_document_jobs(
    db: Session, *, document_id: int, now: datetime | None = None,
) -> int:
    count = repository.cancel_document_jobs(db, document_id=document_id, now=now)
    db.commit()
    return count


def get_document_job_counts(db: Session, *, document_id: int) -> dict[str, int]:
    return repository.document_job_counts(db, document_id=document_id)
