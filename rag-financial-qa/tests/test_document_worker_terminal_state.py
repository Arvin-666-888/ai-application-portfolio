import asyncio
import hashlib
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models.models import Document, KnowledgeBase, User
from app.repositories import document_job_repository as repository
from app.services import document_ingestion_service as ingestion
from app.workers import document_worker


def _state(tmp_path: Path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'terminal.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    db = Session(engine, expire_on_commit=False)
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"pdf")
    user = User(username="worker", hashed_password="secret")
    kb = KnowledgeBase(name="kb", user=user)
    document = Document(
        filename="manual.pdf",
        file_type=".pdf",
        file_size=3,
        file_sha256=hashlib.sha256(b"pdf").hexdigest(),
        storage_path=str(source),
        ingestion_status="running",
        status="processing",
    )
    kb.documents.append(document)
    db.add(user)
    db.commit()
    return db, document, engine


def test_terminal_job_failure_updates_document_state(tmp_path, monkeypatch):
    db, document, engine = _state(tmp_path)
    job, _ = repository.enqueue(
        db,
        document_id=document.id,
        job_type=ingestion.INGEST_JOB_TYPE,
        idempotency_key="terminal-ingest",
        max_attempts=1,
    )
    job.status = "running"
    job.claimed_by = "worker-1"
    job.attempt_count = 1
    db.commit()

    def fail_ingest(*args):
        raise RuntimeError("permanent parse failure")

    monkeypatch.setattr(document_worker, "run_ingest", fail_ingest)
    asyncio.run(document_worker.run_claimed_job(db, job, "worker-1"))

    db.refresh(document)
    assert document.status == "failed"
    assert document.ingestion_status == "failed"
    assert "permanent parse failure" in document.error_message
    db.close()
    engine.dispose()
