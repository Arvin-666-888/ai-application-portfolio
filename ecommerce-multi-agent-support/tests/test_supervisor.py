import asyncio

import pytest
from pydantic import ValidationError

from app.agents.contracts import RouteDecision
from app.agents.supervisor import SupervisorRouter


@pytest.mark.parametrize(
    ("message", "expected_route"),
    [
        ("推荐一款 300 元以内的 65W 充电器", "catalog"),
        ("订单 VLT-2026-0001 到哪里了", "order"),
        ("包裹 VLT-2026-0001 已损坏，我要退款", "aftersales"),
        ("帮我写一首关于夏天的诗", "unsupported"),
        ("预测明天股票价格", "unsupported"),
    ],
)
def test_rule_supervisor_routes_supported_intents(message, expected_route):
    router = SupervisorRouter(llm_enabled=False)
    decision = asyncio.run(router.decide(message))
    assert decision.route == expected_route
    assert decision.source == "rule_fallback"


def test_aftersales_has_priority_over_order_keywords():
    router = SupervisorRouter(llm_enabled=False)
    decision = asyncio.run(router.decide("订单 VLT-2026-0001 的包裹坏了，可以退款吗？"))
    assert decision.route == "aftersales"


def test_model_failure_falls_back_to_rules():
    async def failing_model(_: str) -> RouteDecision:
        raise RuntimeError("model unavailable")

    router = SupervisorRouter(model_router=failing_model, llm_enabled=True)
    decision = asyncio.run(router.decide("查询订单物流"))
    assert decision.route == "order"
    assert decision.source == "rule_fallback"


def test_route_contract_rejects_unknown_route():
    with pytest.raises(ValidationError):
        RouteDecision(
            route="payment",
            confidence=0.8,
            reason="Unknown route",
            source="llm",
        )
