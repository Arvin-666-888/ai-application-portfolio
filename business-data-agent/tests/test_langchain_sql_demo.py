import pytest

pytest.importorskip("langchain_core")

from examples.langchain_sql_agent_demo import run_offline_demo


def test_langchain_sql_agent_offline_demo_uses_schema_and_sql_tools():
    result = run_offline_demo("2024年每月收入趋势如何？", database_path=None)
    tools = [item["tool"] for item in result["tool_trace"]]
    assert tools[:2] == ["get_schema", "execute_sql"]
    assert "revenue_records" in result["sql_query"]


def test_langchain_sql_agent_blocks_dangerous_sql():
    result = run_offline_demo("2024年每月收入趋势如何？", database_path=None)
    blocked = result["dangerous_sql_check"]
    assert blocked["blocked"] is True
    assert "DROP" in blocked["sql"]
