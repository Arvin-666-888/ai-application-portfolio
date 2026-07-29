from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


def _is_sqlite_url(database_url: str) -> bool:
    return database_url.startswith("sqlite")


connect_args = {"check_same_thread": False} if _is_sqlite_url(settings.DATABASE_URL) else {}
engine = create_engine(
    settings.DATABASE_URL,
    connect_args=connect_args,
    echo=settings.DEBUG,
)


@event.listens_for(engine, "connect")
def _configure_sqlite_connection(dbapi_connection, connection_record):
    del connection_record
    if dbapi_connection.__class__.__module__.split(".")[0] != "sqlite3":
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={settings.SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _migrate_legacy_index_versions() -> None:
    from app.models.models import Document
    from app.utils.vector_store import vector_store

    db = SessionLocal()
    try:
        documents = db.query(Document).filter(
            Document.status == "ready",
            Document.active_index_version == "",
        ).all()
        for document in documents:
            vector_store.migrate_legacy_document(document.kb_id, document.id)
            document.active_index_version = "legacy"
        if documents:
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    from app.models import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_columns()
    _migrate_legacy_index_versions()


def _ensure_sqlite_columns():
    if not _is_sqlite_url(settings.DATABASE_URL):
        return

    # create_all() intentionally does not alter an existing partial table. Route all
    # SQLite upgrades through the same verified, idempotent migration used by the CLI.
    from scripts.migrate_router_v2 import apply_migration

    apply_migration(engine)
