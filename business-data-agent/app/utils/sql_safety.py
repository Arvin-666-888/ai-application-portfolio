import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

FORBIDDEN_KEYWORDS = [
    "DROP", "DELETE", "INSERT", "UPDATE", "ALTER",
    "CREATE", "TRUNCATE", "REPLACE", "GRANT", "REVOKE",
    "EXEC", "EXECUTE", "INTO OUTFILE", "LOAD_FILE",
    "INFORMATION_SCHEMA", "SLEEP", "BENCHMARK",
]

_FORBIDDEN_FUNCTIONS = {
    "GET_LOCK",
    "IS_FREE_LOCK",
    "IS_USED_LOCK",
    "LAST_INSERT_ID",
    "MASTER_POS_WAIT",
    "RELEASE_ALL_LOCKS",
    "RELEASE_LOCK",
    "SERVICE_GET_READ_LOCKS",
    "SERVICE_RELEASE_LOCKS",
}

_FORBIDDEN_AST_TYPES = (
    exp.DDL,
    exp.DML,
    exp.Grant,
    exp.Revoke,
    exp.Into,
    exp.Lock,
    exp.LockingStatement,
    exp.Analyze,
    exp.Export,
    exp.LoadData,
    exp.Cache,
    exp.Uncache,
)


def _keyword_pattern(keyword: str) -> str:
    parts = [re.escape(part) for part in keyword.split()]
    return rf"\b{'\\s+'.join(parts)}\b"


def _has_comments(statement: exp.Expression) -> bool:
    return any(node.comments for node in statement.walk())


def _has_star_projection(statement: exp.Expression) -> bool:
    if not isinstance(statement, exp.Select):
        return False

    return any(
        isinstance(projection, exp.Star)
        or (
            isinstance(projection, exp.Column)
            and isinstance(projection.this, exp.Star)
        )
        for projection in statement.expressions
    )


def _requires_limit(statement: exp.Expression) -> bool:
    if _has_star_projection(statement):
        return True
    return isinstance(statement, (exp.Select, exp.SetOperation))


def _sql_without_string_literals(
    statement: exp.Expression,
    dialect: str | None,
) -> str:
    def mask_strings(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Literal) and node.is_string:
            return exp.Literal.string("")
        return node

    return statement.copy().transform(mask_strings).sql(
        dialect=dialect,
        comments=False,
    )


def _run_keyword_guard(statement: exp.Expression, dialect: str | None) -> None:
    sql_for_scan = _sql_without_string_literals(statement, dialect)
    for keyword in FORBIDDEN_KEYWORDS:
        if re.search(_keyword_pattern(keyword), sql_for_scan, flags=re.IGNORECASE):
            raise PermissionError(f"SQL 包含禁止的关键字: {keyword}")


def _run_side_effect_guard(statement: exp.Expression) -> None:
    for node in statement.walk():
        if isinstance(node, exp.PropertyEQ):
            raise PermissionError("SQL 包含禁止的会话变量赋值")
        if isinstance(node, exp.Anonymous) and node.name.upper() in _FORBIDDEN_FUNCTIONS:
            raise PermissionError(f"SQL 包含有副作用的函数: {node.name.upper()}")


def _enforce_limit(statement: exp.Expression, max_rows: int) -> exp.Expression:
    if not _requires_limit(statement):
        return statement

    limit = statement.args.get("limit")
    if limit is None:
        return statement.limit(max_rows)

    limit_expression = limit.expression
    if not isinstance(limit_expression, exp.Literal) or not limit_expression.is_int:
        raise PermissionError("LIMIT 必须是可验证的整数")
    if int(limit_expression.this) > max_rows:
        return statement.limit(max_rows, copy=False)
    return statement


def parse_and_guard_sql(
    sql: str,
    max_rows: int | None = None,
    dialect: str | None = None,
) -> str:
    if not isinstance(sql, str) or not sql.strip():
        raise PermissionError("SQL 不能为空")
    if max_rows is not None and (isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows <= 0):
        raise ValueError("max_rows 必须是正整数")

    try:
        parsed = sqlglot.parse(sql, read=dialect)
    except (ParseError, ValueError, TypeError) as exc:
        raise PermissionError(f"SQL 无法安全解析: {exc}") from exc

    statements = [statement for statement in parsed if statement is not None]
    effective_statements = [
        statement for statement in statements if not isinstance(statement, exp.Semicolon)
    ]
    if len(effective_statements) != 1 or len(statements) != 1:
        raise PermissionError("不允许执行多条 SQL 语句")

    statement = effective_statements[0]
    if not isinstance(statement, (exp.Select, exp.SetOperation)):
        raise PermissionError("只允许只读 SELECT 查询")

    forbidden_node = next(
        (node for node in statement.walk() if isinstance(node, _FORBIDDEN_AST_TYPES)),
        None,
    )
    if forbidden_node is not None:
        raise PermissionError(f"SQL 包含禁止的操作: {type(forbidden_node).__name__}")

    if _has_comments(statement):
        raise PermissionError("不允许包含 SQL 注释")

    _run_keyword_guard(statement, dialect)
    _run_side_effect_guard(statement)

    if max_rows is not None:
        statement = _enforce_limit(statement, max_rows)

    return statement.sql(dialect=dialect, comments=False)


def validate_sql(sql: str, dialect: str | None = None) -> tuple[bool, str]:
    try:
        parse_and_guard_sql(sql, dialect=dialect)
    except (PermissionError, ValueError) as exc:
        return False, str(exc)
    return True, "验证通过"


def sanitize_sql(
    sql: str,
    max_rows: int = 1000,
    dialect: str | None = None,
) -> str:
    return parse_and_guard_sql(sql, max_rows=max_rows, dialect=dialect)


def validate_table_name(name: str) -> bool:
    return bool(re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name))
