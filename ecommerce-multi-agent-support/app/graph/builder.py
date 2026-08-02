from langgraph.graph import END, START, StateGraph

from app.agents.aftersales import AftersalesAgent
from app.agents.catalog import CatalogAgent
from app.agents.state import GraphState
from app.agents.supervisor import SupervisorRouter
from app.graph.nodes import (
    aftersales_handling_dispatch_node,
    build_aftersales_node,
    build_catalog_node,
    build_order_node,
    build_supervisor_node,
    logistics_tracking_dispatch_node,
    order_query_dispatch_node,
    product_inquiry_dispatch_node,
    select_route,
    unsupported_dispatch_node,
)
from app.nodes.order import OrderStatusNode


def build_routing_graph(
    router: SupervisorRouter | None = None,
    catalog_agent: CatalogAgent | None = None,
    order_node: OrderStatusNode | None = None,
    aftersales_agent: AftersalesAgent | None = None,
):
    supervisor = router or SupervisorRouter()
    graph = StateGraph(GraphState)
    graph.add_node("supervisor", build_supervisor_node(supervisor))
    graph.add_node("product_inquiry", build_catalog_node(catalog_agent) if catalog_agent else product_inquiry_dispatch_node)
    graph.add_node("order_query", build_order_node(order_node, dispatched_to="order_query") if order_node else order_query_dispatch_node)
    graph.add_node("logistics_tracking", build_order_node(order_node, dispatched_to="logistics_tracking") if order_node else logistics_tracking_dispatch_node)
    graph.add_node("aftersales_handling", build_aftersales_node(aftersales_agent) if aftersales_agent else aftersales_handling_dispatch_node)
    graph.add_node("unsupported", unsupported_dispatch_node)

    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        select_route,
        {
            "product_inquiry": "product_inquiry",
            "order_query": "order_query",
            "logistics_tracking": "logistics_tracking",
            "aftersales_handling": "aftersales_handling",
            "unsupported": "unsupported",
        },
    )
    for node_name in (
        "product_inquiry", "order_query", "logistics_tracking", "aftersales_handling", "unsupported"
    ):
        graph.add_edge(node_name, END)
    return graph.compile()
