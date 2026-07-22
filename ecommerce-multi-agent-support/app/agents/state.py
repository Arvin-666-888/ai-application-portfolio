from operator import add
from typing import Annotated, Any, TypedDict

from app.agents.contracts import RouteName, RouteSource


class GraphState(TypedDict, total=False):
    request_id: str
    session_id: str
    user_id: int
    message: str

    route: RouteName
    route_confidence: float
    route_reason: str
    route_source: RouteSource
    dispatched_to: RouteName

    product_filters: dict[str, Any]
    products: list[dict[str, Any]]
    order_id: str | None
    order_facts: dict[str, Any] | None
    shipment_facts: dict[str, Any] | None
    issue_type: str | None
    policy_result: dict[str, Any] | None
    proposed_action: str | None

    answer: str
    requires_approval: bool
    tool_trace: Annotated[list[dict[str, Any]], add]
    errors: Annotated[list[str], add]
