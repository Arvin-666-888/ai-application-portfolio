from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.models import Document, DocumentJob, KnowledgeBase, User
from scripts import cleanup_stale_indexes as cleanup


class _Collection:
    def __init__(self, metadatas):
        self.metadatas = metadatas

    def get(self, *, include):
        assert include == ["metadatas"]
        return {"metadatas": self.metadatas}


class _Client:
    def __init__(self, metadatas):
        self.metadatas = metadatas

    def list_collections(self):
        return [SimpleNamespace(name="kb_7")]

    def get_collection(self, *, name):
        assert name == "kb_7"
        return _Collection(self.metadatas)


class _Store:
    collection_prefix = "kb"

    def __init__(self, metadatas):
        self.client = _Client(metadatas)
        self.deleted = []

    def delete_document_version(self, kb_id, doc_id, index_version):
        self.deleted.append((kb_id, doc_id, index_version))


def _database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cleanup.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    user = User(id=1, username="u", hashed_password="h")
    kb = KnowledgeBase(id=7, name="kb", user=user)
    document = Document(
        id=1,
        filename="a.pdf",
        file_type=".pdf",
        kb_id=7,
        status="ready",
        active_index_version="v2",
        file_sha256="file-sha",
        parse_policy_fingerprint="policy",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    kb.documents.append(document)
    db.add_all([user, kb])
    db.commit()
    return engine, factory


def _finalize_job(db, document, *, status="running", attempt_count=1, payload=None):
    job = DocumentJob(
        job_type=cleanup.FINALIZE_JOB_TYPE,
        document_id=document.id,
        idempotency_key=f"finalize-{status}-{attempt_count}",
        status=status,
        attempt_count=attempt_count,
        payload=payload,
        available_at=datetime.now(),
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def test_stale_cleanup_lists_only_nonprotected_versions_and_apply_is_explicit(tmp_path):
    engine, factory = _database(tmp_path)
    store = _Store([
        {"doc_id": 1, "index_version": "legacy"},
        {"doc_id": 1, "index_version": "v2"},
        {"doc_id": 999, "index_version": "orphan"},
    ])
    db = factory()
    try:
        stale = cleanup.list_stale_indexes(db, store)
    finally:
        db.close()

    assert [(item.doc_id, item.index_version) for item in stale] == [(1, "legacy")]
    assert store.deleted == []
    assert cleanup.apply_cleanup(stale, store, factory) == 1
    assert store.deleted == [(7, 1, "legacy")]
    engine.dispose()


def test_running_finalize_staging_and_payload_versions_are_protected(tmp_path):
    engine, factory = _database(tmp_path)
    db = factory()
    try:
        document = db.get(Document, 1)
        job = _finalize_job(
            db,
            document,
            status="running",
            attempt_count=2,
            payload={"pending_index_version": "payload-pending"},
        )
        computed = cleanup.staging_index_version(document, job)
    finally:
        db.close()
    store = _Store([
        {"doc_id": 1, "index_version": "v2"},
        {"doc_id": 1, "index_version": computed},
        {"doc_id": 1, "index_version": "payload-pending"},
        {"doc_id": 1, "index_version": "old-stale"},
    ])
    db = factory()
    try:
        stale = cleanup.list_stale_indexes(db, store)
    finally:
        db.close()
    assert [(item.doc_id, item.index_version) for item in stale] == [(1, "old-stale")]
    engine.dispose()


def test_queued_finalize_protects_next_attempt_staging_version(tmp_path):
    engine, factory = _database(tmp_path)
    db = factory()
    try:
        document = db.get(Document, 1)
        job = _finalize_job(db, document, status="queued", attempt_count=2)
        computed = cleanup.staging_index_version(document, job)
    finally:
        db.close()
    store = _Store([
        {"doc_id": 1, "index_version": computed},
        {"doc_id": 1, "index_version": "stale"},
    ])
    db = factory()
    try:
        stale = cleanup.list_stale_indexes(db, store)
    finally:
        db.close()
    assert [(item.doc_id, item.index_version) for item in stale] == [(1, "stale")]
    engine.dispose()


def test_apply_rechecks_and_skips_version_that_became_active(tmp_path):
    engine, factory = _database(tmp_path)
    store = _Store([{"doc_id": 1, "index_version": "candidate"}])
    db = factory()
    try:
        stale = cleanup.list_stale_indexes(db, store)
    finally:
        db.close()
    assert len(stale) == 1

    db = factory()
    try:
        document = db.get(Document, 1)
        document.active_index_version = "candidate"
        db.commit()
    finally:
        db.close()

    assert cleanup.apply_cleanup(stale, store, factory) == 0
    assert store.deleted == []
    engine.dispose()


def test_apply_rechecks_and_skips_new_running_finalize_staging_version(tmp_path):
    engine, factory = _database(tmp_path)
    db = factory()
    try:
        document = db.get(Document, 1)
        transient_job = DocumentJob(
            id=99,
            job_type=cleanup.FINALIZE_JOB_TYPE,
            document_id=1,
            idempotency_key="future-finalize",
            status="running",
            attempt_count=1,
            available_at=datetime.now(),
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        candidate = cleanup.staging_index_version(document, transient_job)
    finally:
        db.close()

    store = _Store([{"doc_id": 1, "index_version": candidate}])
    db = factory()
    try:
        stale = cleanup.list_stale_indexes(db, store)
    finally:
        db.close()
    assert len(stale) == 1

    db = factory()
    try:
        db.add(transient_job)
        db.commit()
    finally:
        db.close()
    assert cleanup.apply_cleanup(stale, store, factory) == 0
    assert store.deleted == []
    engine.dispose()


def test_main_defaults_to_dry_run_and_only_apply_deletes(monkeypatch, capsys):
    stale = [cleanup.StaleIndex(7, 1, "legacy", 1, "v2")]
    calls = []
    session = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(cleanup, "SessionLocal", lambda: session)
    monkeypatch.setattr(cleanup, "list_stale_indexes", lambda db: stale)
    monkeypatch.setattr(cleanup, "apply_cleanup", lambda items: calls.append(items) or len(items))

    assert cleanup.main([]) == 0
    assert calls == []
    assert "DRY-RUN" in capsys.readouterr().out

    assert cleanup.main(["--apply"]) == 0
    assert calls == [stale]
    assert "APPLY" in capsys.readouterr().out
