from sqlalchemy import func, select


def test_health_and_seed_counts(client):
    from app.database import SessionLocal
    from app.models.tables import OrderTable, ProductTable, ShipmentTable, UserTable

    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["version"] == "1.0.0"

    with SessionLocal() as db:
        assert db.scalar(select(func.count(UserTable.id))) == 12
        assert db.scalar(select(func.count(ProductTable.id))) == 60
        assert db.scalar(select(func.count(OrderTable.id))) == 100
        assert db.scalar(select(func.count(ShipmentTable.id))) == 100


def test_seed_is_idempotent(client):
    from app.config import settings
    from app.database import SessionLocal
    from app.services.seed_service import seed_demo_data

    with SessionLocal() as db:
        counts = seed_demo_data(db, seed=settings.DEMO_DATA_SEED)
    assert counts == {"users": 12, "products": 60, "orders": 100, "shipments": 100}
