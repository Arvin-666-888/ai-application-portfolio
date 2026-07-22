from decimal import Decimal


def test_catalog_repository_filters_products(client):
    from app.adapters.sqlite import SQLiteCatalogRepository
    from app.database import SessionLocal

    with SessionLocal() as db:
        products = SQLiteCatalogRepository(db).search(
            category="charger",
            max_price=Decimal("300"),
            limit=10,
        )
    assert products
    assert all(item.category == "charger" for item in products)
    assert all(item.price <= Decimal("300") for item in products)
    assert all(item.stock > 0 for item in products)


def test_order_repository_enforces_ownership(client):
    from app.adapters.sqlite import SQLiteOrderRepository
    from app.database import SessionLocal

    with SessionLocal() as db:
        repository = SQLiteOrderRepository(db)
        owned = repository.get_owned_order(order_no="VLT-2026-0001", user_id=1)
        blocked = repository.get_owned_order(order_no="VLT-2026-0002", user_id=1)
    assert owned is not None
    assert owned.user_id == 1
    assert blocked is None


def test_shipment_repository_enforces_ownership(client):
    from app.adapters.sqlite import SQLiteShipmentRepository
    from app.database import SessionLocal

    with SessionLocal() as db:
        repository = SQLiteShipmentRepository(db)
        owned = repository.get_owned_order_shipment(order_no="VLT-2026-0001", user_id=1)
        blocked = repository.get_owned_order_shipment(order_no="VLT-2026-0002", user_id=1)
    assert owned is not None
    assert blocked is None
