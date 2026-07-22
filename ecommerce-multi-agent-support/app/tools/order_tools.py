from dataclasses import dataclass
from time import perf_counter

from app.domain.models import Order, Shipment
from app.ports import OrderRepository, ShipmentRepository


@dataclass(frozen=True, slots=True)
class GetOrderStatusToolResult:
    order: dict | None
    shipment: dict | None
    trace: dict


class GetOrderStatusTool:
    name = "get_order_status"

    def __init__(
        self,
        order_repository: OrderRepository,
        shipment_repository: ShipmentRepository,
    ) -> None:
        self.order_repository = order_repository
        self.shipment_repository = shipment_repository

    def execute(
        self,
        *,
        order_no: str,
        user_id: int,
        request_id: str,
    ) -> GetOrderStatusToolResult:
        started = perf_counter()
        order = self.order_repository.get_owned_order(order_no=order_no, user_id=user_id)
        shipment = None
        if order is not None:
            shipment = self.shipment_repository.get_owned_order_shipment(
                order_no=order_no,
                user_id=user_id,
            )

        order_data = self._serialize_order(order) if order else None
        shipment_data = self._serialize_shipment(shipment) if shipment else None
        trace = {
            "step": 1,
            "request_id": request_id,
            "tool": self.name,
            "arguments": {"order_no": order_no},
            "success": order is not None,
            "result_count": 1 if order is not None else 0,
            "duration_ms": round((perf_counter() - started) * 1000, 2),
        }
        return GetOrderStatusToolResult(
            order=order_data,
            shipment=shipment_data,
            trace=trace,
        )

    @staticmethod
    def _serialize_order(order: Order) -> dict:
        return {
            "id": order.id,
            "order_no": order.order_no,
            "status": order.status,
            "total_amount": str(order.total_amount),
            "created_at": order.created_at.isoformat(),
            "items": [
                {
                    "product_id": item.product_id,
                    "sku": item.sku,
                    "product_name": item.product_name,
                    "quantity": item.quantity,
                    "unit_price": str(item.unit_price),
                }
                for item in order.items
            ],
        }

    @staticmethod
    def _serialize_shipment(shipment: Shipment) -> dict:
        return {
            "id": shipment.id,
            "order_id": shipment.order_id,
            "order_no": shipment.order_no,
            "carrier": shipment.carrier,
            "tracking_no": shipment.tracking_no,
            "status": shipment.status,
            "exception_type": shipment.exception_type,
            "estimated_delivery_at": (
                shipment.estimated_delivery_at.isoformat()
                if shipment.estimated_delivery_at is not None
                else None
            ),
            "updated_at": shipment.updated_at.isoformat(),
        }
