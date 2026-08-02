import jwt

from app.config import settings
from tests.conftest import auth_headers, create_sample_datasource


def test_mock_roas_analysis_returns_scoped_shop_data(client):
    headers = auth_headers(client, shop_id="amazon-us")
    ds_id = create_sample_datasource(client, headers)

    response = client.post(
        "/api/analysis/ask",
        headers=headers,
        json={"ds_id": ds_id, "question": "2026 年广告 ROAS 和 ROI 趋势如何？"},
    )

    assert response.status_code == 200
    body = response.json()
    assert "ad_performance" in body["sql_query"]
    assert "attributed_refunds" in body["sql_query"]
    assert "attributed_platform_fees" in body["sql_query"]
    assert "attributed_cogs" in body["sql_query"]
    assert len(body["data"]) == 2
    assert body["data"][0]["ad_roi"] != body["data"][0]["roas"] - 1
    assert {row["platform"] for row in body["data"]} == {"Amazon"}
    assert {row["currency"] for row in body["data"]} == {"USD"}
    assert [item["tool"] for item in body["tool_trace"]] == ["get_schema", "execute_sql"]


def test_product_selection_partitions_currency_rank_and_reports_actual_scope(client):
    headers = auth_headers(client, shop_id="amazon-us")
    ds_id = create_sample_datasource(client, headers)

    response = client.post(
        "/api/analysis/ask",
        headers=headers,
        json={"ds_id": ds_id, "question": "哪些商品的经营贡献更适合继续选品？"},
    )

    assert response.status_code == 200
    body = response.json()
    assert "PARTITION BY marketplace, currency" in body["sql_query"]
    assert {row["currency"] for row in body["data"]} == {"USD"}
    assert {row["marketplace"] for row in body["data"]} == {"US"}
    assert {row["timezone"] for row in body["data"]} == {"America/Los_Angeles"}
    assert {row["period_start"] for row in body["data"]} == {"2026-01-05", "2026-01-20"}
    assert {row["period_end"] for row in body["data"]} == {"2026-02-08", "2026-02-22"}
    assert [row["contribution_rank"] for row in body["data"]] == [1, 2]
    assert "marketplace、currency 分区排名" in body["answer"]
    assert "period_start、period_end、marketplace、currency 与 timezone" in body["answer"]


def test_inventory_analysis_record_and_exports_are_tenant_scoped(client):
    owner_headers = auth_headers(client, username="owner", shop_id="tiktok-uk")
    other_headers = auth_headers(client, username="other", shop_id="shopee-sg")
    ds_id = create_sample_datasource(client, owner_headers)

    ask_response = client.post(
        "/api/analysis/ask",
        headers=owner_headers,
        json={"ds_id": ds_id, "question": "库存周转和断货风险如何？"},
    )
    assert ask_response.status_code == 200
    body = ask_response.json()
    assert len(body["data"]) == 1
    assert "average_inventory_units_30d = 0" in body["sql_query"]
    assert "trailing_30d_units_sold = 0" in body["sql_query"]
    assert "is_stockout" in body["sql_query"]
    assert "is_inventory_unknown" in body["sql_query"]
    assert "on_hand_units IS NULL" in body["sql_query"]
    assert body["data"][0]["platform"] == "TikTok Shop"
    assert body["data"][0]["is_stockout"] == 0
    record_id = body["id"]

    records = client.get("/api/analysis/records", headers=owner_headers)
    assert records.status_code == 200
    assert records.json()[0]["shop_id"] == "tiktok-uk"
    assert records.json()[0]["tool_count"] == 2

    assert client.get(f"/api/analysis/records/{record_id}", headers=other_headers).status_code == 404
    assert client.get(f"/api/analysis/export/csv/{record_id}", headers=other_headers).status_code == 404
    assert client.get(f"/api/analysis/export/report/{record_id}", headers=other_headers).status_code == 404

    report = client.get(f"/api/analysis/export/report/{record_id}", headers=owner_headers)
    assert report.status_code == 200
    assert "execute_sql" in report.content.decode("utf-8")


def test_same_username_can_exist_in_different_shops_and_jwt_binds_shop(client):
    first = auth_headers(client, username="operator", shop_id="amazon-us")
    second = auth_headers(client, username="operator", shop_id="tiktok-uk")

    first_payload = jwt.decode(
        first["Authorization"].split()[1], settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
    )
    second_payload = jwt.decode(
        second["Authorization"].split()[1], settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
    )
    assert first_payload["shop_id"] == "amazon-us"
    assert second_payload["shop_id"] == "tiktok-uk"
    assert first_payload["sub"] != second_payload["sub"]


def test_invalid_jwt_subject_and_shop_claim_return_401(client):
    invalid_payloads = [
        {"sub": "not-an-integer", "shop_id": "amazon-us"},
        {"sub": "1", "shop_id": ["amazon-us"]},
    ]

    for payload in invalid_payloads:
        token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
        response = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "无效的Token"


def test_datasource_isolation_checks_user_and_shop(client):
    owner_headers = auth_headers(client, username="owner_user", shop_id="amazon-us")
    other_headers = auth_headers(client, username="other_user", shop_id="amazon-us")
    ds_id = create_sample_datasource(client, owner_headers)
    assert client.get(f"/api/datasources/{ds_id}/schema", headers=other_headers).status_code == 404


def test_agent_fallback_uses_roas_result():
    from app.services.agent_service import ToolExecutor, _build_fallback_answer

    executor = ToolExecutor(connector=None)
    executor.last_query_result = [
        {"report_date": "2026-01-31", "currency": "USD", "roas": 4.0},
        {"report_date": "2026-02-28", "currency": "USD", "roas": 4.5},
    ]
    answer = _build_fallback_answer("广告 ROAS 趋势", executor)
    assert "SQL 返回 2 行" in answer
    assert "roas 从 4.0 变化到 4.5" in answer
    assert "currency" in answer


def test_agent_fallback_uses_requested_roi_metric():
    from app.services.agent_service import ToolExecutor, _build_fallback_answer

    executor = ToolExecutor(connector=None)
    executor.last_query_result = [
        {"report_date": "2026-01-31", "currency": "USD", "roas": 4.0, "ad_roi": 1.2},
        {"report_date": "2026-02-28", "currency": "USD", "roas": 4.5, "ad_roi": 0.8},
    ]
    answer = _build_fallback_answer("广告 ROI 趋势", executor)
    assert "ad_roi 从 1.2 变化到 0.8" in answer
    assert "roas 从" not in answer


def test_inventory_fallback_handles_null_zero_and_stockout():
    from app.services.agent_service import ToolExecutor, _build_fallback_answer

    executor = ToolExecutor(connector=None)
    executor.last_query_result = [
        {"sku": "UNKNOWN", "inventory_turnover_days": None, "on_hand_units": None},
        {"sku": "NO-SALES", "inventory_turnover_days": None, "on_hand_units": 10},
        {"sku": "STOCKOUT", "inventory_turnover_days": 0.0, "on_hand_units": 0},
        {"sku": "NORMAL", "inventory_turnover_days": 5.0, "on_hand_units": 20},
    ]
    answer = _build_fallback_answer("库存周转", executor)
    assert "STOCKOUT，约 0.0 天" in answer
    assert "断货风险 SKU：STOCKOUT" in answer
    assert "Unknown inventory risk SKU：UNKNOWN" in answer


def test_agent_fallback_reports_actual_result_scope():
    from app.services.agent_service import ToolExecutor, _build_fallback_answer

    executor = ToolExecutor(connector=None)
    executor.last_query_result = [
        {
            "report_date": "2026-01-31",
            "marketplace": "US",
            "currency": "USD",
            "timezone": "America/Los_Angeles",
            "roas": 4.0,
        },
        {
            "report_date": "2026-02-28",
            "marketplace": "US",
            "currency": "USD",
            "timezone": "America/Los_Angeles",
            "roas": 4.5,
        },
    ]

    answer = _build_fallback_answer("广告 ROAS 趋势", executor)

    assert "时间 2026-01-31 至 2026-02-28" in answer
    assert "marketplace US" in answer
    assert "currency USD" in answer
    assert "timezone America/Los_Angeles" in answer


def test_agent_result_appends_actual_scope_once_for_mock_and_real_answers():
    from app.services.agent_service import ToolExecutor, _agent_result

    executor = ToolExecutor(connector=None)
    executor.last_query_result = [{
        "report_date": "2026-02-28",
        "marketplace": "US",
        "currency": "USD",
        "timezone": "America/Los_Angeles",
    }]

    result = _agent_result(executor, "模型回答")
    assert "2026-02-28" in result["answer"]
    assert "marketplace US" in result["answer"]
    assert "currency USD" in result["answer"]
    assert "timezone America/Los_Angeles" in result["answer"]

    repeated = _agent_result(executor, result["answer"])
    assert repeated["answer"].count("查询结果实际范围：") == 1


def test_business_aggregation_policy_rejects_cross_currency_amount_queries():
    from app.services.agent_service import _enforce_business_aggregation_policy

    invalid_queries = [
        "SELECT sku, SUM(gross_sales) AS sales FROM sales_records GROUP BY sku",
        "SELECT marketplace, currency, sku, RANK() OVER (ORDER BY operating_contribution DESC) "
        "FROM sales_records",
    ]
    for sql in invalid_queries:
        try:
            _enforce_business_aggregation_policy(sql, dialect="sqlite")
        except PermissionError:
            pass
        else:
            raise AssertionError(f"business aggregation policy should reject: {sql}")

    _enforce_business_aggregation_policy(
        "SELECT marketplace, currency, sku, SUM(gross_sales) AS sales "
        "FROM sales_records GROUP BY marketplace, currency, sku",
        dialect="sqlite",
    )
    _enforce_business_aggregation_policy(
        "SELECT marketplace, currency, sku, "
        "RANK() OVER (PARTITION BY marketplace, currency ORDER BY operating_contribution DESC) "
        "FROM sales_records",
        dialect="sqlite",
    )


def test_agent_prompt_defines_business_metrics_and_currency_boundary():
    from app.services.agent_service import AGENT_SYSTEM_PROMPT, REAL_MODEL_TOOL_POLICY, TOOLS

    assert "ROAS = attributed_sales / ad_spend" in AGENT_SYSTEM_PROMPT
    assert "attributed_refunds" in AGENT_SYSTEM_PROMPT
    assert "average_inventory_units_30d" in AGENT_SYSTEM_PROMPT
    assert "30 天库存周转率" in AGENT_SYSTEM_PROMPT
    assert "竞品价差" in AGENT_SYSTEM_PROMPT
    assert "禁止跨币种直接" in AGENT_SYSTEM_PROMPT
    assert "跨境电商经营知识库" in str(TOOLS)
    assert "平台规则、广告归因口径、选品标准、库存策略和竞品定价依据" in str(TOOLS)
    assert "get_schema -> execute_sql -> 最终回答" in REAL_MODEL_TOOL_POLICY
