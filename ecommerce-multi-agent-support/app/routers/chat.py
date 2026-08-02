from uuid import uuid4

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.agents.catalog import CatalogAgent
from app.agents.aftersales import AftersalesAgent
from app.database import get_db
from app.graph import build_routing_graph
from app.models.tables import UserTable
from app.nodes.order import OrderStatusNode
from app.schemas.schemas import AuditLogResponse, ChatPreviewRequest, ChatPreviewResponse
from app.services.audit_service import list_user_audits, record_audit
from app.services.auth_service import get_current_user
from app.services.repository_factory import build_repositories
from app.services.policy_service import AftersalesPolicyService
from app.tools import EvaluateAftersalesPolicyTool, GetOrderStatusTool, SearchProductsTool


router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


async def run_chat(
    payload: ChatPreviewRequest,
    *,
    db: Session,
    current_user: UserTable,
) -> ChatPreviewResponse:
    request_id = f"req_{uuid4().hex}"
    repositories = build_repositories(db)
    catalog_agent = CatalogAgent(SearchProductsTool(repositories.catalog))
    order_tool = GetOrderStatusTool(repositories.orders, repositories.shipments)
    order_node = OrderStatusNode(order_tool)
    aftersales_agent = AftersalesAgent(
        order_tool,
        EvaluateAftersalesPolicyTool(AftersalesPolicyService()),
    )
    graph = build_routing_graph(
        catalog_agent=catalog_agent,
        order_node=order_node,
        aftersales_agent=aftersales_agent,
    )
    result = await graph.ainvoke(
        {
            "request_id": request_id,
            "session_id": payload.session_id,
            "user_id": current_user.id,
            "shop_id": current_user.shop_id,
            "market": current_user.market,
            "timezone": current_user.timezone,
            "message": payload.message,
            "tool_trace": [],
            "errors": [],
        }
    )
    response = ChatPreviewResponse(
        request_id=result["request_id"],
        session_id=result["session_id"],
        status="completed",
        route=result["route"],
        route_confidence=result["route_confidence"],
        route_source=result["route_source"],
        dispatched_to=result["dispatched_to"],
        answer=result.get("answer"),
        product_filters=result.get("product_filters", {}),
        products=result.get("products", []),
        order_id=result.get("order_id"),
        order_facts=result.get("order_facts"),
        shipment_facts=result.get("shipment_facts"),
        issue_type=result.get("issue_type"),
        policy_result=result.get("policy_result"),
        proposed_action=result.get("proposed_action"),
        requires_approval=result.get("requires_approval", False),
        tool_trace=result.get("tool_trace", []),
        errors=result.get("errors", []),
    )
    record_audit(
        db,
        shop_id=current_user.shop_id,
        user_id=current_user.id,
        order_id=(response.order_facts or {}).get("id"),
        request_id=request_id,
        action="multi_agent_chat",
        success=not response.errors,
        input_summary={
            "session_id": payload.session_id,
            "message_length": len(payload.message),
        },
        result_summary={
            "route": response.route,
            "route_source": response.route_source,
            "tools": [item.get("tool") for item in response.tool_trace],
            "requires_approval": response.requires_approval,
            "products_returned": len(response.products),
            "order_facts_present": response.order_facts is not None,
            "shipment_facts_present": response.shipment_facts is not None,
            "proposed_action": response.proposed_action,
        },
    )
    return response


@router.post("", response_model=ChatPreviewResponse)
async def chat(
    payload: ChatPreviewRequest,
    db: Session = Depends(get_db),
    current_user: UserTable = Depends(get_current_user),
) -> ChatPreviewResponse:
    return await run_chat(payload, db=db, current_user=current_user)


@router.post("/preview", response_model=ChatPreviewResponse, deprecated=True)
async def chat_preview(
    payload: ChatPreviewRequest,
    db: Session = Depends(get_db),
    current_user: UserTable = Depends(get_current_user),
) -> ChatPreviewResponse:
    return await run_chat(payload, db=db, current_user=current_user)


@router.get("/audits", response_model=list[AuditLogResponse])
def get_chat_audits(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: UserTable = Depends(get_current_user),
) -> list[dict]:
    return list_user_audits(
        db, shop_id=current_user.shop_id, user_id=current_user.id, limit=limit
    )
