import asyncio

import pytest

from app.adapters.sqlite import SQLiteOrderRepository, SQLiteShipmentRepository
from app.agents.aftersales import AftersalesAgent
from app.database import SessionLocal
from app.services.policy_service import AftersalesPolicyService
from app.tools import EvaluateAftersalesPolicyTool, GetOrderStatusTool


def build_agent(db, **kwargs):
    order_tool = GetOrderStatusTool(
        SQLiteOrderRepository(db),
        SQLiteShipmentRepository(db),
    )
    return AftersalesAgent(
        order_tool,
        EvaluateAftersalesPolicyTool(AftersalesPolicyService()),
        **kwargs,
    )


def test_aftersales_agent_classifies_damage_and_refund(client):
    with SessionLocal() as db:
        agent = build_agent(db, llm_enabled=False)
        result = asyncio.run(
            agent.run(
                message="订单 VLT-2026-0015 的商品破损了，我要退款",
                user_id=3,
                shop_id="shop-uk",
                request_id="req_damage",
            )
        )

    assert result["issue_type"] == "damaged"
    assert result["shipment_facts"]["exception_type"] == "damaged"
    assert result["proposed_action"] == "refund_review"
    assert result["requires_approval"] is True
    assert [item["tool"] for item in result["tool_trace"]] == [
        "get_order_status",
        "evaluate_aftersales_policy",
    ]
    assert "当前未执行" in result["answer"]


def test_aftersales_agent_distinguishes_claim_from_system_fact(client):
    with SessionLocal() as db:
        agent = build_agent(db, llm_enabled=False)
        result = asyncio.run(
            agent.run(
                message="订单 VLT-2026-0001 的商品破损了，需要换货",
                user_id=1,
                shop_id="shop-us",
                request_id="req_claim_only",
            )
        )

    assert result["issue_type"] == "damaged"
    assert result["shipment_facts"]["exception_type"] == "none"
    assert "用户陈述的问题类型：damaged" in result["answer"]
    assert "物流系统记录的异常类型：none" in result["answer"]
    assert "未确认 damaged" in result["policy_result"]["rationale"]


def test_aftersales_agent_blocks_cross_user_order(client):
    with SessionLocal() as db:
        agent = build_agent(db, llm_enabled=False)
        result = asyncio.run(
            agent.run(
                message="订单 VLT-2026-0002 延误了，我要补偿",
                user_id=1,
                shop_id="shop-us",
                request_id="req_blocked",
            )
        )

    assert result["order_facts"] is None
    assert result["shipment_facts"] is None
    assert result["policy_result"] is None
    assert result["requires_approval"] is False
    assert [item["tool"] for item in result["tool_trace"]] == ["get_order_status"]


def test_aftersales_agent_asks_for_missing_order_number(client):
    with SessionLocal() as db:
        agent = build_agent(db, llm_enabled=False)
        result = asyncio.run(
            agent.run(
                message="商品坏了，我想退款",
                user_id=1,
                shop_id="shop-us",
                request_id="req_missing",
            )
        )

    assert result["order_id"] is None
    assert result["policy_result"] is None
    assert result["tool_trace"] == []
    assert "请提供" in result["answer"]


def test_address_change_never_reaches_external_classifier(client):
    captured = []

    async def capturing_classifier(message):
        captured.append(message)
        raise AssertionError("address PII must not reach the model")

    with SessionLocal() as db:
        agent = build_agent(
            db,
            model_classifier=capturing_classifier,
            llm_enabled=True,
        )
        result = asyncio.run(
            agent.run(
                message="订单 VLT-2026-0001 修改收货地址为张三 13800138000 北京市朝阳区建国路88号",
                user_id=1,
                shop_id="shop-us",
                request_id="req_address_privacy",
            )
        )

    assert captured == []
    assert result["issue_type"] == "address_change"
    assert result["proposed_action"] == "address_change_review"
    assert result["requires_approval"] is True
    assert "13800138000" not in str(result)


async def _unexpected_classifier(message):
    raise AssertionError(f"local-only intent reached external classifier: {message}")


@pytest.mark.parametrize(
    ("message", "expected_issue", "expected_action"),
    [
        ("订单 VLT-2026-0001 寄到张三 13800138000 北京市朝阳区证券大厦88号", "address_change", "change_address"),
        ("取消 VLT-2026-0001", "cancel_request", "cancel"),
    ],
)
def test_local_only_natural_intents_stay_synchronized(message, expected_issue, expected_action, client):
    with SessionLocal() as db:
        agent = build_agent(db, model_classifier=_unexpected_classifier, llm_enabled=True)
        decision = asyncio.run(agent.classify(message))

    assert decision.issue_type == expected_issue
    assert decision.requested_action == expected_action
    assert decision.source == "rule_fallback"


def test_aftersales_model_failure_uses_rule_classifier(client):
    async def failing_classifier(_):
        raise RuntimeError("model unavailable")

    with SessionLocal() as db:
        agent = build_agent(
            db,
            model_classifier=failing_classifier,
            llm_enabled=True,
        )
        result = asyncio.run(
            agent.run(
                message="订单 VLT-2026-0001 损坏了，我要退款",
                user_id=1,
                shop_id="shop-us",
                request_id="req_fallback",
            )
        )

    assert result["issue_type"] == "damaged"
    assert result["proposed_action"] == "refund_review"
