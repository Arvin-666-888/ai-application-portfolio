import argparse
import sys
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import MetaData, create_engine, func, inspect, select
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE_PATH = PROJECT_ROOT / "storage" / "data_analyst.db"
DEFAULT_SOURCE_URL = f"sqlite:///{DEFAULT_SOURCE_PATH.as_posix()}"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import Base
from app.models import models as _models


TABLE_ORDER = ("users", "datasources", "analysis_records")


def _batches(rows: list[dict], size: int = 500) -> Iterable[list[dict]]:
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def _validate_source(source_url: str) -> None:
    url = make_url(source_url)
    if url.drivername == "sqlite" and url.database:
        source_path = Path(url.database)
        if not source_path.is_file():
            raise FileNotFoundError(f"源 SQLite 数据库不存在: {source_path}")


def migrate(
    source_url: str,
    target_url: str,
    *,
    replace_existing: bool = False,
) -> dict[str, int]:
    if source_url == target_url:
        raise ValueError("源数据库和目标数据库不能相同")

    _validate_source(source_url)
    source = create_engine(source_url)
    target = create_engine(target_url, pool_pre_ping=True)
    try:
        source_tables = set(inspect(source).get_table_names())
        missing_tables = set(TABLE_ORDER) - source_tables
        if missing_tables:
            missing = ", ".join(sorted(missing_tables))
            raise ValueError(f"源数据库缺少必需表: {missing}")

        target_inspector = inspect(target)
        existing_target_tables = set(target_inspector.get_table_names())
        populated_tables: list[str] = []
        with target.connect() as target_conn:
            for table_name in TABLE_ORDER:
                if table_name not in existing_target_tables:
                    continue
                table = Base.metadata.tables[table_name]
                if target_conn.scalar(select(func.count()).select_from(table)):
                    populated_tables.append(table_name)
        if populated_tables and not replace_existing:
            names = ", ".join(populated_tables)
            raise ValueError(
                f"目标数据库已有数据（{names}）；如确认覆盖，请显式传入 --replace-existing"
            )

        Base.metadata.create_all(target)
        source_metadata = MetaData()
        source_metadata.reflect(source, only=list(TABLE_ORDER))

        copied: dict[str, int] = {}
        with source.connect() as source_conn, target.begin() as target_conn:
            if replace_existing:
                for table_name in reversed(TABLE_ORDER):
                    target_conn.execute(Base.metadata.tables[table_name].delete())

            for table_name in TABLE_ORDER:
                source_table = source_metadata.tables.get(table_name)
                target_table = Base.metadata.tables[table_name]
                if source_table is None:
                    copied[table_name] = 0
                    continue

                rows = [dict(row) for row in source_conn.execute(select(source_table)).mappings()]
                for batch in _batches(rows):
                    target_conn.execute(target_table.insert(), batch)
                copied[table_name] = len(rows)
        return copied
    finally:
        source.dispose()
        target.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="将 business-data-agent 元数据从 SQLite 迁移到 MySQL")
    parser.add_argument("--source", default=DEFAULT_SOURCE_URL)
    parser.add_argument("--target", required=True)
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="清空目标三张元数据表后再迁移；默认目标非空时拒绝执行",
    )
    args = parser.parse_args()

    result = migrate(
        args.source,
        args.target,
        replace_existing=args.replace_existing,
    )
    for table_name, row_count in result.items():
        print(f"{table_name}: {row_count} rows copied")


if __name__ == "__main__":
    main()
