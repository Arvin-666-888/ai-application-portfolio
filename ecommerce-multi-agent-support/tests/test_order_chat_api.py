from conftest import login_headers


def test_chat_order_route_returns_owned_facts(client):
    headers = login_headers(client, "demo_user_01")
    response = client.post(
        "/api/v1/chat/preview",
        headers=headers,
        json={
            "message": "订单 VLT-2026-0001 到哪里了",
            "session_id": "order-session",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["route"] == "logistics_tracking"
    assert data["order_id"] == "VLT-2026-0001"
    assert data["order_facts"]["order_no"] == "VLT-2026-0001"
    assert data["shipment_facts"]["order_no"] == "VLT-2026-0001"
    assert data["tool_trace"][0]["tool"] == "get_order_status"


def test_chat_order_route_blocks_cross_user_access(client):
    headers = login_headers(client, "demo_user_01")
    response = client.post(
        "/api/v1/chat/preview",
        headers=headers,
        json={
            "message": "订单 VLT-2026-0002 到哪里了",
            "session_id": "order-session",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["route"] == "logistics_tracking"
    assert data["order_facts"] is None
    assert data["shipment_facts"] is None
    assert data["answer"] == "订单不存在或无权访问，请检查订单号是否属于当前账号。"
    assert data["tool_trace"][0]["success"] is False


def test_chat_order_route_asks_for_missing_order_number(client):
    headers = login_headers(client, "demo_user_01")
    response = client.post(
        "/api/v1/chat/preview",
        headers=headers,
        json={
            "message": "我的订单到哪里了",
            "session_id": "order-session",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["route"] == "logistics_tracking"
    assert data["order_id"] is None
    assert data["tool_trace"] == []
    assert "请提供" in data["answer"]
