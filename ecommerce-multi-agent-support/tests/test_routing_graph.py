import asyncio

import pytest

from app.agents.supervisor import SupervisorRouter
from app.graph import build_routing_graph


@pytest.mark.parametrize(
    ("message", "expected_route"),
    [
        ("比较两款充电器的价格和参数", "catalog"),
        ("我的订单什么时候送到", "order"),
        ("收到的商品错发了，需要换货", "aftersales"),
        ("解释一下量子力学", "unsupported"),
    ],
)
def test_graph_dispatches_to_selected_node(message, expected_route):
    graph = build_routing_graph(SupervisorRouter(llm_enabled=False))
    result = asyncio.run(
        graph.ainvoke(
            {
                "request_id": "req_test",
                "session_id": "session_test",
                "user_id": 1,
                "message": message,
                "tool_trace": [],
                "errors": [],
            }
        )
    )
    assert result["route"] == expected_route
    assert result["dispatched_to"] == expected_route
