import logging

from sqlalchemy import create_engine, inspect, text

from app.utils.sql_safety import (
    BUSINESS_TABLES,
    enforce_shop_scope,
    parse_and_guard_sql,
    validate_table_name,
)

logger = logging.getLogger("business_data_agent.db_connector")


class DatabaseConnector:
    def __init__(self, connection_string: str, shop_id: str):
        if not shop_id:
            raise ValueError("shop_id 不能为空")
        self.connection_string = connection_string
        self.shop_id = shop_id
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
        except Exception as exc:
            logger.error("Connection test failed: %s", exc)
            return False

    def get_tables(self) -> list[str]:
        try:
            available = set(inspect(self.engine).get_table_names())
            return sorted(available & BUSINESS_TABLES)
        except Exception as exc:
            logger.error("Get tables failed: %s", exc)
            return []

    def get_schema(self) -> list[dict]:
        try:
            inspector = inspect(self.engine)
            schema_info = []
            available_tables = set(inspector.get_table_names())
            for table_name in sorted(available_tables & BUSINESS_TABLES):
                columns = inspector.get_columns(table_name)
                if "shop_id" not in {column["name"] for column in columns}:
                    raise PermissionError(f"业务表缺少 shop_id: {table_name}")
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
        except Exception as exc:
            logger.error("Get schema failed: %s", exc)
            return []

    def execute_query(self, sql: str, max_rows: int = 1000) -> list[dict]:
        dialect = self.engine.dialect.name
        limited_sql = parse_and_guard_sql(sql, max_rows=max_rows, dialect=dialect)
        scoped_sql = enforce_shop_scope(limited_sql, dialect=dialect)
        with self.engine.connect() as conn:
            result = conn.execute(text(scoped_sql), {"shop_id": self.shop_id})
            columns = result.keys()
            return [dict(zip(columns, row)) for row in result.fetchall()]

    def preview_table(self, table_name: str, rows: int = 5) -> list[dict]:
        if not validate_table_name(table_name) or table_name not in BUSINESS_TABLES:
            raise ValueError(f"Invalid business table name: {table_name}")
        if isinstance(rows, bool) or not isinstance(rows, int) or not 1 <= rows <= 100:
            raise ValueError("rows 必须是 1 到 100 之间的整数")
        quoted_table = self.engine.dialect.identifier_preparer.quote(table_name)
        return self.execute_query(f"SELECT * FROM {quoted_table} LIMIT {rows}", max_rows=rows)

    def dispose(self):
        if self._engine:
            self._engine.dispose()
            self._engine = None


class ConnectionManager:
    _connectors: dict[tuple[int, int, str], DatabaseConnector] = {}

    @classmethod
    def get_connector(
        cls,
        ds_id: int,
        user_id: int,
        shop_id: str,
        connection_string: str,
    ) -> DatabaseConnector:
        key = (ds_id, user_id, shop_id)
        connector = cls._connectors.get(key)
        if connector is None or connector.connection_string != connection_string:
            if connector is not None:
                connector.dispose()
            connector = DatabaseConnector(connection_string, shop_id)
            cls._connectors[key] = connector
        return connector

    @classmethod
    def remove_connector(cls, ds_id: int, user_id: int, shop_id: str):
        connector = cls._connectors.pop((ds_id, user_id, shop_id), None)
        if connector:
            connector.dispose()
