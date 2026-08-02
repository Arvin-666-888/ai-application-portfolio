import asyncio

import pytest
from pydantic import ValidationError

from app.agents.contracts import RouteDecision
from app.agents.supervisor import SupervisorRouter


@pytest.mark.parametrize(
    ("message", "expected_route"),
    [
        ("推荐一款 300 元以内的 65W 充电器", "product_inquiry"),
        ("订单 VLT-2026-0001 到哪里了", "logistics_tracking"),
        ("track order vlt-2026-0001", "logistics_tracking"),
        ("订单 VLT-2026-0001 什么时候送到", "logistics_tracking"),
        ("包裹 VLT-2026-0001 已损坏，我要退款", "aftersales_handling"),
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
    assert decision.route == "aftersales_handling"


def test_address_change_routes_to_aftersales_before_order():
    router = SupervisorRouter(llm_enabled=False)
    decision = asyncio.run(
        router.decide("订单 VLT-2026-0001 修改收货地址为敏感地址内容")
    )
    assert decision.route == "aftersales_handling"


def test_address_change_never_reaches_external_router():
    captured = []

    async def capturing_model(message: str) -> RouteDecision:
        captured.append(message)
        return RouteDecision(
            route="order_query", confidence=0.9, reason="model", source="llm"
        )

    router = SupervisorRouter(model_router=capturing_model, llm_enabled=True)
    message = "订单 VLT-2026-0001 寄到张三 13800138000 北京市朝阳区证券大厦88号"
    decision = asyncio.run(router.decide(message))

    assert decision.route == "aftersales_handling"
    assert captured == []


def test_delivery_capability_question_is_not_address_change():
    router = SupervisorRouter(llm_enabled=False)
    decision = asyncio.run(router.decide("新地址支持配送吗"))
    assert decision.route != "aftersales_handling"


@pytest.mark.parametrize("message", ["这个订单不要了 VLT-2026-0001", "cancel my order VLT-2026-0001", "取消 VLT-2026-0001"])
def test_explicit_cancel_never_reaches_external_router(message):
    captured = []

    async def capturing_model(model_message: str) -> RouteDecision:
        captured.append(model_message)
        return RouteDecision(
            route="order_query", confidence=0.9, reason="model", source="llm"
        )

    router = SupervisorRouter(model_router=capturing_model, llm_enabled=True)
    decision = asyncio.run(router.decide(message))

    assert decision.route == "aftersales_handling"
    assert captured == []


def test_invoice_negation_is_not_treated_as_order_cancellation():
    router = SupervisorRouter(llm_enabled=False)
    decision = asyncio.run(
        router.decide("不要这个订单号的发票，订单本身保留 VLT-2026-0001")
    )
    assert decision.route == "order_query"


def test_model_failure_falls_back_to_rules():
    async def failing_model(_: str) -> RouteDecision:
        raise RuntimeError("model unavailable")

    router = SupervisorRouter(model_router=failing_model, llm_enabled=True)
    decision = asyncio.run(router.decide("查询订单物流"))
    assert decision.route == "logistics_tracking"
    assert decision.source == "rule_fallback"


def test_route_contract_rejects_unknown_route():
    with pytest.raises(ValidationError):
        RouteDecision(
            route="payment",
            confidence=0.8,
            reason="Unknown route",
            source="llm",
        )
