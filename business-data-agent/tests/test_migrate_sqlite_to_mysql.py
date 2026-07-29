from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models.models import User
from migrate_sqlite_to_mysql import migrate


def _database_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _seed_source(url: str, username: str = "source-user") -> None:
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(username=username, hashed_password="hash"))
        session.commit()
    engine.dispose()


def _usernames(url: str) -> list[str]:
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            return list(session.scalars(select(User.username).order_by(User.id)))
    finally:
        engine.dispose()


def test_migration_copies_into_empty_target(tmp_path):
    source_url = _database_url(tmp_path / "source.db")
    target_url = _database_url(tmp_path / "target.db")
    _seed_source(source_url)

    copied = migrate(source_url, target_url)

    assert copied == {"users": 1, "datasources": 0, "analysis_records": 0}
    assert _usernames(target_url) == ["source-user"]


def test_migration_rejects_non_empty_target_without_explicit_replace(tmp_path):
    source_url = _database_url(tmp_path / "source.db")
    target_url = _database_url(tmp_path / "target.db")
    _seed_source(source_url)
    _seed_source(target_url, username="target-user")

    with pytest.raises(ValueError, match="--replace-existing"):
        migrate(source_url, target_url)

    assert _usernames(target_url) == ["target-user"]


def test_migration_rejects_partial_non_empty_target_without_schema_changes(tmp_path):
    source_url = _database_url(tmp_path / "source.db")
    target_url = _database_url(tmp_path / "target.db")
    _seed_source(source_url)

    engine = create_engine(target_url)
    metadata = MetaData()
    users = Table(
        "users",
        metadata,
        Column("id", Integer, primary_key=True),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(users.insert().values(id=1))
    tables_before = inspect(engine).get_table_names()
    engine.dispose()

    with pytest.raises(ValueError, match="--replace-existing"):
        migrate(source_url, target_url)

    target_engine = create_engine(target_url)
    try:
        assert inspect(target_engine).get_table_names() == tables_before
    finally:
        target_engine.dispose()


def test_migration_replaces_target_only_when_explicitly_requested(tmp_path):
    source_url = _database_url(tmp_path / "source.db")
    target_url = _database_url(tmp_path / "target.db")
    _seed_source(source_url)
    _seed_source(target_url, username="target-user")

    migrate(source_url, target_url, replace_existing=True)

    assert _usernames(target_url) == ["source-user"]


def test_migration_rejects_missing_source_database(tmp_path):
    with pytest.raises(FileNotFoundError):
        migrate(
            _database_url(tmp_path / "missing.db"),
            _database_url(tmp_path / "target.db"),
        )
