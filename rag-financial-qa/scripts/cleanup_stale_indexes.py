#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import SessionLocal  # noqa: E402
from app.models.models import Document, DocumentJob  # noqa: E402
from app.services.document_ingestion_service import (  # noqa: E402
    FINALIZE_JOB_TYPE,
    index_version,
)
from app.utils.vector_store import vector_store  # noqa: E402

PENDING_FINALIZE_STATUSES = frozenset({"queued", "running", "stale"})


@dataclass(frozen=True)
class StaleIndex:
    kb_id: int
    doc_id: int
    index_version: str
    chunk_count: int
    active_index_version: str


def staging_index_version(document: Document, job: DocumentJob) -> str:
    attempt_count = int(job.attempt_count)
    if job.status in {"queued", "stale"}:
        attempt_count += 1
    return hashlib.sha256(
        f"{index_version(document)}:{job.id}:{attempt_count}".encode("utf-8")
    ).hexdigest()[:32]


def _payload_versions(job: DocumentJob) -> set[str]:
    versions: set[str] = set()
    for mapping in (job.payload, job.result):
        if not isinstance(mapping, dict):
            continue
        for key in ("pending_index_version", "staging_index_version", "index_version"):
            value = str(mapping.get(key) or "")
            if value:
                versions.add(value)
    return versions


def protected_targets(db) -> dict[tuple[int, int], set[str]]:
    documents = db.scalars(select(Document)).all()
    result = {
        (int(document.kb_id), int(document.id)): {
            str(document.active_index_version or "")
        }
        for document in documents
    }
    by_id = {int(document.id): document for document in documents}
    jobs = db.scalars(
        select(DocumentJob).where(
            DocumentJob.job_type == FINALIZE_JOB_TYPE,
            DocumentJob.status.in_(PENDING_FINALIZE_STATUSES),
        )
    ).all()
    for job in jobs:
        document = by_id.get(int(job.document_id))
        if document is None:
            continue
        versions = result[(int(document.kb_id), int(document.id))]
        versions.add(staging_index_version(document, job))
        versions.update(_payload_versions(job))
    return result


def list_stale_indexes(db, store=vector_store) -> list[StaleIndex]:
    protected = protected_targets(db)
    stale: list[StaleIndex] = []
    for collection_info in store.client.list_collections():
        name = collection_info.name
        prefix = f"{store.collection_prefix}_"
        if not name.startswith(prefix):
            continue
        try:
            kb_id = int(name[len(prefix):])
        except ValueError:
            continue
        collection = store.client.get_collection(name=name)
        data = collection.get(include=["metadatas"])
        counts: dict[tuple[int, str], int] = defaultdict(int)
        for metadata in data.get("metadatas") or []:
            metadata = metadata or {}
            try:
                doc_id = int(metadata["doc_id"])
            except (KeyError, TypeError, ValueError):
                continue
            version = str(metadata.get("index_version") or "legacy")
            counts[(doc_id, version)] += 1
        for (doc_id, version), count in counts.items():
            versions = protected.get((kb_id, doc_id))
            # Unknown/deleted document state is not sufficient proof that an index is safe.
            if versions is None or version in versions:
                continue
            active = next(iter(versions), "")
            stale.append(StaleIndex(kb_id, doc_id, version, count, active))
    return sorted(stale, key=lambda item: (item.kb_id, item.doc_id, item.index_version))


def _still_safe_to_delete(db, item: StaleIndex) -> bool:
    # Force a fresh snapshot. On SQLite, retain a write-reserving lock until the
    # external delete returns so a finalize publish cannot race this last check.
    db.rollback()
    bind = db.get_bind()
    if bind.dialect.name == "sqlite":
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    db.expire_all()
    versions = protected_targets(db).get((item.kb_id, item.doc_id))
    return versions is not None and item.index_version not in versions


def apply_cleanup(
    stale: list[StaleIndex],
    store=vector_store,
    db_factory=SessionLocal,
) -> int:
    deleted = 0
    for item in stale:
        db = db_factory()
        try:
            if not _still_safe_to_delete(db, item):
                continue
            store.delete_document_version(item.kb_id, item.doc_id, item.index_version)
            deleted += 1
        finally:
            db.close()
    return deleted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="List stale document index versions; delete only with --apply.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete versions still stale after an apply-time database recheck.",
    )
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        stale = list_stale_indexes(db)
    finally:
        db.close()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"{mode}: found {len(stale)} stale document index version(s).")
    for item in stale:
        active = item.active_index_version or "<none>"
        print(
            f"- kb={item.kb_id} doc={item.doc_id} version={item.index_version} "
            f"chunks={item.chunk_count} active={active}"
        )
    if args.apply:
        deleted = apply_cleanup(stale)
        skipped = len(stale) - deleted
        print(
            f"Deleted {deleted} stale document index version(s); "
            f"skipped {skipped} after apply-time state recheck."
        )
    elif stale:
        print("No indexes deleted. Re-run with --apply to delete the listed versions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
