from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models.models import Document, DocumentJob, KnowledgeBase, User
from app.repositories import document_job_repository as repository
from app.services import document_ingestion_service as ingestion
from app.workers import document_worker
from app.utils.pdf_parse_router import PDFPageRoute, PDFParseResult
from app.utils.table_pdf_parser import ParsedBlock
from app.utils.vector_store import vector_store


def _session(tmp_path: Path) -> Session:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'worker.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def _document(db: Session, tmp_path: Path) -> Document:
    user = User(username="worker", hashed_password="secret")
    kb = KnowledgeBase(name="kb", user=user)
    content = b"pdf"
    path = tmp_path / "report.pdf"
    path.write_bytes(content)
    document = Document(
        filename="report.pdf",
        file_type=".pdf",
        file_size=len(content),
        kb_id=1,
        file_sha256=hashlib.sha256(content).hexdigest(),
        storage_path=str(path),
        ingestion_status="queued",
    )
    kb.documents.append(document)
    db.add(user)
    db.commit()
    return document


def _result(table_count: int = 0) -> PDFParseResult:
    route = PDFPageRoute(
        source="report.pdf",
        page_number=1,
        reasons=("financial_table_title",),
        selected=True,
        l2_attempted=True,
        l2_status="empty" if not table_count else "succeeded",
        table_layer="L2" if table_count else "",
        table_count=table_count,
        degraded=not bool(table_count),
    )
    return PDFParseResult(
        status="degraded" if not table_count else "succeeded",
        blocks=(ParsedBlock("足够长的正文内容", {"content_type": "text"}),),
        page_routes=(route,),
        warnings=("l2_no_valid_table:p1",) if not table_count else (),
        page_count=1,
        selected_page_count=1,
        dropped_page_count=0,
        policy_fingerprint="f" * 64,
    )


def test_ocr_idempotency_key_is_scoped_to_document():
    common = {
        "pdf_sha256": "a" * 64,
        "page_number": 7,
        "engine_fingerprint": "e" * 64,
    }

    assert ingestion.ocr_idempotency_key(document_id=1, **common) != (
        ingestion.ocr_idempotency_key(document_id=2, **common)
    )


def test_ingest_creates_persistent_ocr_job_and_snapshot(tmp_path, monkeypatch):
    db = _session(tmp_path)
    document = _document(db, tmp_path)
    monkeypatch.setattr(ingestion.settings, "DOCUMENT_PARSE_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setattr(ingestion.settings, "PDF_PADDLE_EXPECTED_ENGINE_FINGERPRINT", "e" * 64)
    monkeypatch.setattr(
        ingestion,
        "_parse_file_with_result",
        lambda *args, **kwargs: (
            [ParsedBlock("足够长的正文内容", {"content_type": "text"})],
            _result(),
        ),
    )
    job, _ = repository.enqueue(
        db,
        document_id=document.id,
        job_type=ingestion.INGEST_JOB_TYPE,
        idempotency_key="ingest",
        available_at=datetime(2026, 1, 1),
    )
    db.commit()

    result = ingestion.run_ingest(db, job)

    jobs = list(db.scalars(select(DocumentJob).where(DocumentJob.document_id == document.id)))
    assert result["ocr_jobs_created"] == 1
    assert {item.job_type for item in jobs} == {
        ingestion.INGEST_JOB_TYPE,
        ingestion.OCR_JOB_TYPE,
    }
    assert Path(result["snapshot_path"]).is_file()
    assert document.ingestion_status == "l1_l2_ready"
    assert document.enrichment_status == "ocr_pending"
    assert document.parse_audit["status"] == "degraded"
    assert "ground_truth" not in str(document.parse_audit).lower()


def test_ingest_retry_keeps_existing_required_ocr_pending(tmp_path, monkeypatch):
    db = _session(tmp_path)
    document = _document(db, tmp_path)
    monkeypatch.setattr(
        ingestion.settings, "DOCUMENT_PARSE_SNAPSHOT_DIR", str(tmp_path / "snapshots")
    )
    monkeypatch.setattr(
        ingestion.settings, "PDF_PADDLE_EXPECTED_ENGINE_FINGERPRINT", "e" * 64
    )
    monkeypatch.setattr(
        ingestion,
        "_parse_file_with_result",
        lambda *args, **kwargs: (
            [ParsedBlock("足够长的正文内容", {"content_type": "text"})],
            _result(),
        ),
    )
    job, _ = repository.enqueue(
        db,
        document_id=document.id,
        job_type=ingestion.INGEST_JOB_TYPE,
        idempotency_key="ingest-retry",
    )
    existing, _ = repository.enqueue(
        db,
        document_id=document.id,
        job_type=ingestion.OCR_JOB_TYPE,
        idempotency_key=ingestion.ocr_idempotency_key(
            document_id=document.id,
            pdf_sha256=document.file_sha256,
            page_number=1,
            engine_fingerprint="e" * 64,
        ),
        physical_page_number=1,
        pdf_sha256=document.file_sha256,
        engine_fingerprint="e" * 64,
    )
    db.commit()

    result = ingestion.run_ingest(db, job)

    assert result["ocr_jobs_created"] == 0
    assert result["ocr_jobs_required"] == 1
    assert result["ocr_jobs_pending"] == 1
    assert document.enrichment_status == "ocr_pending"
    assert repository.list_document_jobs(
        db, document_id=document.id, job_type=ingestion.OCR_JOB_TYPE,
    ) == [existing]
    assert not repository.list_document_jobs(
        db, document_id=document.id, job_type=ingestion.FINALIZE_JOB_TYPE,
    )


def test_ingest_retry_with_terminal_required_ocr_enqueues_finalize(tmp_path, monkeypatch):
    db = _session(tmp_path)
    document = _document(db, tmp_path)
    monkeypatch.setattr(
        ingestion.settings, "DOCUMENT_PARSE_SNAPSHOT_DIR", str(tmp_path / "snapshots")
    )
    monkeypatch.setattr(
        ingestion.settings, "PDF_PADDLE_EXPECTED_ENGINE_FINGERPRINT", "e" * 64
    )
    monkeypatch.setattr(
        ingestion,
        "_parse_file_with_result",
        lambda *args, **kwargs: (
            [ParsedBlock("足够长的正文内容", {"content_type": "text"})],
            _result(),
        ),
    )
    job, _ = repository.enqueue(
        db,
        document_id=document.id,
        job_type=ingestion.INGEST_JOB_TYPE,
        idempotency_key="ingest-terminal-retry",
    )
    existing, _ = repository.enqueue(
        db,
        document_id=document.id,
        job_type=ingestion.OCR_JOB_TYPE,
        idempotency_key=ingestion.ocr_idempotency_key(
            document_id=document.id,
            pdf_sha256=document.file_sha256,
            page_number=1,
            engine_fingerprint="e" * 64,
        ),
        physical_page_number=1,
        pdf_sha256=document.file_sha256,
        engine_fingerprint="e" * 64,
    )
    existing.status = "failed"
    db.commit()

    result = ingestion.run_ingest(db, job)

    assert result["ocr_jobs_created"] == 0
    assert result["ocr_jobs_required"] == 1
    assert result["ocr_jobs_pending"] == 0
    assert document.enrichment_status == "ocr_terminal"
    assert repository.list_document_jobs(
        db, document_id=document.id, job_type=ingestion.FINALIZE_JOB_TYPE,
    )


def test_ingest_without_ocr_need_enqueues_finalize(tmp_path, monkeypatch):
    db = _session(tmp_path)
    document = _document(db, tmp_path)
    monkeypatch.setattr(ingestion.settings, "DOCUMENT_PARSE_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setattr(ingestion.settings, "PDF_PADDLE_EXPECTED_ENGINE_FINGERPRINT", "e" * 64)
    monkeypatch.setattr(
        ingestion,
        "_parse_file_with_result",
        lambda *args, **kwargs: (
            [ParsedBlock("足够长的正文内容", {"content_type": "text"})],
            _result(table_count=1),
        ),
    )
    job, _ = repository.enqueue(
        db,
        document_id=document.id,
        job_type=ingestion.INGEST_JOB_TYPE,
        idempotency_key="ingest-no-ocr",
    )
    db.commit()

    ingestion.run_ingest(db, job)

    jobs = repository.list_document_jobs(db, document_id=document.id)
    assert [item.job_type for item in jobs] == [
        ingestion.INGEST_JOB_TYPE,
        ingestion.FINALIZE_JOB_TYPE,
    ]
    assert document.enrichment_status == "not_required"


def test_ingest_uses_frozen_options_without_mutating_global_settings(tmp_path, monkeypatch):
    db = _session(tmp_path)
    document = _document(db, tmp_path)
    monkeypatch.setattr(ingestion.settings, "DOCUMENT_PARSE_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setattr(ingestion.settings, "PDF_PADDLE_ARTIFACT_ENABLED", True)
    monkeypatch.setattr(ingestion.settings, "PDF_PADDLE_EXPECTED_ENGINE_FINGERPRINT", "")
    seen = {}

    def fake_parse(*args, **kwargs):
        seen["options"] = kwargs["runtime_options"]
        assert ingestion.settings.PDF_PADDLE_ARTIFACT_ENABLED is True
        return [ParsedBlock("足够长的正文内容", {"content_type": "text"})], _result(table_count=1)

    monkeypatch.setattr(ingestion, "_parse_file_with_result", fake_parse)
    job, _ = repository.enqueue(
        db,
        document_id=document.id,
        job_type=ingestion.INGEST_JOB_TYPE,
        idempotency_key="ingest-frozen-options",
    )
    db.commit()

    ingestion.run_ingest(db, job)

    assert seen["options"].paddle_artifact_enabled is False
    assert ingestion.settings.PDF_PADDLE_ARTIFACT_ENABLED is True


def test_finalize_waits_for_nonterminal_ocr(tmp_path):
    db = _session(tmp_path)
    document = _document(db, tmp_path)
    ocr, _ = repository.enqueue(
        db,
        document_id=document.id,
        job_type=ingestion.OCR_JOB_TYPE,
        idempotency_key="ocr-pending",
    )
    finalize, _ = repository.enqueue(
        db,
        document_id=document.id,
        job_type=ingestion.FINALIZE_JOB_TYPE,
        idempotency_key="finalize",
    )
    db.commit()

    try:
        asyncio.run(ingestion.run_finalize(db, finalize))
    except RuntimeError as exc:
        assert str(ocr.id) in str(exc)
    else:
        raise AssertionError("finalize must wait for OCR terminal state")


def _claimed_finalize(db: Session, document: Document, *, worker_id: str = "worker-1") -> DocumentJob:
    job, _ = repository.enqueue(
        db,
        document_id=document.id,
        job_type=ingestion.FINALIZE_JOB_TYPE,
        idempotency_key=f"finalize-{document.id}-{worker_id}",
    )
    job.status = "running"
    job.claimed_by = worker_id
    job.attempt_count = 1
    db.commit()
    return job


def test_finalize_retains_old_active_version_for_delayed_gc(tmp_path, monkeypatch):
    db = _session(tmp_path)
    document = _document(db, tmp_path)
    document.active_index_version = "legacy"
    monkeypatch.setattr(
        ingestion.settings,
        "DOCUMENT_PARSE_SNAPSHOT_DIR",
        str(tmp_path / "snapshots"),
    )
    artifact = tmp_path / "completed-artifact.json"
    artifact.write_text('{"status":"completed"}', encoding="utf-8")
    ocr, _ = repository.enqueue(
        db,
        document_id=document.id,
        job_type=ingestion.OCR_JOB_TYPE,
        idempotency_key="ocr-finalize-options",
        physical_page_number=1,
        pdf_sha256=document.file_sha256,
    )
    ocr.status = "completed"
    ocr.artifact_locator = str(artifact)
    ocr.artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    db.commit()
    job = _claimed_finalize(db, document)
    seen = {}

    async def fake_process(db_arg, doc_id, **kwargs):
        seen["options"] = kwargs["parse_runtime_options"]
        seen["protected_during_build"] = repository.active_index_build_targets(db_arg)
        assert ingestion.settings.PDF_PADDLE_ARTIFACT_ENABLED is False
        return 3

    monkeypatch.setattr(ingestion.settings, "PDF_PADDLE_ARTIFACT_ENABLED", False)
    monkeypatch.setattr(ingestion, "process_document", fake_process)
    monkeypatch.setattr(
        vector_store,
        "delete_document_version",
        lambda kb_id, doc_id, version: seen.setdefault("deleted", []).append(
            (kb_id, doc_id, version)
        ),
    )

    result = asyncio.run(ingestion.run_finalize(db, job))

    assert result["active_index_version"] != "legacy"
    assert seen["protected_during_build"] == {
        (document.kb_id, document.id, result["pending_index_version"])
    }
    assert result["old_index_cleanup"] == {
        "old_active_version": "legacy",
        "policy": "retained_for_delayed_gc",
        "attempted": False,
        "deleted": False,
        "warning": "",
    }
    assert "deleted" not in seen
    assert seen["options"].paddle_artifact_enabled is True
    expected_root = ingestion.verified_artifact_snapshot_root(document.id, job.id)
    assert Path(seen["options"].paddle_artifact_dir) == expected_root
    assert ingestion.settings.PDF_PADDLE_ARTIFACT_ENABLED is False


def test_finalize_does_not_physically_delete_old_version(tmp_path, monkeypatch):
    db = _session(tmp_path)
    document = _document(db, tmp_path)
    document.active_index_version = "legacy"
    job = _claimed_finalize(db, document, worker_id="worker-retain")

    async def fake_process(*args, **kwargs):
        return 2

    def fail_if_cleanup_is_called(*args, **kwargs):
        raise AssertionError("finalize must leave old versions to delayed GC")

    monkeypatch.setattr(ingestion, "process_document", fake_process)
    monkeypatch.setattr(
        vector_store, "delete_document_version", fail_if_cleanup_is_called,
    )

    result = asyncio.run(ingestion.run_finalize(db, job))

    db.refresh(document)
    assert document.active_index_version == result["active_index_version"]
    assert document.active_index_version != "legacy"
    assert result["old_index_cleanup"] == {
        "old_active_version": "legacy",
        "policy": "retained_for_delayed_gc",
        "attempted": False,
        "deleted": False,
        "warning": "",
    }


def test_freeze_completed_artifact_uses_verified_immutable_copy(tmp_path, monkeypatch):
    db = _session(tmp_path)
    document = _document(db, tmp_path)
    monkeypatch.setattr(
        ingestion.settings,
        "DOCUMENT_PARSE_SNAPSHOT_DIR",
        str(tmp_path / "snapshots"),
    )
    artifact = tmp_path / "artifact.json"
    artifact.write_text('{"status":"completed"}', encoding="utf-8")
    ocr, _ = repository.enqueue(
        db,
        document_id=document.id,
        job_type=ingestion.OCR_JOB_TYPE,
        idempotency_key="ocr-frozen",
        physical_page_number=1,
        pdf_sha256=document.file_sha256,
    )
    ocr.status = "completed"
    ocr.artifact_locator = str(artifact)
    ocr.artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    db.commit()

    root = ingestion.freeze_completed_ocr_artifacts(
        [ocr], document_id=document.id, finalize_job_id=99,
    )
    frozen = root / document.file_sha256[:12] / "p0001.json"
    artifact.write_text('{"status":"tampered"}', encoding="utf-8")

    assert frozen.read_text(encoding="utf-8") == '{"status":"completed"}'


def test_finalize_rejects_artifact_changed_after_job_completion(tmp_path):
    db = _session(tmp_path)
    document = _document(db, tmp_path)
    artifact = tmp_path / "artifact.json"
    artifact.write_text('{"status":"completed"}', encoding="utf-8")
    ocr, _ = repository.enqueue(
        db,
        document_id=document.id,
        job_type=ingestion.OCR_JOB_TYPE,
        idempotency_key="ocr-tampered",
    )
    ocr.status = "completed"
    ocr.artifact_locator = str(artifact)
    ocr.artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    db.commit()
    artifact.write_text('{"status":"completed","tampered":true}', encoding="utf-8")

    try:
        ingestion.verify_completed_ocr_artifacts([ocr])
    except RuntimeError as exc:
        assert "SHA mismatch" in str(exc)
    else:
        raise AssertionError("tampered artifact must be rejected")


def test_reconcile_does_not_regress_ready_or_finalizing_status(tmp_path):
    db = _session(tmp_path)
    document = _document(db, tmp_path)
    ocr, _ = repository.enqueue(
        db,
        document_id=document.id,
        job_type=ingestion.OCR_JOB_TYPE,
        idempotency_key="terminal-protected-status",
    )
    ocr.status = "failed"
    db.commit()

    for status in ("enriched_ready", "degraded_ready", "finalizing"):
        document.enrichment_status = status
        db.commit()

        ingestion.enqueue_finalize_if_ocr_terminal(db, document.id)

        db.refresh(document)
        assert document.enrichment_status == status


def test_failed_ocr_still_enqueues_degraded_finalize(tmp_path):
    db = _session(tmp_path)
    document = _document(db, tmp_path)
    ocr, _ = repository.enqueue(
        db,
        document_id=document.id,
        job_type=ingestion.OCR_JOB_TYPE,
        idempotency_key="ocr-failed",
    )
    ocr.status = "failed"
    db.commit()

    created = ingestion.enqueue_finalize_if_ocr_terminal(db, document.id)

    assert created is True
    assert document.enrichment_status == "ocr_terminal"
    assert repository.list_document_jobs(
        db, document_id=document.id, job_type=ingestion.FINALIZE_JOB_TYPE,
    )
