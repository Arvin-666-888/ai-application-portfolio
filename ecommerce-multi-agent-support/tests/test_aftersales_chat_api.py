from sqlalchemy import select

from app.database import SessionLocal
from app.models.tables import OrderTable
from conftest import login_headers


def get_order_status(order_no: str) -> str:
    with SessionLocal() as db:
        return db.scalar(select(OrderTable.status).where(OrderTable.order_no == order_no))


def test_chat_aftersales_proposes_but_does_not_execute_refund(client):
    headers = login_headers(client, "demo_user_03")
    before = get_order_status("VLT-2026-0015")

    response = client.post(
        "/api/v1/chat/preview",
        headers=headers,
        json={
            "message": "订单 VLT-2026-0015 的商品破损了，我要退款",
            "session_id": "aftersales-session",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["route"] == "aftersales_handling"
    assert data["dispatched_to"] == "aftersales_handling"
    assert data["issue_type"] == "damaged"
    assert data["proposed_action"] == "refund_review"
    assert data["requires_approval"] is True
    assert data["policy_result"]["policy_code"] == "V1-DAMAGED"
    assert get_order_status("VLT-2026-0015") == before


def test_chat_aftersales_cross_user_access_stops_before_policy(client):
    headers = login_headers(client, "demo_user_01")
    response = client.post(
        "/api/v1/chat/preview",
        headers=headers,
        json={
            "message": "订单 VLT-2026-0015 的商品破损了，我要退款",
            "session_id": "aftersales-session",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["order_facts"] is None
    assert data["shipment_facts"] is None
    assert data["policy_result"] is None
    assert data["requires_approval"] is False
    assert [item["tool"] for item in data["tool_trace"]] == ["get_order_status"]


def test_address_change_proposes_approval_without_storing_address(client):
    sensitive_address = "221B Baker Street, London NW1 6XE"
    headers = login_headers(client, "demo_user_01")
    before = get_order_status("VLT-2026-0001")
    response = client.post(
        "/api/v1/chat",
        headers=headers,
        json={
            "message": f"订单 VLT-2026-0001 修改收货地址为 {sensitive_address}",
            "session_id": "address-change",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["route"] == "aftersales_handling"
    assert data["issue_type"] == "address_change"
    assert data["proposed_action"] == "address_change_review"
    assert data["requires_approval"] is True
    assert get_order_status("VLT-2026-0001") == before
    assert sensitive_address not in str(data["tool_trace"])

    audit = client.get("/api/v1/chat/audits", headers=headers).json()[0]
    assert sensitive_address not in str(audit)
