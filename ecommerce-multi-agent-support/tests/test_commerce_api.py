from sqlalchemy import func, select

from conftest import login_headers


def test_user_can_read_owned_order_and_shipment(client):
    headers = login_headers(client, "demo_user_01")
    order = client.get("/api/v1/orders/VLT-2026-0001", headers=headers)
    shipment = client.get("/api/v1/orders/VLT-2026-0001/shipment", headers=headers)

    assert order.status_code == 200
    assert order.json()["order_no"] == "VLT-2026-0001"
    assert shipment.status_code == 200
    assert shipment.json()["order_no"] == "VLT-2026-0001"


def test_user_cannot_read_another_users_order(client):
    from app.database import SessionLocal
    from app.models.tables import AuditLogTable

    headers = login_headers(client, "demo_user_01")
    response = client.get("/api/v1/orders/VLT-2026-0002", headers=headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Order not found or access denied"
    with SessionLocal() as db:
        failed_audits = db.scalar(
            select(func.count(AuditLogTable.id)).where(AuditLogTable.success.is_(False))
        )
    assert failed_audits == 1


def test_product_search_uses_repository_contract(client):
    headers = login_headers(client, "demo_user_01")
    response = client.get(
        "/api/v1/products",
        headers=headers,
        params={"category": "charger", "max_price": "300", "limit": 5},
    )
    assert response.status_code == 200
    assert 1 <= len(response.json()) <= 5
    assert all(item["category"] == "charger" for item in response.json())
