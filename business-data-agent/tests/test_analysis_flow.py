from tests.conftest import auth_headers, create_sample_datasource


def test_mock_analysis_returns_sql_data_and_tool_trace(client):
    headers = auth_headers(client)
    ds_id = create_sample_datasource(client, headers)

    response = client.post(
        "/api/analysis/ask",
        headers=headers,
        json={"ds_id": ds_id, "question": "2024 年每月收入趋势如何？"},
    )

    assert response.status_code == 200
    body = response.json()
    assert "revenue_records" in body["sql_query"]
    assert len(body["data"]) == 12
    assert [item["tool"] for item in body["tool_trace"]] == ["get_schema", "execute_sql"]
    assert body["tool_trace"][1]["success"] is True


def test_analysis_record_and_report_include_trace(client):
    headers = auth_headers(client, username="report_user")
    ds_id = create_sample_datasource(client, headers)

    ask_response = client.post(
        "/api/analysis/ask",
        headers=headers,
        json={"ds_id": ds_id, "question": "2024 年收入趋势"},
    )
    record_id = ask_response.json()["id"]

    records_response = client.get("/api/analysis/records", headers=headers)
    assert records_response.status_code == 200
    assert records_response.json()[0]["tool_count"] >= 2

    report_response = client.get(f"/api/analysis/export/report/{record_id}", headers=headers)
    assert report_response.status_code == 200
    report = report_response.content.decode("utf-8")
    assert "## Agent 工具调用轨迹" in report
    assert "execute_sql" in report


def test_mock_analysis_supports_product_line_gross_margin(client):
    headers = auth_headers(client, username="gross_margin_user")
    ds_id = create_sample_datasource(client, headers)

    response = client.post(
        "/api/analysis/ask",
        headers=headers,
        json={"ds_id": ds_id, "question": "各产品线毛利率是多少？"},
    )

    assert response.status_code == 200
    body = response.json()
    assert "product_line" in body["sql_query"]
    assert "gross_margin" in body["sql_query"]
    assert len(body["data"]) == 3
    assert {row["product_line"] for row in body["data"]} == {"云订阅服务", "数据分析平台", "智能终端"}


def test_datasource_isolation_between_users(client):
    owner_headers = auth_headers(client, username="owner_user")
    other_headers = auth_headers(client, username="other_user")
    ds_id = create_sample_datasource(client, owner_headers)

    response = client.get(f"/api/datasources/{ds_id}/schema", headers=other_headers)

    assert response.status_code == 404


def test_tool_executor_uses_connector_dialect_for_mysql_sql(monkeypatch):
    from app.services.agent_service import ToolExecutor

    class Dialect:
        name = "mysql"

    class Engine:
        dialect = Dialect()

    class Connector:
        engine = Engine()

        def execute_query(self, sql):
            assert "`amount`" in sql
            assert "`orders`" in sql
            return [{"amount": 1}]

    executor = ToolExecutor(connector=Connector())
    result = executor._execute_sql("SELECT `amount` FROM `orders`")

    assert "查询返回 1 行数据" in result
    assert "`amount`" in executor.last_sql


def test_agent_fallback_answer_uses_existing_sql_result():
    from app.services.agent_service import ToolExecutor, _build_fallback_answer
    from app.config import settings

    executor = ToolExecutor(connector=None)
    executor.last_sql = "SELECT record_month, SUM(revenue) AS total_revenue FROM revenue_records GROUP BY record_month"
    executor.last_query_result = [
        {"record_month": "2024-01", "total_revenue": 100.0},
        {"record_month": "2024-02", "total_revenue": 130.0},
    ]

    answer = _build_fallback_answer("2024 年每月收入趋势如何？", executor)

    assert "分析步骤过多" not in answer
    assert f"{settings.MAX_AGENT_STEPS} 轮工具调用" in answer
    assert "SQL 返回 2 行" in answer
    assert "上升趋势" in answer


def test_real_model_tool_policy_limits_preview_usage():
    from app.config import settings
    from app.services.agent_service import REAL_MODEL_TOOL_POLICY, _bounded_preview_rows, _tools_for_real_model

    assert settings.MAX_AGENT_STEPS == 8
    assert "get_schema -> execute_sql -> 最终回答" in REAL_MODEL_TOOL_POLICY
    assert "不要为简单趋势" in REAL_MODEL_TOOL_POLICY

    preview_tool = next(
        tool["function"]
        for tool in _tools_for_real_model()
        if tool["function"]["name"] == "preview_table"
    )

    assert "不要在简单趋势" in preview_tool["description"]
    assert preview_tool["parameters"]["properties"]["rows"]["maximum"] == settings.MAX_PREVIEW_ROWS
    assert _bounded_preview_rows(10) == settings.MAX_PREVIEW_ROWS
    assert _bounded_preview_rows("bad") == settings.MAX_PREVIEW_ROWS
    assert _bounded_preview_rows(-5) == 1
