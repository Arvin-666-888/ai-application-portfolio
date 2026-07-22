from decimal import Decimal

from conftest import login_headers


def test_chat_preview_requires_authentication(client):
    response = client.post(
        "/api/v1/chat/preview",
        json={"message": "推荐一个充电器", "session_id": "chat-session"},
    )
    assert response.status_code == 401


def test_chat_preview_executes_catalog_tool(client):
    headers = login_headers(client, "demo_user_01")
    response = client.post(
        "/api/v1/chat/preview",
        headers=headers,
        json={
            "message": "推荐一款 300 元以内的 65W 充电器",
            "session_id": "chat-session",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["route"] == "catalog"
    assert data["dispatched_to"] == "catalog"
    assert data["product_filters"]["category"] == "charger"
    assert data["product_filters"]["power_w"] == 65
    assert data["products"]
    assert all(Decimal(item["price"]) <= Decimal("300") for item in data["products"])
    assert all(item["specifications"]["power_w"] == 65 for item in data["products"])
    assert data["tool_trace"][0]["tool"] == "search_products"
    assert all(item["sku"] in data["answer"] for item in data["products"])


def test_chat_preview_calls_only_order_tool_for_order_route(client):
    headers = login_headers(client, "demo_user_01")
    response = client.post(
        "/api/v1/chat/preview",
        headers=headers,
        json={
            "message": "订单 VLT-2026-0001 到哪里了",
            "session_id": "chat-session",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["route"] == "order"
    assert [trace["tool"] for trace in data["tool_trace"]] == ["get_order_status"]
    assert data["products"] == []
    assert data["order_facts"]["order_no"] == "VLT-2026-0001"
