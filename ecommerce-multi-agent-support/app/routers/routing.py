from uuid import uuid4

from fastapi import APIRouter, Depends

from app.graph import build_routing_graph
from app.models.tables import UserTable
from app.schemas.schemas import RoutingPreviewRequest, RoutingPreviewResponse
from app.services.auth_service import get_current_user


router = APIRouter(prefix="/api/v1/routing", tags=["routing"])
routing_graph = build_routing_graph()


@router.post("/preview", response_model=RoutingPreviewResponse)
async def preview_route(
    payload: RoutingPreviewRequest,
    current_user: UserTable = Depends(get_current_user),
) -> RoutingPreviewResponse:
    request_id = f"req_{uuid4().hex}"
    result = await routing_graph.ainvoke(
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
    return RoutingPreviewResponse(
        request_id=result["request_id"],
        session_id=result["session_id"],
        route=result["route"],
        route_confidence=result["route_confidence"],
        route_reason=result["route_reason"],
        route_source=result["route_source"],
        dispatched_to=result["dispatched_to"],
    )
