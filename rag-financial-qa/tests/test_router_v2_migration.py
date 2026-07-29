from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.models.models import DocumentJob, RagRun


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "migrate_router_v2.py"
spec = importlib.util.spec_from_file_location("migrate_router_v2", SCRIPT_PATH)
migration = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(migration)


def _legacy_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username VARCHAR(50) NOT NULL UNIQUE,
                hashed_password VARCHAR(128) NOT NULL,
                created_at DATETIME
            )
        """))
        connection.execute(text("""
            CREATE TABLE knowledge_bases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL,
                description VARCHAR(500),
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at DATETIME
            )
        """))
        connection.execute(text("""
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename VARCHAR(255) NOT NULL,
                file_type VARCHAR(20) NOT NULL,
                file_size INTEGER,
                chunk_count INTEGER,
                status VARCHAR(20),
                kb_id INTEGER NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
                created_at DATETIME
            )
        """))
        connection.execute(text("""
            CREATE TABLE conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title VARCHAR(200),
                kb_id INTEGER NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at DATETIME
            )
        """))
    return engine


def _assert_router_schema(engine):
    inspector = inspect(engine)
    job_columns = {column["name"] for column in inspector.get_columns("document_jobs")}
    assert {"idempotency_key", "lease_expires_at", "artifact_sha256", "last_error"} <= job_columns
    job_indexes = {item["name"] for item in inspector.get_indexes("document_jobs")}
    assert {"ix_document_jobs_claim", "ix_document_jobs_document_id", "ix_document_jobs_status"} <= job_indexes
    assert any(
        item.get("column_names") == ["idempotency_key"]
        for item in inspector.get_unique_constraints("document_jobs")
    )
    assert any("statusin(" in migration._normalize_sql(item.get("sqltext"))
               for item in inspector.get_check_constraints("document_jobs"))

    run_columns = {column["name"] for column in inspector.get_columns("rag_runs")}
    assert {"trace_id", "verification_status", "total_tokens", "question_sha256"} <= run_columns
    run_indexes = {item["name"] for item in inspector.get_indexes("rag_runs")}
    assert {"ix_rag_runs_user_created", "ix_rag_runs_trace_id", "ix_rag_runs_status"} <= run_indexes
    assert (
        any(
            item.get("column_names") == ["trace_id"]
            for item in inspector.get_unique_constraints("rag_runs")
        )
        or any(
            item.get("column_names") == ["trace_id"] and item.get("unique")
            for item in inspector.get_indexes("rag_runs")
        )
    )
    assert any("statusin(" in migration._normalize_sql(item.get("sqltext"))
               for item in inspector.get_check_constraints("rag_runs"))


def test_migration_check_apply_and_second_apply_are_idempotent(tmp_path):
    engine = _legacy_database(tmp_path)
    plan = migration.migration_plan(engine)
    assert any("file_sha256" in operation for operation in plan)
    assert any("document_jobs" in operation for operation in plan)
    assert any("rag_runs" in operation for operation in plan)

    applied = migration.apply_migration(engine)
    assert applied == plan
    assert migration.migration_plan(engine) == []
    assert migration.apply_migration(engine) == []

    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("documents")}
    assert {
        "file_sha256", "ingestion_status", "enrichment_status", "parse_profile",
        "parse_policy_fingerprint", "parse_audit", "active_index_version",
        "storage_path", "page_count", "updated_at",
    } <= columns
    _assert_router_schema(engine)


def test_partial_tables_are_rebuilt_with_rows_and_constraints(tmp_path):
    engine = _legacy_database(tmp_path)
    migration.apply_migration(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users (id, username, hashed_password) VALUES (1, 'u', 'h')"
        ))
        connection.execute(text(
            "INSERT INTO knowledge_bases (id, name, user_id) VALUES (1, 'kb', 1)"
        ))
        connection.execute(text(
            "INSERT INTO documents (id, filename, file_type, kb_id, created_at) "
            "VALUES (1, 'a.pdf', '.pdf', 1, CURRENT_TIMESTAMP)"
        ))
        connection.execute(text(
            "INSERT INTO conversations (id, title, kb_id, user_id) VALUES (1, 'c', 1, 1)"
        ))
    with Session(engine) as db:
        db.add(DocumentJob(
            job_type="document_finalize_v2",
            document_id=1,
            idempotency_key="job-key",
            status="queued",
        ))
        db.add(RagRun(
            trace_id="trace-1",
            user_id=1,
            kb_id=1,
            conversation_id=1,
            status="started",
            question_sha256="question",
        ))
        db.commit()
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX ix_document_jobs_claim"))
        connection.execute(text("ALTER TABLE document_jobs RENAME TO document_jobs_full"))
        connection.execute(text("""
            CREATE TABLE document_jobs AS
            SELECT id, job_type, document_id, idempotency_key, status, created_at
            FROM document_jobs_full
        """))
        connection.execute(text("DROP TABLE document_jobs_full"))
        connection.execute(text("ALTER TABLE rag_runs RENAME TO rag_runs_full"))
        connection.execute(text("""
            CREATE TABLE rag_runs AS
            SELECT id, trace_id, user_id, kb_id, conversation_id, status,
                   question_sha256, created_at
            FROM rag_runs_full
        """))
        connection.execute(text("DROP TABLE rag_runs_full"))

    plan = migration.migration_plan(engine)
    assert any("REBUILD document_jobs" in operation for operation in plan)
    assert any("REBUILD rag_runs" in operation for operation in plan)
    migration.apply_migration(engine)
    assert migration.migration_plan(engine) == []
    _assert_router_schema(engine)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT idempotency_key FROM document_jobs")) == "job-key"
        assert connection.scalar(text("SELECT trace_id FROM rag_runs")) == "trace-1"
    assert migration.apply_migration(engine) == []


def test_missing_required_business_column_with_rows_fails_fast(tmp_path):
    engine = _legacy_database(tmp_path)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE document_jobs (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO document_jobs (id) VALUES (1)"))
    with pytest.raises(migration.UnsafeMigrationError, match="job_type"):
        migration.apply_migration(engine)
    assert any("REBUILD document_jobs" in item for item in migration.migration_plan(engine))


def test_interrupted_temporary_table_is_recovered_or_discarded(tmp_path):
    engine = _legacy_database(tmp_path)
    migration.apply_migration(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE rag_runs RENAME TO __router_v2_new_rag_runs"))
        connection.execute(text("CREATE TABLE document_jobs_shadow (id INTEGER)"))
        connection.execute(text(
            "CREATE TABLE __router_v2_new_document_jobs AS SELECT * FROM document_jobs"
        ))
    plan = migration.migration_plan(engine)
    assert any("RECOVER rag_runs" in item for item in plan)
    assert any("RECOVER document_jobs" in item for item in plan)
    migration.apply_migration(engine)
    assert migration.migration_plan(engine) == []
    _assert_router_schema(engine)


def test_migration_cli_check_exit_codes(tmp_path):
    engine = _legacy_database(tmp_path)
    url = str(engine.url)
    engine.dispose()
    assert migration.main(["--check", "--database-url", url]) == 1
    assert migration.main(["--apply", "--database-url", url]) == 0
    assert migration.main(["--check", "--database-url", url]) == 0
    assert migration.main(["--apply", "--database-url", url]) == 0
