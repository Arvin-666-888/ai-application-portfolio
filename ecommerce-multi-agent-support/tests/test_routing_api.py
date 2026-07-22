from conftest import login_headers


def test_routing_preview_requires_authentication(client):
    response = client.post(
        "/api/v1/routing/preview",
        json={"message": "推荐一个充电器", "session_id": "session-1"},
    )
    assert response.status_code == 401


def test_routing_preview_dispatches_aftersales(client):
    headers = login_headers(client, "demo_user_01")
    response = client.post(
        "/api/v1/routing/preview",
        headers=headers,
        json={
            "message": "订单 VLT-2026-0001 的包裹坏了，我要退款",
            "session_id": "session-1",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["route"] == "aftersales"
    assert data["dispatched_to"] == "aftersales"
    assert data["route_source"] == "rule_fallback"
