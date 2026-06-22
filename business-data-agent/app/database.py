from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=settings.DEBUG,
)

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
    Base.metadata.create_all(bind=engine)
    _ensure_demo_columns()


def _ensure_demo_columns():
    """Keep the checked-in SQLite demo DB compatible after small schema upgrades."""
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
