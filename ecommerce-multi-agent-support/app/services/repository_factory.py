from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.adapters.sqlite import SQLiteCatalogRepository, SQLiteOrderRepository, SQLiteShipmentRepository
from app.config import settings
from app.ports import CatalogRepository, OrderRepository, ShipmentRepository


@dataclass(slots=True)
class Repositories:
    catalog: CatalogRepository
    orders: OrderRepository
    shipments: ShipmentRepository


def build_repositories(db: Session) -> Repositories:
    if settings.COMMERCE_BACKEND != "sqlite":
        raise RuntimeError("WooCommerce adapter is planned for V3 and is not available in V1")
    return Repositories(
        catalog=SQLiteCatalogRepository(db),
        orders=SQLiteOrderRepository(db),
        shipments=SQLiteShipmentRepository(db),
    )
