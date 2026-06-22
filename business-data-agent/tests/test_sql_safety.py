import pytest

from app.utils.sql_safety import sanitize_sql, validate_sql, validate_table_name


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
    ],
)
def test_dangerous_sql_is_rejected(sql):
    is_valid, message = validate_sql(sql)

    assert is_valid is False
    assert message


@pytest.mark.parametrize("table_name", ["revenue_records", "budget_2024", "_tmp"])
def test_safe_table_names_are_allowed(table_name):
    assert validate_table_name(table_name) is True


@pytest.mark.parametrize("table_name", ["revenue-records", "users;drop", "../users", "1table"])
def test_unsafe_table_names_are_rejected(table_name):
    assert validate_table_name(table_name) is False
