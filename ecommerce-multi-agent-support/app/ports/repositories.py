from decimal import Decimal
from typing import Protocol

from app.domain.models import Order, Product, Shipment


class CatalogRepository(Protocol):
    def search(
        self,
        *,
        keyword: str | None = None,
        category: str | None = None,
        max_price: Decimal | None = None,
        in_stock_only: bool = True,
        limit: int = 10,
    ) -> list[Product]: ...

    def get_by_sku(self, sku: str) -> Product | None: ...


class OrderRepository(Protocol):
    def get_owned_order(self, *, order_no: str, user_id: int) -> Order | None: ...

    def list_owned_orders(self, *, user_id: int, limit: int = 10) -> list[Order]: ...


class ShipmentRepository(Protocol):
    def get_owned_order_shipment(
        self,
        *,
        order_no: str,
        user_id: int,
    ) -> Shipment | None: ...
