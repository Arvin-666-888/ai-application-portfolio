from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import time
import uuid

from app.config import settings
from app.database import SessionLocal, init_db
from app.services import document_job_service
from app.services.document_ingestion_service import (
    FINALIZE_JOB_TYPE,
    INGEST_JOB_TYPE,
    run_finalize,
    run_ingest,
    reconcile_terminal_ocr_jobs,
)
from app.workers.heartbeat import JobHeartbeat


logger = logging.getLogger("kb_qa.document_worker")
JOB_TYPES = (INGEST_JOB_TYPE, FINALIZE_JOB_TYPE)


def default_worker_id() -> str:
    return f"document:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


async def run_claimed_job(db, job, worker_id: str) -> None:
    try:
        heartbeat_interval = max(1.0, settings.DOCUMENT_JOB_LEASE_SECONDS / 3)
        with JobHeartbeat(
            job_id=job.id,
            worker_id=worker_id,
            interval_seconds=heartbeat_interval,
        ) as heartbeat:
            if job.job_type == INGEST_JOB_TYPE:
                result = await asyncio.to_thread(run_ingest, db, job)
            elif job.job_type == FINALIZE_JOB_TYPE:
                result = await run_finalize(db, job)
            else:
                raise ValueError(f"unsupported document job type: {job.job_type}")
        if heartbeat.lease_lost:
            raise RuntimeError("job lease was lost during processing")
        if not document_job_service.complete_job(
            db, job_id=job.id, worker_id=worker_id, result=result,
        ):
            raise RuntimeError("job lease was lost before completion")
        logger.info("completed job=%s type=%s", job.id, job.job_type)
    except Exception as exc:
        status = document_job_service.fail_job(
            db, job_id=job.id, worker_id=worker_id, error=str(exc),
        )
        logger.exception("job=%s type=%s failed; next_status=%s", job.id, job.job_type, status)


async def run_once(worker_id: str) -> bool:
    db = SessionLocal()
    try:
        document_job_service.recover_stale_jobs(db)
        reconcile_terminal_ocr_jobs(db)
        for job_type in JOB_TYPES:
            job = document_job_service.claim_next_job(
                db, worker_id=worker_id, job_type=job_type,
            )
            if job is not None:
                await run_claimed_job(db, job, worker_id)
                return True
        return False
    finally:
        db.close()


async def main_loop(worker_id: str, once: bool) -> int:
    init_db()
    while True:
        processed = await run_once(worker_id)
        if once:
            return 0
        if not processed:
            await asyncio.sleep(settings.DOCUMENT_WORKER_POLL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run persistent Router V2 ingest/finalize jobs.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--worker-id", default=default_worker_id())
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        return asyncio.run(main_loop(args.worker_id, args.once))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
