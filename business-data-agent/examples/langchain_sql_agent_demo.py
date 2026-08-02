"""LangChain comparison demo for the business data Agent project.

The production demo in app/services/agent_service.py uses a hand-written
Function Calling loop. This script wraps the same database capabilities as
LangChain tools, so the two approaches can be compared in interviews.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from langchain_core.tools import tool
except ImportError as exc:  # pragma: no cover - exercised by users without optional deps
    raise SystemExit(
        "Missing LangChain optional dependencies. Install them with:\n"
        "  pip install -r requirements-langchain.txt"
    ) from exc

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.utils.db_connector import DatabaseConnector  # noqa: E402
from app.utils.sql_safety import sanitize_sql, validate_sql  # noqa: E402


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class LangChainSqlDemo:
    def __init__(self, database_path: Path, shop_id: str = "amazon-us"):
        self.connector = DatabaseConnector(f"sqlite:///{database_path.as_posix()}", shop_id)
        self.tool_trace: list[dict] = []

    def record(self, name: str, arguments: dict, result: object, success: bool = True) -> None:
        preview = json.dumps(result, ensure_ascii=False, default=str)
        self.tool_trace.append({
            "step": len(self.tool_trace) + 1,
            "tool": name,
            "arguments": arguments,
            "success": success,
            "result_preview": preview[:500],
        })

    def build_tools(self):
        demo = self

        @tool
        def list_tables() -> str:
            """List available database tables."""
            result = demo.connector.get_tables()
            demo.record("list_tables", {}, result)
            return json.dumps(result, ensure_ascii=False)

        @tool
        def get_schema() -> str:
            """Return table and column schema for the business database."""
            result = demo.connector.get_schema()
            demo.record("get_schema", {}, result)
            return json.dumps(result, ensure_ascii=False, default=str)

        @tool
        def execute_sql(sql: str) -> str:
            """Execute a safe read-only SELECT query and return rows as JSON."""
            ok, reason = validate_sql(sql)
            if not ok:
                result = {"blocked": True, "reason": reason, "sql": sql}
                demo.record("execute_sql", {"sql": sql}, result, success=False)
                return json.dumps(result, ensure_ascii=False)

            safe_sql = sanitize_sql(sql, max_rows=1000)
            rows = demo.connector.execute_query(safe_sql)
            result = {"sql": safe_sql, "rows": rows}
            demo.record("execute_sql", {"sql": sql}, result)
            return json.dumps(result, ensure_ascii=False, default=str)

        return [list_tables, get_schema, execute_sql]


def choose_offline_sql(question: str) -> str | None:
    if any(word in question.upper() for word in ["ROAS", "ROI", "广告"]):
        return """
        SELECT report_date, platform, marketplace, currency,
               SUM(attributed_sales) / NULLIF(SUM(ad_spend), 0) AS roas,
               (SUM(attributed_sales) - SUM(attributed_refunds) -
                SUM(attributed_platform_fees) - SUM(attributed_cogs) - SUM(ad_spend)) /
               NULLIF(SUM(ad_spend), 0) AS ad_roi
        FROM ad_performance
        GROUP BY report_date, platform, marketplace, currency
        ORDER BY report_date
        """
    if any(word in question for word in ["库存", "周转"]):
        return """
        SELECT sku, product_name, currency,
               average_inventory_units_30d,
               trailing_30d_units_sold / NULLIF(average_inventory_units_30d, 0) AS inventory_turnover_rate_30d,
               30.0 * average_inventory_units_30d / NULLIF(trailing_30d_units_sold, 0) AS inventory_turnover_days
        FROM inventory_snapshots
        ORDER BY inventory_turnover_days
        """
    return None


def check_dangerous_sql(tools) -> dict:
    execute_sql = next(item for item in tools if item.name == "execute_sql")
    raw = execute_sql.invoke({"sql": "DROP TABLE ad_performance"})
    return json.loads(raw)


def run_offline_demo(question: str, database_path: Path | None) -> dict:
    database_path = database_path or (PROJECT_ROOT / "sample_data" / "sample.db")
    demo = LangChainSqlDemo(database_path)
    tools = demo.build_tools()
    get_schema = next(item for item in tools if item.name == "get_schema")
    execute_sql = next(item for item in tools if item.name == "execute_sql")

    schema = get_schema.invoke({})
    sql = choose_offline_sql(question)
    data = None
    answer = "[LangChain mock回答] 已用 LangChain @tool 封装 get_schema，但该问题没有命中内置 SQL 示例。"
    if sql:
        data_raw = execute_sql.invoke({"sql": sql})
        data = json.loads(data_raw)
        row_count = len(data.get("rows", []))
        answer = (
            f"[LangChain mock回答] 问题：{question}\n"
            f"工具链：get_schema -> execute_sql。查询返回 {row_count} 行。"
            "真实模型编排可在配置 API_KEY 后使用 create_agent 演示。"
        )

    dangerous_check = check_dangerous_sql(tools)
    return {
        "mode": "mock",
        "question": question,
        "answer": answer,
        "schema_preview": schema[:500],
        "sql_query": sanitize_sql(sql, max_rows=1000) if sql else "",
        "data_preview": data,
        "dangerous_sql_check": dangerous_check,
        "tool_trace": demo.tool_trace,
    }


def run_real_agent(question: str, database_path: Path) -> dict:
    load_env_file(PROJECT_ROOT / ".env")
    api_key = os.getenv("API_KEY", "")
    if not api_key:
        return run_offline_demo(question, database_path)

    try:
        from langchain.agents import create_agent
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("Install langchain and langchain-openai to use create_agent real mode.") from exc

    database_path = database_path or (PROJECT_ROOT / "sample_data" / "sample.db")
    demo = LangChainSqlDemo(database_path)
    tools = demo.build_tools()
    llm = ChatOpenAI(
        api_key=api_key,
        base_url=os.getenv("BASE_URL", "https://api.openai.com/v1"),
        model=os.getenv("MODEL", "gpt-4o-mini"),
        temperature=0,
    )
    agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=(
            "你是跨境电商经营数据分析 Agent。必须先查看 schema，再只生成 SELECT SQL。"
            "金额按 currency 分组，禁止跨币种直接聚合；shop_id 由服务端强制施加。"
        ),
    )
    result = agent.invoke({"messages": [{"role": "user", "content": question}]})
    messages = result.get("messages", []) if isinstance(result, dict) else []
    final_answer = str(messages[-1].content) if messages else str(result)
    return {
        "mode": "real_llm",
        "question": question,
        "answer": final_answer,
        "tool_trace": demo.tool_trace,
        "dangerous_sql_check": check_dangerous_sql(tools),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the LangChain comparison SQL Agent demo.")
    parser.add_argument("--question", default="2026年广告ROAS和ROI趋势如何？")
    parser.add_argument("--mock", action="store_true", help="Force offline tool demo mode.")
    parser.add_argument("--database", default=str(PROJECT_ROOT / "sample_data" / "sample.db"))
    args = parser.parse_args()

    database_path = Path(args.database).resolve()
    if args.mock:
        result = run_offline_demo(args.question, database_path)
    else:
        result = run_real_agent(args.question, database_path)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
