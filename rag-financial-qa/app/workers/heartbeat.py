from __future__ import annotations

import logging
import threading
from types import TracebackType
from typing import Type

from app.database import SessionLocal
from app.services import document_job_service


logger = logging.getLogger("kb_qa.job_heartbeat")


class JobHeartbeat:
    def __init__(
        self,
        *,
        job_id: int,
        worker_id: str,
        interval_seconds: float,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        self.job_id = job_id
        self.worker_id = worker_id
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lease_lost = False

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            db = SessionLocal()
            try:
                if not document_job_service.heartbeat_job(
                    db, job_id=self.job_id, worker_id=self.worker_id,
                ):
                    self.lease_lost = True
                    logger.error("Job %s lease ownership was lost", self.job_id)
                    return
            except Exception:
                logger.exception("Job %s heartbeat failed", self.job_id)
            finally:
                db.close()

    def __enter__(self) -> "JobHeartbeat":
        self._thread = threading.Thread(
            target=self._run,
            name=f"job-heartbeat-{self.job_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: Type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds * 2))
