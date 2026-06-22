import re

FORBIDDEN_KEYWORDS = [
    "DROP", "DELETE", "INSERT", "UPDATE", "ALTER",
    "CREATE", "TRUNCATE", "REPLACE", "GRANT", "REVOKE",
    "EXEC", "EXECUTE", "INTO OUTFILE", "LOAD_FILE",
    "INFORMATION_SCHEMA", "SLEEP", "BENCHMARK",
]


def validate_sql(sql: str) -> tuple[bool, str]:
    sql_upper = sql.upper().strip()

    if not sql_upper.startswith("SELECT"):
        return False, "只允许 SELECT 查询"

    if sql_upper.startswith("SELECT INTO"):
        return False, "不允许 SELECT INTO"

    for keyword in FORBIDDEN_KEYWORDS:
        pattern = r'\b' + keyword + r'\b'
        if re.search(pattern, sql_upper):
            return False, f"SQL 包含禁止的关键字: {keyword}"

    if ";" in sql.rstrip(";"):
        return False, "不允许执行多条 SQL 语句"

    if re.search(r'--|/\*|\*/', sql):
        return False, "不允许包含 SQL 注释"

    return True, "验证通过"


def sanitize_sql(sql: str, max_rows: int = 1000) -> str:
    sql = sql.strip()
    if sql.endswith(";"):
        sql = sql[:-1].strip()

    if "LIMIT" not in sql.upper():
        sql += f" LIMIT {max_rows}"

    return sql


def validate_table_name(name: str) -> bool:
    return bool(re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name))
