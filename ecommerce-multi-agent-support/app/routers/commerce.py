from decimal import Decimal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.tables import UserTable
from app.schemas.schemas import OrderResponse, ProductResponse, ShipmentResponse
from app.services.audit_service import record_audit
from app.services.auth_service import get_current_user
from app.services.repository_factory import build_repositories


router = APIRouter(prefix="/api/v1", tags=["commerce"])


@router.get("/products", response_model=list[ProductResponse])
def search_products(
    keyword: str | None = Query(default=None, max_length=100),
    category: str | None = Query(default=None, max_length=80),
    max_price: Decimal | None = Query(default=None, gt=0),
    in_stock_only: bool = True,
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
    _: UserTable = Depends(get_current_user),
) -> list:
    return build_repositories(db).catalog.search(
        keyword=keyword,
        category=category,
        max_price=max_price,
        in_stock_only=in_stock_only,
        limit=limit,
    )


@router.get("/orders", response_model=list[OrderResponse])
def list_orders(
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: UserTable = Depends(get_current_user),
) -> list:
    return build_repositories(db).orders.list_owned_orders(user_id=current_user.id, limit=limit)


@router.get("/orders/{order_no}", response_model=OrderResponse)
def get_order(
    order_no: str,
    db: Session = Depends(get_db),
    current_user: UserTable = Depends(get_current_user),
):
    request_id = f"req_{uuid4().hex}"
    order = build_repositories(db).orders.get_owned_order(order_no=order_no, user_id=current_user.id)
    record_audit(
        db,
        user_id=current_user.id,
        order_id=order.id if order else None,
        request_id=request_id,
        action="get_owned_order",
        success=order is not None,
        input_summary={"order_no": order_no},
        result_summary={"found": order is not None},
    )
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found or access denied",
        )
    return order


@router.get("/orders/{order_no}/shipment", response_model=ShipmentResponse)
def get_shipment(
    order_no: str,
    db: Session = Depends(get_db),
    current_user: UserTable = Depends(get_current_user),
):
    shipment = build_repositories(db).shipments.get_owned_order_shipment(
        order_no=order_no,
        user_id=current_user.id,
    )
    if shipment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shipment not found or access denied",
        )
    return shipment
