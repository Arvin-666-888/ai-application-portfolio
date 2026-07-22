from langgraph.graph import END, START, StateGraph

from app.agents.state import GraphState
from app.agents.aftersales import AftersalesAgent
from app.agents.catalog import CatalogAgent
from app.agents.supervisor import SupervisorRouter
from app.nodes.order import OrderStatusNode
from app.graph.nodes import (
    aftersales_dispatch_node,
    build_aftersales_node,
    build_catalog_node,
    build_order_node,
    build_supervisor_node,
    catalog_dispatch_node,
    order_dispatch_node,
    select_route,
    unsupported_dispatch_node,
)


def build_routing_graph(
    router: SupervisorRouter | None = None,
    catalog_agent: CatalogAgent | None = None,
    order_node: OrderStatusNode | None = None,
    aftersales_agent: AftersalesAgent | None = None,
):
    supervisor = router or SupervisorRouter()
    graph = StateGraph(GraphState)

    graph.add_node("supervisor", build_supervisor_node(supervisor))
    graph.add_node("catalog", build_catalog_node(catalog_agent) if catalog_agent else catalog_dispatch_node)
    graph.add_node("order", build_order_node(order_node) if order_node else order_dispatch_node)
    graph.add_node(
        "aftersales",
        build_aftersales_node(aftersales_agent) if aftersales_agent else aftersales_dispatch_node,
    )
    graph.add_node("unsupported", unsupported_dispatch_node)

    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        select_route,
        {
            "catalog": "catalog",
            "order": "order",
            "aftersales": "aftersales",
            "unsupported": "unsupported",
        },
    )
    graph.add_edge("catalog", END)
    graph.add_edge("order", END)
    graph.add_edge("aftersales", END)
    graph.add_edge("unsupported", END)
    return graph.compile()
