#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, NamedTuple

from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings  # noqa: E402

DOCUMENT_COLUMNS = {
    "error_message": "VARCHAR(500) NOT NULL DEFAULT ''",
    "file_sha256": "VARCHAR(64) NOT NULL DEFAULT ''",
    "ingestion_status": "VARCHAR(20) NOT NULL DEFAULT 'pending'",
    "enrichment_status": "VARCHAR(20) NOT NULL DEFAULT 'pending'",
    "parse_profile": "VARCHAR(50) NOT NULL DEFAULT ''",
    "parse_policy_fingerprint": "VARCHAR(64) NOT NULL DEFAULT ''",
    "parse_audit": "JSON",
    "active_index_version": "VARCHAR(100) NOT NULL DEFAULT ''",
    "storage_path": "VARCHAR(1000) NOT NULL DEFAULT ''",
    "page_count": "INTEGER NOT NULL DEFAULT 0",
    "updated_at": "DATETIME",
}
ROUTER_TABLES = ("document_jobs", "rag_runs")
TEMP_PREFIX = "__router_v2_new_"


class UnsafeMigrationError(RuntimeError):
    """Raised when existing rows cannot be mapped to the Router V2 schema safely."""


class TableIssues(NamedTuple):
    missing_columns: tuple[str, ...] = ()
    mismatched_columns: tuple[str, ...] = ()
    missing_primary_key: tuple[str, ...] = ()
    missing_indexes: tuple[str, ...] = ()
    missing_uniques: tuple[tuple[str, ...], ...] = ()
    missing_checks: tuple[str, ...] = ()
    missing_foreign_keys: tuple[tuple[str, ...], ...] = ()

    @property
    def requires_rebuild(self) -> bool:
        return bool(
            self.missing_columns
            or self.mismatched_columns
            or self.missing_primary_key
            or self.missing_uniques
            or self.missing_checks
            or self.missing_foreign_keys
        )

    @property
    def any(self) -> bool:
        return self.requires_rebuild or bool(self.missing_indexes)


def _load_metadata():
    from app.database import Base
    from app.models import models  # noqa: F401

    return Base.metadata


def _normalize_sql(value: str | None) -> str:
    return "".join((value or "").lower().replace('"', "").replace("`", "").split())


def _column_tuple(value: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    return tuple(value or ())


def _expected_indexes(table) -> dict[str, tuple[str, ...]]:
    return {
        index.name: tuple(column.name for column in index.columns)
        for index in table.indexes
        if index.name
    }


def _existing_unique_sets(inspector, table_name: str) -> set[tuple[str, ...]]:
    unique_sets = {
        _column_tuple(item.get("column_names"))
        for item in inspector.get_unique_constraints(table_name)
    }
    unique_sets.update(
        _column_tuple(item.get("column_names"))
        for item in inspector.get_indexes(table_name)
        if item.get("unique")
    )
    return unique_sets


def _table_issues(engine, table_name: str) -> TableIssues:
    metadata = _load_metadata()
    target = metadata.tables[table_name]
    inspector = inspect(engine)
    existing_column_details = {
        column["name"]: column for column in inspector.get_columns(table_name)
    }
    existing_columns = set(existing_column_details)
    missing_columns = tuple(
        column.name for column in target.columns if column.name not in existing_columns
    )
    mismatched_columns = tuple(
        column.name
        for column in target.columns
        if column.name in existing_column_details
        and column.nullable is False
        and bool(existing_column_details[column.name].get("nullable", True))
    )
    existing_primary_key = _column_tuple(
        inspector.get_pk_constraint(table_name).get("constrained_columns")
    )
    expected_primary_key = tuple(column.name for column in target.primary_key.columns)
    missing_primary_key = (
        expected_primary_key if existing_primary_key != expected_primary_key else ()
    )

    existing_indexes = {
        item.get("name"): _column_tuple(item.get("column_names"))
        for item in inspector.get_indexes(table_name)
    }
    missing_indexes = tuple(
        name
        for name, columns in sorted(_expected_indexes(target).items())
        if existing_indexes.get(name) != columns
    )

    existing_uniques = _existing_unique_sets(inspector, table_name)
    expected_uniques = {
        (column.name,)
        for column in target.columns
        if column.unique
    }
    expected_uniques.update(
        tuple(column.name for column in constraint.columns)
        for constraint in target.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    )
    missing_uniques = tuple(sorted(expected_uniques - existing_uniques))

    existing_checks = {
        _normalize_sql(item.get("sqltext"))
        for item in inspector.get_check_constraints(table_name)
    }
    expected_checks = {
        _normalize_sql(str(constraint.sqltext))
        for constraint in target.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }
    missing_checks = tuple(sorted(expected_checks - existing_checks))

    existing_foreign_keys = {
        (
            *(_column_tuple(item.get("constrained_columns"))),
            str(item.get("referred_table") or ""),
            *(_column_tuple(item.get("referred_columns"))),
            str((item.get("options") or {}).get("ondelete") or "").upper(),
        )
        for item in inspector.get_foreign_keys(table_name)
    }
    expected_foreign_keys = {
        (
            *(column.name for column in constraint.columns),
            next(iter(constraint.elements)).column.table.name,
            *(element.column.name for element in constraint.elements),
            str(constraint.ondelete or "").upper(),
        )
        for constraint in target.foreign_key_constraints
    }
    missing_foreign_keys = tuple(sorted(expected_foreign_keys - existing_foreign_keys))

    return TableIssues(
        missing_columns=missing_columns,
        mismatched_columns=mismatched_columns,
        missing_primary_key=missing_primary_key,
        missing_indexes=missing_indexes,
        missing_uniques=missing_uniques,
        missing_checks=missing_checks,
        missing_foreign_keys=missing_foreign_keys,
    )


def _issue_plan(table_name: str, issues: TableIssues) -> list[str]:
    plan: list[str] = []
    plan.extend(f"REBUILD {table_name}: add missing column {name}" for name in issues.missing_columns)
    plan.extend(
        f"REBUILD {table_name}: enforce NOT NULL on {name}"
        for name in issues.mismatched_columns
    )
    if issues.missing_primary_key:
        plan.append(
            f"REBUILD {table_name}: enforce PRIMARY KEY "
            f"({', '.join(issues.missing_primary_key)})"
        )
    plan.extend(
        f"CREATE INDEX {name} on {table_name}" for name in issues.missing_indexes
    )
    plan.extend(
        f"REBUILD {table_name}: add UNIQUE ({', '.join(columns)})"
        for columns in issues.missing_uniques
    )
    plan.extend(
        f"REBUILD {table_name}: add CHECK ({sqltext})" for sqltext in issues.missing_checks
    )
    plan.extend(
        f"REBUILD {table_name}: add FOREIGN KEY ({', '.join(columns)})"
        for columns in issues.missing_foreign_keys
    )
    return plan


def migration_plan(engine) -> list[str]:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    plan: list[str] = []
    if "documents" in tables:
        columns = {column["name"] for column in inspector.get_columns("documents")}
        for name, ddl in DOCUMENT_COLUMNS.items():
            if name not in columns:
                plan.append(f"ALTER TABLE documents ADD COLUMN {name} {ddl}")
    for table_name in ROUTER_TABLES:
        temporary = f"{TEMP_PREFIX}{table_name}"
        if temporary in tables:
            action = "recover" if table_name not in tables else "discard stale"
            plan.append(f"RECOVER {table_name}: {action} interrupted temporary table")
        if table_name not in tables:
            plan.append(f"CREATE {table_name} table and indexes from SQLAlchemy metadata")
        else:
            plan.extend(_issue_plan(table_name, _table_issues(engine, table_name)))
    return plan


def _recover_interrupted_tables(engine) -> None:
    with engine.begin() as connection:
        tables = set(inspect(connection).get_table_names())
        for table_name in ROUTER_TABLES:
            temporary = f"{TEMP_PREFIX}{table_name}"
            if temporary not in tables:
                continue
            if table_name in tables:
                connection.execute(text(f'DROP TABLE "{temporary}"'))
            else:
                connection.execute(
                    text(f'ALTER TABLE "{temporary}" RENAME TO "{table_name}"')
                )


def _sql_default(column) -> str | None:
    if column.nullable:
        return "NULL"
    if column.default is None:
        return None
    value: Any = column.default.arg
    if callable(value):
        return "CURRENT_TIMESTAMP"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


def _copy_expressions(connection, source_name: str, target) -> list[str]:
    source_columns = {
        column["name"] for column in inspect(connection).get_columns(source_name)
    }
    row_count = connection.scalar(text(f'SELECT COUNT(*) FROM "{source_name}"')) or 0
    expressions: list[str] = []
    unsafe: list[str] = []
    for column in target.columns:
        if column.name in source_columns:
            expressions.append(f'"{column.name}"')
            continue
        default = _sql_default(column)
        if default is None:
            unsafe.append(column.name)
        else:
            expressions.append(f'{default} AS "{column.name}"')
    if row_count and unsafe:
        raise UnsafeMigrationError(
            f"cannot safely migrate {source_name}: {row_count} existing row(s) and "
            f"missing required column(s) without defaults: {', '.join(unsafe)}"
        )
    return expressions


def _validate_unique_data(connection, source_name: str, target) -> None:
    source_columns = {
        column["name"] for column in inspect(connection).get_columns(source_name)
    }
    unique_sets = {
        (column.name,) for column in target.columns if column.unique
    }
    unique_sets.update(
        tuple(column.name for column in constraint.columns)
        for constraint in target.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    )
    for columns in unique_sets:
        if not set(columns) <= source_columns:
            continue
        quoted = ", ".join(f'"{name}"' for name in columns)
        duplicate = connection.execute(text(
            f'SELECT {quoted}, COUNT(*) FROM "{source_name}" '
            f'GROUP BY {quoted} HAVING COUNT(*) > 1 LIMIT 1'
        )).first()
        if duplicate is not None:
            raise UnsafeMigrationError(
                f"cannot safely add UNIQUE ({', '.join(columns)}) to {source_name}: "
                "duplicate existing values"
            )


def _create_missing_indexes(connection, table_name: str) -> None:
    target = _load_metadata().tables[table_name]
    existing = {
        item.get("name"): _column_tuple(item.get("column_names"))
        for item in inspect(connection).get_indexes(table_name)
    }
    for index in sorted(target.indexes, key=lambda item: item.name or ""):
        if not index.name:
            continue
        expected_columns = tuple(column.name for column in index.columns)
        if index.name in existing and existing[index.name] != expected_columns:
            connection.execute(text(f'DROP INDEX "{index.name}"'))
            existing.pop(index.name)
        if index.name not in existing:
            index.create(bind=connection, checkfirst=False)


def _rebuild_table(engine, table_name: str) -> None:
    target = _load_metadata().tables[table_name]
    temporary_name = f"{TEMP_PREFIX}{table_name}"
    temporary_metadata = MetaData()
    for dependency in target.metadata.sorted_tables:
        if dependency.name != table_name:
            dependency.to_metadata(temporary_metadata)
    temporary = target.to_metadata(temporary_metadata, name=temporary_name)

    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        try:
            with connection.begin():
                tables = set(inspect(connection).get_table_names())
                if temporary_name in tables:
                    connection.execute(text(f'DROP TABLE "{temporary_name}"'))
                expressions = _copy_expressions(connection, table_name, target)
                _validate_unique_data(connection, table_name, target)
                connection.execute(CreateTable(temporary))
                row_count = connection.scalar(
                    text(f'SELECT COUNT(*) FROM "{table_name}"')
                ) or 0
                try:
                    if row_count:
                        target_columns = ", ".join(
                            f'"{column.name}"' for column in target.columns
                        )
                        connection.execute(text(
                            f'INSERT INTO "{temporary_name}" ({target_columns}) '
                            f'SELECT {", ".join(expressions)} FROM "{table_name}"'
                        ))
                except IntegrityError as exc:
                    raise UnsafeMigrationError(
                        f"cannot safely rebuild {table_name}: existing rows violate "
                        f"Router V2 constraints ({exc.orig})"
                    ) from exc
                connection.execute(text(f'DROP TABLE "{table_name}"'))
                connection.execute(text(
                    f'ALTER TABLE "{temporary_name}" RENAME TO "{table_name}"'
                ))
                _create_missing_indexes(connection, table_name)
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def apply_migration(engine) -> list[str]:
    if engine.dialect.name != "sqlite":
        raise RuntimeError("migrate_router_v2.py currently supports SQLite only")
    metadata = _load_metadata()
    before = migration_plan(engine)

    _recover_interrupted_tables(engine)
    with engine.begin() as connection:
        tables = set(inspect(connection).get_table_names())
        if "documents" in tables:
            columns = {column["name"] for column in inspect(connection).get_columns("documents")}
            for name, ddl in DOCUMENT_COLUMNS.items():
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE documents ADD COLUMN {name} {ddl}"))
            connection.execute(text(
                "UPDATE documents SET updated_at = "
                "COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)"
            ))
        for table_name in ROUTER_TABLES:
            if table_name not in tables:
                metadata.tables[table_name].create(bind=connection, checkfirst=True)

    for table_name in ROUTER_TABLES:
        issues = _table_issues(engine, table_name)
        if issues.requires_rebuild:
            _rebuild_table(engine, table_name)
        else:
            with engine.begin() as connection:
                _create_missing_indexes(connection, table_name)

    remaining = migration_plan(engine)
    if remaining:
        raise RuntimeError("migration remains incomplete: " + "; ".join(remaining))
    return before


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check or apply Router V2 SQLite schema")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--apply", action="store_true")
    parser.add_argument("--database-url", default=settings.DATABASE_URL)
    args = parser.parse_args(argv)

    engine = create_engine(args.database_url, connect_args={"check_same_thread": False})
    try:
        if engine.dialect.name != "sqlite":
            print("ERROR: only SQLite is supported", file=sys.stderr)
            return 2
        if args.apply:
            try:
                operations = apply_migration(engine)
            except (RuntimeError, UnsafeMigrationError) as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 2
            print(f"Router V2 schema is current; applied {len(operations)} operation(s).")
            return 0
        operations = migration_plan(engine)
        if operations:
            print("Router V2 schema changes required:")
            for item in operations:
                print(f"- {item}")
            return 1
        print("Router V2 schema is current; no changes required.")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
