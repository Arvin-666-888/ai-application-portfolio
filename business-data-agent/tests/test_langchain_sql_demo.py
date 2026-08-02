from pathlib import Path

import pytest

pytest.importorskip("langchain_core")

from app.config import settings
from app.main import _init_sample_data
from examples.langchain_sql_agent_demo import run_offline_demo


def _run(question: str):
    _init_sample_data()
    return run_offline_demo(question, database_path=Path(settings.SAMPLE_DB_PATH))


def test_langchain_sql_agent_offline_demo_uses_scoped_ad_tools():
    result = _run("2026年广告ROAS和ROI趋势如何？")
    tools = [item["tool"] for item in result["tool_trace"]]
    assert tools[:2] == ["get_schema", "execute_sql"]
    assert "ad_performance" in result["sql_query"]
    assert {row["platform"] for row in result["data_preview"]["rows"]} == {"Amazon"}


def test_langchain_sql_agent_blocks_dangerous_sql():
    result = _run("库存周转如何？")
    blocked = result["dangerous_sql_check"]
    assert blocked["blocked"] is True
    assert "DROP" in blocked["sql"]
