from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_]+$")
    password: str = Field(min_length=8, max_length=100)


class UserLogin(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=8, max_length=100)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    shop_id: str
    market: str
    timezone: str
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
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


class OrderItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    product_id: int
    sku: str
    product_name: str
    quantity: int
    unit_price: Decimal


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    shop_id: str
    order_no: str
    status: str
    total_amount: Decimal
    currency: str
    created_at: datetime
    items: list[OrderItemResponse]


class ShipmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    order_id: int
    order_no: str
    carrier: str
    tracking_no: str
    status: str
    exception_type: str
    estimated_delivery_at: datetime | None
    updated_at: datetime


class HealthResponse(BaseModel):
    status: str
    app: str
    version: str
    commerce_backend: str


class RoutingPreviewRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(min_length=1, max_length=100)


class RoutingPreviewResponse(BaseModel):
    request_id: str
    session_id: str
    route: str
    route_confidence: float
    route_reason: str
    route_source: str
    dispatched_to: str


class ChatPreviewRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(min_length=1, max_length=100)


class ChatPreviewResponse(BaseModel):
    request_id: str
    session_id: str
    status: str
    route: str
    route_confidence: float
    route_source: str
    dispatched_to: str
    answer: str | None = None
    product_filters: dict[str, Any] = Field(default_factory=dict)
    products: list[dict[str, Any]] = Field(default_factory=list)
    order_id: str | None = None
    order_facts: dict[str, Any] | None = None
    shipment_facts: dict[str, Any] | None = None
    issue_type: str | None = None
    policy_result: dict[str, Any] | None = None
    proposed_action: str | None = None
    requires_approval: bool = False
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class AuditLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    shop_id: str
    request_id: str
    action: str
    success: bool
    input_summary: dict[str, Any]
    result_summary: dict[str, Any]
    created_at: datetime
