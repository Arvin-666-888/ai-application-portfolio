from app.agents.state import GraphState
from app.agents.aftersales import AftersalesAgent
from app.agents.catalog import CatalogAgent
from app.agents.supervisor import SupervisorRouter
from app.nodes.order import OrderStatusNode


def build_supervisor_node(router: SupervisorRouter):
    async def supervisor_node(state: GraphState) -> dict:
        decision = await router.decide(state["message"])
        return {
            "route": decision.route,
            "route_confidence": decision.confidence,
            "route_reason": decision.reason,
            "route_source": decision.source,
        }

    return supervisor_node


def catalog_dispatch_node(_: GraphState) -> dict:
    return {"dispatched_to": "catalog"}


def build_catalog_node(agent: CatalogAgent):
    async def catalog_node(state: GraphState) -> dict:
        return await agent.run(
            message=state["message"],
            request_id=state["request_id"],
        )

    return catalog_node


def order_dispatch_node(_: GraphState) -> dict:
    return {"dispatched_to": "order"}


def build_order_node(node: OrderStatusNode):
    def order_node(state: GraphState) -> dict:
        return node.run(
            message=state["message"],
            user_id=state["user_id"],
            request_id=state["request_id"],
        )

    return order_node


def aftersales_dispatch_node(_: GraphState) -> dict:
    return {"dispatched_to": "aftersales"}


def build_aftersales_node(agent: AftersalesAgent):
    async def aftersales_node(state: GraphState) -> dict:
        return await agent.run(
            message=state["message"],
            user_id=state["user_id"],
            request_id=state["request_id"],
        )

    return aftersales_node


def unsupported_dispatch_node(_: GraphState) -> dict:
    return {
        "dispatched_to": "unsupported",
        "answer": (
            "当前系统只支持商品推荐与比较、本人订单和物流查询，以及售后问题评估。"
            "我不能处理与电商客服无关的请求，也不会执行支付、退款或订单修改。"
        ),
        "requires_approval": False,
        "tool_trace": [],
    }


def select_route(state: GraphState) -> str:
    return state.get("route", "unsupported")
