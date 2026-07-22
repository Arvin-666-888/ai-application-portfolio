import asyncio

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
                request_id="req_missing",
            )
        )

    assert result["order_id"] is None
    assert result["policy_result"] is None
    assert result["tool_trace"] == []
    assert "请提供" in result["answer"]


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
                request_id="req_fallback",
            )
        )

    assert result["issue_type"] == "damaged"
    assert result["proposed_action"] == "refund_review"
