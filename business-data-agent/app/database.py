from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


def _engine_options(database_url: str) -> dict:
    options = {
        "echo": settings.DEBUG,
        "pool_pre_ping": True,
    }
    if database_url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
    else:
        options.update({"pool_recycle": 1800, "pool_size": 10, "max_overflow": 20})
    return options


engine = create_engine(settings.DATABASE_URL, **_engine_options(settings.DATABASE_URL))

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from app.models import models as _models

    Base.metadata.create_all(bind=engine)
    _ensure_demo_columns()


def _ensure_demo_columns():
    """Keep the checked-in SQLite demo DB compatible after small schema upgrades."""
    if engine.dialect.name != "sqlite":
        return

    inspector = inspect(engine)
    if "analysis_records" not in inspector.get_table_names():
        return

    existing_columns = {column["name"] for column in inspector.get_columns("analysis_records")}
    required_columns = {
        "tool_trace": "TEXT DEFAULT '[]'",
        "rag_sources": "TEXT DEFAULT '[]'",
    }

    with engine.begin() as conn:
        for column_name, column_type in required_columns.items():
            if column_name not in existing_columns:
                conn.execute(text(f"ALTER TABLE analysis_records ADD COLUMN {column_name} {column_type}"))
