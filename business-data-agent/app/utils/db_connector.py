import logging
from typing import Optional

from sqlalchemy import create_engine, text, inspect

from app.utils.sql_safety import validate_table_name

logger = logging.getLogger("kb_qa.db_connector")


class DatabaseConnector:
    def __init__(self, connection_string: str):
        self.connection_string = connection_string
        self._engine = None

    @property
    def engine(self):
        if self._engine is None:
            self._engine = create_engine(
                self.connection_string,
                connect_args={"check_same_thread": False} if "sqlite" in self.connection_string else {},
            )
        return self._engine

    def test_connection(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception as e:
            logger.error(f"Connection test failed: {e}")
            return False

    def get_tables(self) -> list[str]:
        try:
            inspector = inspect(self.engine)
            return inspector.get_table_names()
        except Exception as e:
            logger.error(f"Get tables failed: {e}")
            return []

    def get_schema(self) -> list[dict]:
        try:
            inspector = inspect(self.engine)
            schema_info = []
            for table_name in inspector.get_table_names():
                columns = inspector.get_columns(table_name)
                pk_columns = inspector.get_pk_constraint(table_name).get("constrained_columns", [])
                fks = inspector.get_foreign_keys(table_name)

                schema_info.append({
                    "table": table_name,
                    "columns": [
                        {
                            "name": col["name"],
                            "type": str(col["type"]),
                            "nullable": col.get("nullable", True),
                            "primary_key": col["name"] in pk_columns,
                        }
                        for col in columns
                    ],
                    "foreign_keys": [
                        {"from": fk["constrained_columns"], "to": fk["referred_table"]}
                        for fk in fks
                    ],
                })
            return schema_info
        except Exception as e:
            logger.error(f"Get schema failed: {e}")
            return []

    def execute_query(self, sql: str) -> list[dict]:
        with self.engine.connect() as conn:
            result = conn.execute(text(sql))
            columns = result.keys()
            rows = [dict(zip(columns, row)) for row in result.fetchall()]
            return rows

    def preview_table(self, table_name: str, rows: int = 5) -> list[dict]:
        if not validate_table_name(table_name):
            raise ValueError(f"Invalid table name: {table_name}")
        return self.execute_query(f'SELECT * FROM "{table_name}" LIMIT {rows}')

    def dispose(self):
        if self._engine:
            self._engine.dispose()
            self._engine = None


class ConnectionManager:
    _connectors: dict[int, DatabaseConnector] = {}

    @classmethod
    def get_connector(cls, ds_id: int, connection_string: str) -> DatabaseConnector:
        if ds_id not in cls._connectors:
            cls._connectors[ds_id] = DatabaseConnector(connection_string)
        return cls._connectors[ds_id]

    @classmethod
    def remove_connector(cls, ds_id: int):
        connector = cls._connectors.pop(ds_id, None)
        if connector:
            connector.dispose()

    @classmethod
    def has_connector(cls, ds_id: int) -> bool:
        return ds_id in cls._connectors
