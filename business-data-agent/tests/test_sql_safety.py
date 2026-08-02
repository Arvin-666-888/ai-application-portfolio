import pytest
from sqlalchemy import text

from app.utils.db_connector import DatabaseConnector
from app.utils.sql_safety import (
    enforce_shop_scope,
    parse_and_guard_sql,
    sanitize_sql,
    validate_sql,
)


def test_parse_and_guard_sql_keeps_original_select_only_rules():
    assert validate_sql("SELECT sku FROM sales_records") == (True, "验证通过")
    assert sanitize_sql("SELECT * FROM sales_records", max_rows=20).endswith("LIMIT 20")


@pytest.mark.parametrize("sql", [
    "DELETE FROM sales_records",
    "SELECT * FROM sales_records; DROP TABLE sales_records",
    "SELECT * FROM sales_records -- bypass",
    "SELECT * FROM information_schema.tables",
    "UPDATE sales_records SET gross_sales = 0",
    "INSERT INTO sales_records(shop_id) VALUES ('x')",
    "CREATE TABLE copied AS SELECT * FROM sales_records",
    "ALTER TABLE sales_records ADD COLUMN note TEXT",
    "TRUNCATE TABLE sales_records",
    "EXPLAIN DELETE FROM sales_records",
    "SELECT GET_LOCK('agent-lock', 1)",
    "SELECT @x := 1",
])
def test_unsafe_sql_is_rejected(sql):
    with pytest.raises(PermissionError):
        parse_and_guard_sql(sql, dialect="mysql" if "GET_LOCK" in sql or "@x" in sql else None)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM sales_records; /* harmless */ DROP TABLE users",
    "SELECT * FROM sales_records -- comment\n; DELETE FROM sales_records",
    "DR/**/OP TABLE users",
])
def test_comment_bypasses_are_rejected(sql):
    with pytest.raises(PermissionError):
        parse_and_guard_sql(sql)


def test_shop_scope_covers_alias_and_join():
    scoped = enforce_shop_scope(
        "SELECT s.sku, i.on_hand_units FROM sales_records s "
        "JOIN inventory_snapshots i ON s.sku = i.sku"
    )
    assert "s.shop_id = :shop_id" in scoped
    assert "i.shop_id = :shop_id" in scoped
    assert scoped.count(":shop_id") == 2


def test_shop_scope_covers_cte_and_subquery():
    scoped = enforce_shop_scope(
        "WITH sales AS (SELECT sku FROM sales_records) "
        "SELECT * FROM (SELECT sku FROM ad_performance) ads JOIN sales ON ads.sku = sales.sku"
    )
    assert "sales_records.shop_id = :shop_id" in scoped
    assert "ad_performance.shop_id = :shop_id" in scoped
    assert scoped.count(":shop_id") == 2


def test_shop_scope_recognizes_sibling_cte_reference():
    scoped = enforce_shop_scope(
        "WITH base AS (SELECT sku FROM sales_records), "
        "ranked AS (SELECT sku FROM base) SELECT sku FROM ranked"
    )
    assert "sales_records.shop_id = :shop_id" in scoped
    assert scoped.count(":shop_id") == 1


def test_shop_scope_covers_each_union_branch():
    scoped = enforce_shop_scope(
        "SELECT sku FROM sales_records UNION ALL SELECT sku FROM competitor_prices"
    )
    assert "sales_records.shop_id = :shop_id" in scoped
    assert "competitor_prices.shop_id = :shop_id" in scoped
    assert scoped.count(":shop_id") == 2


def test_shop_scope_preserves_existing_where_and_uses_placeholder():
    scoped = enforce_shop_scope("SELECT sku FROM sales_records WHERE currency = 'USD'")
    assert "currency = 'USD' AND sales_records.shop_id = :shop_id" in scoped
    assert "amazon-us" not in scoped


@pytest.mark.parametrize("sql", [
    "SELECT * FROM users",
    "SELECT * FROM sales_records JOIN users ON users.id = sales_records.id",
    "SELECT 1",
])
def test_shop_scope_fails_closed_for_unapproved_or_unscopable_queries(sql):
    with pytest.raises(PermissionError):
        enforce_shop_scope(sql)


def test_connector_executes_bound_shop_parameter_without_leakage():
    connector = DatabaseConnector("sqlite:///:memory:", "amazon-us")
    with connector.engine.begin() as conn:
        conn.execute(text("CREATE TABLE sales_records (shop_id TEXT, sku TEXT)"))
        conn.execute(text("INSERT INTO sales_records VALUES ('amazon-us', 'A'), ('tiktok-uk', 'B')"))
    try:
        assert connector.execute_query("SELECT sku FROM sales_records") == [{"sku": "A"}]
        assert connector.preview_table("sales_records", 5)[0]["shop_id"] == "amazon-us"
    finally:
        connector.dispose()


def test_connector_rejects_write_and_unapproved_table():
    connector = DatabaseConnector("sqlite:///:memory:", "amazon-us")
    try:
        with pytest.raises(PermissionError):
            connector.execute_query("DROP TABLE sales_records")
        with pytest.raises(ValueError):
            connector.preview_table("users", 5)
    finally:
        connector.dispose()


@pytest.mark.parametrize("max_rows", [0, -1, True, 1.5])
def test_max_rows_must_be_positive_integer(max_rows):
    with pytest.raises(ValueError):
        sanitize_sql("SELECT * FROM sales_records", max_rows=max_rows)
