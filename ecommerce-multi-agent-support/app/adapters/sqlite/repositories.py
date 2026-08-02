import json
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, selectinload

from app.domain.models import Order, OrderItem, Product, Shipment
from app.models.tables import OrderItemTable, OrderTable, ProductTable, ShipmentTable


def _bounded_limit(value: int, maximum: int = 50) -> int:
    return max(1, min(value, maximum))


def _to_product(row: ProductTable) -> Product:
    try:
        specifications = json.loads(row.specifications)
    except (TypeError, json.JSONDecodeError):
        specifications = {}
    return Product(
        id=row.id,
        shop_id=row.shop_id,
        sku=row.sku,
        name=row.name,
        category=row.category,
        price=Decimal(row.price),
        currency=row.currency,
        stock=row.stock,
        specifications=specifications,
        is_active=row.is_active,
    )


def _to_order(row: OrderTable) -> Order:
    items = [
        OrderItem(
            product_id=item.product_id,
            sku=item.product.sku,
            product_name=item.product.name,
            quantity=item.quantity,
            unit_price=Decimal(item.unit_price),
        )
        for item in row.items
    ]
    return Order(
        id=row.id,
        shop_id=row.shop_id,
        order_no=row.order_no,
        user_id=row.user_id,
        status=row.status,
        total_amount=Decimal(row.total_amount),
        currency=row.currency,
        created_at=row.created_at,
        items=items,
    )


class SQLiteCatalogRepository:
    def __init__(self, session: Session):
        self.session = session

    def search(
        self,
        *,
        shop_id: str,
        keyword: str | None = None,
        category: str | None = None,
        max_price: Decimal | None = None,
        in_stock_only: bool = True,
        limit: int = 10,
    ) -> list[Product]:
        # MIGRATION: shop scope is applied at the first real catalog mapping boundary.
        statement: Select[tuple[ProductTable]] = select(ProductTable).where(
            ProductTable.shop_id == shop_id,
            ProductTable.is_active.is_(True),
        )
        if keyword:
            normalized = f"%{keyword.strip().lower()}%"
            statement = statement.where(
                func.lower(ProductTable.name).like(normalized)
                | func.lower(ProductTable.sku).like(normalized)
                | func.lower(ProductTable.specifications).like(normalized)
            )
        if category:
            statement = statement.where(func.lower(ProductTable.category) == category.strip().lower())
        if max_price is not None:
            statement = statement.where(ProductTable.price <= max_price)
        if in_stock_only:
            statement = statement.where(ProductTable.stock > 0)
        statement = statement.order_by(ProductTable.price, ProductTable.id).limit(_bounded_limit(limit))
        return [_to_product(row) for row in self.session.scalars(statement).all()]

    def get_by_sku(self, *, shop_id: str, sku: str) -> Product | None:
        row = self.session.scalar(
            select(ProductTable).where(
                ProductTable.shop_id == shop_id,
                ProductTable.sku == sku.strip(),
                ProductTable.is_active.is_(True),
            )
        )
        return _to_product(row) if row else None


class SQLiteOrderRepository:
    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def _base_statement() -> Select[tuple[OrderTable]]:
        return select(OrderTable).options(
            selectinload(OrderTable.items).selectinload(OrderItemTable.product)
        )

    def get_owned_order(self, *, shop_id: str, order_no: str, user_id: int) -> Order | None:
        # MIGRATION: authorization is enforced by shop + user + order number together.
        row = self.session.scalar(
            self._base_statement().where(
                OrderTable.shop_id == shop_id,
                OrderTable.order_no == order_no.strip(),
                OrderTable.user_id == user_id,
            )
        )
        return _to_order(row) if row else None

    def list_owned_orders(self, *, shop_id: str, user_id: int, limit: int = 10) -> list[Order]:
        rows = self.session.scalars(
            self._base_statement()
            .where(OrderTable.shop_id == shop_id, OrderTable.user_id == user_id)
            .order_by(OrderTable.created_at.desc(), OrderTable.id.desc())
            .limit(_bounded_limit(limit))
        ).all()
        return [_to_order(row) for row in rows]


class SQLiteShipmentRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_owned_order_shipment(
        self, *, shop_id: str, order_no: str, user_id: int
    ) -> Shipment | None:
        # MIGRATION: shipment reuse keeps repository logic single-sourced and tenant-scoped.
        row = self.session.execute(
            select(ShipmentTable, OrderTable.order_no)
            .join(OrderTable, ShipmentTable.order_id == OrderTable.id)
            .where(
                OrderTable.shop_id == shop_id,
                OrderTable.order_no == order_no.strip(),
                OrderTable.user_id == user_id,
            )
        ).one_or_none()
        if row is None:
            return None
        shipment, owned_order_no = row
        return Shipment(
            id=shipment.id,
            order_id=shipment.order_id,
            order_no=owned_order_no,
            carrier=shipment.carrier,
            tracking_no=shipment.tracking_no,
            status=shipment.status,
            exception_type=shipment.exception_type,
            estimated_delivery_at=shipment.estimated_delivery_at,
            updated_at=shipment.updated_at,
        )
