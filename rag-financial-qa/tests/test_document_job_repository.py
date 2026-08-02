from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models.models import Document, DocumentJob, KnowledgeBase, User
from app.repositories import document_job_repository as repository


def _session(tmp_path: Path) -> Session:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'jobs.db'}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def configure(connection, record):
        del record
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")

    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def _document(db: Session) -> Document:
    user = User(username="tester", hashed_password="secret")
    kb = KnowledgeBase(name="kb", user=user)
    document = Document(filename="report.pdf", file_type=".pdf", kb_id=1)
    kb.documents.append(document)
    db.add(user)
    db.commit()
    return document


def test_active_index_build_targets_expose_only_nonterminal_pending_versions(tmp_path):
    db = _session(tmp_path)
    document = _document(db)
    job, _ = repository.enqueue(
        db,
        document_id=document.id,
        job_type="document_finalize_v2",
        idempotency_key="active-build",
    )
    job.status = "running"
    job.result = {
        "build_status": "building",
        "pending_index_version": "pending-v2",
    }
    unrelated, _ = repository.enqueue(
        db,
        document_id=document.id,
        job_type="ocr_page_v2",
        idempotency_key="not-an-index-build",
    )
    unrelated.status = "running"
    unrelated.result = {"pending_index_version": "ignore-me"}
    db.commit()

    assert repository.active_index_build_targets(db) == {
        (document.kb_id, document.id, "pending-v2")
    }
    assert repository.list_active_index_builds(db) == [{
        "kb_id": document.kb_id,
        "document_id": document.id,
        "index_version": "pending-v2",
        "job_id": job.id,
        "status": "running",
    }]

    job.status = "completed"
    db.commit()
    assert repository.active_index_build_targets(db) == set()


def test_enqueue_is_idempotent_and_claim_is_atomic(tmp_path):
    db = _session(tmp_path)
    document = _document(db)
    now = datetime(2026, 1, 1)
    first, created = repository.enqueue(
        db, document_id=document.id, job_type="ocr_page", idempotency_key="same-key",
        physical_page_number=1, pdf_sha256="a" * 64,
        engine_fingerprint="b" * 64, available_at=now,
    )
    duplicate, duplicate_created = repository.enqueue(
        db, document_id=document.id, job_type="ocr_page", idempotency_key="same-key",
    )
    db.commit()
    assert created is True
    assert duplicate_created is False
    assert duplicate.id == first.id
    assert db.scalar(select(DocumentJob).where(DocumentJob.idempotency_key == "same-key"))

    claimed = repository.claim(db, worker_id="worker-1", lease_seconds=30, now=now)
    second_claim = repository.claim(db, worker_id="worker-2", lease_seconds=30, now=now)
    db.commit()
    assert claimed.id == first.id
    assert claimed.status == "running"
    assert claimed.attempt_count == 1
    assert second_claim is None


def test_heartbeat_complete_and_worker_ownership(tmp_path):
    db = _session(tmp_path)
    document = _document(db)
    now = datetime(2026, 1, 1)
    job, _ = repository.enqueue(
        db, document_id=document.id, job_type="parse", idempotency_key="complete", available_at=now,
    )
    repository.claim(db, worker_id="worker-1", lease_seconds=30, now=now)
    assert repository.heartbeat(
        db, job_id=job.id, worker_id="other", lease_seconds=30, now=now
    ) is False
    assert repository.heartbeat(
        db, job_id=job.id, worker_id="worker-1", lease_seconds=30,
        now=now + timedelta(seconds=5),
    ) is True
    assert repository.complete(
        db, job_id=job.id, worker_id="worker-1", result={"chunks": 3},
        artifact_locator="artifact.json", artifact_sha256="c" * 64,
        now=now + timedelta(seconds=10),
    ) is True
    db.commit()
    db.refresh(job)
    assert job.status == "completed"
    assert job.result == {"chunks": 3}
    assert job.artifact_sha256 == "c" * 64


def test_retry_backoff_then_failed_and_manual_requeue(tmp_path):
    db = _session(tmp_path)
    document = _document(db)
    now = datetime(2026, 1, 1)
    job, _ = repository.enqueue(
        db, document_id=document.id, job_type="ocr", idempotency_key="retry",
        max_attempts=2, available_at=now,
    )
    repository.claim(db, worker_id="w", lease_seconds=30, now=now)
    status = repository.fail_or_retry(
        db, job_id=job.id, worker_id="w", error="temporary",
        retry_base_seconds=5, retry_max_seconds=30, now=now,
    )
    db.commit()
    db.refresh(job)
    assert status == "queued"
    assert job.available_at == now + timedelta(seconds=5)

    repository.claim(db, worker_id="w", lease_seconds=30, now=job.available_at)
    status = repository.fail_or_retry(
        db, job_id=job.id, worker_id="w", error="permanent",
        retry_base_seconds=5, retry_max_seconds=30, now=job.available_at,
    )
    db.commit()
    db.refresh(job)
    assert status == "failed"
    assert repository.manual_requeue(db, job_id=job.id, reset_attempts=True, now=now) is True
    db.commit()
    db.refresh(job)
    assert job.status == "queued"
    assert job.attempt_count == 0


def test_exhausted_lease_recovery_becomes_failed(tmp_path):
    db = _session(tmp_path)
    document = _document(db)
    now = datetime(2026, 1, 1)
    job, _ = repository.enqueue(
        db,
        document_id=document.id,
        job_type="ocr",
        idempotency_key="exhausted-crash",
        max_attempts=1,
        available_at=now,
    )
    repository.claim(db, worker_id="dead", lease_seconds=1, now=now)

    assert repository.recover_stale(db, now=now + timedelta(seconds=2)) == 1
    db.commit()
    db.refresh(job)

    assert job.status == "failed"
    assert job.completed_at is not None
    assert repository.claim(
        db, worker_id="other", lease_seconds=30, now=now + timedelta(seconds=2)
    ) is None


def test_stale_recovery_and_cancel(tmp_path):
    db = _session(tmp_path)
    document = _document(db)
    now = datetime(2026, 1, 1)
    stale, _ = repository.enqueue(
        db, document_id=document.id, job_type="ocr", idempotency_key="stale", available_at=now,
    )
    repository.claim(db, worker_id="dead", lease_seconds=5, now=now)
    assert repository.recover_stale(db, now=now + timedelta(seconds=6)) == 1
    db.commit()
    db.refresh(stale)
    assert stale.status == "stale"

    queued, _ = repository.enqueue(
        db, document_id=document.id, job_type="parse", idempotency_key="cancel", available_at=now,
    )
    assert repository.cancel(db, job_id=queued.id, now=now) is True
    claimed = repository.claim(
        db, worker_id="w", lease_seconds=30, now=now + timedelta(seconds=6)
    )
    assert claimed is not None
    assert claimed.id == stale.id
    db.commit()
    db.refresh(queued)
    assert queued.status == "cancelled"
