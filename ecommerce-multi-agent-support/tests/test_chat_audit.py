from conftest import login_headers


def test_stable_chat_endpoint_persists_redacted_audit(client):
    headers = login_headers(client, "demo_user_01")
    message = "推荐一款 300 元以内的 65W 充电器"
    response = client.post(
        "/api/v1/chat",
        headers=headers,
        json={"message": message, "session_id": "audit-session"},
    )
    assert response.status_code == 200

    audits = client.get("/api/v1/chat/audits", headers=headers)
    assert audits.status_code == 200
    latest = audits.json()[0]
    assert latest["action"] == "multi_agent_chat"
    assert latest["result_summary"]["route"] == "catalog"
    assert latest["result_summary"]["tools"] == ["search_products"]
    assert latest["input_summary"]["message_length"] == len(message)
    assert message not in str(latest)


def test_audit_list_is_scoped_to_current_user(client):
    user_1 = login_headers(client, "demo_user_01")
    user_2 = login_headers(client, "demo_user_02")
    client.post(
        "/api/v1/chat",
        headers=user_1,
        json={"message": "订单 VLT-2026-0001 到哪里了", "session_id": "u1"},
    )

    user_2_audits = client.get("/api/v1/chat/audits", headers=user_2)
    assert user_2_audits.status_code == 200
    assert user_2_audits.json() == []


def test_aftersales_audit_records_approval_without_execution(client):
    headers = login_headers(client, "demo_user_03")
    response = client.post(
        "/api/v1/chat",
        headers=headers,
        json={
            "message": "订单 VLT-2026-0015 的商品破损了，我要退款",
            "session_id": "approval-audit",
        },
    )
    assert response.status_code == 200

    latest = client.get("/api/v1/chat/audits", headers=headers).json()[0]
    assert latest["result_summary"]["route"] == "aftersales"
    assert latest["result_summary"]["requires_approval"] is True
    assert latest["result_summary"]["proposed_action"] == "refund_review"


def test_legacy_preview_endpoint_remains_compatible(client):
    headers = login_headers(client, "demo_user_01")
    response = client.post(
        "/api/v1/chat/preview",
        headers=headers,
        json={"message": "推荐一个充电器", "session_id": "legacy"},
    )
    assert response.status_code == 200
    assert response.json()["route"] == "catalog"
