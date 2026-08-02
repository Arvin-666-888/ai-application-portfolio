from __future__ import annotations

import argparse
import hashlib
import logging
import os
import socket
import sys
import time
import uuid
from pathlib import Path

from app.config import settings
from app.database import SessionLocal, init_db
from app.services import document_job_service
from app.services.document_ingestion_service import (
    OCR_JOB_TYPE,
    enqueue_finalize_if_ocr_terminal,
)
from app.workers.heartbeat import JobHeartbeat
from app.utils.paddle_ocr_artifact import (
    PaddleOCRArtifactError,
    build_engine_profile,
    create_engine,
    portable_artifact_locator,
    run_page_ocr,
    validate_runtime,
)


logger = logging.getLogger("kb_qa.paddle_worker")
SUPPORTED_DEPLOYMENT_MODE = "windows_same_root"


def _sqlite_database_path(database_url: str) -> Path | None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or database_url.startswith("sqlite:////"):
        return None
    value = database_url[len(prefix):]
    return Path(value) if value and value != ":memory:" else None


def validate_deployment_contract(*, platform: str | None = None, cwd: Path | None = None) -> Path:
    """Fail fast unless every process uses one Windows checkout as its namespace."""
    actual_platform = platform or sys.platform
    runtime_root = (cwd or Path.cwd()).resolve()
    configured_root = Path(settings.PADDLE_WORKER_SHARED_ROOT)
    if settings.PADDLE_WORKER_DEPLOYMENT_MODE != SUPPORTED_DEPLOYMENT_MODE:
        raise PaddleOCRArtifactError(
            "Paddle worker requires PADDLE_WORKER_DEPLOYMENT_MODE=windows_same_root; "
            "Docker API + host Paddle worker is unsupported with the SQLite queue"
        )
    if actual_platform != "win32":
        raise PaddleOCRArtifactError("windows_same_root Paddle worker requires Windows")
    if configured_root.is_absolute() or configured_root.resolve() != runtime_root:
        raise PaddleOCRArtifactError(
            "Run the Paddle worker from PADDLE_WORKER_SHARED_ROOT (normally the repository root)"
        )
    database_path = _sqlite_database_path(settings.DATABASE_URL)
    if database_path is None or database_path.is_absolute():
        raise PaddleOCRArtifactError(
            "windows_same_root requires a relative sqlite:/// DATABASE_URL"
        )
    for name in (
        "UPLOAD_DIR",
        "DOCUMENT_PARSE_SNAPSHOT_DIR",
        "PDF_PADDLE_ARTIFACT_DIR",
        "PADDLE_WORKER_LOCK_FILE",
    ):
        path = Path(getattr(settings, name))
        if path.is_absolute():
            raise PaddleOCRArtifactError(f"windows_same_root requires relative {name}")
        try:
            path.resolve().relative_to(runtime_root)
        except ValueError as exc:
            raise PaddleOCRArtifactError(f"{name} escapes PADDLE_WORKER_SHARED_ROOT") from exc
    return runtime_root


def smoke_check(device: str, lock_file: Path) -> dict:
    """Validate topology, imports, and installed pins without initializing Paddle models."""
    shared_root = validate_deployment_contract()
    profile = build_engine_profile(device, lock_file)
    validated = validate_runtime(profile)
    __import__("paddleocr")
    return {
        "status": "ok",
        "shared_root": str(shared_root),
        "configuration_fingerprint": profile["configuration_fingerprint"],
        "runtime_versions": validated["runtime_versions"],
    }


def default_worker_id() -> str:
    return f"paddle:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def run_once(*, worker_id: str, engine, profile: dict) -> bool:
    db = SessionLocal()
    try:
        document_job_service.recover_stale_jobs(db)
        job = document_job_service.claim_next_job(
            db, worker_id=worker_id, job_type=OCR_JOB_TYPE,
        )
        if job is None:
            return False
        payload = job.payload or {}
        try:
            heartbeat_interval = max(1.0, settings.DOCUMENT_JOB_LEASE_SECONDS / 3)
            with JobHeartbeat(
                job_id=job.id,
                worker_id=worker_id,
                interval_seconds=heartbeat_interval,
            ) as heartbeat:
                target, artifact = run_page_ocr(
                    engine=engine,
                    source_path=payload["storage_path"],
                    source=payload["source"],
                    pdf_sha256=job.pdf_sha256,
                    page_number=job.physical_page_number,
                    reasons=list(payload.get("reasons") or []),
                    profile=profile,
                    artifact_root=settings.PDF_PADDLE_ARTIFACT_DIR,
                )
            if heartbeat.lease_lost:
                raise RuntimeError("job lease was lost during OCR")
            if artifact["status"] != "completed":
                raise RuntimeError((artifact.get("error") or {}).get("message", "OCR failed"))
            artifact_sha = hashlib.sha256(target.read_bytes()).hexdigest()
            artifact_locator = portable_artifact_locator(
                target,
                artifact_root=settings.PDF_PADDLE_ARTIFACT_DIR,
                shared_root=settings.PADDLE_WORKER_SHARED_ROOT,
            )
            if not document_job_service.complete_job(
                db,
                job_id=job.id,
                worker_id=worker_id,
                result={"table_count": artifact["table_count"]},
                artifact_locator=artifact_locator,
                artifact_sha256=artifact_sha,
            ):
                raise RuntimeError("job lease was lost before completion")
            enqueue_finalize_if_ocr_terminal(db, job.document_id)
            logger.info("completed OCR job=%s page=%s", job.id, job.physical_page_number)
        except Exception as exc:
            next_status = document_job_service.fail_job(
                db, job_id=job.id, worker_id=worker_id, error=str(exc),
            )
            if next_status == "failed":
                enqueue_finalize_if_ocr_terminal(db, job.document_id)
            logger.exception("OCR job=%s failed; next_status=%s", job.id, next_status)
        return True
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run persistent Router V2 PaddleOCR page jobs.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--smoke-check", action="store_true")
    parser.add_argument("--worker-id", default=default_worker_id())
    parser.add_argument("--device", default=settings.PADDLE_WORKER_DEVICE)
    parser.add_argument("--lock-file", type=Path, default=Path(settings.PADDLE_WORKER_LOCK_FILE))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    if args.smoke_check:
        checked = smoke_check(args.device, args.lock_file)
        expected = settings.PDF_PADDLE_EXPECTED_ENGINE_FINGERPRINT
        if expected and checked["configuration_fingerprint"] != expected:
            raise SystemExit("Paddle worker fingerprint does not match PDF_PADDLE_EXPECTED_ENGINE_FINGERPRINT")
        logger.info(
            "Paddle worker smoke check passed; fingerprint=%s runtime=%s",
            checked["configuration_fingerprint"],
            checked["runtime_versions"],
        )
        return 0
    validate_deployment_contract()
    profile = build_engine_profile(args.device, args.lock_file)
    if profile["configuration_fingerprint"] != settings.PDF_PADDLE_EXPECTED_ENGINE_FINGERPRINT:
        raise SystemExit("Paddle worker fingerprint does not match PDF_PADDLE_EXPECTED_ENGINE_FINGERPRINT")
    init_db()
    engine = create_engine(profile)
    try:
        while True:
            processed = run_once(worker_id=args.worker_id, engine=engine, profile=profile)
            if args.once:
                return 0
            if not processed:
                time.sleep(settings.DOCUMENT_WORKER_POLL_SECONDS)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
