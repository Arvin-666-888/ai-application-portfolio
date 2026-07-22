import json
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.tables import OrderItemTable, OrderTable, ProductTable, ShipmentTable, UserTable
from app.services.auth_service import hash_password


PRODUCT_TEMPLATES = [
    ("GaN Charger", "charger", {"power_w": 65, "ports": 3, "protocol": "PD 3.0"}),
    ("Travel Charger", "charger", {"power_w": 45, "ports": 2, "protocol": "PD 3.0"}),
    ("Power Bank", "power_bank", {"capacity_mah": 20000, "power_w": 30}),
    ("Magnetic Power Bank", "power_bank", {"capacity_mah": 10000, "power_w": 20}),
    ("Braided USB-C Cable", "cable", {"length_m": 1.5, "power_w": 100}),
    ("USB-C Hub", "hub", {"ports": 7, "video": "4K HDMI"}),
    ("Wireless Charger", "wireless_charger", {"power_w": 15, "standard": "Qi2"}),
    ("Laptop Stand", "accessory", {"material": "aluminum", "adjustable": True}),
    ("Bluetooth Earbuds", "audio", {"battery_hours": 30, "noise_control": "ANC"}),
    ("Travel Adapter", "adapter", {"regions": ["US", "EU", "UK", "AU"], "usb_ports": 4}),
]

ORDER_STATUSES = ["paid", "processing", "shipped", "delivered", "cancelled", "refund_pending"]
SHIPMENT_STATUSES = ["label_created", "in_transit", "out_for_delivery", "delivered", "exception"]
EXCEPTION_TYPES = ["none", "delayed", "damaged", "wrong_item", "lost", "customs_hold"]
CARRIERS = ["DHL", "FedEx", "UPS", "YunExpress"]


def seed_demo_data(db: Session, *, seed: int) -> dict[str, int]:
    existing = {
        "users": db.scalar(select(func.count(UserTable.id))) or 0,
        "products": db.scalar(select(func.count(ProductTable.id))) or 0,
        "orders": db.scalar(select(func.count(OrderTable.id))) or 0,
        "shipments": db.scalar(select(func.count(ShipmentTable.id))) or 0,
    }
    if any(existing.values()):
        return existing

    rng = random.Random(seed)
    shared_password_hash = hash_password("DemoPass123!")
    users = [
        UserTable(username=f"demo_user_{index:02d}", password_hash=shared_password_hash)
        for index in range(1, 13)
    ]
    db.add_all(users)
    db.flush()

    products: list[ProductTable] = []
    for index in range(1, 51):
        base_name, category, specifications = PRODUCT_TEMPLATES[(index - 1) % len(PRODUCT_TEMPLATES)]
        variant = (index - 1) // len(PRODUCT_TEMPLATES) + 1
        price = Decimal(str(round(39 + index * 7.35 + rng.uniform(-8, 8), 2)))
        product = ProductTable(
            sku=f"VC-{category[:3].upper()}-{index:04d}",
            name=f"VoltCore {base_name} V{variant}",
            category=category,
            price=price,
            stock=rng.randint(0, 180),
            specifications=json.dumps(
                {**specifications, "variant": variant, "warranty_months": rng.choice([12, 18, 24])},
                ensure_ascii=False,
            ),
            is_active=True,
        )
        products.append(product)
    db.add_all(products)
    db.flush()

    base_time = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    orders: list[OrderTable] = []
    shipments: list[ShipmentTable] = []
    for index in range(1, 101):
        user = users[(index - 1) % len(users)]
        chosen_products = rng.sample(products, k=rng.randint(1, 3))
        quantities = [rng.randint(1, 2) for _ in chosen_products]
        total = sum((product.price * quantity for product, quantity in zip(chosen_products, quantities)), Decimal("0"))
        created_at = base_time + timedelta(hours=index * 13)
        status = ORDER_STATUSES[(index - 1) % len(ORDER_STATUSES)]
        order = OrderTable(
            order_no=f"VLT-2026-{index:04d}",
            user_id=user.id,
            status=status,
            total_amount=total,
            created_at=created_at,
        )
        for product, quantity in zip(chosen_products, quantities):
            order.items.append(
                OrderItemTable(
                    product_id=product.id,
                    quantity=quantity,
                    unit_price=product.price,
                )
            )
        orders.append(order)
        db.add(order)
        db.flush()

        shipment_status = "delivered" if status == "delivered" else SHIPMENT_STATUSES[(index - 1) % len(SHIPMENT_STATUSES)]
        exception_type = EXCEPTION_TYPES[(index - 1) % len(EXCEPTION_TYPES)] if shipment_status == "exception" else "none"
        shipments.append(
            ShipmentTable(
                order_id=order.id,
                carrier=CARRIERS[(index - 1) % len(CARRIERS)],
                tracking_no=f"VC{2026000000 + index}",
                status=shipment_status,
                exception_type=exception_type,
                estimated_delivery_at=created_at + timedelta(days=rng.randint(3, 12)),
                updated_at=created_at + timedelta(days=rng.randint(1, 5)),
            )
        )

    db.add_all(shipments)
    db.commit()
    return {"users": len(users), "products": len(products), "orders": len(orders), "shipments": len(shipments)}
