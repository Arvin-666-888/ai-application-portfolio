import sqlite3

import pytest
from sqlalchemy import create_engine, inspect, text

from app import database


def _create_legacy_database(path):
    connection = sqlite3.connect(path)
    connection.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username VARCHAR(50) NOT NULL UNIQUE,
            hashed_password VARCHAR(128) NOT NULL,
            created_at DATETIME
        );
        CREATE TABLE datasources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(100) NOT NULL,
            db_type VARCHAR(20) NOT NULL,
            connection_string VARCHAR(500) NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at DATETIME
        );
        CREATE TABLE analysis_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question VARCHAR(1000) NOT NULL,
            answer TEXT DEFAULT '',
            sql_query TEXT DEFAULT '',
            query_result TEXT DEFAULT '[]',
            chart_path VARCHAR(255) DEFAULT '',
            ds_id INTEGER NOT NULL REFERENCES datasources(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at DATETIME
        );
        INSERT INTO users (id, username, hashed_password, created_at)
        VALUES (7, 'legacy-user', 'legacy-hash', '2026-01-01 00:00:00');
        INSERT INTO users (id, username, hashed_password, created_at)
        VALUES (8, 'second-user', 'second-hash', '2026-01-01 00:00:00');
        INSERT INTO datasources
            (id, name, db_type, connection_string, user_id, created_at)
        VALUES (11, 'legacy-source', 'sqlite', 'sqlite:///legacy.db', 7, '2026-01-02 00:00:00');
        INSERT INTO analysis_records
            (id, question, ds_id, user_id, created_at)
        VALUES (13, 'legacy-question', 11, 7, '2026-01-03 00:00:00');
    """)
    connection.commit()
    connection.close()


def test_legacy_username_unique_migrates_idempotently(monkeypatch, tmp_path):
    db_path = tmp_path / "legacy.db"
    _create_legacy_database(db_path)
    migration_engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setattr(database, "engine", migration_engine)

    database.init_db()
    with migration_engine.begin() as connection:
        connection.execute(text("DROP INDEX ix_datasources_owner_shop"))
        connection.execute(text(
            "CREATE INDEX ix_datasources_owner_shop ON datasources (shop_id, user_id)"
        ))
        connection.execute(text("DROP INDEX ix_analysis_records_owner_shop"))
        connection.execute(text(
            "CREATE INDEX ix_analysis_records_owner_shop "
            "ON analysis_records (shop_id, user_id)"
        ))
    database.init_db()

    with migration_engine.begin() as connection:
        assert connection.execute(text(
            "SELECT id, shop_id, username FROM users ORDER BY id"
        )).all() == [
            (7, "legacy-shop", "legacy-user"),
            (8, "legacy-shop", "second-user"),
        ]
        assert connection.execute(text(
            "SELECT id, user_id, shop_id FROM datasources"
        )).all() == [(11, 7, "legacy-shop")]
        assert connection.execute(text(
            "SELECT id, ds_id, user_id, shop_id FROM analysis_records"
        )).all() == [(13, 11, 7, "legacy-shop")]

        connection.execute(text("""
            INSERT INTO users (shop_id, username, hashed_password)
            VALUES ('amazon-us', 'legacy-user', 'new-hash')
        """))
        with pytest.raises(Exception):
            connection.execute(text("""
                INSERT INTO users (shop_id, username, hashed_password)
                VALUES ('amazon-us', 'legacy-user', 'duplicate-hash')
            """))

    indexes = {
        index["name"]: index["column_names"]
        for table in ("users", "datasources", "analysis_records")
        for index in inspect(migration_engine).get_indexes(table)
    }
    assert indexes["ux_users_shop_username"] == ["shop_id", "username"]
    assert indexes["ix_datasources_owner_shop"] == ["user_id", "shop_id"]
    assert indexes["ix_analysis_records_owner_shop"] == ["user_id", "shop_id"]
    with migration_engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []

    migration_engine.dispose()


def test_partial_tenant_columns_backfill_child_shop_from_user(monkeypatch, tmp_path):
    db_path = tmp_path / "partial.db"
    _create_legacy_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute("ALTER TABLE users ADD COLUMN shop_id TEXT NOT NULL DEFAULT 'legacy-shop'")
    connection.execute("UPDATE users SET shop_id = 'amazon-us' WHERE id = 7")
    connection.commit()
    connection.close()
    migration_engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setattr(database, "engine", migration_engine)

    database.init_db()
    database.init_db()

    with migration_engine.connect() as connection:
        assert connection.execute(text(
            "SELECT shop_id FROM datasources WHERE id = 11"
        )).scalar_one() == "amazon-us"
        assert connection.execute(text(
            "SELECT shop_id FROM analysis_records WHERE id = 13"
        )).scalar_one() == "amazon-us"
    migration_engine.dispose()


def test_analysis_migration_fails_when_datasource_owner_mismatches(monkeypatch, tmp_path):
    db_path = tmp_path / "mismatch.db"
    _create_legacy_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute("UPDATE analysis_records SET user_id = 8 WHERE id = 13")
    connection.commit()
    connection.close()
    migration_engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setattr(database, "engine", migration_engine)

    with pytest.raises(RuntimeError, match="datasource/user shop mismatch"):
        database.init_db()
    migration_engine.dispose()


@pytest.mark.parametrize("schema_extra, message", [
    ("ALTER TABLE users ADD COLUMN external_id TEXT;", "Cannot safely rebuild legacy users table"),
    (
        "CREATE TRIGGER users_audit AFTER UPDATE ON users BEGIN SELECT 1; END;",
        "Cannot safely rebuild legacy users table with triggers",
    ),
])
def test_legacy_users_rebuild_fails_closed_for_unknown_schema(
    monkeypatch, tmp_path, schema_extra, message,
):
    db_path = tmp_path / "unsupported.db"
    _create_legacy_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.executescript(schema_extra)
    connection.commit()
    connection.close()
    migration_engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setattr(database, "engine", migration_engine)

    with pytest.raises(RuntimeError, match=message):
        database.init_db()
    migration_engine.dispose()
