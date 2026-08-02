from __future__ import annotations

import time

from app.workers.heartbeat import JobHeartbeat


def test_job_heartbeat_renews_lease(monkeypatch):
    calls = []

    class DB:
        def close(self):
            calls.append("closed")

    monkeypatch.setattr("app.workers.heartbeat.SessionLocal", DB)
    monkeypatch.setattr(
        "app.workers.heartbeat.document_job_service.heartbeat_job",
        lambda db, *, job_id, worker_id: calls.append((job_id, worker_id)) or True,
    )

    with JobHeartbeat(job_id=7, worker_id="worker", interval_seconds=0.01):
        time.sleep(0.035)

    assert (7, "worker") in calls
    assert "closed" in calls


def test_job_heartbeat_records_lost_lease(monkeypatch):
    class DB:
        def close(self):
            pass

    monkeypatch.setattr("app.workers.heartbeat.SessionLocal", DB)
    monkeypatch.setattr(
        "app.workers.heartbeat.document_job_service.heartbeat_job",
        lambda *args, **kwargs: False,
    )

    with JobHeartbeat(job_id=8, worker_id="worker", interval_seconds=0.01) as heartbeat:
        time.sleep(0.025)

    assert heartbeat.lease_lost is True
