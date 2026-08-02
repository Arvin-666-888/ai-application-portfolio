from decimal import Decimal
from typing import Protocol

from app.domain.models import Order, Product, Shipment


class CatalogRepository(Protocol):
    def search(
        self,
        *,
        shop_id: str,
        keyword: str | None = None,
        category: str | None = None,
        max_price: Decimal | None = None,
        in_stock_only: bool = True,
        limit: int = 10,
    ) -> list[Product]: ...

    def get_by_sku(self, *, shop_id: str, sku: str) -> Product | None: ...


class OrderRepository(Protocol):
    def get_owned_order(self, *, shop_id: str, order_no: str, user_id: int) -> Order | None: ...

    def list_owned_orders(self, *, shop_id: str, user_id: int, limit: int = 10) -> list[Order]: ...


class ShipmentRepository(Protocol):
    def get_owned_order_shipment(
        self, *, shop_id: str, order_no: str, user_id: int
    ) -> Shipment | None: ...
