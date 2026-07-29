import pytest
from sqlalchemy import text

from app.utils.db_connector import DatabaseConnector
from app.utils.sql_safety import (
    parse_and_guard_sql,
    sanitize_sql,
    validate_sql,
    validate_table_name,
)


def test_select_query_is_allowed_and_limited():
    is_valid, message = validate_sql("SELECT record_month, revenue FROM revenue_records")

    assert is_valid is True
    assert message == "验证通过"
    assert sanitize_sql("SELECT * FROM revenue_records", max_rows=20).endswith("LIMIT 20")


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM revenue_records",
        "SELECT * FROM revenue_records; DROP TABLE revenue_records",
        "SELECT * FROM revenue_records -- bypass",
        "SELECT * FROM information_schema.tables",
        "UPDATE revenue_records SET revenue = 0",
        "INSERT INTO revenue_records(record_month) VALUES ('2024-01')",
        "CREATE TABLE copied AS SELECT * FROM revenue_records",
        "ALTER TABLE revenue_records ADD COLUMN note TEXT",
        "TRUNCATE TABLE revenue_records",
        "EXPLAIN DELETE FROM revenue_records",
    ],
)
def test_dangerous_sql_is_rejected_by_compatible_validator(sql):
    is_valid, message = validate_sql(sql)

    assert is_valid is False
    assert message


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM revenue_records; /* harmless */ DROP TABLE users",
        "SELECT * FROM revenue_records -- comment\n; DELETE FROM revenue_records",
        "DR/**/OP TABLE users",
    ],
)
def test_comment_bypass_attempts_raise_permission_error(sql):
    with pytest.raises(PermissionError):
        parse_and_guard_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM users WHERE 1=1",
        "UPDATE users SET username = 'x' WHERE 1=1",
    ],
)
def test_write_operations_with_tautology_raise_permission_error(sql):
    with pytest.raises(PermissionError):
        sanitize_sql(sql)


def test_read_only_tautology_is_allowed_and_limited():
    safe_sql = sanitize_sql("SELECT * FROM revenue_records WHERE 1=1", max_rows=25)

    assert "WHERE 1 = 1" in safe_sql
    assert safe_sql.endswith("LIMIT 25")


@pytest.mark.parametrize(
    ("sql", "expected_suffix"),
    [
        ("SELECT * FROM revenue_records", "LIMIT 1000"),
        ("SELECT revenue FROM revenue_records", "LIMIT 1000"),
        ("SELECT COUNT(*) FROM revenue_records", "LIMIT 1000"),
        ("SELECT revenue_records.* FROM revenue_records", "LIMIT 1000"),
        ("SELECT * FROM revenue_records LIMIT 20", "LIMIT 20"),
        ("SELECT * FROM revenue_records LIMIT 999999", "LIMIT 1000"),
    ],
)
def test_ast_limit_policy(sql, expected_suffix):
    safe_sql = sanitize_sql(sql)

    assert safe_sql.endswith(expected_suffix)
    assert safe_sql.count("LIMIT") == 1


def test_count_star_is_not_classified_as_star_projection(monkeypatch):
    from app.utils import sql_safety

    observed = []
    original = sql_safety._has_star_projection

    def capture(statement):
        result = original(statement)
        observed.append(result)
        return result

    monkeypatch.setattr(sql_safety, "_has_star_projection", capture)

    sanitize_sql("SELECT COUNT(*) FROM revenue_records")

    assert observed == [False]


def test_qualified_star_is_classified_as_star_projection(monkeypatch):
    from app.utils import sql_safety

    observed = []
    original = sql_safety._has_star_projection

    def capture(statement):
        result = original(statement)
        observed.append(result)
        return result

    monkeypatch.setattr(sql_safety, "_has_star_projection", capture)

    sanitize_sql("SELECT revenue_records.* FROM revenue_records")

    assert observed == [True]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "SELECT * FROM revenue_records; DROP TABLE users",
    ],
)
def test_multiple_statements_raise_permission_error(sql):
    with pytest.raises(PermissionError):
        parse_and_guard_sql(sql)


def test_single_statement_with_trailing_semicolon_is_allowed():
    assert sanitize_sql("SELECT * FROM revenue_records;").endswith("LIMIT 1000")


def test_valid_cte_select_is_allowed():
    sql = """
        WITH monthly AS (
            SELECT record_month, revenue FROM revenue_records
        )
        SELECT * FROM monthly
    """

    safe_sql = sanitize_sql(sql)

    assert safe_sql.startswith("WITH monthly AS")
    assert safe_sql.endswith("LIMIT 1000")


def test_dangerous_statement_nested_in_cte_is_rejected():
    sql = "WITH removed AS (DELETE FROM users RETURNING *) SELECT * FROM removed"

    with pytest.raises(PermissionError, match="Delete"):
        parse_and_guard_sql(sql)


def test_dangerous_keyword_inside_string_literal_is_allowed():
    safe_sql = sanitize_sql("SELECT 'DROP TABLE users' AS note")

    assert "DROP TABLE users" in safe_sql
    assert safe_sql.endswith("LIMIT 1000")


def test_sanitize_sql_rejects_invalid_sql_instead_of_rewriting_text():
    with pytest.raises(PermissionError):
        sanitize_sql("this is not sql")


@pytest.mark.parametrize("max_rows", [0, -1, True, 1.5])
def test_max_rows_must_be_a_positive_integer(max_rows):
    with pytest.raises(ValueError):
        sanitize_sql("SELECT * FROM revenue_records", max_rows=max_rows)


def test_mysql_quoted_identifiers_are_preserved():
    safe_sql = sanitize_sql(
        "SELECT `amount` FROM `orders`",
        max_rows=10,
        dialect="mysql",
    )

    assert "`amount`" in safe_sql
    assert "`orders`" in safe_sql
    assert safe_sql.endswith("LIMIT 10")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT GET_LOCK('agent-lock', 1)",
        "SELECT RELEASE_LOCK('agent-lock')",
        "SELECT LAST_INSERT_ID(123)",
        "SELECT @x := 1",
    ],
)
def test_mysql_selects_with_side_effects_are_rejected(sql):
    with pytest.raises(PermissionError):
        sanitize_sql(sql, dialect="mysql")


def test_dynamic_limit_is_rejected_when_row_cap_cannot_be_verified():
    with pytest.raises(PermissionError, match="LIMIT"):
        sanitize_sql("SELECT * FROM revenue_records LIMIT ?", max_rows=100)


@pytest.mark.parametrize("rows", [0, 101, True])
def test_preview_row_limit_is_bounded(rows):
    connector = DatabaseConnector("sqlite:///:memory:")
    try:
        with pytest.raises(ValueError, match="1 到 100"):
            connector.preview_table("revenue_records", rows)
    finally:
        connector.dispose()


def test_database_execution_boundary_rejects_write_sql():
    connector = DatabaseConnector("sqlite:///:memory:")
    with connector.engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))

    try:
        with pytest.raises(PermissionError):
            connector.execute_query("DROP TABLE users")

        with connector.engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM users")).scalar_one() == 0
    finally:
        connector.dispose()


@pytest.mark.parametrize("table_name", ["revenue_records", "budget_2024", "_tmp"])
def test_safe_table_names_are_allowed(table_name):
    assert validate_table_name(table_name) is True


@pytest.mark.parametrize("table_name", ["revenue-records", "users;drop", "../users", "1table"])
def test_unsafe_table_names_are_rejected(table_name):
    assert validate_table_name(table_name) is False
