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

    # MIGRATION: upgrade legacy SQLite tables before metadata creates indexes on new columns.
    _ensure_demo_columns()
    Base.metadata.create_all(bind=engine)


def _ensure_demo_columns():
    """MIGRATION: incrementally add tenant metadata to legacy SQLite databases."""
    if engine.dialect.name != "sqlite":
        return

    inspector = inspect(engine)
    available_tables = set(inspector.get_table_names())
    if "users" not in available_tables:
        return

    with engine.begin() as conn:
        user_columns = {column["name"] for column in inspect(conn).get_columns("users")}
        if "shop_id" not in user_columns:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN shop_id "
                "VARCHAR(64) NOT NULL DEFAULT 'legacy-shop'"
            ))

    _migrate_legacy_users_unique_constraint()

    child_columns = {
        "datasources": {"shop_id": "VARCHAR(64) NOT NULL DEFAULT 'legacy-shop'"},
        "analysis_records": {
            "shop_id": "VARCHAR(64) NOT NULL DEFAULT 'legacy-shop'",
            "tool_trace": "TEXT DEFAULT '[]'",
            "rag_sources": "TEXT DEFAULT '[]'",
        },
    }
    with engine.begin() as conn:
        for table_name, required_columns in child_columns.items():
            if table_name not in available_tables:
                continue
            existing = {column["name"] for column in inspect(conn).get_columns(table_name)}
            for column_name, column_type in required_columns.items():
                if column_name not in existing:
                    conn.execute(text(
                        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                    ))

        if "datasources" in available_tables:
            orphaned_datasources = conn.execute(text("""
                SELECT d.id
                FROM datasources AS d
                LEFT JOIN users AS u ON u.id = d.user_id
                WHERE u.id IS NULL
            """)).fetchall()
            if orphaned_datasources:
                raise RuntimeError(
                    "Cannot migrate datasource shop ownership: "
                    f"missing users for datasource ids {[row[0] for row in orphaned_datasources]}"
                )
            conn.execute(text("""
                UPDATE datasources
                SET shop_id = (
                    SELECT users.shop_id FROM users WHERE users.id = datasources.user_id
                )
            """))

        if "analysis_records" in available_tables:
            invalid_analysis = conn.execute(text("""
                SELECT a.id
                FROM analysis_records AS a
                LEFT JOIN users AS u ON u.id = a.user_id
                LEFT JOIN datasources AS d ON d.id = a.ds_id
                WHERE u.id IS NULL OR d.id IS NULL
                   OR d.user_id <> a.user_id OR d.shop_id <> u.shop_id
            """)).fetchall()
            if invalid_analysis:
                raise RuntimeError(
                    "Cannot migrate analysis shop ownership: datasource/user shop mismatch "
                    f"for analysis ids {[row[0] for row in invalid_analysis]}"
                )
            conn.execute(text("""
                UPDATE analysis_records
                SET shop_id = (
                    SELECT datasources.shop_id
                    FROM datasources
                    WHERE datasources.id = analysis_records.ds_id
                )
            """))

        _ensure_sqlite_index(
            conn, "ux_users_shop_username", "users", ("shop_id", "username"),
            unique=True,
        )
        _ensure_sqlite_index(conn, "ix_users_shop_id", "users", ("shop_id",))
        if "datasources" in available_tables:
            _ensure_sqlite_index(
                conn, "ix_datasources_owner_shop", "datasources", ("user_id", "shop_id"),
            )
        if "analysis_records" in available_tables:
            _ensure_sqlite_index(
                conn, "ix_analysis_records_owner_shop", "analysis_records",
                ("user_id", "shop_id"),
            )

        violations = conn.execute(text("PRAGMA foreign_key_check")).fetchall()
        if violations:
            raise RuntimeError(f"SQLite foreign key check failed after migration: {violations}")


def _ensure_sqlite_index(
    conn,
    index_name: str,
    table_name: str,
    columns: tuple[str, ...],
    unique: bool = False,
) -> None:
    existing = {
        row[1]: (bool(row[2]), tuple(
            info[2] for info in conn.execute(text(
                f"PRAGMA index_info('{row[1]}')"
            )).fetchall()
        ))
        for row in conn.execute(text(f"PRAGMA index_list('{table_name}')")).fetchall()
    }
    expected = (unique, columns)
    if index_name in existing and existing[index_name] != expected:
        conn.execute(text(f"DROP INDEX {index_name}"))
    qualifier = "UNIQUE " if unique else ""
    conn.execute(text(
        f"CREATE {qualifier}INDEX IF NOT EXISTS {index_name} "
        f"ON {table_name} ({', '.join(columns)})"
    ))


def _migrate_legacy_users_unique_constraint():
    """MIGRATION: users.username global unique -> unique within each shop."""
    with engine.connect() as conn:
        user_columns = [row[1] for row in conn.execute(text(
            "PRAGMA table_info('users')"
        )).fetchall()]
        unique_columns = {
            tuple(row[2] for row in conn.execute(text(
                f"PRAGMA index_info('{index_name}')"
            )).fetchall())
            for _, index_name, is_unique, *_ in conn.execute(text(
                "PRAGMA index_list('users')"
            )).fetchall()
            if is_unique
        }
        needs_rebuild = ("username",) in unique_columns
        if needs_rebuild:
            expected_columns = {"id", "shop_id", "username", "hashed_password", "created_at"}
            unknown_columns = set(user_columns) - expected_columns
            missing_columns = expected_columns - set(user_columns)
            if unknown_columns or missing_columns:
                raise RuntimeError(
                    "Cannot safely rebuild legacy users table; "
                    f"unknown columns={sorted(unknown_columns)}, missing columns={sorted(missing_columns)}"
                )
            triggers = conn.execute(text("""
                SELECT name FROM sqlite_master
                WHERE type = 'trigger' AND tbl_name = 'users'
            """)).fetchall()
            if triggers:
                raise RuntimeError(
                    "Cannot safely rebuild legacy users table with triggers: "
                    f"{[row[0] for row in triggers]}"
                )
    if not needs_rebuild:
        return

    raw_connection = engine.raw_connection()
    cursor = raw_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = OFF")
        cursor.execute("PRAGMA legacy_alter_table = ON")
        cursor.execute("BEGIN")
        cursor.execute("ALTER TABLE users RENAME TO users_legacy_unique")
        cursor.execute("""
            CREATE TABLE users (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                shop_id VARCHAR(64) NOT NULL DEFAULT 'legacy-shop',
                username VARCHAR(50) NOT NULL,
                hashed_password VARCHAR(128) NOT NULL,
                created_at DATETIME,
                CONSTRAINT uq_users_shop_username UNIQUE (shop_id, username)
            )
        """)
        cursor.execute("""
            INSERT INTO users (id, shop_id, username, hashed_password, created_at)
            SELECT id, COALESCE(NULLIF(shop_id, ''), 'legacy-shop'),
                   username, hashed_password, created_at
            FROM users_legacy_unique
        """)
        cursor.execute("DROP TABLE users_legacy_unique")
        raw_connection.commit()
    except Exception:
        raw_connection.rollback()
        raise
    finally:
        cursor.execute("PRAGMA legacy_alter_table = OFF")
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()
        raw_connection.close()
