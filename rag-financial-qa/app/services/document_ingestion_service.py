from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.models.models import Document, DocumentJob
from app.repositories import document_job_repository as job_repository
from app.services import document_job_service
from app.services.document_service import (
    ParseRuntimeOptions,
    _parse_file_with_result,
    process_document,
)
from app.utils.paddle_artifact_adapter import ARTIFACT_SCHEMA


INGEST_JOB_TYPE = "document_ingest_v2"
OCR_JOB_TYPE = "ocr_page_v2"
FINALIZE_JOB_TYPE = "document_finalize_v2"
SNAPSHOT_SCHEMA = "document-parse-snapshot-v2"
INDEX_PROFILE_VERSION = "router-v2-index-v1"

logger = logging.getLogger("kb_qa.document_ingestion")


def document_storage_path(document: Document) -> Path:
    if document.storage_path:
        return Path(document.storage_path)
    return Path(settings.UPLOAD_DIR) / f"{document.id}_{document.filename}"


def parse_snapshot_path(document_id: int, pdf_sha256: str) -> Path:
    return (
        Path(settings.DOCUMENT_PARSE_SNAPSHOT_DIR)
        / f"doc_{document_id}"
        / f"{pdf_sha256[:12]}.json"
    )


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary.write_bytes(raw)
    temporary.replace(path)
    return hashlib.sha256(raw).hexdigest()


def _serialize_blocks(blocks) -> list[dict[str, Any]]:
    return [{"content": block.content, "metadata": dict(block.metadata)} for block in blocks]


def _serialize_parse_result(result) -> dict[str, Any]:
    if result is None:
        return {
            "schema_version": "non-router-parse-v1",
            "status": "succeeded",
            "page_routes": [],
            "warnings": [],
            "page_count": 0,
            "selected_page_count": 0,
            "dropped_page_count": 0,
            "policy_fingerprint": "",
        }
    return {
        "schema_version": result.schema_version,
        "status": result.status,
        "page_routes": [asdict(route) for route in result.page_routes],
        "warnings": list(result.warnings),
        "page_count": result.page_count,
        "selected_page_count": result.selected_page_count,
        "dropped_page_count": result.dropped_page_count,
        "policy_fingerprint": result.policy_fingerprint,
    }


def ingest_idempotency_key(document: Document) -> str:
    identity = f"{document.id}:{INGEST_JOB_TYPE}:{document.file_sha256}:{settings.PDF_PARSE_PROFILE}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def ocr_idempotency_key(
    *, document_id: int, pdf_sha256: str, page_number: int,
    engine_fingerprint: str,
) -> str:
    identity = (
        f"{document_id}:{pdf_sha256}:{page_number}:"
        f"{engine_fingerprint}:{ARTIFACT_SCHEMA}"
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def index_version(document: Document) -> str:
    identity = (
        f"{document.id}:{document.file_sha256}:{INDEX_PROFILE_VERSION}:"
        f"{document.parse_policy_fingerprint}"
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def finalize_idempotency_key(document: Document) -> str:
    identity = (
        f"{document.id}:{FINALIZE_JOB_TYPE}:{document.file_sha256}:"
        f"{index_version(document)}"
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def enqueue_ingest_job(db: Session, document: Document) -> tuple[DocumentJob, bool]:
    return document_job_service.enqueue_job(
        db,
        document_id=document.id,
        job_type=INGEST_JOB_TYPE,
        idempotency_key=ingest_idempotency_key(document),
        pdf_sha256=document.file_sha256,
        schema_version=SNAPSHOT_SCHEMA,
        profile_version=settings.PDF_PARSE_PROFILE,
        payload={"storage_path": str(document_storage_path(document))},
        priority=100,
    )


def run_ingest(db: Session, job: DocumentJob) -> dict[str, Any]:
    document = db.get(Document, job.document_id)
    if document is None:
        raise ValueError("document not found")
    path = document_storage_path(document)
    if not path.is_file():
        raise FileNotFoundError(f"document file not found: {path}")
    actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if document.file_sha256 and actual_sha != document.file_sha256:
        raise ValueError("document file SHA changed after upload")
    document.file_sha256 = actual_sha
    document.ingestion_status = "running"
    document.enrichment_status = "pending"
    document.status = "processing"
    document.error_message = ""
    db.commit()

    # L3 is intentionally disabled during ingest. Missing L3 pages become persistent OCR jobs.
    parse_options = ParseRuntimeOptions.from_settings().with_artifacts(enabled=False)
    blocks, parse_result = _parse_file_with_result(
        str(path),
        doc_id=document.id,
        source=document.filename,
        runtime_options=parse_options,
    )

    parse_audit = _serialize_parse_result(parse_result)
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA,
        "document_id": document.id,
        "source": document.filename,
        "pdf_sha256": actual_sha,
        "parse_profile": settings.PDF_PARSE_PROFILE,
        "parse_result_schema": (
            parse_result.schema_version if parse_result is not None else "non-router-parse-v1"
        ),
        "blocks": _serialize_blocks(blocks),
        "parse_audit": parse_audit,
    }
    snapshot_path = parse_snapshot_path(document.id, actual_sha)
    snapshot_sha = _write_json_atomic(snapshot_path, snapshot)

    document.page_count = int(parse_audit.get("page_count", 0))
    document.parse_profile = settings.PDF_PARSE_PROFILE
    document.parse_policy_fingerprint = str(parse_audit.get("policy_fingerprint", ""))
    document.parse_audit = parse_audit
    document.ingestion_status = "l1_l2_ready"

    required_ocr_jobs: list[DocumentJob] = []
    ocr_jobs_created = 0
    if document.file_type == ".pdf" and parse_result is not None:
        fingerprint = settings.PDF_PADDLE_EXPECTED_ENGINE_FINGERPRINT
        if not fingerprint:
            document.enrichment_status = "disabled"
        else:
            for route in parse_result.page_routes:
                if not route.selected or route.table_count > 0:
                    continue
                ocr_job, created = job_repository.enqueue(
                    db,
                    document_id=document.id,
                    job_type=OCR_JOB_TYPE,
                    idempotency_key=ocr_idempotency_key(
                        document_id=document.id,
                        pdf_sha256=actual_sha,
                        page_number=route.page_number,
                        engine_fingerprint=fingerprint,
                    ),
                    physical_page_number=route.page_number,
                    pdf_sha256=actual_sha,
                    engine_fingerprint=fingerprint,
                    schema_version=ARTIFACT_SCHEMA,
                    profile_version=settings.PDF_PARSE_PROFILE,
                    payload={
                        "storage_path": str(path),
                        "source": document.filename,
                        "reasons": list(route.reasons),
                    },
                    priority=50,
                    max_attempts=settings.DOCUMENT_JOB_MAX_ATTEMPTS,
                )
                required_ocr_jobs.append(ocr_job)
                ocr_jobs_created += int(created)
            if required_ocr_jobs:
                document.enrichment_status = (
                    "ocr_pending"
                    if any(
                        item.status in {"queued", "running", "stale"}
                        for item in required_ocr_jobs
                    )
                    else "ocr_terminal"
                )
            else:
                document.enrichment_status = "not_required"

    ocr_jobs_pending = sum(
        item.status in {"queued", "running", "stale"}
        for item in required_ocr_jobs
    )
    if not ocr_jobs_pending:
        job_repository.enqueue(
            db,
            document_id=document.id,
            job_type=FINALIZE_JOB_TYPE,
            idempotency_key=finalize_idempotency_key(document),
            pdf_sha256=actual_sha,
            schema_version=SNAPSHOT_SCHEMA,
            profile_version=INDEX_PROFILE_VERSION,
            payload={"snapshot_path": str(snapshot_path)},
            priority=10,
        )
    db.commit()
    return {
        "snapshot_path": str(snapshot_path),
        "snapshot_sha256": snapshot_sha,
        "ocr_jobs_created": ocr_jobs_created,
        "ocr_jobs_required": len(required_ocr_jobs),
        "ocr_jobs_pending": ocr_jobs_pending,
        "parse_status": parse_audit["status"],
    }


def enqueue_finalize_if_ocr_terminal(db: Session, document_id: int) -> bool:
    document = db.get(Document, document_id)
    if document is None:
        return False
    jobs = job_repository.list_document_jobs(
        db, document_id=document_id, job_type=OCR_JOB_TYPE,
    )
    if jobs and any(job.status in {"queued", "running", "stale"} for job in jobs):
        return False
    _, created = job_repository.enqueue(
        db,
        document_id=document.id,
        job_type=FINALIZE_JOB_TYPE,
        idempotency_key=finalize_idempotency_key(document),
        pdf_sha256=document.file_sha256,
        schema_version=SNAPSHOT_SCHEMA,
        profile_version=INDEX_PROFILE_VERSION,
        payload={"snapshot_path": str(parse_snapshot_path(document.id, document.file_sha256))},
        priority=10,
    )
    if document.enrichment_status not in {
        "finalizing", "enriched_ready", "degraded_ready",
    }:
        document.enrichment_status = "ocr_terminal"
    db.commit()
    return created


def publish_index_version(
    db: Session,
    *,
    document_id: int,
    job_id: int,
    worker_id: str,
    attempt_count: int,
    old_version: str,
    new_version: str,
    chunk_count: int,
) -> bool:
    active_matches = (
        Document.active_index_version == old_version
        if old_version
        else Document.active_index_version.in_(("", None))
    )
    lease_owned = exists().where(
        DocumentJob.id == job_id,
        DocumentJob.document_id == document_id,
        DocumentJob.status == "running",
        DocumentJob.claimed_by == worker_id,
        DocumentJob.attempt_count == attempt_count,
    )
    changed = db.execute(
        update(Document)
        .where(
            Document.id == document_id,
            Document.status != "deleting",
            active_matches,
            lease_owned,
        )
        .values(
            active_index_version=new_version,
            status="ready",
            ingestion_status="completed",
            chunk_count=chunk_count,
        )
    )
    db.commit()
    return changed.rowcount == 1


def verified_artifact_snapshot_root(
    document_id: int,
    finalize_job_id: int,
) -> Path:
    return (
        Path(settings.DOCUMENT_PARSE_SNAPSHOT_DIR)
        / f"doc_{document_id}"
        / f"finalize_{finalize_job_id}_artifacts"
    )


def freeze_completed_ocr_artifacts(
    jobs: list[DocumentJob],
    *,
    document_id: int,
    finalize_job_id: int,
) -> Path:
    root = verified_artifact_snapshot_root(document_id, finalize_job_id)
    for job in jobs:
        if job.status != "completed":
            continue
        source = Path(job.artifact_locator)
        if not source.is_file():
            raise RuntimeError(f"completed OCR artifact is missing: job={job.id}")
        raw = source.read_bytes()
        actual_sha = hashlib.sha256(raw).hexdigest()
        if not job.artifact_sha256 or actual_sha != job.artifact_sha256:
            raise RuntimeError(f"completed OCR artifact SHA mismatch: job={job.id}")
        target = (
            root
            / job.pdf_sha256[:12]
            / f"p{int(job.physical_page_number):04d}.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_bytes(raw)
        temporary.replace(target)
    return root


def verify_completed_ocr_artifacts(jobs: list[DocumentJob]) -> None:
    for job in jobs:
        if job.status != "completed":
            continue
        path = Path(job.artifact_locator)
        if not path.is_file():
            raise RuntimeError(f"completed OCR artifact is missing: job={job.id}")
        actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if not job.artifact_sha256 or actual_sha != job.artifact_sha256:
            raise RuntimeError(f"completed OCR artifact SHA mismatch: job={job.id}")


def reconcile_terminal_ocr_jobs(db: Session) -> int:
    document_ids = set(
        db.scalars(
            select(DocumentJob.document_id).where(
                DocumentJob.job_type == OCR_JOB_TYPE
            )
        ).all()
    )
    return sum(
        enqueue_finalize_if_ocr_terminal(db, document_id)
        for document_id in document_ids
    )


async def run_finalize(db: Session, job: DocumentJob) -> dict[str, Any]:
    original_worker_id = str(job.claimed_by)
    original_attempt_count = int(job.attempt_count)
    document = db.get(Document, job.document_id)
    if document is None:
        raise ValueError("document not found")
    ocr_jobs = job_repository.list_document_jobs(
        db, document_id=document.id, job_type=OCR_JOB_TYPE,
    )
    pending = [item.id for item in ocr_jobs if item.status in {"queued", "running", "stale"}]
    if pending:
        raise RuntimeError(f"OCR jobs are not terminal: {pending[:10]}")
    failed_pages = [
        item.physical_page_number for item in ocr_jobs if item.status in {"failed", "cancelled"}
    ]
    frozen_artifact_root = freeze_completed_ocr_artifacts(
        ocr_jobs,
        document_id=document.id,
        finalize_job_id=job.id,
    )
    verify_completed_ocr_artifacts(ocr_jobs)
    if document.status == "deleting":
        raise RuntimeError("document is being deleted")
    document.enrichment_status = "finalizing"
    pending_index_version = hashlib.sha256(
        f"{index_version(document)}:{job.id}:{job.attempt_count}".encode("utf-8")
    ).hexdigest()[:32]
    old_active_version = document.active_index_version
    job.result = {
        "build_status": "building",
        "pending_index_version": pending_index_version,
        "old_active_version": old_active_version or "",
    }
    db.commit()
    try:
        # Reuse the normal three-layer parser with immutable per-run artifact options;
        # failed pages degrade to their preserved L1 text.
        parse_options = ParseRuntimeOptions.from_settings()
        if ocr_jobs:
            parse_options = parse_options.with_artifacts(
                enabled=True,
                artifact_dir=frozen_artifact_root,
            )
        staged_chunk_count = await process_document(
            db,
            document.id,
            index_version=pending_index_version,
            publish_document_state=False,
            parse_runtime_options=parse_options,
        )
        current_job = db.get(DocumentJob, job.id)
        if current_job is not None:
            db.refresh(current_job)
        db.refresh(document)
        if (
            current_job is None
            or current_job.status != "running"
            or current_job.claimed_by != original_worker_id
            or current_job.attempt_count != original_attempt_count
            or document.status == "deleting"
        ):
            from app.utils.vector_store import vector_store
            vector_store.delete_document_version(
                document.kb_id, document.id, pending_index_version,
            )
            raise RuntimeError("finalize publication fence was lost")

    except Exception:
        from app.utils.vector_store import vector_store
        vector_store.delete_document_version(
            document.kb_id, document.id, pending_index_version,
        )
        raise
    db.refresh(document)
    if document.status == "deleting":
        from app.utils.vector_store import vector_store
        vector_store.delete_document_version(
            document.kb_id, document.id, pending_index_version,
        )
        raise RuntimeError("document was deleted during finalization")
    if document.error_message:
        from app.utils.vector_store import vector_store
        vector_store.delete_document_version(
            document.kb_id, document.id, pending_index_version,
        )
        raise RuntimeError(document.error_message)
    current_job = db.get(DocumentJob, job.id)
    if current_job is not None:
        db.refresh(current_job)
    if (
        current_job is None
        or current_job.status != "running"
        or current_job.claimed_by != original_worker_id
        or current_job.attempt_count != original_attempt_count
    ):
        from app.utils.vector_store import vector_store
        vector_store.delete_document_version(
            document.kb_id, document.id, pending_index_version,
        )
        raise RuntimeError("finalize publication fence was lost before commit")
    document.enrichment_status = "degraded_ready" if failed_pages else "enriched_ready"
    db.commit()
    if not publish_index_version(
        db,
        document_id=document.id,
        job_id=job.id,
        worker_id=original_worker_id,
        attempt_count=original_attempt_count,
        old_version=old_active_version,
        new_version=pending_index_version,
        chunk_count=int(staged_chunk_count or 0),
    ):
        from app.utils.vector_store import vector_store
        vector_store.delete_document_version(
            document.kb_id, document.id, pending_index_version,
        )
        raise RuntimeError("finalize publication compare-and-swap failed")
    db.refresh(document)
    cleanup = {
        "old_active_version": old_active_version or "",
        "policy": "retained_for_delayed_gc",
        "attempted": False,
        "deleted": False,
        "warning": "",
    }
    job.result = {
        "build_status": "published",
        "pending_index_version": pending_index_version,
        "published_index_version": pending_index_version,
        "old_active_version": old_active_version or "",
        "old_index_cleanup": cleanup,
    }
    db.commit()
    return {
        "chunk_count": document.chunk_count,
        "active_index_version": document.active_index_version,
        "pending_index_version": pending_index_version,
        "build_status": "published",
        "failed_ocr_pages": failed_pages,
        "status": document.enrichment_status,
        "old_index_cleanup": cleanup,
    }


def get_job(db: Session, job_id: int) -> DocumentJob | None:
    return db.scalar(select(DocumentJob).where(DocumentJob.id == job_id))
