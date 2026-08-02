from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class Product:
    id: int
    shop_id: str
    sku: str
    name: str
    category: str
    price: Decimal
    currency: str
    stock: int
    specifications: dict[str, Any]
    is_active: bool


@dataclass(frozen=True, slots=True)
class OrderItem:
    product_id: int
    sku: str
    product_name: str
    quantity: int
    unit_price: Decimal


@dataclass(frozen=True, slots=True)
class Order:
    id: int
    shop_id: str
    order_no: str
    user_id: int
    status: str
    total_amount: Decimal
    currency: str
    created_at: datetime
    items: list[OrderItem] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Shipment:
    id: int
    order_id: int
    order_no: str
    carrier: str
    tracking_no: str
    status: str
    exception_type: str
    estimated_delivery_at: datetime | None
    updated_at: datetime
